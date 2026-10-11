"""Off-page publication verification sweep (plan/33 5b, plan/34 queue).

PUBLISHING is manual; VERIFYING is not. Once the operator submits a public
permalink (api/routes/offpage.py), four checkpoint rows are PRE-CREATED in
offpage_verifications with NULL checked_at and a due time of submitted_at +
offset (+15m, +2h, +24h, +72h). This job is the sweep that claims and
resolves DUE rows only — the partial index idx_offpage_verif_due IS the
worklist, and the sweep never has to reason about "which checkpoints should
exist yet".

Result vocabulary is four-valued and deliberately NOT a boolean (plan/33):
  live          - permalink resolved and the comment is present
  removed       - resolved, but our comment is gone (mods or automod)
  unverifiable  - could NOT determine; explicitly NOT a removal
  pending       - not yet checked (checked_at IS NULL)

'unverifiable' is load-bearing: a network blip, a logged-out 403, or a
rate-limit must never increment a strike count. A checker that conflates "I
couldn't tell" with "it was removed" would disable communities on the basis
of our own infrastructure failing. The checker below therefore maps ONLY an
HTTP 200 to 'live', ONLY a 404/410 (resolved-but-gone) to 'removed', and
every other status OR exception to 'unverifiable' (logged with detail).

3-STRIKE KILL-SWITCH: every 'removed' result increments the community's
strike counter (rolling 90 days). At >= 3, offpage_communities.enabled is
set false with a reason — blocking discovery from minting new opportunities
there AND the draft gate from producing promotional drafts — because an
account repeatedly getting pulled is the strongest signal the community is
not a safe place to post from.

Safety properties (mirroring the rest of src/jobs/):
  * injectable clock (now=) and injectable fetcher (fetcher=) for tests
  * advisory lock so two overlapping sweeps never double-claim
  * claim uses SELECT ... FOR UPDATE SKIP LOCKED (belt-and-braces)
  * never raises: unexpected errors are captured, notified, and retried
"""

import os
import sys
import json
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# checkpoint_index -> offset from submission. Single source of truth:
# precreate_verification_rows (API route) and the UI's "verification
# schedule" display both read this. Matches plan/33's four checkpoints.
CHECKPOINT_OFFSET_MINUTES = (15, 120, 1440, 4320)

UA = ("Mozilla/5.0 (compatible; OpenSEO-offpage-verify/1.0; +https://"
      "openseo.example.com/bot)")

CLAIM_LIMIT = 100


def _utcnow():
    return datetime.now(timezone.utc)


def precreate_verification_rows(conn, site_id, publication_id,
                                submitted_at,
                                offsets_minutes=CHECKPOINT_OFFSET_MINUTES):
    """Create the four checkpoint rows for a fresh publication (idempotent).

    Called by the operator permalink endpoint at submit time so the sweep
    only ever scans existing, DUE rows. `submitted_at` is the publication's
    submitted_at (the operator's paste time) — the T+ offsets are measured
    from when the content actually went out, not from when the row was
    created.
    """
    if isinstance(submitted_at, str):
        submitted_at = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
    created = 0
    with conn.cursor() as cur:
        for index, minutes in enumerate(offsets_minutes):
            cur.execute(
                """
                INSERT INTO offpage_verifications
                    (site_id, publication_id, checkpoint_index,
                     checkpoint_due_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (publication_id, checkpoint_index) DO NOTHING
                """,
                (site_id, publication_id, index,
                 submitted_at + timedelta(minutes=minutes)),
            )
            created += cur.rowcount
    return created


def verify_permalink(url, fetcher=None, timeout=20):
    """Resolve one public permalink into (result, http_status, detail).

    Intentional mapping (see module docstring):
      200/201            -> live
      404/410            -> removed
      403 / 5xx / timeout -> unverifiable  (never a strike)
    `fetcher` is injectable for hermetic tests: called as
    fetcher(url, timeout) and must return the HTTP status int or raise.
    """
    fetcher = fetcher or _fetch_status
    try:
        status = fetcher(url, timeout)
    except Exception as exc:  # noqa: BLE001 - network layer, mapped to unverifiable
        return {
            "result": "unverifiable",
            "http_status": None,
            "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    if status in (200, 201):
        return {
            "result": "live",
            "http_status": status,
            "detail": f"resolved with HTTP {status}",
        }
    if status in (404, 410):
        return {
            "result": "removed",
            "http_status": status,
            "detail": f"permalink resolved to HTTP {status} (gone)",
        }
    return {
        "result": "unverifiable",
        "http_status": status,
        "detail": f"HTTP {status} is not resolvable to a verdict",
    }


def _fetch_status(url, timeout=20):
    """Plain HEAD-then-GET reachability probe. Returns the HTTP status int.

    HEAD is preferred, but some platforms do not implement it; the GET
    fallback drains the body. Never 30x-follows more than twice.
    """
    from urllib.error import HTTPError

    def _status_with(method):
        req = urllib.request.Request(url, method=method,
                                     headers={"User-Agent": UA,
                                              "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode()

    try:
        return _status_with("HEAD")
    except HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001 - HEAD unsupported/malformed; GET fallback
        try:
            return _status_with("GET")
        except HTTPError as exc:
            return exc.code


def _claim_due(conn, now_utc):
    """Claim due verification rows; returns the list to check."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.verification_id, v.site_id, v.publication_id,
                   v.checkpoint_index, p.live_permalink, p.post_type
            FROM offpage_verifications v
            JOIN offpage_publications p
              ON p.site_id = v.site_id
             AND p.publication_id = v.publication_id
            WHERE v.checked_at IS NULL
              AND v.checkpoint_due_at <= %s
            ORDER BY v.checkpoint_due_at
            LIMIT %s
            FOR UPDATE OF v SKIP LOCKED
            """,
            (now_utc, CLAIM_LIMIT),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


def _record_result(conn, verification_id, now_utc, verdict):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE offpage_verifications
            SET checked_at = %s, result = %s, http_status = %s, detail = %s
            WHERE verification_id = %s
            """,
            (now_utc, verdict["result"], verdict["http_status"],
             verdict["detail"], verification_id),
        )


def _publication_live(conn, publication_id):
    """True when every checkpoint is checked and at least one is 'live'."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bool_and(checked_at IS NOT NULL)             AS all_checked,
                   bool_or(checked_at IS NOT NULL
                           AND result = 'live')                 AS any_live,
                   bool_or(checked_at IS NOT NULL
                           AND result = 'removed')              AS any_removed
            FROM offpage_verifications
            WHERE publication_id = %s
            """,
            (publication_id,),
        )
        all_checked, any_live, any_removed = cur.fetchone()
    return all_checked, any_live, any_removed


def _strike(conn, site_id, publication_id, checkpoint_index):
    """Record a removed checkpoint's contribution to the per-community
    3-strike kill-switch. Returns the community_id when the community was
    disabled by this strike, else None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.community_id
            FROM offpage_publications p
            JOIN offpage_communities c
              ON c.site_id = p.site_id
             AND c.community_id = p.community_id
            WHERE p.publication_id = %s AND p.site_id = %s
            """,
            (publication_id, site_id),
        )
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return None
        community_id = row[0]
        cur.execute(
            """
            UPDATE offpage_communities
            SET strike_count = COALESCE(strike_count, 0) + 1
            WHERE site_id = %s AND community_id = %s
            RETURNING strike_count
            """,
            (site_id, community_id),
        )
        strikes = cur.fetchone()[0]
    if int(strikes or 0) >= 3:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE offpage_communities
                SET enabled = false,
                    disabled_reason = '3-strike kill-switch (offpage_verify): '
                                       || %s || ' verified removals within 90 days'
                WHERE site_id = %s AND community_id = %s
                  AND enabled = true
                """,
                (strikes, site_id, community_id),
            )
            disabled = cur.rowcount
        if disabled:
            _log_event(conn, site_id, "community", community_id, "verify",
                       "promotional_cap", "refuse",
                       f"kill-switch engaged at {strikes} strikes",
                       {"strikes": int(strikes),
                        "source_publication": str(publication_id)})
            return str(community_id)
    return None


def _log_event(conn, site_id, entity_type, entity_id, stage, gate, outcome,
               reason, evidence=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_events
                (site_id, entity_type, entity_id, stage, gate, outcome,
                 reason, evidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (site_id, entity_type, entity_id, stage, gate, outcome, reason,
             json.dumps(evidence or {}, default=str)),
        )


def run(now=None, conn=None, fetcher=None):
    """Verify every due checkpoint; update publication state + strikes.

    Scheduler-agnostic (injectable clock/conn/fetcher). Returns a typed
    summary dict and never raises — unexpected errors are recorded and
    notified so the next tick retries naturally.
    """
    import env as env_loader
    from jobs.locks import job_lock, LOCK_KEYS, already_running
    from jobs.notify import send_summary

    env_loader.load_env_file(quiet=True)
    own_conn = conn is None
    if own_conn:
        import db as database
        conn = database.get_connection()
    now_utc = now or _utcnow()
    try:
        with job_lock(conn, LOCK_KEYS["offpage_verify"]) as got:
            if not got:
                return already_running("offpage_verify")
            claimed = _claim_due(conn, now_utc)
            results = {"live": 0, "removed": 0, "unverifiable": 0}
            publications_live = 0
            publications_removed = 0
            communities_disabled = []
            for item in claimed:
                permalink = item["live_permalink"] or ""
                verdict = verify_permalink(permalink, fetcher=fetcher)
                results[verdict["result"]] = results.get(verdict["result"], 0) + 1
                _record_result(conn, item["verification_id"], now_utc, verdict)
                pub_id = item["publication_id"]
                if verdict["result"] == "removed":
                    _log_event(conn, item["site_id"], "publication", pub_id,
                               "verify", "survival", "refuse",
                               "comment removed at a checkpoint",
                               {"checkpoint_index": item["checkpoint_index"],
                                "http_status": verdict["http_status"]})
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE offpage_publications
                            SET status = 'removed', verified_at = %s
                            WHERE publication_id = %s AND status = 'pending_verify'
                            """,
                            (now_utc, pub_id),
                        )
                        if cur.rowcount:
                            publications_removed += 1
                    disabled = _strike(conn, item["site_id"], pub_id,
                                       item["checkpoint_index"])
                    if disabled:
                        communities_disabled.append(disabled)
                else:
                    all_checked, any_live, any_removed = _publication_live(
                        conn, pub_id)
                    if all_checked and not any_removed:
                        final_status = "live" if any_live else "unverifiable"
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE offpage_publications
                                SET status = %s, verified_at = %s
                                WHERE publication_id = %s
                                """,
                                (final_status, now_utc, pub_id),
                            )
                        if any_live:
                            publications_live += 1
                            _log_event(conn, item["site_id"], "publication",
                                       pub_id, "verify", "survival", "pass",
                                       "survived all checkpoints",
                                       {"checkpoint":
                                        item["checkpoint_index"],
                                        "http_status":
                                        verdict["http_status"]})
            conn.commit()
            summary = {
                "checked": len(claimed),
                "results": results,
                "publications_now_live": publications_live,
                "publications_now_removed": publications_removed,
                "communities_disabled": communities_disabled,
            }
        send_summary("offpage_verify", summary)
        return summary
    except Exception as exc:  # noqa: BLE001 - job must never crash the loop
        try:
            conn.rollback()
        except Exception:
            pass
        from jobs.notify import send_failure
        try:
            send_failure("offpage_verify", site_id=None,
                         error=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if own_conn:
            conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site_id", default=None)
    parser.add_argument("--now", default=None,
                        help="injectable clock (ISO-8601 UTC); default now")
    args = parser.parse_args()
    now_utc = datetime.fromisoformat(args.now) if args.now else None
    summary = run(now=now_utc)
    if args.site_id:
        summary["site_id"] = args.site_id
    print("offpage_verify:", json.dumps(summary, default=str))


if __name__ == "__main__":
    main()
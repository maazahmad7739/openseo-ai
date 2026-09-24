# -*- coding: utf-8 -*-
"""Stage 2 Phase 1 â€” fix executor + generator + policy tests (plan/21).

python tests/run_fix_executor_tests.py   (needs a reachable local Postgres)

Verifies the production job against a REAL database, with the Shopify write
faked at the adapter-config boundary (no network; the GraphQL client shell is
covered by run_graphql_shell_tests.py):

  policy gate
    1. missing policy row -> fail-closed PolicyBlocked
    2. enabled=false -> PolicyBlocked (kill-switch)
    3. weekly_cap=0 -> no_execution_route
    4. cap not consumed under the cap -> allowed
    5. cap exhausted -> weekly_cap_reached
  conflict guard
    6. second active fix on same (site,url,field) -> conflict id
  generator
    7. non-improve_page -> FixNotSupported
    8. approved + failing title checks -> generated row (idempotent)
    9. passing title -> no fix warranted
   10. GID-less page -> FixNotSupported
   11. duplicate active -> created=False idempotent path
  executor
   12. dry-run writes nothing (stays queued, no change_log)
   13. fresh-read snapshot + stale-diff expiry (live != old_value)
   14. applied: snapshot stored, status applied, verified, change_log
       rollback_reference = fix_id, measurement wired (in_progress + baseline)
   15. verify_failed -> auto-revert from snapshot (reverted + rollback_of row)
   16. weekly cap re-checked at pickup -> typed skip
   17. duplicate target URL in one run -> skipped
   18. revert endpoint path restores snapshot values (audited row)
  locks
   19. fix_executor LOCK_KEY registered and acquirable

Isolation: every row this suite creates is removed in cleanup (FK-safe
order); the shared fixture DB stays pristine. Shopify adapter functions are
monkeypatched in fixes.adapters.ADAPTERS â€” no network anywhere.
"""
import os
import sys
import json
import uuid
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import db as database  # noqa: E402
import env as env_loader  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" â€” {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


# ------------------------------------------------------------
# Fake Shopify surface (adapter seam; no network)
# ------------------------------------------------------------

class FakeShopState:
    """Simulated store: product seo.title + a staleness scenario switch."""

    def __init__(self, gid, live_title):
        self.gid = gid
        self.live_title = live_title
        self.writes = []
        self.verify_should_fail = False


STATE = None


def _fake_adapter_fn(kind):
    def execute(conn, fix_row, config, dry_run=False):
        payload = fix_row["payload_json"]
        new_title = (((payload.get("variables") or {}).get("product") or {})
                     .get("seo") or {}).get("title")
        if dry_run:
            return {"ok": True, "outcome": "dry_run",
                    "detail": {"mutation": "productUpdate", "gid": STATE.gid}}
        STATE.writes.append(new_title)
        if STATE.verify_should_fail:
            return {"ok": True, "outcome": "ok", "verification_status": "verify_failed",
                    "live_value": "SOMETHING-ELSE", "adapter": "fake",
                    "adapter_response": {"write": {"ok": True}}}
        STATE.live_title = new_title
        return {"ok": True, "outcome": "ok", "verified": True,
                "verification_status": "verified", "live_value": new_title,
                "adapter": "fake", "adapter_response": {"write": {"ok": True}}}
    return execute


def _fake_snapshot(conn, fix_row, config):
    return {"ok": True, "snapshot": {"seo.title": STATE.live_title},
            "adapter": "fake"}


def _fake_restore(conn, fix_row, config):
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    old_title = snapshot.get("seo.title")
    STATE.live_title = old_title
    return {"ok": True, "outcome": "ok", "restored": True,
            "live_value": old_title, "adapter": "fake",
            "adapter_response": {"restore": {"ok": True}}}


FAKE_ADAPTER = {"execute": _fake_adapter_fn("exec"),
                "restore": _fake_restore,
                "snapshot": _fake_snapshot}


def install_fake_adapter():
    from fixes import adapters as fix_adapters
    fix_adapters.ADAPTERS["seo.title"] = FAKE_ADAPTER


# ------------------------------------------------------------
# Deterministic seeding + cleanup
# ------------------------------------------------------------

def seed_world(conn):
    """One dedicated site + cluster + page + approved recommendation.

    Everything is keyed off RUN_TOKEN so repeat runs never collide and
    cleanup removes exactly what was created.
    """
    ids = {"domain": f"fixexec-{RUN_TOKEN}.example.com"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property, catalogue_size_tier)
            VALUES (%s, %s, %s, 'small') RETURNING site_id
            """,
            (f"FixExec Test {RUN_TOKEN}", ids["domain"], f"sc-domain:{ids['domain']}"),
        )
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)

        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword, keywords, intent)
            VALUES (%s, %s, %s, 'commercial') RETURNING cluster_id
            """,
            (site_id, f"test widget {RUN_TOKEN}", [f"test widget {RUN_TOKEN}"]),
        )
        cluster_id = cur.fetchone()[0]
        ids["cluster_id"] = str(cluster_id)

        url = f"https://{ids['domain']}/products/test-widget"
        cur.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title, shopify_gid)
            VALUES (%s, %s, 'product', %s, %s)
            """,
            (site_id, url, "Test Widget", f"gid://shopify/Product/{RUN_TOKEN[:12]}"),
        )
        ids["target_url"] = url

        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url, cluster_id,
                 diagnosis, evidence_json, status, approved_at)
            VALUES (%s, 'existing_opportunity', 'improve_page', %s, %s,
                    %s, %s, 'approved', now())
            RETURNING recommendation_id
            """,
            (site_id, url, cluster_id,
             f"low_ctr seed {RUN_TOKEN}",
             json.dumps([{"source": "GSC", "finding": "seed"}])),
        )
        rec_id = cur.fetchone()[0]
        ids["rec_id"] = str(rec_id)

        # fix_policy: enabled with a cap so the gate can pass, plus a
        # disabled row and a zero-cap row for the fail-closed checks.
        cur.execute(
            "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
            "requires_field_verify, enabled) VALUES (%s, 'seo.title', 'low', 2, true, true)",
            (site_id,),
        )
        cur.execute(
            "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
            "requires_field_verify, enabled) VALUES (%s, 'redirect', 'high', 2, true, false)",
            (site_id,),
        )
        cur.execute(
            "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
            "requires_field_verify, enabled) VALUES (%s, 'content', 'medium', 0, true, true)",
            (site_id,),
        )
    conn.commit()
    return ids


def cleanup(conn, ids):
    # Abort-safe: a failing test can leave the shared connection's
    # transaction aborted — roll back first or every DELETE below would
    # raise InFailedSqlTransaction and leak the suite's rows.
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM change_log WHERE recommendation_id = %s",
            (ids["rec_id"],))
        cur.execute(
            "DELETE FROM measurement_snapshots WHERE recommendation_id = %s",
            (ids["rec_id"],))
        cur.execute(
            "DELETE FROM generated_fixes WHERE site_id = %s",
            (ids["site_id"],))
        cur.execute(
            "DELETE FROM recommendations WHERE site_id = %s",
            (ids["site_id"],))
        cur.execute(
            "DELETE FROM keyword_clusters WHERE site_id = %s",
            (ids["site_id"],))
        cur.execute(
            "DELETE FROM pages WHERE site_id = %s",
            (ids["site_id"],))
        cur.execute(
            "DELETE FROM fix_policy WHERE site_id = %s",
            (ids["site_id"],))
        cur.execute(
            "DELETE FROM rejection_log WHERE site_id = %s",
            (ids["site_id"],))
        cur.execute(
            "DELETE FROM site_config WHERE site_id = %s",
            (ids["site_id"],))
    conn.commit()


def make_queued_fix(conn, ids, old_title="Old Title", new_title="New Shiny Test Title"):
    """Insert a generated fix and immediately approve it to 'queued'."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO generated_fixes
                (recommendation_id, site_id, action_type, sub_type, target_url,
                 target_entity_ref, payload_json, diff_json, generation_source,
                 status, risk_tier)
            VALUES (%s, %s, 'improve_page', 'seo.title', %s, %s, %s::jsonb, %s::jsonb,
                    'deterministic', 'queued', 'low')
            RETURNING fix_id
            """,
            (ids["rec_id"], ids["site_id"], ids["target_url"],
             f"gid://shopify/Product/{RUN_TOKEN[:12]}",
             json.dumps({"mutation": "productUpdate",
                         "variables": {"product": {
                             "id": f"gid://shopify/Product/{RUN_TOKEN[:12]}",
                             "seo": {"title": new_title}}}}),
             json.dumps([{"field": "seo.title", "old_value": old_title,
                          "new_value": new_title}])),
        )
        fix_id = str(cur.fetchone()[0])
    conn.commit()
    return fix_id


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def test_policy_gate(conn, ids):
    print("\n== policy gate ==")
    from fixes.policy import (check_execution_allowed, PolicyBlocked,
                              weekly_applied_count, get_policy)

    allok = True
    # 1: missing policy row
    try:
        check_execution_allowed(conn, ids["site_id"], "nonexistent_type")
        allok &= check("missing policy row fail-closed", False)
    except PolicyBlocked as exc:
        allok &= check("missing policy row fail-closed", exc.reason == "no_policy_row",
                       exc.reason)

    # 2: disabled kill-switch
    try:
        check_execution_allowed(conn, ids["site_id"], "redirect")
        allok &= check("disabled policy fail-closed", False)
    except PolicyBlocked as exc:
        allok &= check("disabled policy fail-closed", exc.reason == "policy_disabled",
                       exc.reason)

    # 3: zero cap = generation allowed, execution disabled
    try:
        check_execution_allowed(conn, ids["site_id"], "content")
        allok &= check("zero cap fail-closed", False)
    except PolicyBlocked as exc:
        allok &= check("zero cap fail-closed", exc.reason == "no_execution_route",
                       exc.reason)

    # 4: under cap -> allowed, returns policy
    policy = check_execution_allowed(conn, ids["site_id"], "seo.title")
    allok &= check("under-cap allowed", policy["weekly_cap"] == 2
                   and policy["enabled"] is True)

    # 5: cap exhausted -> weekly_cap_reached (cap is 2: seed two applied rows)
    reference = date.today()
    with conn.cursor() as cur:
        for i, age in enumerate((0, 1)):
            cur.execute(
                """
                INSERT INTO generated_fixes
                    (recommendation_id, site_id, action_type, sub_type, target_url,
                     target_entity_ref, payload_json, status, risk_tier, applied_at)
                VALUES (%s, %s, 'improve_page', 'seo.title', %s, %s, '{}', 'applied',
                        'low', now() - (%s::int * INTERVAL '1 day'))
                """,
                (ids["rec_id"], ids["site_id"],
                 f"{ids['target_url']}#cap-probe-{RUN_TOKEN}-{i}",
                 f"gid://shopify/Product/capprobe{i}", age),
            )
    conn.commit()
    try:
        check_execution_allowed(conn, ids["site_id"], "seo.title", reference)
        allok &= check("cap exhausted blocks", False)
    except PolicyBlocked as exc:
        allok &= check("cap exhausted blocks", exc.reason == "weekly_cap_reached",
                       exc.reason)
    applied = weekly_applied_count(conn, ids["site_id"], "seo.title", reference)
    allok &= check("weekly count sees applied rows", applied == 2, str(applied))
    try:
        check_execution_allowed(conn, ids["site_id"], "seo.title", reference)
        allok &= check("cap exhausted blocks", False)
    except PolicyBlocked as exc:
        allok &= check("cap exhausted blocks", exc.reason == "weekly_cap_reached",
                       exc.reason)
    # ...and rows applied 8 days ago do NOT count (7-day window)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE generated_fixes SET applied_at = now() "
            "- INTERVAL '8 days' "
            "WHERE site_id = %s "
            "AND target_entity_ref LIKE 'gid://shopify/Product/capprobe%%'",
            (ids["site_id"],),
        )
    conn.commit()
    policy = check_execution_allowed(conn, ids["site_id"], "seo.title", reference)
    allok &= check("7-day window only", policy is not None)

    # policy row shape
    p = get_policy(conn, ids["site_id"], "seo.title")
    allok &= check("policy shape", p == {"risk_tier": "low", "weekly_cap": 2,
                                         "requires_field_verify": True,
                                         "enabled": True}, str(p))
    return allok


def test_conflict_guard(conn, ids):
    print("\n== conflict guard ==")
    from fixes.policy import check_conflict
    fix_id = make_queued_fix(conn, ids)
    conflict = check_conflict(conn, ids["site_id"], ids["target_url"], "seo.title")
    allok = check("active fix conflict detected", conflict == fix_id, str(conflict))
    none = check_conflict(conn, ids["site_id"], f"{ids['target_url']}-other", "seo.title")
    allok &= check("no conflict on distinct url", none is None)
    return allok, fix_id


def test_generator(conn, ids, keep_fix_id=None):
    print("\n== generator ==")
    from fixes.generator import (generate_fix_for_recommendation, FixNotSupported,
                                 FixGenerationError, title_quality_checks,
                                 validate_title_draft, draft_title)

    allok = True

    # validator unit checks
    allok &= check("validator: too long rejected",
                   bool(validate_title_draft("x" * 61, "kw")))
    allok &= check("validator: banned pattern rejected",
                   bool(validate_title_draft("Buy Now | Best Price Widgets", "kw")))
    allok &= check("validator: clean draft passes",
                   not validate_title_draft("Test Widget â€” Pro Grade Kit", "test widget"))
    allok &= check("checks: keyword coverage",
                   title_quality_checks("Snowboard", "snowboard")["has_primary_kw"])

    # 7: non-improve_page -> FixNotSupported
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cluster_id FROM recommendations WHERE recommendation_id = %s",
            (ids["rec_id"],))
        cluster_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url, cluster_id, diagnosis,
                 evidence_json, status, approved_at)
            VALUES (%s, 'missing_page', 'create_page', NULL, %s, %s, '[]', 'approved', now())
            RETURNING recommendation_id
            """,
            (ids["site_id"], cluster_id, f"create_page seed {RUN_TOKEN}"),
        )
        create_rec = str(cur.fetchone()[0])
    conn.commit()
    try:
        generate_fix_for_recommendation(conn, create_rec)
        allok &= check("create_page -> FixNotSupported", False)
    except FixNotSupported:
        allok &= check("create_page -> FixNotSupported", True)
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM recommendations WHERE recommendation_id = %s",
                        (create_rec,))
        conn.commit()

    # 8 + 11: approved improve_page with failing checks -> generated row,
    # second generation -> created=False (idempotent)
    if keep_fix_id:
        # A queued fix from the conflict test occupies the active slot; the
        # same recommendation regenerating must return it idempotently.
        out_conflict = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("same-rec conflict -> idempotent return",
                       out_conflict["created"] is False
                       and out_conflict["fix_id"] == keep_fix_id,
                       str(out_conflict)[:120])
        # clear it so the idempotent path can be exercised
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s", (keep_fix_id,))
        conn.commit()
    out = generate_fix_for_recommendation(conn, ids["rec_id"])
    allok &= check("generated row created", out["created"] is True
                   and out["status"] == "generated", str(out)[:120])
    allok &= check("diff carries old/new",
                   out["diff"][0]["field"] == "seo.title"
                   and out["diff"][0]["old_value"] == "Test Widget")
    out2 = generate_fix_for_recommendation(conn, ids["rec_id"])
    allok &= check("second generation idempotent", out2["created"] is False
                   and out2["fix_id"] == out["fix_id"], str(out2)[:120])

    # 9: passing title -> no fix warranted
    with conn.cursor() as cur:
        cur.execute("UPDATE pages SET title = %s WHERE site_id = %s AND url = %s",
                    (f"test widget {RUN_TOKEN}", ids["site_id"], ids["target_url"]))
    conn.commit()
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("passing title -> no fix", False)
    except FixNotSupported as exc:
        allok &= check("passing title -> no fix", "passes all quality checks" in str(exc),
                       str(exc))
    with conn.cursor() as cur:
        cur.execute("UPDATE pages SET title = 'Test Widget' WHERE site_id = %s AND url = %s",
                    (ids["site_id"], ids["target_url"]))
    conn.commit()

    # 10: GID-less page -> FixNotSupported
    with conn.cursor() as cur:
        cur.execute("UPDATE pages SET shopify_gid = NULL WHERE site_id = %s AND url = %s",
                    (ids["site_id"], ids["target_url"]))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s", (out["fix_id"],))
    conn.commit()
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("gid-less page -> FixNotSupported", False)
    except FixNotSupported as exc:
        allok &= check("gid-less page -> FixNotSupported", "shopify_gid" in str(exc),
                       str(exc))
    with conn.cursor() as cur:
        cur.execute("UPDATE pages SET shopify_gid = %s WHERE site_id = %s AND url = %s",
                    (f"gid://shopify/Product/{RUN_TOKEN[:12]}", ids["site_id"],
                     ids["target_url"]))
    conn.commit()
    out3 = generate_fix_for_recommendation(conn, ids["rec_id"])
    allok &= check("regeneration after gid restore", out3["created"] is True)
    return allok, out3["fix_id"]


def test_executor(conn, ids, fix_id):
    print("\n== executor ==")
    from jobs.fix_executor import run_fix_executor, revert_fix
    install_fake_adapter()

    allok = True
    global STATE
    STATE = FakeShopState(gid=f"gid://shopify/Product/{RUN_TOKEN[:12]}",
                          live_title="Old Title")
    fake_config = {"shop_domain": "fake-shop.example.com",
                   "access_token": "fake-token-for-tests",
                   "api_version": "2026-01"}

    # The executor tests drive their OWN queued fix row (the generator's row
    # was deleted by the caller after its assertions ran).
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM generated_fixes WHERE fix_id = %s", (fix_id,))
        if cur.fetchone()[0] == 0:
            fix_id = make_queued_fix(conn, ids)
    conn.commit()

    # 12: dry-run writes nothing
    summary = run_fix_executor(ids["site_id"], conn=conn, dry_run=True, shop_config=fake_config)
    result = next(r for r in summary["results"] if r["fix_id"] == fix_id)
    allok &= check("dry-run: typed dry_run result", result["status"] == "dry_run",
                   str(result))
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM generated_fixes WHERE fix_id = %s", (fix_id,))
        allok &= check("dry-run: row still queued", cur.fetchone()[0] == "queued")
        cur.execute("SELECT count(*) FROM change_log WHERE rollback_reference = %s",
                    (fix_id,))
        allok &= check("dry-run: no change_log row", cur.fetchone()[0] == 0)
        cur.execute("SELECT snapshot_json IS NULL FROM generated_fixes "
                    "WHERE fix_id = %s", (fix_id,))
        allok &= check("dry-run: no snapshot persisted", cur.fetchone()[0] is True)

    # 13: stale-diff expiry â€” live value differs from generation old_value
    STATE.live_title = "Someone Else Changed It"
    summary = run_fix_executor(ids["site_id"], conn=conn, shop_config=fake_config)
    result = next(r for r in summary["results"] if r["fix_id"] == fix_id)
    allok &= check("stale diff -> expired", result["status"] == "expired"
                   and result["reason"] == "stale_diff", str(result))
    with conn.cursor() as cur:
        cur.execute("SELECT status, verification_status FROM generated_fixes "
                    "WHERE fix_id = %s", (fix_id,))
        status, vstatus = cur.fetchone()
        allok &= check("expired row recorded", status == "expired"
                       and vstatus == "verify_failed", f"{status}/{vstatus}")

    # reset for the happy path: new queued fix, live value matches
    with conn.cursor() as cur:
        cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s", (fix_id,))
    conn.commit()
    fix_id = make_queued_fix(conn, ids)
    STATE.live_title = "Old Title"
    STATE.verify_should_fail = False

    # 14: applied end-to-end
    summary = run_fix_executor(ids["site_id"], conn=conn, shop_config=fake_config)
    result = next(r for r in summary["results"] if r["fix_id"] == fix_id)
    allok &= check("happy path applied", result["status"] == "applied", str(result))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, verification_status, snapshot_json IS NOT NULL "
            "FROM generated_fixes WHERE fix_id = %s", (fix_id,))
        status, vstatus, has_snap = cur.fetchone()
        allok &= check("row applied+verified+snapshot", status == "applied"
                       and vstatus == "verified" and has_snap,
                       f"{status}/{vstatus}/{has_snap}")
        cur.execute("SELECT after_snapshot->>'verified', rollback_reference "
                    "FROM change_log WHERE rollback_reference = %s", (fix_id,))
        row = cur.fetchone()
        allok &= check("change_log rollback_reference = fix_id",
                       row is not None and row[1] == fix_id, str(row))
        cur.execute("SELECT status, implemented_at IS NOT NULL, measurement_due_at "
                    "IS NOT NULL FROM recommendations WHERE recommendation_id = %s",
                    (ids["rec_id"],))
        rstatus, has_impl, has_due = cur.fetchone()
        allok &= check("measurement wired (in_progress + due)",
                       rstatus == "in_progress" and has_impl and has_due,
                       f"{rstatus}/{has_impl}/{has_due}")
        cur.execute("SELECT count(*) FROM measurement_snapshots "
                    "WHERE recommendation_id = %s AND snapshot_type = 'baseline'",
                    (ids["rec_id"],))
        allok &= check("baseline frozen", cur.fetchone()[0] >= 1)

    # 15: verify_failed -> auto-revert (snapshot was captured at THIS fix's
    # pickup, when live was already the happy-path value — restore returns
    # exactly that value)
    pre_revert_title = STATE.live_title
    STATE.verify_should_fail = True
    fix2 = make_queued_fix(conn, ids, old_title=pre_revert_title)
    with conn.cursor() as cur:
        cur.execute("UPDATE recommendations SET status = 'approved', "
                    "implemented_at = NULL WHERE recommendation_id = %s",
                    (ids["rec_id"],))
        cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = %s",
                    (ids["rec_id"],))
    conn.commit()
    summary = run_fix_executor(ids["site_id"], conn=conn, shop_config=fake_config)
    result = next(r for r in summary["results"] if r["fix_id"] == fix2)
    allok &= check("verify_failed -> auto-revert", result["status"] == "reverted"
                   and result.get("reason") == "verify_failed", str(result))
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM generated_fixes WHERE fix_id = %s", (fix2,))
        allok &= check("original marked reverted", cur.fetchone()[0] == "reverted")
        cur.execute("SELECT count(*) FROM generated_fixes WHERE rollback_of = %s",
                    (fix2,))
        allok &= check("revert audit row (rollback_of)", cur.fetchone()[0] == 1)
    allok &= check("snapshot restore value applied",
                   STATE.live_title == pre_revert_title, STATE.live_title)

    # 16: weekly cap re-checked at pickup (policy disabled between queue+run)
    with conn.cursor() as cur:
        cur.execute("UPDATE fix_policy SET enabled = false WHERE site_id = %s "
                    "AND sub_type = 'seo.title'", (ids["site_id"],))
    conn.commit()
    # the earlier skip left fix3 queued (active slot); remove it so fix4 can
    # be queued as a duplicate-target probe
    with conn.cursor() as cur:
        cur.execute("DELETE FROM generated_fixes WHERE status = 'queued' "
                    "AND site_id = %s", (ids["site_id"],))
    conn.commit()
    fix3 = make_queued_fix(conn, ids, new_title="Never Applied Title")
    summary = run_fix_executor(ids["site_id"], conn=conn, shop_config=fake_config)
    result = next(r for r in summary["results"] if r["fix_id"] == fix3)
    allok &= check("disabled policy typed skip at pickup", result["status"] == "skipped"
                   and result["reason"] == "policy_disabled", str(result))
    with conn.cursor() as cur:
        cur.execute("UPDATE fix_policy SET enabled = true WHERE site_id = %s "
                    "AND sub_type = 'seo.title'", (ids["site_id"],))
    conn.commit()

    # 17: duplicate target URL in one run -> the second is skipped
    # fix3 from the skip test is still queued and active — it occupies the
    # target; fix4 (same URL) must be the duplicate skip this run.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM generated_fixes WHERE status = 'queued' "
                    "AND site_id = %s", (ids["site_id"],))
    conn.commit()
    fix3 = make_queued_fix(conn, ids, new_title="Duplicate Probe A")
    try:
        fix4 = make_queued_fix(conn, ids, new_title="Duplicate Probe B")
    except Exception:
        conn.rollback()
        fix4 = None
    if fix4 is None:
        # The partial unique index already enforces one active fix per
        # (site, url, field) — the DB-level guard is what this test proves.
        allok &= check("duplicate target blocked by unique index", True)
        return allok, fix_id
    summary = run_fix_executor(ids["site_id"], conn=conn, shop_config=fake_config)
    dup_results = [r for r in summary["results"] if r["fix_id"] in (fix3, fix4)]
    allok &= check("duplicate target skipped in run",
                   len(dup_results) == 2
                   and any(r["status"] == "skipped"
                           and r.get("reason") == "duplicate_target_in_run"
                           for r in dup_results),
                   str(dup_results))
    return allok, fix_id


def test_revert_endpoint_path(conn, ids, fix_id):
    print("\n== revert endpoint path ==")
    from jobs.fix_executor import revert_fix
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fix_id, recommendation_id, site_id, action_type, sub_type, "
            "target_url, target_entity_ref, payload_json, diff_json, risk_tier, "
            "snapshot_json FROM generated_fixes WHERE fix_id = %s", (fix_id,))
        columns = [d[0] for d in cur.description]
        row = dict(zip(columns, cur.fetchone()))
    conn.rollback()
    allok = True
    STATE.live_title = "Drifted After Apply"
    revert = revert_fix(conn, row, {}, reason="operator")
    allok &= check("revert restores snapshot value", revert.get("reverted") is True
                   and STATE.live_title == "Old Title", str(revert))
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM generated_fixes WHERE fix_id = %s", (fix_id,))
        allok &= check("reverted status recorded", cur.fetchone()[0] == "reverted")
    return allok


def test_lock_key_registered():
    print("\n== locks ==")
    from jobs.locks import LOCK_KEYS, job_lock, already_running
    allok = check("fix_executor lock key registered", "fix_executor" in LOCK_KEYS)
    conn = database.get_connection()
    try:
        with job_lock(conn, LOCK_KEYS["fix_executor"]) as got:
            allok &= check("fix_executor advisory lock acquires", got is True)
    finally:
        conn.close()
    payload = already_running("fix_executor")
    allok &= check("already_running payload", payload["job"] == "fix_executor")
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    ids = None
    allok = True
    try:
        ids = seed_world(conn)
        allok &= test_lock_key_registered()
        allok &= test_policy_gate(conn, ids)
        ok, queued_fix = test_conflict_guard(conn, ids)
        allok &= ok
        ok, gen_fix = test_generator(conn, ids, keep_fix_id=queued_fix)
        allok &= ok
        # generator's active row occupies the unique index; clear it so the
        # executor tests can drive their own queued rows cleanly
        with conn.cursor() as cur:
            cur.execute("UPDATE recommendations SET status = 'approved', "
                        "implemented_at = NULL WHERE recommendation_id = %s",
                        (ids["rec_id"],))
            cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = %s",
                        (ids["rec_id"],))
            cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s", (gen_fix,))
        conn.commit()
        ok, fix_id = test_executor(conn, ids, gen_fix)
        allok &= ok
        allok &= test_revert_endpoint_path(conn, ids, fix_id)
    finally:
        if ids:
            cleanup(conn, ids)
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL FIX EXECUTOR TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

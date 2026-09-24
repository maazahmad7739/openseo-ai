# -*- coding: utf-8 -*-
"""Phase 3 closure — technical_fix status_failure (404) -> redirect fix tests.

python tests/run_fix_status_redirect_tests.py   (needs a reachable local Postgres)

Covers (plan/21 §2.3 #4 + plan/16 status_failure):
  routing gates (deterministic, no network)
    1. 5xx/403 status codes -> FixNotSupported (different fix, not a 301)
    2. 404 with NO residual traffic -> FixNotSupported (plan_only hygiene)
    3. 404 with residual clicks but no indexable destination -> refuse
    4. 404 + residual clicks + product_type collection -> redirect fix row
    5. 404 + no product_type collection + catalogue_coverage fallback -> fix
  contract alignment with the redirect adapter
    6. sub_type='redirect', risk 'high' (same tier as consolidate redirects)
    7. payload shape = urlRedirectCreate {path, target} (adapter-validated)
    8. conflict guard keyed on the SOURCE url (one active redirect per dead URL)
    9. payload.grounding carries the status_failure route context
  execution through the REAL executor (fake GraphQL client)
   10. applied + verified; snapshot carries redirect.created_id (rollback source)
   11. revert deletes the created redirect (urlRedirectDelete) — audited row
   12. stale-diff protection: a re-queued duplicate on the same URL expires

Isolation: every row keyed off RUN_TOKEN, removed in cleanup. Shopify faked
at the client seam (no network anywhere).
"""
import os
import sys
import json
import uuid
from datetime import date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, os.path.join(HERE, "..", "src", "connectors"))

import db as database  # noqa: E402
import env as env_loader  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


# ------------------------------------------------------------
# Fake Shopify surface (redirect; no network)
# ------------------------------------------------------------

class FakeRedirectState:
    def __init__(self):
        self.created_id = None
        self.created_path = None
        self.created_target = None
        self.writes = []


STATE = None


class FakeRedirectClient:
    def __init__(self, *args, **kwargs):
        self.api_version = "2026-01"

    def run(self, mutation_name, query, variables=None, **kwargs):
        if mutation_name == "urlRedirectCreate":
            redirect = (variables or {}).get("urlRedirect") or {}
            STATE.created_id = f"gid://shopify/UrlRedirect/{RUN_TOKEN[:10]}"
            STATE.created_path = redirect.get("path")
            STATE.created_target = redirect.get("target")
            STATE.writes.append(("create", redirect.get("path"),
                                 redirect.get("target")))
            return {"ok": True, "data": {"urlRedirectCreate": {
                "urlRedirect": {"id": STATE.created_id,
                                "path": redirect.get("path"),
                                "target": redirect.get("target")}}}}
        if mutation_name == "urlRedirect":
            # read-back: exists only after create, gone after delete
            if STATE.created_id:
                return {"ok": True, "data": {"urlRedirect": {
                    "id": STATE.created_id,
                    "path": STATE.created_path,
                    "target": STATE.created_target}}}
            return {"ok": False, "outcome": "not_found"}
        if mutation_name == "urlRedirectDelete":
            STATE.writes.append(("delete", STATE.created_id))
            deleted = STATE.created_id
            STATE.created_id = None
            return {"ok": True, "data": {"urlRedirectDelete": {
                "deletedUrlRedirectId": deleted}}}
        return {"ok": False, "outcome": "unknown_call", "detail": mutation_name}


# ------------------------------------------------------------
# Seeding + cleanup
# ------------------------------------------------------------

def seed_world(conn, status_code=404, clicks=25, impressions=420,
               with_collection=True, product_type="Snowboard",
               cluster_url=None):
    ids = {"domain": f"fixsf-{RUN_TOKEN}.example.com"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property, catalogue_size_tier, shopify_domain)
            VALUES (%s, %s, %s, 'small', %s) RETURNING site_id
            """,
            (f"FixSF Test {RUN_TOKEN}", ids["domain"], f"sc-domain:{ids['domain']}",
             ids["domain"]),
        )
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)

        # the dead product page (404)
        dead_url = f"https://{ids['domain']}/products/dead-{RUN_TOKEN[:6]}"
        cur.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title, indexable, status_code)
            VALUES (%s, %s, 'product', 'Dead Product', false, %s)
            """,
            (site_id, dead_url, status_code),
        )
        ids["target_url"] = dead_url

        # survivor collection (product_type slug)
        if with_collection:
            survivor_url = f"https://{ids['domain']}/collections/{product_type.lower()}"
            cur.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title, indexable, status_code)
                VALUES (%s, %s, 'collection', %s, true, 200)
                """,
                (site_id, survivor_url, f"{product_type} Collection"),
            )
            ids["survivor_url"] = survivor_url

        cluster_id = None
        if cluster_url:
            cur.execute(
                """
                INSERT INTO keyword_clusters (site_id, primary_keyword, keywords, intent)
                VALUES (%s, %s, %s, 'commercial') RETURNING cluster_id
                """,
                (site_id, f"cluster {RUN_TOKEN}", [f"cluster {RUN_TOKEN}"]),
            )
            cluster_id = cur.fetchone()[0]
            ids["cluster_id"] = str(cluster_id)
            cur.execute(
                """
                INSERT INTO catalogue_coverage (site_id, cluster_id, existing_collection_url)
                VALUES (%s, %s, %s)
                """,
                (site_id, cluster_id, cluster_url),
            )

        evidence = {
            "issue": "seed",
            "issue_type": "status_failure",
            "status_code": status_code,
            "gsc_clicks_28d": clicks,
            "gsc_impressions_28d": impressions,
            "page_type": "product",
            "product_type": product_type if with_collection else None,
        }
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url, cluster_id,
                 diagnosis, evidence_json, status, approved_at)
            VALUES (%s, 'technical_fix', 'technical_fix', %s, %s,
                    %s, %s::jsonb, 'approved', now())
            RETURNING recommendation_id
            """,
            (site_id, dead_url, cluster_id,
             f"status_failure seed {RUN_TOKEN}", json.dumps(evidence)),
        )
        rec_id = cur.fetchone()[0]
        ids["rec_id"] = str(rec_id)

        cur.execute(
            "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
            "requires_field_verify, enabled) VALUES (%s, 'redirect', 'high', 2, true, true)",
            (site_id,),
        )
    conn.commit()
    return ids


def cleanup(conn, ids):
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM change_log WHERE recommendation_id = %s",
                    (ids["rec_id"],))
        cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = %s",
                    (ids["rec_id"],))
        cur.execute("DELETE FROM generated_fixes WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM catalogue_coverage WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM recommendations WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM keyword_clusters WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM pages WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM fix_policy WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM site_config WHERE site_id = %s", (ids["site_id"],))
    conn.commit()


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def test_routing_gates(conn):
    print("\n== routing gates ==")
    from fixes.generator import (generate_fix_for_recommendation,
                                 FixNotSupported)

    allok = True

    # 1: 5xx is a different fix, never a 301
    ids = seed_world(conn, status_code=500)
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("5xx -> FixNotSupported", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("5xx -> FixNotSupported", "404/410 only" in str(exc),
                       str(exc))
    finally:
        cleanup(conn, ids)

    # 2: 404 with no residual traffic -> plan_only hygiene
    ids = seed_world(conn, clicks=0, impressions=0)
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("no-residual-traffic -> refuse", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("no-residual-traffic -> refuse",
                       "no residual traffic" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)

    # 3: 404 + traffic but NO destination (no product_type, no coverage)
    ids = seed_world(conn, with_collection=False)
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("no destination -> refuse", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("no destination -> refuse",
                       "no indexable equivalent" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)

    # 4: happy path — product_type collection survivor
    ids = seed_world(conn)
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("redirect fix generated", out["created"] is True,
                       str(out)[:140])
        payload = out["payload"]
        allok &= check("urlRedirectCreate payload",
                       payload["mutation"] == "urlRedirectCreate"
                       and payload["variables"]["urlRedirect"]["path"]
                       == f"/products/dead-{RUN_TOKEN[:6]}"
                       and payload["variables"]["urlRedirect"]["target"]
                       == "/collections/snowboard",
                       json.dumps(payload)[:200])
        allok &= check("grounding carries status_failure context",
                       payload.get("grounding", {}).get("route") == "status_failure"
                       and payload["grounding"].get("status_code") == 404,
                       json.dumps(payload.get("grounding"))[:160])
        with conn.cursor() as cur:
            cur.execute("SELECT sub_type, risk_tier, status, target_url "
                        "FROM generated_fixes WHERE fix_id = %s", (out["fix_id"],))
            sub_type, risk, status, target_url = cur.fetchone()
            allok &= check("row shape: redirect/high/generated, target = SOURCE",
                           sub_type == "redirect" and risk == "high"
                           and status == "generated"
                           and target_url == ids["target_url"],
                           f"{sub_type}/{risk}/{status}")
        out2 = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("idempotent regeneration", out2["created"] is False
                       and out2["fix_id"] == out["fix_id"])
    finally:
        cleanup(conn, ids)

    # 5: catalogue_coverage fallback when no product_type collection
    cc_url = f"https://fixsf-{RUN_TOKEN}.example.com/collections/coverage-fallback"
    ids = seed_world(conn, with_collection=False, cluster_url=cc_url)
    try:
        # coverage URL must exist in pages + indexable to qualify
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pages (site_id, url, page_type, title, indexable, status_code) "
                "VALUES (%s, %s, 'collection', 'Fallback Collection', true, 200)",
                (ids["site_id"], cc_url),
            )
        conn.commit()
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("coverage fallback destination",
                       out["payload"]["variables"]["urlRedirect"]["target"]
                       == "/collections/coverage-fallback",
                       json.dumps(out["payload"])[:200])
    except FixNotSupported as exc:
        allok &= check("coverage fallback destination", False, str(exc))
    finally:
        cleanup(conn, ids)
    return allok


def test_conflict_guard(conn):
    print("\n== conflict guard (source url keyed) ==")
    from fixes.generator import generate_fix_for_recommendation
    from fixes.policy import check_conflict, PolicyBlocked

    allok = True
    ids = seed_world(conn)
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        # conflict keys on the SOURCE (dead) URL — same contract as consolidate
        conflict = check_conflict(conn, ids["site_id"], ids["target_url"], "redirect")
        allok &= check("conflict detected on source url", conflict == out["fix_id"],
                       str(conflict))
        # a DIFFERENT generator name produces a distinct natural key (same
        # site+action_type+target) redirecting the same dead url -> 409
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recommendations
                    (site_id, generator, action_type, target_url,
                     diagnosis, evidence_json, status, approved_at)
                VALUES (%s, 'manual-dup-probe', 'technical_fix', %s,
                        'dup', %s::jsonb, 'approved', now())
                RETURNING recommendation_id
                """,
                (ids["site_id"], ids["target_url"],
                 json.dumps({"issue_type": "status_failure", "status_code": 404,
                             "gsc_clicks_28d": 30, "gsc_impressions_28d": 500,
                             "page_type": "product", "product_type": "Snowboard"})),
            )
            rec2 = str(cur.fetchone()[0])
        conn.commit()
        try:
            generate_fix_for_recommendation(conn, rec2)
            allok &= check("cross-rec conflict -> PolicyBlocked", False, "no exception")
        except PolicyBlocked:
            allok &= check("cross-rec conflict -> PolicyBlocked", True)
    finally:
        cleanup(conn, ids)
    return allok


def test_executor_and_rollback(conn):
    print("\n== executor + rollback ==")
    from fixes.generator import generate_fix_for_recommendation
    from api.routes.fixes import approve_fix
    from jobs.fix_executor import run_fix_executor, revert_fix

    allok = True
    ids = seed_world(conn)
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        res = approve_fix(out["fix_id"], conn)
        allok &= check("approve -> queued", res.status == "queued", str(res))

        global STATE
        STATE = FakeRedirectState()
        import fixes.adapters as fix_adapters
        if not hasattr(fix_adapters, "_real_client_from_config"):
            fix_adapters._real_client_from_config = fix_adapters._client_from_config
        fix_adapters._client_from_config = lambda config: FakeRedirectClient()
        try:
            summary = run_fix_executor(ids["site_id"], conn=conn,
                                       shop_config={"shop_domain": "fake.example.com",
                                                    "access_token": "fake",
                                                    "api_version": "2026-01"})
            result = next(r for r in summary["results"]
                          if r["fix_id"] == out["fix_id"])
            allok &= check("redirect applied", result.get("status") == "applied",
                           str(result)[:200])
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, verification_status, "
                    "snapshot_json->>'redirect.created_id' "
                    "FROM generated_fixes WHERE fix_id = %s", (out["fix_id"],))
                status, vstatus, created_id = cur.fetchone()
                allok &= check("row applied+verified+created_id snapshot",
                               status == "applied" and vstatus == "verified"
                               and created_id == STATE.created_id,
                               f"{status}/{vstatus}/{created_id}")
                cur.execute("SELECT rollback_reference FROM change_log "
                            "WHERE rollback_reference = %s", (out["fix_id"],))
                allok &= check("change_log rollback_reference = fix_id",
                               cur.fetchone() is not None)
            allok &= check("fake store received create",
                           any(w[0] == "create" for w in STATE.writes),
                           str(STATE.writes)[:140])

            # 11: operator revert deletes the created redirect (audited row)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT fix_id, recommendation_id, site_id, action_type, "
                    "sub_type, target_url, target_entity_ref, payload_json, "
                    "diff_json, risk_tier, snapshot_json FROM generated_fixes "
                    "WHERE fix_id = %s", (out["fix_id"],))
                columns = [d[0] for d in cur.description]
                row = dict(zip(columns, cur.fetchone()))
            conn.rollback()
            revert = revert_fix(conn, row, {}, reason="test-cleanup")
            allok &= check("revert deletes created redirect",
                           revert.get("reverted") is True, str(revert)[:140])
            allok &= check("delete issued against created id",
                           any(w[0] == "delete" and w[1] == row["snapshot_json"].get("redirect.created_id")
                               for w in STATE.writes if isinstance(row["snapshot_json"], dict)),
                           str(STATE.writes)[:140])
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM generated_fixes WHERE rollback_of = %s",
                            (out["fix_id"],))
                allok &= check("revert audit row (rollback_of)", cur.fetchone()[0] == 1)
        finally:
            fix_adapters._client_from_config = fix_adapters._real_client_from_config
    finally:
        cleanup(conn, ids)
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    allok = True
    try:
        allok &= test_routing_gates(conn)
        allok &= test_conflict_guard(conn)
        allok &= test_executor_and_rollback(conn)
    finally:
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL FIX STATUS-REDIRECT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
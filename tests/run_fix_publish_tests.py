# -*- coding: utf-8 -*-
"""Stage 2 Phase 3 — publish-state fix path tests (plan/21 §2.3 #1/#3).

python tests/run_fix_publish_tests.py   (needs a reachable local Postgres)

Covers:
  generator routing
    1. non-publish issue_type -> FixNotSupported
    2. mechanism_hypothesis != publish_state -> FixNotSupported (plan_only)
    3. product page -> product_publish_product row (productUpdate/status)
    4. idempotent regeneration
    5. GID-less page -> FixNotSupported
  scope mapping (registry-derived, single source of truth)
    6. product_publish_product -> write_products
    7. collection_publish -> write_publications
  adapter payload validation (no network)
    8. drift fields rejected BEFORE any network call
    9. dry-run typed, nothing written
  executor e2e (fake GraphQL client)
   10. applied + verified + snapshot (status ACTIVE)
   11. verify_failed -> auto-revert from snapshot
   12. change_log rollback_reference = fix_id
   13. collection publish: payload carries publicationId; revert unpublishes
   14. 3-strike access_denied -> policy disabled

Isolation: every row keyed off RUN_TOKEN, removed in cleanup. Shopify is
faked at the client seam (no network anywhere).
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
# Fake Shopify surface (publish-state; no network)
# ------------------------------------------------------------

class FakePublishState:
    def __init__(self, gid, status="DRAFT", published=False):
        self.gid = gid
        self.status = status
        self.published = published
        self.writes = []
        self.verify_should_fail = False
        self.access_denied = False
        self.reads = 0


STATE = None


class FakePublishClient:
    """Stand-in for ShopifyGraphQLClient covering product+collection reads.

    verify_should_fail drifts ONLY the post-write read-back: the first read
    (executor pickup snapshot) returns the true state, every read after the
    first productUpdate/publish write returns the drifted value.
    """

    def __init__(self, *args, **kwargs):
        pass

    def run(self, mutation_name, query, variables=None, **kwargs):
        if mutation_name in ("product", "collection"):
            drifted = STATE.verify_should_fail and STATE.writes
            return {"ok": True, "data": {
                mutation_name: {
                    "id": STATE.gid,
                    "status": ("ARCHIVED" if drifted
                               and STATE.gid.startswith("gid://shopify/Product")
                               else STATE.status),
                    "publishedOnPublication": (False if drifted
                                               else STATE.published),
                }}}
        if STATE.access_denied:
            return {"ok": False, "outcome": "access_denied",
                    "userErrors": [],
                    "errors": [{"code": "access_denied",
                                "message": "write_products not granted"}]}
        if mutation_name == "productUpdate":
            product = (variables or {}).get("product") or {}
            if "status" in product:
                STATE.writes.append(("status", product.get("status")))
                STATE.status = product.get("status")
            return {"ok": True, "outcome": "ok", "userErrors": [],
                    "data": {"productUpdate": {"product": {"id": product.get("id")}}}}
        if mutation_name == "publishablePublish":
            STATE.writes.append(("publish", variables.get("publicationId")))
            STATE.published = True
            return {"ok": True, "outcome": "ok", "userErrors": [],
                    "data": {"publishablePublish": {"publishable": {"id": variables.get("id")}}}}
        if mutation_name == "publishableUnpublish":
            STATE.writes.append(("unpublish", variables.get("publicationId")))
            STATE.published = False
            return {"ok": True, "outcome": "ok", "userErrors": [],
                    "data": {"publishableUnpublish": {"publishable": {"id": variables.get("id")}}}}
        if mutation_name == "publications":
            return {"ok": True, "data": {"publications": {"nodes": [
                {"id": f"gid://shopify/Publication/{RUN_TOKEN[:8]}",
                 "title": "Online Store"}]}}}
        return {"ok": False, "outcome": "unknown_call", "detail": mutation_name}


def install_fake_client():
    import fixes.adapters as fix_adapters
    if not hasattr(fix_adapters, "_real_client_from_config"):
        fix_adapters._real_client_from_config = fix_adapters._client_from_config
    fix_adapters._client_from_config = lambda config: FakePublishClient()


def restore_client():
    import fixes.adapters as fix_adapters
    if hasattr(fix_adapters, "_real_client_from_config"):
        fix_adapters._client_from_config = fix_adapters._real_client_from_config


# ------------------------------------------------------------
# Seeding + cleanup
# ------------------------------------------------------------

def seed_world(conn, page_type="product", issue="sitemap_index_mismatch",
               hypothesis="publish_state", gid=None):
    ids = {"domain": f"fixpub-{RUN_TOKEN}.example.com"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property, catalogue_size_tier, shopify_domain)
            VALUES (%s, %s, %s, 'small', %s) RETURNING site_id
            """,
            (f"FixPub Test {RUN_TOKEN}", ids["domain"], f"sc-domain:{ids['domain']}",
             ids["domain"]),
        )
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)

        url = f"https://{ids['domain']}/{page_type}s/test-{page_type}"
        gid = gid or f"gid://shopify/{'Product' if page_type == 'product' else 'Collection'}/{RUN_TOKEN[:12]}"
        cur.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title, shopify_gid, indexable)
            VALUES (%s, %s, %s, %s, %s, false)
            """,
            (site_id, url, page_type, f"Test {page_type.title()}", gid),
        )
        ids["target_url"] = url
        ids["gid"] = gid

        evidence = {
            "issue": "seed",
            "issue_type": issue,
            "indexable": False,
            "gsc_clicks_28d": 42,
            "mechanism_hypothesis": hypothesis,
        }
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url,
                 diagnosis, evidence_json, status, approved_at)
            VALUES (%s, 'technical_fix', 'technical_fix', %s,
                    %s, %s::jsonb, 'approved', now())
            RETURNING recommendation_id
            """,
            (site_id, url, f"technical seed {RUN_TOKEN}",
             json.dumps(evidence)),
        )
        rec_id = cur.fetchone()[0]
        ids["rec_id"] = str(rec_id)

        for sub_type, risk, cap in (("product_publish_product", "medium", 5),
                                    ("collection_publish", "medium", 3)):
            cur.execute(
                "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
                "requires_field_verify, enabled) VALUES (%s, %s, %s, %s, true, true)",
                (site_id, sub_type, risk, cap),
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
        cur.execute("DELETE FROM recommendations WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM pages WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM fix_policy WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM system_config WHERE config_key LIKE %s",
                    (f"fix.scopemiss.{ids['site_id']}.%",))
        cur.execute("DELETE FROM system_config WHERE config_key = "
                    "'shopify.publication_id'")
        cur.execute("DELETE FROM site_config WHERE site_id = %s", (ids["site_id"],))
    conn.commit()


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def test_generator_routing(conn):
    print("\n== generator routing ==")
    from fixes.generator import (generate_fix_for_recommendation,
                                 FixNotSupported)

    allok = True

    # 1: non-publish issue_type
    ids = seed_world(conn, issue="canonical_conflict")
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("non-publish issue 422", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("non-publish issue 422",
                       "publish-state fix applies to" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)

    # 2: mechanism_hypothesis != publish_state (canonical block = plan_only)
    ids = seed_world(conn, issue="not_indexable", hypothesis="canonical_block")
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("canonical hypothesis plan_only", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("canonical hypothesis plan_only",
                       "not publish-controlled" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)

    # 3 + 4: product page happy path + idempotent regeneration
    ids = seed_world(conn, page_type="product")
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("product publish fix generated", out["created"] is True,
                       str(out)[:140])
        payload = out["payload"]
        allok &= check("productUpdate payload",
                       payload["mutation"] == "productUpdate"
                       and payload["variables"]["product"]["status"] == "ACTIVE"
                       and payload["scope_required"] == "write_products",
                       json.dumps(payload)[:160])
        with conn.cursor() as cur:
            cur.execute("SELECT sub_type, risk_tier, status FROM generated_fixes "
                        "WHERE fix_id = %s", (out["fix_id"],))
            sub_type, risk, status = cur.fetchone()
            allok &= check("row shape", sub_type == "product_publish_product"
                           and risk == "medium" and status == "generated",
                           f"{sub_type}/{risk}/{status}")
        out2 = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("idempotent regeneration", out2["created"] is False
                       and out2["fix_id"] == out["fix_id"])
    finally:
        cleanup(conn, ids)

    # 5: GID-less page
    ids = seed_world(conn, page_type="product", gid=None)
    with conn.cursor() as cur:
        cur.execute("UPDATE pages SET shopify_gid = NULL WHERE site_id = %s",
                    (ids["site_id"],))
    conn.commit()
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("gid-less -> FixNotSupported", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("gid-less -> FixNotSupported",
                       "shopify_gid" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)

    # collection page: no publication id (no credentials) -> refuses
    ids = seed_world(conn, page_type="collection")
    _saved_token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
    try:
        import fixes.generator as gen
        os.environ.pop("SHOPIFY_ACCESS_TOKEN", None)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM system_config WHERE config_key = "
                        "'shopify.publication_id'")
        conn.commit()
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("collection without publication id refuses", False,
                       "no exception")
    except FixNotSupported as exc:
        allok &= check("collection without publication id refuses",
                       "publication id" in str(exc), str(exc))
    finally:
        if _saved_token is not None:
            os.environ["SHOPIFY_ACCESS_TOKEN"] = _saved_token
        cleanup(conn, ids)
    return allok


def test_scope_mapping():
    print("\n== scope mapping ==")
    from connectors.shopify import required_scopes_for_sub_types
    allok = check("product_publish_product -> write_products",
                  required_scopes_for_sub_types(["product_publish_product"])
                  == ["write_products"])
    allok &= check("collection_publish -> write_publications",
                   required_scopes_for_sub_types(["collection_publish"])
                   == ["write_publications"])
    return allok


def test_adapter_validation():
    print("\n== adapter payload validation ==")
    from fixes.adapters import get_adapter

    allok = True
    STATE = FakePublishState("gid://shopify/Product/x")
    install_fake_client()
    try:
        adapter = get_adapter("product_publish_product")
        # 8: drift fields rejected BEFORE network
        bad = {"mutation": "productUpdate",
               "variables": {"product": {"id": "gid://x", "status": "ACTIVE",
                                         "seo": {"title": "drift"}}}}
        result = adapter["execute"](None, {"target_entity_ref": "gid://x",
                                           "payload_json": bad}, config={})
        allok &= check("drift fields rejected before network",
                       result.get("outcome") == "payload_invalid", str(result)[:120])

        # 9: dry-run typed
        good = {"mutation": "productUpdate",
                "variables": {"product": {"id": "gid://x", "status": "ACTIVE"}}}
        dry = adapter["execute"](None, {"target_entity_ref": "gid://x",
                                        "payload_json": good},
                                 config={}, dry_run=True)
        allok &= check("dry-run typed", dry.get("outcome") == "dry_run"
                       and STATE.writes == [], str(dry)[:120])
    finally:
        restore_client()
    return allok


def test_executor_product_publish(conn):
    print("\n== executor: product publish e2e ==")
    from fixes.generator import generate_fix_for_recommendation
    from api.routes.fixes import approve_fix
    from jobs.fix_executor import run_fix_executor

    allok = True
    ids = seed_world(conn, page_type="product")
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        res = approve_fix(out["fix_id"], conn)
        allok &= check("approve -> queued", res.status == "queued", str(res))

        global STATE
        STATE = FakePublishState(ids["gid"], status="DRAFT")
        install_fake_client()
        try:
            summary = run_fix_executor(ids["site_id"], conn=conn,
                                       shop_config={"shop_domain": "fake.example.com",
                                                    "access_token": "fake",
                                                    "api_version": "2026-01"})
            result = next(r for r in summary["results"]
                          if r["fix_id"] == out["fix_id"])
            allok &= check("applied", result.get("status") == "applied",
                           str(result)[:140])
            with conn.cursor() as cur:
                cur.execute("SELECT status, verification_status, "
                            "snapshot_json->>'product.status' "
                            "FROM generated_fixes WHERE fix_id = %s",
                            (out["fix_id"],))
                status, vstatus, snap_status = cur.fetchone()
                allok &= check("row applied+verified+snapshot",
                               status == "applied" and vstatus == "verified"
                               and snap_status == "DRAFT",
                               f"{status}/{vstatus}/{snap_status}")
                cur.execute("SELECT rollback_reference FROM change_log "
                            "WHERE rollback_reference = %s", (out["fix_id"],))
                allok &= check("change_log rollback_reference = fix_id",
                               cur.fetchone() is not None)
            allok &= check("fake store received status write",
                           ("status", "ACTIVE") in STATE.writes,
                           str(STATE.writes)[:120])

            # 11: verify_failed -> auto-revert (snapshot status DRAFT restored)
            # reset the recommendation to approved so generation can run again
            with conn.cursor() as cur:
                cur.execute("UPDATE recommendations SET status = 'approved', "
                            "implemented_at = NULL WHERE recommendation_id = %s",
                            (ids["rec_id"],))
                cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = %s",
                            (ids["rec_id"],))
                cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s "
                            "AND status <> 'generated'", (out["fix_id"],))
            conn.commit()
            # expire the applied row's unique-slot by marking it reverted first
            with conn.cursor() as cur:
                cur.execute("UPDATE generated_fixes SET status = 'reverted' "
                            "WHERE fix_id = %s AND status = 'applied'",
                            (out["fix_id"],))
            conn.commit()
            STATE.status = "DRAFT"
            STATE.published = False
            STATE.writes = []
            # verify_should_fail drifts only the POST-WRITE read-back (see
            # FakePublishClient): pickup snapshot stays true, write-verify
            # returns drifted -> auto-revert path.
            STATE.verify_should_fail = True
            fix2 = generate_fix_for_recommendation(conn, ids["rec_id"])
            approve_fix(fix2["fix_id"], conn)
            summary = run_fix_executor(ids["site_id"], conn=conn,
                                       shop_config={"shop_domain": "fake.example.com",
                                                    "access_token": "fake",
                                                    "api_version": "2026-01"})
            result2 = next(r for r in summary["results"]
                           if r["fix_id"] == fix2["fix_id"])
            allok &= check("verify_failed -> auto-revert",
                           result2.get("status") == "reverted", str(result2)[:140])
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM generated_fixes WHERE fix_id = %s",
                            (fix2["fix_id"],))
                allok &= check("original marked reverted",
                               cur.fetchone()[0] == "reverted")
            # The restore wrote the snapshot value (DRAFT) — the revert write
            # landed even though its own read-back saw the drifted store.
            allok &= check("revert write issued with snapshot value",
                           ("status", "DRAFT") in STATE.writes[-2:],
                           str(STATE.writes))
            # Turn drift off; the store state equals the snapshot value now.
            STATE.verify_should_fail = False
            allok &= check("snapshot status restored", STATE.status == "DRAFT",
                           STATE.status)
        finally:
            restore_client()
    finally:
        cleanup(conn, ids)
    return allok


def test_executor_collection_publish(conn):
    print("\n== executor: collection publish e2e ==")
    from fixes.generator import generate_fix_for_recommendation
    from api.routes.fixes import approve_fix
    from jobs.fix_executor import run_fix_executor

    allok = True
    ids = seed_world(conn, page_type="collection")
    try:
        # cache a publication id so generation can resolve it offline
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO system_config (config_key, config_value) "
                "VALUES ('shopify.publication_id', %s) "
                "ON CONFLICT (config_key) DO UPDATE SET config_value = EXCLUDED.config_value",
                (f"gid://shopify/Publication/{RUN_TOKEN[:8]}",))
        conn.commit()
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("collection fix generated", out["created"] is True,
                       str(out)[:140])
        allok &= check("payload carries publicationId",
                       out["payload"]["mutation"] == "publishablePublish"
                       and out["payload"]["scope_required"] == "write_publications"
                       and out["payload"]["variables"]["publicationId"],
                       json.dumps(out["payload"])[:160])
        approve_fix(out["fix_id"], conn)

        global STATE
        STATE = FakePublishState(ids["gid"], published=False)
        install_fake_client()
        try:
            summary = run_fix_executor(ids["site_id"], conn=conn,
                                       shop_config={"shop_domain": "fake.example.com",
                                                    "access_token": "fake",
                                                    "api_version": "2026-01"})
            result = next(r for r in summary["results"]
                          if r["fix_id"] == out["fix_id"])
            allok &= check("collection applied", result.get("status") == "applied",
                           str(result)[:140])
            with conn.cursor() as cur:
                cur.execute("SELECT snapshot_json->>'publishable.published_on_publication', "
                            "snapshot_json->>'publication_id' "
                            "FROM generated_fixes WHERE fix_id = %s", (out["fix_id"],))
                snap_pub, snap_pid = cur.fetchone()
                allok &= check("snapshot captured pre-state",
                               snap_pub == "false" and snap_pid,
                               f"{snap_pub}/{snap_pid}")
            allok &= check("publish write received publicationId",
                           ("publish", f"gid://shopify/Publication/{RUN_TOKEN[:8]}")
                           in STATE.writes, str(STATE.writes)[:140])

            # revert: snapshot said not-published -> unpublish (rollback)
            from jobs.fix_executor import revert_fix
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT fix_id, recommendation_id, site_id, action_type, "
                    "sub_type, target_url, target_entity_ref, payload_json, "
                    "diff_json, risk_tier, snapshot_json FROM generated_fixes "
                    "WHERE fix_id = %s", (out["fix_id"],))
                columns = [d[0] for d in cur.description]
                row = dict(zip(columns, cur.fetchone()))
            conn.rollback()
            revert = revert_fix(conn, row, {}, reason="test")
            allok &= check("revert unpublishes", revert.get("reverted") is True,
                           str(revert)[:140])
            allok &= check("unpublish write issued",
                           ("unpublish", f"gid://shopify/Publication/{RUN_TOKEN[:8]}")
                           in STATE.writes, str(STATE.writes)[:140])
        finally:
            restore_client()
    finally:
        cleanup(conn, ids)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM system_config WHERE config_key = "
                        "'shopify.publication_id'")
        conn.commit()
    return allok


def test_scope_strike_disable(conn):
    print("\n== 3-strike scope disable ==")
    from fixes.generator import generate_fix_for_recommendation
    from api.routes.fixes import approve_fix
    from jobs.fix_executor import run_fix_executor

    allok = True
    ids = seed_world(conn, page_type="product")
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        approve_fix(out["fix_id"], conn)
        global STATE
        STATE = FakePublishState(ids["gid"])
        STATE.access_denied = True
        install_fake_client()
        try:
            summary = run_fix_executor(ids["site_id"], conn=conn,
                                       shop_config={"shop_domain": "fake.example.com",
                                                    "access_token": "fake",
                                                    "api_version": "2026-01"})
            # strike 1 recorded
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT config_value FROM system_config WHERE config_key = %s",
                    (f"fix.scopemiss.{ids['site_id']}.product_publish_product",))
                row = cur.fetchone()
                allok &= check("strike 1 recorded", row is not None, str(row))
            # two more strikes -> policy disabled
            from jobs.fix_executor import _record_scope_strike
            for _ in range(2):
                _record_scope_strike(conn, ids["site_id"], "product_publish_product")
            with conn.cursor() as cur:
                cur.execute("SELECT enabled FROM fix_policy WHERE site_id = %s "
                            "AND sub_type = 'product_publish_product'",
                            (ids["site_id"],))
                allok &= check("3-strike disables policy",
                               cur.fetchone()[0] is False)
        finally:
            restore_client()
    finally:
        cleanup(conn, ids)
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    allok = True
    try:
        allok &= test_generator_routing(conn)
        allok &= test_scope_mapping()
        allok &= test_adapter_validation()
        allok &= test_executor_product_publish(conn)
        allok &= test_executor_collection_publish(conn)
        allok &= test_scope_strike_disable(conn)
    finally:
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL FIX PUBLISH TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
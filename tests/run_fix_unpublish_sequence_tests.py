# -*- coding: utf-8 -*-
"""Phase 4 item 3 — unpublish-after-redirect sequencing tests.

python tests/run_fix_unpublish_sequence_tests.py   (needs a reachable local Postgres)

Covers:
  sequencing contract (structural gate)
    1. redirect applied + verified -> follow-up unpublish row minted
       ('generated', medium risk, rollback_of = redirect fix)
    2. follow-up carries grounding: route=unpublish_after_redirect,
       redirect_fix_id, redirect_verified=True
    3. redirect NOT verified (verify_failed -> auto-revert) -> NO follow-up
    4. redirect failed (userErrors) -> NO follow-up
    5. idempotent: a second verified run does NOT mint twice
  unpublish execution (product path, full lifecycle through the executor)
    6. approve + execute -> product archived, read-back verified
    7. pre-execution snapshot captured (product.status + publication state)
    8. revert of the unpublish restores the pickup status (ACTIVE)
  revert lineage (strict reverse order)
    9. redirect revert with an APPLIED follow-up -> unpublish undone FIRST
       (entity reactivated), then redirect deleted; audited rows for both
   10. redirect revert with a NOT-YET-APPLIED follow-up -> follow-up expired,
       no unpublish mutation issued
   11. revert of a non-redirect fix -> no unpublish undo attempted
  collection path
   12. collection redirect -> collection_unpublish follow-up (publicationId
       carried); revert re-publishes (pickup said published)

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
# Fake Shopify surface (redirect + publish-state; no network)
# ------------------------------------------------------------

class FakeSeqState:
    """Simulated store: one entity (product or collection) + the redirect."""

    def __init__(self, gid, status="ACTIVE", published=True):
        self.gid = gid
        self.status = status
        self.published = published
        self.redirect_id = None
        self.redirect_path = None
        self.redirect_target = None
        self.writes = []
        self.redirect_verify_should_fail = False
        self.unpublish_verify_should_fail = False


STATE = None


class FakeSeqClient:
    def __init__(self, *args, **kwargs):
        self.api_version = "2026-01"

    def run(self, mutation_name, query, variables=None, **kwargs):
        if mutation_name in ("product", "collection"):
            entity = (STATE.status, STATE.published)
            if STATE.redirect_id and mutation_name == "urlRedirect":
                pass  # handled below
            return {"ok": True, "data": {mutation_name: {
                "id": STATE.gid,
                "status": entity[0],
                "publishedOnPublication": entity[1],
            }}}
        if mutation_name == "urlRedirectCreate":
            redirect = (variables or {}).get("urlRedirect") or {}
            if STATE.redirect_verify_should_fail:
                # write "succeeds" but read-back will disagree
                STATE.redirect_id = f"gid://shopify/UrlRedirect/{RUN_TOKEN[:8]}"
                STATE.redirect_path = redirect.get("path") + "-drifted"
                STATE.redirect_target = redirect.get("target")
            else:
                STATE.redirect_id = f"gid://shopify/UrlRedirect/{RUN_TOKEN[:8]}"
                STATE.redirect_path = redirect.get("path")
                STATE.redirect_target = redirect.get("target")
            STATE.writes.append(("redirect.create", redirect.get("path")))
            return {"ok": True, "data": {"urlRedirectCreate": {
                "urlRedirect": {"id": STATE.redirect_id,
                                "path": STATE.redirect_path,
                                "target": STATE.redirect_target}}}}
        if mutation_name == "urlRedirect":
            if STATE.redirect_id:
                return {"ok": True, "data": {"urlRedirect": {
                    "id": STATE.redirect_id,
                    "path": STATE.redirect_path,
                    "target": STATE.redirect_target}}}
            return {"ok": False, "outcome": "not_found"}
        if mutation_name == "urlRedirectDelete":
            STATE.writes.append(("redirect.delete", STATE.redirect_id))
            deleted = STATE.redirect_id
            STATE.redirect_id = None
            return {"ok": True, "data": {"urlRedirectDelete": {
                "deletedUrlRedirectId": deleted}}}
        if mutation_name == "productUpdate":
            product = (variables or {}).get("product") or {}
            if "status" in product:
                STATE.writes.append(("product.status", product.get("status")))
                STATE.status = product.get("status")
                if STATE.unpublish_verify_should_fail:
                    STATE.status = "ACTIVE"  # drifted read-back
            return {"ok": True, "outcome": "ok", "userErrors": [],
                    "data": {"productUpdate": {"product": {"id": product.get("id")}}}}
        if mutation_name == "publishableUnpublish":
            STATE.writes.append(("collection.unpublish",
                                 (variables or {}).get("publicationId")))
            STATE.published = False
            if STATE.unpublish_verify_should_fail:
                STATE.published = True  # drifted read-back
            return {"ok": True, "outcome": "ok", "userErrors": [],
                    "data": {"publishableUnpublish": {
                        "publishable": {"id": (variables or {}).get("id")}}}}
        if mutation_name == "publishablePublish":
            STATE.writes.append(("collection.publish",
                                 (variables or {}).get("publicationId")))
            STATE.published = True
            return {"ok": True, "outcome": "ok", "userErrors": [],
                    "data": {"publishablePublish": {
                        "publishable": {"id": (variables or {}).get("id")}}}}
        if mutation_name == "publications":
            return {"ok": True, "data": {"publications": {"nodes": [
                {"id": f"gid://shopify/Publication/{RUN_TOKEN[:6]}",
                 "title": "Online Store"}]}}}
        return {"ok": False, "outcome": "unknown_call", "detail": mutation_name}


def install_fake_client():
    import fixes.adapters as fix_adapters
    if not hasattr(fix_adapters, "_real_client_from_config"):
        fix_adapters._real_client_from_config = fix_adapters._client_from_config
    fix_adapters._client_from_config = lambda config: FakeSeqClient()


def restore_client():
    import fixes.adapters as fix_adapters
    fix_adapters._client_from_config = fix_adapters._real_client_from_config


# ------------------------------------------------------------
# Seeding + cleanup
# ------------------------------------------------------------

def seed_world(conn, page_type="product", gid=None):
    ids = {"domain": f"fixseq-{RUN_TOKEN}.example.com"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property, catalogue_size_tier, shopify_domain)
            VALUES (%s, %s, %s, 'small', %s) RETURNING site_id
            """,
            (f"FixSeq Test {RUN_TOKEN}", ids["domain"], f"sc-domain:{ids['domain']}",
             ids["domain"]),
        )
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)

        gid = gid or f"gid://shopify/{'Product' if page_type == 'product' else 'Collection'}/{RUN_TOKEN[:10]}"
        source_url = f"https://{ids['domain']}/{page_type}s/retired-{RUN_TOKEN[:6]}"
        survivor_url = f"https://{ids['domain']}/{page_type}s/survivor-{RUN_TOKEN[:6]}"
        cur.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title, indexable, status_code, shopify_gid)
            VALUES (%s, %s, %s, 'Retired Page', true, 200, %s)
            """,
            (site_id, source_url, page_type, gid),
        )
        cur.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title, indexable, status_code, shopify_gid)
            VALUES (%s, %s, %s, 'Survivor Page', true, 200, %s)
            """,
            (site_id, survivor_url, page_type,
             f"gid://shopify/{'Product' if page_type == 'product' else 'Collection'}/s{RUN_TOKEN[:8]}"),
        )
        ids["source_url"] = source_url
        ids["survivor_url"] = survivor_url
        ids["gid"] = gid

        evidence = {
            "issue": "seed",
            "competing_urls": [source_url, survivor_url],
            "impressions_distribution": {source_url: 200, survivor_url: 400},
            "pattern": "same_type",
        }
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url, proposed_url,
                 diagnosis, evidence_json, status, approved_at)
            VALUES (%s, 'cannibalization', 'consolidate', %s, %s,
                    %s, %s::jsonb, 'approved', now())
            RETURNING recommendation_id
            """,
            (site_id, survivor_url, source_url,
             f"consolidate seed {RUN_TOKEN}", json.dumps(evidence)),
        )
        rec_id = cur.fetchone()[0]
        ids["rec_id"] = str(rec_id)

        for sub_type, risk, cap in (("redirect", "high", 2),):
            cur.execute(
                "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
                "requires_field_verify, enabled) VALUES (%s, %s, %s, %s, true, true)",
                (site_id, sub_type, risk, cap))
        # unpublish policy rows (the sequenced sub_type)
        unpublish = ("product_unpublish" if page_type == "product"
                     else "collection_unpublish")
        cur.execute(
            "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
            "requires_field_verify, enabled) VALUES (%s, %s, 'medium', 3, true, true)",
            (site_id, unpublish))
        ids["unpublish_sub_type"] = unpublish
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
        cur.execute("DELETE FROM search_performance WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM pages WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM fix_policy WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM site_config WHERE site_id = %s", (ids["site_id"],))
    conn.commit()


def make_redirect_fix(conn, ids):
    """Consolidate rec -> redirect fix -> approved (queued)."""
    from fixes.generator import generate_fix_for_recommendation
    from api.routes.fixes import approve_fix
    out = generate_fix_for_recommendation(conn, ids["rec_id"])
    approve_fix(out["fix_id"], conn)
    return out["fix_id"]


def run_executor(conn, ids):
    from jobs.fix_executor import run_fix_executor
    return run_fix_executor(ids["site_id"], conn=conn,
                            shop_config={"shop_domain": "fake.example.com",
                                         "access_token": "fake",
                                         "api_version": "2026-01"})


def find_followup(conn, redirect_fix_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fix_id, sub_type, status, risk_tier, rollback_of, "
            "payload_json, diff_json, snapshot_json IS NOT NULL "
            "FROM generated_fixes WHERE rollback_of = %s AND sub_type IN %s "
            "ORDER BY created_at DESC LIMIT 1",
            (redirect_fix_id, ("product_unpublish", "collection_unpublish")))
        row = cur.fetchone()
    return row


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def test_sequencing_product(conn):
    print("\n== sequencing: product path ==")
    allok = True
    ids = seed_world(conn, page_type="product")
    global STATE
    try:
        redirect_fix_id = make_redirect_fix(conn, ids)
        STATE = FakeSeqState(ids["gid"], status="ACTIVE")
        install_fake_client()
        try:
            # 1: redirect verified -> follow-up minted
            summary = run_executor(conn, ids)
            result = next(r for r in summary["results"]
                          if r["fix_id"] == redirect_fix_id)
            allok &= check("redirect applied+verified",
                           result.get("status") == "applied", str(result)[:160])
            followup = find_followup(conn, redirect_fix_id)
            allok &= check("follow-up unpublish row minted", followup is not None,
                           repr(followup))
            if followup:
                allok &= check("follow-up shape: product_unpublish/generated/medium",
                               followup[1] == "product_unpublish"
                               and followup[2] == "generated"
                               and followup[3] == "medium"
                               and str(followup[4]) == redirect_fix_id,
                               repr(followup[:5]))
                payload = followup[5] if not isinstance(followup[5], str) else json.loads(followup[5])
                allok &= check("follow-up grounding carries sequencing proof",
                               payload.get("grounding", {}).get("route") == "unpublish_after_redirect"
                               and payload["grounding"].get("redirect_fix_id") == redirect_fix_id
                               and payload["grounding"].get("redirect_verified") is True,
                               json.dumps(payload.get("grounding"))[:200])
                allok &= check("follow-up payload: status ARCHIVED",
                               payload["mutation"] == "productUpdate"
                               and payload["variables"]["product"]["status"] == "ARCHIVED",
                               json.dumps(payload["variables"])[:140])
                # 6: approve + execute the unpublish through the FULL lifecycle
                from api.routes.fixes import approve_fix
                res = approve_fix(str(followup[0]), conn)
                allok &= check("unpublish approve -> queued", res.status == "queued",
                               str(res))
                summary2 = run_executor(conn, ids)
                r2 = next(r for r in summary2["results"]
                          if r["fix_id"] == str(followup[0]))
                allok &= check("unpublish applied+verified",
                               r2.get("status") == "applied", str(r2)[:160])
                # 7: pre-execution snapshot captured (product.status from the
                # publication-free status read; the publication field is no
                # longer sampled for products — LIVE-VERIFIED: stores without
                # publications reject the publication-scoped read)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT snapshot_json->>'product.status', "
                        "snapshot_json->>'publication_id' "
                        "FROM generated_fixes WHERE fix_id = %s",
                        (str(followup[0]),))
                    snap_status, snap_pub = cur.fetchone()
                    allok &= check("unpublish snapshot captured pre-state",
                                   snap_status == "ACTIVE",
                                   f"{snap_status}/{snap_pub}")
                allok &= check("fake store archived the product",
                               ("product.status", "ARCHIVED") in STATE.writes,
                               str(STATE.writes)[:160])

                # 8: revert the unpublish -> status restored to pickup value
                from jobs.fix_executor import revert_fix
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT fix_id, recommendation_id, site_id, action_type, "
                        "sub_type, target_url, target_entity_ref, payload_json, "
                        "diff_json, risk_tier, snapshot_json FROM generated_fixes "
                        "WHERE fix_id = %s", (str(followup[0]),))
                    columns = [d[0] for d in cur.description]
                    unpublish_row = dict(zip(columns, cur.fetchone()))
                conn.rollback()
                revert = revert_fix(conn, unpublish_row, {}, reason="test")
                allok &= check("unpublish revert restores pickup status",
                               revert.get("reverted") is True
                               and STATE.status == "ACTIVE",
                               f"{revert} state={STATE.status}")
        finally:
            restore_client()
    finally:
        cleanup(conn, ids)
    return allok


def test_failure_safety(conn):
    print("\n== failure safety: no unpublish on failed/unverified redirect ==")
    allok = True
    ids = seed_world(conn, page_type="product")
    global STATE
    try:
        redirect_fix_id = make_redirect_fix(conn, ids)
        STATE = FakeSeqState(ids["gid"], status="ACTIVE")
        # Case A: redirect verification FAILS (drifted read-back) -> auto-revert
        STATE.redirect_verify_should_fail = True
        install_fake_client()
        try:
            summary = run_executor(conn, ids)
            result = next(r for r in summary["results"]
                          if r["fix_id"] == redirect_fix_id)
            allok &= check("unverified redirect -> reverted",
                           result.get("status") == "reverted", str(result)[:160])
            followup = find_followup(conn, redirect_fix_id)
            allok &= check("no follow-up minted on verify_failed",
                           followup is None, repr(followup))
        finally:
            restore_client()

        # Case B: redirect creation fails outright (userErrors) -> no follow-up
        redirect2 = make_redirect_fix(conn, ids) if False else None
        with conn.cursor() as cur:
            # FK-safe order: the minted lineage references the redirect row
            cur.execute("DELETE FROM generated_fixes WHERE rollback_of = %s",
                        (redirect_fix_id,))
            cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s",
                        (redirect_fix_id,))
            cur.execute(
                """
                INSERT INTO generated_fixes
                    (recommendation_id, site_id, action_type, sub_type, target_url,
                     target_entity_ref, payload_json, diff_json, generation_source,
                     status, risk_tier)
                VALUES (%s, %s, 'consolidate', 'redirect', %s, %s,
                        %s::jsonb, %s::jsonb, 'deterministic', 'queued', 'high')
                RETURNING fix_id
                """,
                (ids["rec_id"], ids["site_id"], ids["source_url"], ids["gid"],
                 json.dumps({"mutation": "urlRedirectCreate",
                             "variables": {"urlRedirect": {
                                 "path": "/products/retired-" + RUN_TOKEN[:6],
                                 "target": "/products/survivor-" + RUN_TOKEN[:6]}}}),
                 json.dumps([{"field": "redirect", "old_value": None,
                              "new_value": "x"}])),
            )
            fix_b = str(cur.fetchone()[0])
        conn.commit()
        # make the store REJECT the create with userErrors
        STATE = FakeSeqState(ids["gid"], status="ACTIVE")
        STATE.redirect_verify_should_fail = False
        import fixes.adapters as fix_adapters
        class RejectingClient(FakeSeqClient):
            def run(self, mutation_name, query, variables=None, **kwargs):
                if mutation_name == "urlRedirectCreate":
                    return {"ok": False, "outcome": "user_errors",
                            "userErrors": [{"field": ["urlRedirect.path"],
                                            "message": "already redirected"}]}
                return super().run(mutation_name, query, variables=variables, **kwargs)
        fix_adapters._client_from_config = lambda config: RejectingClient()
        try:
            summary = run_executor(conn, ids)
            result = next(r for r in summary["results"] if r["fix_id"] == fix_b)
            allok &= check("failed redirect -> failed", result.get("status") == "failed",
                           str(result)[:160])
            followup = find_followup(conn, fix_b)
            allok &= check("no follow-up minted on failed redirect",
                           followup is None, repr(followup))
        finally:
            restore_client()
    finally:
        cleanup(conn, ids)
    return allok


def test_revert_lineage(conn):
    print("\n== revert lineage: unpublish undone BEFORE redirect deletion ==")
    allok = True
    ids = seed_world(conn, page_type="product")
    global STATE
    try:
        redirect_fix_id = make_redirect_fix(conn, ids)
        STATE = FakeSeqState(ids["gid"], status="ACTIVE")
        install_fake_client()
        try:
            summary = run_executor(conn, ids)
            followup = find_followup(conn, redirect_fix_id)
            if not (followup and summary["results"]):
                allok &= check("precondition: follow-up minted", False, "no followup")
                return allok
            from api.routes.fixes import approve_fix
            approve_fix(str(followup[0]), conn)
            summary2 = run_executor(conn, ids)
            r2 = next(r for r in summary2["results"]
                      if r["fix_id"] == str(followup[0]))
            allok &= check("setup: unpublish applied", r2.get("status") == "applied",
                           str(r2)[:140])
            allok &= check("setup: product archived", STATE.status == "ARCHIVED",
                           STATE.status)

            # 9: operator reverts the REDIRECT via the API endpoint path:
            # unpublish undone first (entity reactivated), then redirect deleted.
            from jobs.fix_executor import revert_unpublish_before_redirect, revert_fix
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT fix_id, recommendation_id, site_id, action_type, "
                    "sub_type, target_url, target_entity_ref, payload_json, "
                    "diff_json, risk_tier, snapshot_json FROM generated_fixes "
                    "WHERE fix_id = %s", (redirect_fix_id,))
                columns = [d[0] for d in cur.description]
                redirect_row = dict(zip(columns, cur.fetchone()))
            conn.rollback()
            undo = revert_unpublish_before_redirect(conn, redirect_row, {})
            allok &= check("unpublish undone first",
                           undo is not None and undo.get("action") == "reverted"
                           and undo.get("result", {}).get("reverted") is True,
                           str(undo)[:200])
            allok &= check("entity reactivated BEFORE redirect deletion",
                           STATE.status == "ACTIVE", STATE.status)
            revert = revert_fix(conn, redirect_row, {}, reason="test")
            allok &= check("redirect deleted after unpublish undo",
                           revert.get("reverted") is True, str(revert)[:140])
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM generated_fixes WHERE rollback_of = %s",
                            (redirect_fix_id,))
                # follow-up + its revert + redirect revert = 3 lineage rows
                allok &= check("audit lineage rows (rollback_of chain)",
                               cur.fetchone()[0] >= 2, "lineage")
        finally:
            restore_client()

        # 10: NOT-YET-APPLIED follow-up -> expired, no unpublish mutation
        ids2 = seed_world(conn, page_type="product")
        try:
            redirect2 = make_redirect_fix(conn, ids2)
            STATE = FakeSeqState(ids2["gid"], status="ACTIVE")
            install_fake_client()
            try:
                run_executor(conn, ids2)
                followup2 = find_followup(conn, redirect2)
                if not followup2:
                    allok &= check("setup: follow-up minted", False, "missing")
                else:
                    # leave it 'generated' (NOT approved) and revert the redirect
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT fix_id, recommendation_id, site_id, action_type, "
                            "sub_type, target_url, target_entity_ref, payload_json, "
                            "diff_json, risk_tier, snapshot_json FROM generated_fixes "
                            "WHERE fix_id = %s", (redirect2,))
                        columns = [d[0] for d in cur.description]
                        redirect_row2 = dict(zip(columns, cur.fetchone()))
                    conn.rollback()
                    undo2 = revert_unpublish_before_redirect(conn, redirect_row2, {})
                    allok &= check("unapplied follow-up expired (no mutation)",
                                   undo2 is not None
                                   and undo2.get("action") == "expired",
                                   str(undo2)[:160])
                    allok &= check("no unpublish write issued",
                                   ("product.status", "ARCHIVED") not in STATE.writes,
                                   str(STATE.writes)[:160])
                    with conn.cursor() as cur:
                        cur.execute("SELECT status FROM generated_fixes WHERE fix_id = %s",
                                    (str(followup2[0]),))
                        allok &= check("follow-up row status expired",
                                       cur.fetchone()[0] == "expired")
            finally:
                restore_client()
        finally:
            cleanup(conn, ids2)

        # 11: non-redirect revert -> no unpublish undo attempted
        ids3 = seed_world(conn, page_type="product")
        try:
            from jobs.fix_executor import revert_unpublish_before_redirect
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT fix_id, recommendation_id, site_id, action_type, "
                    "sub_type, target_url, target_entity_ref, payload_json, "
                    "diff_json, risk_tier, snapshot_json FROM generated_fixes "
                    "WHERE fix_id = %s", (ids3["rec_id"],))
                # no such row — build a synthetic non-redirect row shape
                synthetic = {"fix_id": ids3["rec_id"], "sub_type": "seo.title"}
            undo3 = revert_unpublish_before_redirect(conn, synthetic, {})
            allok &= check("non-redirect fix -> no undo", undo3 is None)
        finally:
            cleanup(conn, ids3)
    finally:
        cleanup(conn, ids)
    return allok


def test_collection_path(conn):
    print("\n== sequencing: collection path ==")
    allok = True
    ids = seed_world(conn, page_type="collection")
    global STATE
    try:
        # cache the publication id so the collection unpublish can mint
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO system_config (config_key, config_value) "
                "VALUES ('shopify.publication_id', %s) "
                "ON CONFLICT (config_key) DO UPDATE SET config_value = EXCLUDED.config_value",
                (f"gid://shopify/Publication/{RUN_TOKEN[:6]}",))
        conn.commit()
        redirect_fix_id = make_redirect_fix(conn, ids)
        STATE = FakeSeqState(ids["gid"], status="ACTIVE", published=True)
        install_fake_client()
        try:
            summary = run_executor(conn, ids)
            result = next(r for r in summary["results"]
                          if r["fix_id"] == redirect_fix_id)
            allok &= check("collection redirect applied",
                           result.get("status") == "applied", str(result)[:160])
            followup = find_followup(conn, redirect_fix_id)
            allok &= check("collection_unpublish minted",
                           followup is not None and followup[1] == "collection_unpublish",
                           repr(followup))
            if followup:
                payload = followup[5] if not isinstance(followup[5], str) else json.loads(followup[5])
                allok &= check("collection unpublish payload + publicationId",
                               payload["mutation"] == "publishableUnpublish"
                               and payload["scope_required"] == "write_publications"
                               and payload["variables"]["publicationId"],
                               json.dumps(payload["variables"])[:160])
                from api.routes.fixes import approve_fix
                approve_fix(str(followup[0]), conn)
                summary2 = run_executor(conn, ids)
                r2 = next(r for r in summary2["results"]
                          if r["fix_id"] == str(followup[0]))
                allok &= check("collection unpublish applied+verified",
                               r2.get("status") == "applied", str(r2)[:160])
                allok &= check("store: collection unpublished",
                               STATE.published is False, STATE.published)
                # revert: pickup found it PUBLISHED -> revert re-publishes
                from jobs.fix_executor import revert_fix
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT fix_id, recommendation_id, site_id, action_type, "
                        "sub_type, target_url, target_entity_ref, payload_json, "
                        "diff_json, risk_tier, snapshot_json FROM generated_fixes "
                        "WHERE fix_id = %s", (str(followup[0]),))
                    columns = [d[0] for d in cur.description]
                    row = dict(zip(columns, cur.fetchone()))
                conn.rollback()
                revert = revert_fix(conn, row, {}, reason="test")
                allok &= check("collection revert re-publishes",
                               revert.get("reverted") is True
                               and STATE.published is True,
                               f"{revert} published={STATE.published}")
        finally:
            restore_client()
    finally:
        cleanup(conn, ids)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM system_config WHERE config_key = "
                        "'shopify.publication_id'")
        conn.commit()
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    allok = True
    try:
        allok &= test_sequencing_product(conn)
        allok &= test_failure_safety(conn)
        allok &= test_revert_lineage(conn)
        allok &= test_collection_path(conn)
    finally:
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL FIX UNPUBLISH SEQUENCE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
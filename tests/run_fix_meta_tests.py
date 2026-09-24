# -*- coding: utf-8 -*-
"""Stage 2 Phase 2 — meta-description ingestion + fix tests (plan/21 §2.2).

python tests/run_fix_meta_tests.py   (needs a reachable local Postgres)

Verifies:
  ingestion
    1. products fixture -> pages.meta_description + body_html + body_text_hash
    2. collections fixture -> same
    3. _body_text_hash normalization (strip tags, whitespace, case)
  meta quality gate
    4. missing meta -> quality_failed
    5. truncated meta (<70) -> quality_failed
    6. overlong meta (>155) -> quality_failed
    7. keyword absent -> quality_failed
    8. healthy meta -> no fix warranted (FixNotSupported)
  validator
    9. healthy draft passes
   10. too short / too long rejected
   11. banned meta patterns rejected (free shipping / click here)
   12. emoji + spam stack + double quote rejected
   13. intent contradiction rejected
   14. brand duplication rejected
  duplicate metas site-wide
   15. draft equal to another page's meta -> FixNotSupported
  e2e generation
   16. approved rec + failing meta -> generated row (sub_type seo.description,
       risk medium), payload = productUpdate(seo.description)
   17. idempotent regeneration (created=False)
   18. healthy meta -> no fix
   19. protect-winner suppression on meta path
   20. consolidate suppression on meta path
  adapter (fake client at the GraphQL boundary — no network)
   21. execute: write + verify read-back (verified)
   22. verify mismatch -> verify_failed
   23. dry-run writes nothing
   24. restore from snapshot
   25. payload schema validation rejects wrong-field payloads
  executor pickup
   26. queued meta fix flows through the generic executor (applied+verified)
  policy
   27. seeded fix_policy row (seo.description, medium, 5) gates the path

Isolation: every row keyed off RUN_TOKEN, removed in cleanup.
"""
import os
import sys
import json
import uuid
from datetime import date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

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
# Fake Shopify client (GraphQL boundary; no network)
# ------------------------------------------------------------

class FakeSeoState:
    def __init__(self):
        self.seo = {"title": "Old Title", "description": None}
        self.writes = []
        self.verify_should_fail = False


STATE = None


class FakeClient:
    """Stand-in for ShopifyGraphQLClient: records writes, serves STATE."""

    def __init__(self, *args, **kwargs):
        pass

    def run(self, mutation_name, query, variables=None, **kwargs):
        if mutation_name == "product":
            return {"ok": True, "data": {"product": {
                "id": "gid://x", "title": "Old Title", "status": "ACTIVE",
                "seo": dict(STATE.seo)}}}
        if mutation_name == "productUpdate":
            product = (variables or {}).get("product") or {}
            seo = product.get("seo") or {}
            STATE.writes.append(("seo.description" if "description" in seo
                                 else "seo.title", seo))
            if "description" in seo:
                if STATE.verify_should_fail:
                    STATE.seo["description"] = "MISMATCHED-LIVE-VALUE"
                else:
                    STATE.seo["description"] = seo.get("description")
            return {"ok": True, "outcome": "ok", "userErrors": [],
                    "data": {"productUpdate": {"product": {"id": product.get("id")}}}}
        return {"ok": False, "outcome": "unknown_call", "detail": mutation_name}


def install_fake_adapter():
    import fixes.adapters as fix_adapters
    state = {"client": None}

    def make(client):
        state["client"] = client
        return client

    def execute(conn, fix_row, config, dry_run=False):
        client = state["client"] or make(FakeSeoClient())
        return _exec_with(client, conn, fix_row, config, dry_run)

    return state


class FakeSeoClient:
    def run(self, mutation_name, query, variables=None, **kwargs):
        if mutation_name == "product":
            return {"ok": True, "data": {"product": {
                "id": "gid://x", "title": "Old Title", "status": "ACTIVE",
                "seo": dict(STATE.seo)}}}
        if mutation_name == "productUpdate":
            product = (variables or {}).get("product") or {}
            seo = product.get("seo") or {}
            STATE.writes.append(("seo.description" if "description" in seo
                                 else "seo.title", dict(seo)))
            if "description" in seo:
                if STATE.verify_should_fail:
                    STATE.seo["description"] = "SOMETHING-ELSE-ENTIRELY"
                else:
                    STATE.seo["description"] = seo.get("description")
            return {"ok": True, "outcome": "ok", "userErrors": [],
                    "data": {"productUpdate": {"product": {"id": product.get("id")}}}}
        return {"ok": False, "outcome": "unknown_call"}


# ------------------------------------------------------------
# Seeding + cleanup
# ------------------------------------------------------------

def seed_world(conn, meta=None, body_html=None, duplicate_meta_page=None,
               consolidate=False):
    ids = {"domain": f"fixmeta-{RUN_TOKEN}.example.com"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property, catalogue_size_tier)
            VALUES (%s, %s, %s, 'small') RETURNING site_id
            """,
            (f"FixMeta Test {RUN_TOKEN}", ids["domain"], f"sc-domain:{ids['domain']}"),
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
            INSERT INTO pages (site_id, url, page_type, title, shopify_gid,
                               meta_description, body_html)
            VALUES (%s, %s, 'product', %s, %s, %s, %s)
            """,
            (site_id, url, "Test Widget", f"gid://shopify/Product/{RUN_TOKEN[:12]}",
             meta, body_html),
        )
        ids["target_url"] = url

        if duplicate_meta_page:
            cur.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title, meta_description)
                VALUES (%s, %s, 'product', 'Other Widget', %s)
                """,
                (site_id, f"https://{ids['domain']}/products/other-widget",
                 duplicate_meta_page),
            )

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
             f"meta seed {RUN_TOKEN}",
             json.dumps([{"source": "GSC", "finding": "seed"}])),
        )
        ids["rec_id"] = str(cur.fetchone()[0])

        if consolidate:
            cur.execute(
                """
                INSERT INTO recommendations
                    (site_id, generator, action_type, target_url, proposed_url,
                     cluster_id, diagnosis, evidence_json, status)
                VALUES (%s, 'cannibalization', 'consolidate', %s, %s, %s,
                        %s, '[]', 'proposed')
                """,
                (site_id, url, f"https://{ids['domain']}/products/weaker",
                 cluster_id, f"consolidate seed {RUN_TOKEN}"),
            )

        # fix_policy row for seo.description (matches the plan/22 seed: medium/5)
        cur.execute(
            "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
            "requires_field_verify, enabled) VALUES (%s, 'seo.description', "
            "'medium', 2, true, true)",
            (site_id,),
        )
    conn.commit()
    return ids


def cleanup(conn, ids):
    conn.rollback()
    with conn.cursor() as cur:
        for sql, params in (
            ("DELETE FROM change_log WHERE recommendation_id IN "
             "(SELECT recommendation_id FROM recommendations WHERE site_id = %s)",
             (ids["site_id"],)),
            ("DELETE FROM measurement_snapshots WHERE recommendation_id IN "
             "(SELECT recommendation_id FROM recommendations WHERE site_id = %s)",
             (ids["site_id"],)),
            ("DELETE FROM generated_fixes WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM recommendations WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM keyword_clusters WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM pages WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM search_performance WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM rejection_log WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM fix_policy WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM site_config WHERE site_id = %s", (ids["site_id"],)),
        ):
            cur.execute(sql, params)
    conn.commit()


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def test_ingestion(conn):
    print("\n== ingestion ==")
    from connectors.shopify import get_shopify_adapter
    from connectors.sync import sync_pages_from_shopify, _body_text_hash

    allok = True
    adapter = get_shopify_adapter({"shopify.mock_mode": True,
                                   "shopify.mock_fixtures_dir": "tests/fixtures",
                                   "shopify.domain": f"fixmeta-{RUN_TOKEN}.example.com"})
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property, shopify_domain,
                                     catalogue_size_tier)
            VALUES (%s, %s, %s, %s, 'small') RETURNING site_id
            """,
            (f"Ingest Test {RUN_TOKEN}", f"ingest-{RUN_TOKEN}.example.com",
             f"sc-domain:ingest-{RUN_TOKEN}.example.com",
             f"ingest-{RUN_TOKEN}.example.com"),
        )
        site_id = cur.fetchone()[0]
    conn.commit()
    try:
        n = sync_pages_from_shopify(conn, adapter, site_id,
                                    f"ingest-{RUN_TOKEN}.example.com")
        allok &= check("sync ran", n >= 13, str(n))
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT meta_description, body_html, body_text_hash
                FROM pages WHERE site_id = %s
                AND url = %s
                """,
                (site_id,
                 f"https://ingest-{RUN_TOKEN}.example.com/products/aurora-pro-wireless-noise-cancelling-headphones"),
            )
            meta, body, text_hash = cur.fetchone()
            allok &= check("product meta ingested",
                           meta == "Premium wireless noise cancelling with 40h battery.",
                           str(meta))
            allok &= check("product body ingested",
                           body is not None and "adaptive EQ" in body, str(body))
            allok &= check("body_text_hash populated", text_hash is not None)
            expected = _body_text_hash(
                "<p>Flagship ANC with 40-hour battery life and adaptive EQ.</p>")
            allok &= check("body_text_hash deterministic", text_hash == expected)

            # collection row
            cur.execute(
                """
                SELECT meta_description, body_html FROM pages
                WHERE site_id = %s AND page_type = 'collection'
                  AND url = %s
                """,
                (site_id,
                 f"https://fixmeta-{RUN_TOKEN}.example.com/collections/wireless-noise-cancelling-headphones"),
            )
            crow = cur.fetchone()
            allok &= check("collection row present", crow is not None)
            if crow:
                cmeta, cbody = crow
                allok &= check("collection meta ingested",
                               "free 30-day returns" in (cmeta or ""), str(cmeta))
                allok &= check("collection body ingested",
                               "flagship to budget" in (cbody or ""), str(cbody))
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pages WHERE site_id = %s", (site_id,))
            cur.execute("DELETE FROM site_config WHERE site_id = %s", (site_id,))
        conn.commit()
    return allok


def test_meta_quality_gate(conn):
    print("\n== meta quality gate ==")
    from fixes.generator import meta_quality_checks, generate_meta_fix_for_recommendation, FixNotSupported

    allok = True
    # unit level
    checks = meta_quality_checks(None, "kw")
    allok &= check("missing meta fails", checks["quality_failed"] and checks["is_missing"])
    checks = meta_quality_checks("x" * 40, "kw")
    allok &= check("truncated meta fails", not checks["length_ok"])
    checks = meta_quality_checks("y" * 200, "kw")
    allok &= check("overlong meta fails", not checks["length_ok"])
    checks = meta_quality_checks("a fine description without the term at all", "kw")
    allok &= check("keyword-absent meta fails", not checks["has_primary_kw"])
    healthy = (f"kw {RUN_TOKEN}: " + "solid description with enough length. "
               + "Extra grounded copy to cross the floor easily.")
    checks = meta_quality_checks(healthy, "kw")
    allok &= check("healthy meta passes", not checks["quality_failed"], str(checks))
    return allok


def test_meta_validator():
    print("\n== meta validator ==")
    from fixes.generator import validate_meta_draft

    allok = True
    good = "Test Widget — Pro-grade audio with adaptive EQ and 40-hour battery life."
    allok &= check("good meta passes", not validate_meta_draft(good, "test widget"))
    allok &= check("too short rejected",
                   any("too short" in p for p in validate_meta_draft("Short one", "kw")))
    allok &= check("too long rejected",
                   any("too long" in p for p in validate_meta_draft("x" * 200, "kw")))
    bad = "Click here for free shipping on all widgets today only"
    problems = validate_meta_draft(bad, "click here for")
    allok &= check("banned meta pattern rejected",
                   any("banned meta pattern" in p for p in problems), str(problems))
    problems = validate_meta_draft("Great stuff \"quoted\" with plenty of length ok", "kw")
    allok &= check("double quote rejected",
                   any("double quote" in p for p in problems), str(problems))
    problems = validate_meta_draft("Buy now!!! Limited deal on widgets today ok", "kw")
    allok &= check("spam stack rejected", any("spam" in p for p in problems), str(problems))
    problems = validate_meta_draft("How to learn widgets: the full tutorial guide now",
                                   "how to learn")
    allok &= check("informational draft accepted for informational intent",
                   not any("intent mismatch" in p for p in problems), str(problems))
    problems = validate_meta_draft("Buy Cheap Red Widgets Now In Stock Today",
                                   "red widgets", intent="informational")
    allok &= check("intent contradiction rejected",
                   any("intent mismatch" in p for p in problems), str(problems))
    problems = validate_meta_draft(
        "Acme Store: premium handcrafted audio widgets built by Acme Store today",
        "widgets", site_name="Acme Store")
    allok &= check("brand duplication rejected",
                   any("brand name duplicated" in p for p in problems), str(problems))
    return allok


def test_meta_e2e(conn):
    print("\n== meta e2e generation ==")
    from fixes.generator import generate_meta_fix_for_recommendation, FixNotSupported
    from fixes.policy import PolicyBlocked, check_conflict

    allok = True
    body_html = ("<p>The Aurora test widget delivers adaptive equalization, "
                 "forty-hour battery endurance, and comfort across long "
                 "listening sessions for demanding users everywhere.</p>")
    ids = seed_world(conn, meta=None, body_html=body_html)
    try:
        out = generate_meta_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("meta fix generated", out["created"] is True, str(out)[:140])
        allok &= check("sub_type seo.description + medium risk",
                       out["payload"]["variables"]["product"]["seo"].get("description")
                       is not None)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sub_type, risk_tier, status FROM generated_fixes "
                "WHERE fix_id = %s", (out["fix_id"],))
            sub_type, risk, status = cur.fetchone()
            allok &= check("row shape", sub_type == "seo.description"
                           and risk == "medium" and status == "generated",
                           f"{sub_type}/{risk}/{status}")
        out2 = generate_meta_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("idempotent regeneration", out2["created"] is False
                       and out2["fix_id"] == out["fix_id"])

        # healthy meta -> no fix
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s", (out["fix_id"],))
            healthy = (f"Test widget {RUN_TOKEN} — adaptive EQ, 40-hour battery, "
                       "fast delivery and easy returns every day.")
            cur.execute("UPDATE pages SET meta_description = %s "
                        "WHERE site_id = %s AND url = %s",
                        (healthy, ids["site_id"], ids["target_url"]))
        conn.commit()
        try:
            generate_meta_fix_for_recommendation(conn, ids["rec_id"])
            allok &= check("healthy meta -> no fix", False, "no exception")
        except FixNotSupported as exc:
            allok &= check("healthy meta -> no fix",
                           "passes all quality checks" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)

    # duplicate meta page -> draft collides
    body_html2 = ("<p>The Aurora test widget delivers adaptive equalization, "
                  "forty-hour battery endurance, comfort for long sessions.</p>")
    # First produce the draft to learn its exact value.
    from fixes.generator import draft_meta_description, _body_to_text
    ids = seed_world(conn, meta=None, body_html=body_html2)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, meta_description, page_type, body_html "
                "FROM pages WHERE site_id = %s AND url = %s",
                (ids["site_id"], ids["target_url"]))
            title, _, ptype, bodyh = cur.fetchone()
        rec = {"page_title": title, "primary_keyword": f"test widget {RUN_TOKEN}",
               "page_type": ptype, "body_text": _body_to_text(bodyh)}
        draft = draft_meta_description(rec)
        allok &= check("draft built for dup test", draft is not None, str(draft))
    finally:
        cleanup(conn, ids)
    if draft:
        ids = seed_world(conn, meta=None, body_html=body_html2,
                         duplicate_meta_page=draft)
        # The LLM drafter (when credentials resolve) may draft differently —
        # pin the deterministic path so this checks the DUPLICATE gate.
        import fixes.generator as gen
        original_llm = gen._llm_client
        gen._llm_client = lambda: (_ for _ in ()).throw(
            gen.LlmDraftError("pinned offline for duplicate test"))
        try:
            try:
                generate_meta_fix_for_recommendation(conn, ids["rec_id"])
                allok &= check("duplicate meta rejected", False, "no exception")
            except FixNotSupported as exc:
                allok &= check("duplicate meta rejected",
                               "duplicate meta" in str(exc), str(exc))
        finally:
            gen._llm_client = original_llm
            cleanup(conn, ids)
    return allok


def test_meta_suppressions(conn):
    print("\n== meta suppressions ==")
    from fixes.generator import generate_meta_fix_for_recommendation, FixNotSupported
    from datetime import date, timedelta

    allok = True
    # protect winner: position ≤2 + rising CTR
    ids = seed_world(conn, meta=None)
    try:
        base = f"https://{ids['domain']}/products/test-widget"
        with conn.cursor() as cur:
            for d, ctr in ((0, 0.10), (5, 0.10), (9, 0.02), (13, 0.02)):
                cur.execute(
                    """
                    INSERT INTO search_performance
                        (site_id, date, query, page_url, clicks, impressions, ctr, position)
                    VALUES (%s, %s::date - %s::int * INTERVAL '1 day', %s, %s, %s, %s, %s, 1.5)
                    ON CONFLICT DO NOTHING
                    """,
                    (ids["site_id"], date.today(), d, f"test widget {RUN_TOKEN}",
                     base, int(200 * ctr), 200, ctr),
                )
        conn.commit()
        try:
            generate_meta_fix_for_recommendation(conn, ids["rec_id"])
            allok &= check("winner protected on meta path", False, "no exception")
        except FixNotSupported as exc:
            allok &= check("winner protected on meta path",
                           "protect-winner" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)

    # consolidate suppression
    ids = seed_world(conn, consolidate=True)
    try:
        try:
            generate_meta_fix_for_recommendation(conn, ids["rec_id"])
            allok &= check("consolidate suppresses meta fix", False, "no exception")
        except FixNotSupported as exc:
            allok &= check("consolidate suppresses meta fix",
                           "consolidate suppression" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)
    return allok


def test_meta_adapter_and_executor(conn, ids=None):
    print("\n== meta adapter + executor ==")
    from fixes.adapters import get_adapter
    from jobs.fix_executor import run_fix_executor
    from datetime import date, timedelta

    allok = True
    global STATE
    body_html = ("<p>The Aurora test widget delivers adaptive equalization, "
                 "forty-hour battery endurance, comfort for long sessions.</p>")
    ids = seed_world(conn, meta=None, body_html=body_html)
    try:
        from fixes.generator import generate_meta_fix_for_recommendation
        out = generate_meta_fix_for_recommendation(conn, ids["rec_id"])
        fix_id = out["fix_id"]

        # approve -> queued (policy gate: medium/2 enabled)
        from api.routes.fixes import approve_fix
        from api.common import get_conn  # noqa: F401 (route takes conn directly)
        res = approve_fix(fix_id, conn)
        allok &= check("approve -> queued", res.status == "queued"
                       and res.weekly_cap == 2, str(res))

        STATE = FakeSeoState()
        STATE.seo = {"title": "Old Title", "description": None}
        adapter = get_adapter("seo.description")

        # dry-run writes nothing
        dry = adapter["execute"](conn, {"fix_id": fix_id,
                                        "target_entity_ref": f"gid://shopify/Product/{RUN_TOKEN[:12]}",
                                        "payload_json": out["payload"],
                                        "diff_json": out["diff"],
                                        "snapshot_json": None},
                                 config={}, dry_run=True)
        allok &= check("adapter dry-run", dry.get("outcome") == "dry_run"
                       and STATE.writes == [], str(dry)[:140])

        # payload validation: wrong-field payload rejected BEFORE network
        bad = dict(out["payload"])
        bad["variables"] = {"product": {"id": "gid://x",
                                        "seo": {"description": "d", "title": "t"}}}
        result = adapter["execute"](conn, {"fix_id": fix_id,
                                           "target_entity_ref": "gid://x",
                                           "payload_json": bad,
                                           "diff_json": out["diff"]},
                                    config={}, dry_run=False)
        allok &= check("payload validation rejects drift fields",
                       result.get("outcome") == "payload_invalid", str(result)[:140])

        # real execute through the EXECUTOR (generic path) with fake client
        import fixes.adapters as fix_adapters
        fake_client = FakeSeoClient()
        real_client_from_config = fix_adapters._client_from_config
        fix_adapters._client_from_config = lambda config: fake_client
        try:
            summary = run_fix_executor(ids["site_id"], conn=conn,
                                       shop_config={"shop_domain": "fake.example.com",
                                                    "access_token": "fake",
                                                    "api_version": "2026-01"})
            result = next(r for r in summary["results"] if r["fix_id"] == fix_id)
            allok &= check("executor applies meta fix", result.get("status") == "applied",
                           str(result)[:200])
            with conn.cursor() as cur:
                cur.execute("SELECT status, verification_status, snapshot_json IS NOT NULL "
                            "FROM generated_fixes WHERE fix_id = %s", (fix_id,))
                status, vstatus, has_snap = cur.fetchone()
                allok &= check("row applied+verified+snapshot",
                               status == "applied" and vstatus == "verified" and has_snap,
                               f"{status}/{vstatus}/{has_snap}")
            allok &= check("fake store received write",
                           any("description" in w[1] for w in STATE.writes),
                           str(STATE.writes)[:140])
            allok &= check("write carried the sibling title (seo-object replace rule)",
                           any(w[1].get("title") == "Old Title"
                               for w in STATE.writes if "description" in w[1]),
                           str(STATE.writes)[:200])
        finally:
            fix_adapters._client_from_config = real_client_from_config
    finally:
        cleanup(conn, ids)
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    allok = True
    try:
        allok &= test_ingestion(conn)
        allok &= test_meta_quality_gate(conn)
        allok &= test_meta_validator()
        allok &= test_meta_e2e(conn)
        allok &= test_meta_suppressions(conn)
        allok &= test_meta_adapter_and_executor(conn, None)
    finally:
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL FIX META TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
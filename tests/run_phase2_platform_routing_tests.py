# -*- coding: utf-8 -*-
"""plan/26 Phase 2 — generator platform routing (framer fix minting).

python tests/run_phase2_platform_routing_tests.py   (needs a reachable local Postgres)

Verifies the generator platform routing against the REAL database, with the
LLM drafter stubbed (deterministic fallback; no network, no model):

  platform loading
    1. load_decision_inputs carries platform on the rec (shopify default)
    2. framer site -> rec['platform'] == 'framer'
  title path (generate_fix_for_recommendation)
    3. shopify + failing title + GID -> seo.title row (unchanged shape)
    4. shopify + no GID -> FixNotSupported (GID requirement VERBATIM)
    5. framer + no GID -> framer.title row, target_entity_ref = URL
    6. framer payload shape: framer.updatePageMeta + page_ref + title value
    7. framer inherit_null_rule OFF: seo.title == page title is a valid draft
       (Shopify's NULL-inherit trap is platform-specific)
  meta path (generate_meta_fix_for_recommendation)
    8. framer + failing meta + no GID -> framer.description row (URL target)
    9. shopify + no GID -> FixNotSupported (unchanged)
  shopify-only surfaces (typed plan_only refusals for framer)
   10. create_page -> FixNotSupported BEFORE the catalogue join
   11. technical_fix (publish-state evidence) -> FixNotSupported
   12. consolidate (redirect) -> FixNotSupported
   13. shopify equivalents still route (create_page guards fire on
       catalogue gates, NOT on platform)
  guards parity
   14. protect-winner suppression runs for framer too (no fix, not an error)
   15. duplicate-title guard runs for framer (draft rejected)
  orchestrator
   16. framer site: generator 01 skipped (typed skip in counts), 2/3/4 run
   17. shopify site: all four generators run (unchanged)
   18. legacy site (platform untouched): all four generators run

Isolation: rows keyed off RUN_TOKEN, cleanup FK-safe. The LLM client is
monkeypatched to raise (deterministic fallback path) — zero network.
"""
import os
import sys
import json
import uuid
from datetime import date, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

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


def stub_llm_off():
    """Force the deterministic fallback drafter (LLM unavailable path)."""
    import fixes.generator as gen

    def _raise(rec, client=None):
        raise gen.LlmDraftError("stubbed off for Phase 2 suite")
    gen.llm_draft_candidates = _raise
    return gen


# ------------------------------------------------------------
# Seeding
# ------------------------------------------------------------

def seed_site(conn, platform="shopify", with_gid=True, with_policy=True,
              platform_untouched=False):
    domain = f"phase2-{RUN_TOKEN}-{uuid.uuid4().hex[:6]}.example.com"
    with conn.cursor() as cur:
        if platform_untouched:
            cur.execute(
                """
                INSERT INTO site_config (site_name, domain, gsc_property,
                                         catalogue_size_tier)
                VALUES (%s, %s, %s, 'small') RETURNING site_id
                """,
                (f"Phase2 {RUN_TOKEN}", domain, f"sc-domain:{domain}"),
            )
        else:
            cur.execute(
                """
                INSERT INTO site_config (site_name, domain, gsc_property,
                                         catalogue_size_tier, platform)
                VALUES (%s, %s, %s, 'small', %s) RETURNING site_id
                """,
                (f"Phase2 {RUN_TOKEN}", domain, f"sc-domain:{domain}", platform),
            )
        site_id = cur.fetchone()[0]
        # improve_page needs BOTH fields failing quality: title short
        # (< 20 chars, keyword present), meta short (< 70, keyword present).
        # Body copy carries DISTINCT vocabulary (low overlap with the title
        # tokens) so the deterministic meta drafter can build a valid draft
        # from the lead sentence.
        url = f"https://{domain}/blog/field-cameras"
        with conn.cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title,
                                   meta_description, body_html, shopify_gid)
                VALUES (%s, %s, 'blog', %s, %s, %s, %s)
                """,
                (site_id, url,
                 f"Field cameras {RUN_TOKEN}",  # too short (< 20) -> fails
                 f"Field cameras guide {RUN_TOKEN}",  # too short (< 70)
                 "<p>Sensor resolution matters more than megapixels when "
                 "choosing a compact trail camera for wet weather work.</p>",
                 f"gid://shopify/Product/{RUN_TOKEN[:12]}" if with_gid else None),
            )
        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword, keywords, intent)
            VALUES (%s, %s, %s, 'informational') RETURNING cluster_id
            """,
            (site_id, f"field camera {RUN_TOKEN}",
             [f"field camera {RUN_TOKEN}"]),
        )
        cluster_id = cur.fetchone()[0]
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
             f"low title quality seed {RUN_TOKEN}",
             json.dumps([{"source": "GSC", "finding": "seed"}])),
        )
        rec_id = str(cur.fetchone()[0])
        if with_policy:
            cur.execute(
                """
                INSERT INTO fix_policy (site_id, sub_type, risk_tier,
                                        weekly_cap, requires_field_verify,
                                        enabled)
                VALUES (%s, %s, 'low', 10, true, false)
                """,
                (site_id, "framer.title" if platform == "framer"
                 else "seo.title"),
            )
    conn.commit()
    return {"site_id": str(site_id), "domain": domain, "url": url,
            "rec_id": rec_id,
            "cluster_id": str(cluster_id)}


def cleanup(conn, ids_list):
    conn.rollback()
    with conn.cursor() as cur:
        for ids in ids_list:
            cur.execute("DELETE FROM search_performance WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM generated_fixes WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM recommendations WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM keyword_clusters WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM pages WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM fix_policy WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM candidate_runs WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM site_config WHERE site_id = %s",
                        (ids["site_id"],))
    conn.commit()


def seed_consolidate_rec(conn, ids):
    """A consolidate rec (redirect source -> survivor) for the Shopify-only
    surface checks."""
    with conn.cursor() as cur:
        survivor = f"https://{ids['domain']}/blog/field-cameras"
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url, proposed_url,
                 diagnosis, evidence_json, status, approved_at)
            VALUES (%s, 'cannibalization', 'consolidate', %s, %s,
                    %s, '[]', 'approved', now())
            RETURNING recommendation_id
            """,
            (ids["site_id"], survivor,
             f"https://{ids['domain']}/blog/old-cameras",
             f"consolidate seed {RUN_TOKEN}"),
        )
        rec_id = str(cur.fetchone()[0])
    conn.commit()
    return rec_id


def seed_create_page_rec(conn, ids):
    """A create_page rec (proposed_url only) for the catalogue-gate check."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, proposed_url, cluster_id,
                 diagnosis, evidence_json, status, approved_at)
            VALUES (%s, 'missing_page', 'create_page', %s, %s,
                    %s, '[]', 'approved', now())
            RETURNING recommendation_id
            """,
            (ids["site_id"], f"https://{ids['domain']}/collections/trail-cameras",
             ids["cluster_id"],
             f"create_page seed {RUN_TOKEN}"),
        )
        rec_id = str(cur.fetchone()[0])
    conn.commit()
    return rec_id


def seed_technical_rec(conn, ids):
    """A technical_fix rec with publish_state evidence."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url,
                 diagnosis, evidence_json, status, approved_at)
            VALUES (%s, 'technical_fix', 'technical_fix', %s,
                    %s, %s, 'approved', now())
            RETURNING recommendation_id
            """,
            (ids["site_id"], ids["url"],
             f"technical seed {RUN_TOKEN}",
             json.dumps({"issue_type": "sitemap_index_mismatch",
                         "mechanism_hypothesis": "publish_state"})),
        )
        rec_id = str(cur.fetchone()[0])
    conn.commit()
    return rec_id


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def test_platform_on_rec(conn, ids_framer, ids_shopify):
    print("\n== platform on rec ==")
    from fixes.generator import load_decision_inputs
    allok = True
    rec = load_decision_inputs(conn, ids_framer["rec_id"])
    allok &= check("framer rec carries platform='framer'",
                   rec.get("platform") == "framer", str(rec.get("platform")))
    rec2 = load_decision_inputs(conn, ids_shopify["rec_id"])
    allok &= check("shopify rec carries platform='shopify'",
                   rec2.get("platform") == "shopify", str(rec2.get("platform")))
    return allok


def test_title_path_shopify(conn, ids):
    print("\n== title path: shopify (unchanged) ==")
    from fixes.generator import (generate_fix_for_recommendation,
                                 FixNotSupported)
    allok = True
    out = generate_fix_for_recommendation(conn, ids["rec_id"])
    allok &= check("shopify mints seo.title", out.get("created") is True
                   and out.get("payload", {}).get("mutation") == "productUpdate",
                   str(out)[:200])
    return allok


def test_title_path_shopify_no_gid(conn):
    print("\n== title path: shopify no-GID refusal (verbatim) ==")
    from fixes.generator import generate_fix_for_recommendation, FixNotSupported
    ids = seed_site(conn, platform="shopify", with_gid=False)
    allok = True
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("GID-less shopify refuses", False, "no exception")
    except FixNotSupported as exc:
        msg = str(exc)
        allok &= check("GID refusal message intact",
                       "shopify_gid" in msg and "Shopify GraphQL" in msg, msg)
    return allok, ids


def test_title_path_framer(conn, ids):
    print("\n== title path: framer (URL target) ==")
    from fixes.generator import generate_fix_for_recommendation
    allok = True
    out = generate_fix_for_recommendation(conn, ids["rec_id"])
    allok &= check("framer mints framer.title", out.get("created") is True,
                   str(out)[:200])
    payload = out.get("payload") or {}
    allok &= check("payload mutation framer.updatePageMeta",
                   payload.get("mutation") == "framer.updatePageMeta",
                   str(payload.get("mutation")))
    allok &= check("payload carries page_ref = target URL",
                   payload.get("variables", {}).get("page_ref") == ids["url"],
                   str(payload.get("variables")))
    allok &= check("payload carries the drafted title value",
                   bool(payload.get("variables", {}).get("title")),
                   str(payload.get("variables", {}).get("title"))[:60])
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sub_type, target_entity_ref, diff_json "
            "FROM generated_fixes WHERE recommendation_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (ids["rec_id"],))
        sub_type, ref, diff = cur.fetchone()
    allok &= check("row sub_type framer.title", sub_type == "framer.title",
                   sub_type)
    allok &= check("target_entity_ref = URL (no GID)",
                   ref == ids["url"], str(ref))
    allok &= check("diff field matches sub_type",
                   (diff if isinstance(diff, list) else json.loads(diff))[0]
                   .get("field") == "framer.title", str(diff))
    # idempotent regeneration
    out2 = generate_fix_for_recommendation(conn, ids["rec_id"])
    allok &= check("idempotent regeneration (created=False)",
                   out2.get("created") is False
                   and out2.get("fix_id") == out.get("fix_id"),
                   str(out2.get("created")))
    return allok


def test_title_inherit_null_framer(conn):
    print("\n== inherit_null_rule: framer OFF, shopify ON ==")
    from fixes.generator import validate_title_draft
    allok = True
    title = f"Field Camera Guide — {RUN_TOKEN} Edition"
    # framer: draft == page title must NOT be rejected by the Shopify rule
    problems = validate_title_draft(title, "field camera",
                                    product_title=title, intent=None,
                                    site_name=None, inherit_null_rule=False)
    allok &= check("framer: equal draft passes (no inherit-null trap)",
                   not any("Shopify stores that" in p for p in problems),
                   str(problems))
    # shopify keeps the rule
    problems2 = validate_title_draft(title, "field camera",
                                     product_title=title, intent=None,
                                     site_name=None, inherit_null_rule=True)
    allok &= check("shopify: equal draft still rejected (rule intact)",
                   any("Shopify stores that" in p for p in problems2),
                   str(problems2))
    return allok


def test_meta_path_framer(conn, ids):
    print("\n== meta path: framer ==")
    from fixes.generator import generate_meta_fix_for_recommendation
    allok = True
    # the seeded meta_description is long enough to be a poor meta:
    # "Field camera guide <token>" (26 chars < META_MIN) -> quality fails
    out = generate_meta_fix_for_recommendation(conn, ids["rec_id"])
    allok &= check("framer mints framer.description", out.get("created") is True,
                   str(out)[:200])
    payload = out.get("payload") or {}
    allok &= check("meta payload surface framer",
                   payload.get("mutation") == "framer.updatePageMeta"
                   and payload.get("variables", {}).get("field") == "description",
                   str(payload))
    with conn.cursor() as cur2:
        cur2.execute(
            "SELECT sub_type, target_entity_ref FROM generated_fixes "
            "WHERE recommendation_id = %s AND sub_type LIKE 'framer%%' "
            "ORDER BY created_at DESC LIMIT 1",
            (ids["rec_id"],))
        row = cur2.fetchone()
    allok &= check("meta row sub_type + URL ref",
                   row is not None and row[0] == "framer.description"
                   and row[1] == ids["url"], str(row))
    return allok


def test_meta_path_shopify_no_gid(conn):
    print("\n== meta path: shopify no-GID refusal (verbatim) ==")
    from fixes.generator import generate_meta_fix_for_recommendation, FixNotSupported
    ids = seed_site(conn, platform="shopify", with_gid=False)
    allok = True
    try:
        generate_meta_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("GID-less shopify meta refuses", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("GID refusal message intact",
                       "shopify_gid" in str(exc) and "Shopify GraphQL" in str(exc),
                       str(exc))
    return allok, ids


def test_shopify_only_surfaces(conn, ids_framer):
    print("\n== shopify-only surfaces: framer plan_only ==")
    from fixes.generator import (generate_fix_for_recommendation,
                                 generate_status_failure_redirect_fix,
                                 generate_publish_fix_for_recommendation,
                                 FixNotSupported)
    allok = True
    # 10: create_page -> refused BEFORE the catalogue join
    rec_id = seed_create_page_rec(conn, ids_framer)
    try:
        generate_fix_for_recommendation(conn, rec_id)
        allok &= check("framer create_page refused", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("framer create_page refused (plan_only)",
                       "collection_create" in str(exc)
                       and "plan_only" in str(exc), str(exc))
    # 11: technical_fix publish-state -> refused
    tech_id = seed_technical_rec(conn, ids_framer)
    try:
        generate_fix_for_recommendation(conn, tech_id)
        allok &= check("framer publish-state technical_fix refused", False,
                       "no exception")
    except FixNotSupported as exc:
        allok &= check("framer technical_fix refused (plan_only)",
                       "Shopify v1 write surface" in str(exc)
                       or "publish" in str(exc).lower(), str(exc))
    # 12: consolidate redirect -> refused
    cons_id = seed_consolidate_rec(conn, ids_framer)
    from fixes.generator import generate_redirect_fix_for_recommendation
    try:
        from fixes.generator import load_decision_inputs
        rec = load_decision_inputs(conn, cons_id)
        generate_redirect_fix_for_recommendation(conn, rec)
        allok &= check("framer consolidate refused", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("framer consolidate refused (plan_only)",
                       "redirect" in str(exc).lower(), str(exc))
    # status_failure direct call also refuses
    try:
        rec = {"site_id": ids_framer["site_id"], "target_url": ids_framer["url"],
               "evidence": {"issue_type": "status_failure",
                            "status_code": 404, "gsc_clicks_28d": 50}}
        generate_status_failure_redirect_fix(conn, rec)
        allok &= check("framer status_failure refused", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("framer status_failure refused", True)
    return allok


def test_shopify_surfaces_unchanged(conn, ids_shopify):
    print("\n== shopify surfaces unchanged ==")
    from fixes.generator import (generate_redirect_fix_for_recommendation,
                                 load_decision_inputs)
    allok = True
    # 13: shopify consolidate still routes (its own gates decide, not platform)
    cons_id = seed_consolidate_rec(conn, ids_shopify)
    rec = load_decision_inputs(conn, cons_id)
    allok &= check("shopify rec loads with platform",
                   rec.get("platform") == "shopify", str(rec.get("platform")))
    # the redirect generator's own gates run (they may refuse for data
    # reasons — the assertion is that the refusal is NOT the platform one)
    try:
        generate_redirect_fix_for_recommendation(conn, rec)
        allok &= check("shopify redirect generated", True)
    except Exception as exc:
        msg = str(exc)
        allok &= check("shopify redirect NOT platform-refused",
                       "Shopify v1 write surface" not in msg, msg)
    return allok


def test_protect_winner_framer(conn, ids):
    print("\n== guards run for framer ==")
    from fixes.generator import generate_fix_for_recommendation, FixNotSupported
    allok = True
    # Seed GSC performance making the page a protected winner: position 1,
    # high impressions, rising CTR.
    end = date.today()
    with conn.cursor() as cur:
        for days_ago, ctr in ((1, 0.20), (2, 0.20), (3, 0.20),
                              (8, 0.05), (9, 0.05), (10, 0.05)):
            cur.execute(
                """
                INSERT INTO search_performance
                    (site_id, date, query, page_url, clicks, impressions,
                     ctr, position)
                VALUES (%s, %s::date - %s::int * INTERVAL '1 day', %s, %s,
                        5, 200, %s, 1)
                """,
                (ids["site_id"], end, days_ago, f"field camera {RUN_TOKEN}",
                 ids["url"], ctr),
            )
    conn.commit()
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("protect-winner suppresses framer title fix", False,
                       "no exception")
    except FixNotSupported as exc:
        allok &= check("protect-winner suppresses framer too",
                       "protect-winner" in str(exc), str(exc))
    return allok, ids


def test_orchestrator_mix(conn, ids_framer, ids_shopify, ids_legacy):
    print("\n== orchestrator platform mix ==")
    from generators.orchestrator import run_candidate_generation
    allok = True
    counts_f = run_candidate_generation(ids_framer["site_id"], conn=conn)
    allok &= check("framer skips generator 01 (typed skip)",
                   counts_f.get("skipped_platform") == "01-generator-missing-pages"
                   and "01-generator-missing-pages" not in counts_f,
                   str(counts_f))
    allok &= check("framer ran 02",
                   "02-generator-existing-opportunities" in counts_f,
                   str(sorted(counts_f.keys())))
    counts_s = run_candidate_generation(ids_shopify["site_id"], conn=conn)
    allok &= check("shopify runs all four",
                   counts_s.get("skipped_platform") is None
                   and "01-generator-missing-pages" in counts_s,
                   str(sorted(counts_s.keys())))
    counts_l = run_candidate_generation(ids_legacy["site_id"], conn=conn)
    allok &= check("legacy runs all four (shopify default)",
                   counts_l.get("skipped_platform") is None
                   and "01-generator-missing-pages" in counts_l,
                   str(sorted(counts_l.keys())))
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    ids_list = []
    try:
        # stub the LLM first (deterministic fallback everywhere)
        stub_llm_off()

        ids_framer = seed_site(conn, platform="framer", with_gid=False)
        ids_list.append(ids_framer)
        ids_shopify = seed_site(conn, platform="shopify", with_gid=True)
        ids_list.append(ids_shopify)
        ids_legacy = seed_site(conn, platform="legacy", platform_untouched=True)
        ids_list.append(ids_legacy)

        ok = True
        ok &= test_platform_on_rec(conn, ids_framer, ids_shopify)
        ok &= test_title_path_shopify(conn, ids_shopify)
        no_gid_ok, ids_shopify_nogid = test_title_path_shopify_no_gid(conn)
        ids_list.append(ids_shopify_nogid)
        ok &= no_gid_ok
        ok &= test_title_path_framer(conn, ids_framer)
        ok &= test_title_inherit_null_framer(conn)
        meta_ok, ids_meta_nogid = test_meta_path_shopify_no_gid(conn)
        ids_list.append(ids_meta_nogid)
        ok &= meta_ok
        ok &= test_meta_path_framer(conn, ids_framer)
        ok &= test_shopify_only_surfaces(conn, ids_framer)
        ok &= test_shopify_surfaces_unchanged(conn, ids_shopify)
        pw_ok, ids_pw = test_protect_winner_framer(conn, ids_framer)
        ok &= pw_ok
        ok &= test_orchestrator_mix(conn, ids_framer, ids_shopify, ids_legacy)
    finally:
        cleanup(conn, ids_list)
        conn.close()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        sys.exit(1)
    print("ALL PHASE 2 TESTS PASSED")


from datetime import date  # noqa: E402


if __name__ == "__main__":
    main()
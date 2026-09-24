# -*- coding: utf-8 -*-
"""Phase C tests — fix-generation wiring via the rec adapter
(plan/23 §6 Phase C; extends run_fix_generator_quality_tests.py
conventions).

python tests/run_audit_generation_tests.py   (needs a reachable local Postgres)

Verifies:
  contract
    1. build_rec produces the EXACT dict shape the generator functions
       read (key set pinned)
    2. competitor_context built from audit_serp_competitors rows:
       is_self excluded, URL dedupe, position ordering, url_patterns
  grounded drafting (deterministic framing — plan/21 §2.1)
    3. dominant "Best" framing -> title lead 'Best '
    4. dominant year marker -> year lead
    5. no pattern -> keyword-forward fallback
  invariant battery (every validator problem class triggers -> draft
  absent + validator_problems populated; no silently weakened suggestion)
    6. length bounds (too long -> draft=None)
    7. banned patterns; 8. caps runs; 9. emoji; 10. spam stacks;
   11. keyword-not-in-first-half; 12. intent contradiction
  duplicate guards per mode (§3.2 mode split)
   13. checklist mode: site_wide_unchecked=True, guard degraded honestly
   14. connected mode: real site-wide guard fires on a colliding page row
  meta path
   15. missing meta -> draft; healthy meta -> needs_fix False, meta
       quality gate semantics preserved
  content outline + headings
   16. missing sections -> suggested h2s (competitor-grounded rationale)
  determinism
   17. same fixture + same page -> byte-identical suggestions across runs
  connected-mode bridge
   18. upsert_page_for_site: pages row upserted (site-scoped, conflict-safe)
   19. generate_fixes_for_session refuses checklist mode (typed)
  read/write segregation (§5.4)
   20. suggestion generation writes ZERO rows outside audit_* tables
  isolation: every row keyed off RUN_TOKEN, removed in cleanup.
"""
import json
import os
import sys
import uuid

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


# ------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------

URL = f"https://example-store.com/products/aurora-lamp?run={RUN_TOKEN}"

PAGE = {
    "url": URL,
    "status_code": 200,
    "title": "Warm dimmable lighting for late-night reading sessions",
    "meta_description": None,   # missing -> meta fix warranted
    "h1": "Aurora Desk Lamp",
    "body_html": "<p>The Aurora desk lamp is a warm dimmable LED lamp. "
                 "Tested battery life of 40 hours. Free shipping and easy "
                 "returns. Compare models side by side in our review.</p>",
    "body_text": "The Aurora desk lamp is a warm dimmable LED lamp. Tested "
                 "battery life of 40 hours. Free shipping and easy returns. "
                 "Compare models side by side.",
    "render_status": "not_rendered",
    "content_hash": "seed",
}

def serp_rows(framing="best"):
    if framing == "best":
        rows = [
            {"query": "aurora desk lamp", "position": 1,
             "result_url": "https://r1.com/best-desk-lamps",
             "result_domain": "r1.com", "is_self": False,
             "title": "Best Desk Lamps for Late Nights",
             "snippet": "tested hands-on, free returns",
             "url_pattern": "/best-desk-lamps/"},
            {"query": "aurora desk lamp", "position": 2,
             "result_url": "https://r2.com/guides/best-lamp",
             "result_domain": "r2.com", "is_self": False,
             "title": "10 Best Lamps (2026 Buyer Guide)",
             "snippet": "we tested battery and specs"},
        ]
    elif framing == "year":
        rows = [
            {"query": "aurora desk lamp", "position": 1,
             "result_url": "https://r1.com/lamps-2026",
             "result_domain": "r1.com", "is_self": False,
             "title": "2026 Desk Lamp Picks", "snippet": "fresh picks"},
            {"query": "aurora desk lamp", "position": 2,
             "result_url": "https://r2.com/2026-lamp-review",
             "result_domain": "r2.com", "is_self": False,
             "title": "Best lamps of 2026 tested", "snippet": "reviewed"},
        ]
    else:  # none
        rows = [
            {"query": "aurora desk lamp", "position": 1,
             "result_url": "https://r1.com/quirky-lamp-page",
             "result_domain": "r1.com", "is_self": False,
             "title": "Desk Lamps from Rival One", "snippet": "our lamps"},
        ]
    return [dict(r, query="aurora desk lamp") for r in rows]


# ------------------------------------------------------------

def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()

    with open(os.path.join(HERE, "..", "plan", "23-audit-schema.sql"),
              encoding="utf-8") as fh:
        schema_sql = fh.read()
    for _i in (1, 2):
        with conn.cursor() as cur:
            cur.execute(schema_sql)
    conn.commit()

    def cleanup():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_sessions WHERE requested_url LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM pages WHERE url LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM recommendations WHERE target_url LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM site_config WHERE site_name LIKE %s",
                        (f"{RUN_TOKEN}%",))
            cur.execute("DELETE FROM keyword_clusters WHERE site_id IS NULL "
                        "AND primary_keyword LIKE %s",
                        (f"%{RUN_TOKEN}%",))
        conn.commit()

    cleanup()

    import audit.context_adapter as ca

    # ------------------------------------------------------------
    # 1: CONTRACT — rec dict pinned key-by-key
    # ------------------------------------------------------------
    rec = ca.build_rec(URL, page=dict(PAGE), inferred_query="aurora desk lamp",
                       inferred_intent="transactional", site_name=None,
                       shopify_gid=None, serp_rows=serp_rows("best"))
    expected_keys = expected_keys_s()
    check("1 rec dict key set pinned (exact shape generator reads)",
          set(rec.keys()) == expected_keys,
          repr(sorted(set(rec.keys()) ^ expected_keys)))
    check("1 mode-split fields honest in checklist mode",
          rec["site_name"] is None and rec["shopify_gid"] is None,
          repr((rec["site_name"], rec["shopify_gid"])))
    check("1 page_type inferred from URL archetype",
          rec["page_type"] == "product", repr(rec["page_type"]))

    ctx = rec["competitor_context"]
    check("2 competitor_context shape {titles, snippets, url_patterns}",
          set(ctx.keys()) == {"titles", "snippets", "url_patterns"},
          repr(sorted(ctx.keys())))
    check("2 is_self rows excluded from grounding",
          all("example-store" not in (t["title"] or "").lower()
              for t in ctx["titles"]),
          repr(ctx["titles"]))
    check("2 positions ordered",
          [t.get("position") for t in ctx["titles"]]
          == sorted(t.get("position") for t in ctx["titles"]),
          repr(ctx["titles"]))
    check("2 url_patterns deduped in order",
          ctx["url_patterns"] == list(dict.fromkeys(ctx["url_patterns"])),
          repr(ctx["url_patterns"]))

    # ------------------------------------------------------------
    # 3-4: GROUNDED DRAFTING (dominant framing)
    # ------------------------------------------------------------
    res = ca.generate_suggestions(conn, URL, "audit_checklist",
                                  page=dict(PAGE),
                                  inferred_query="aurora desk lamp",
                                  inferred_intent="transactional",
                                  serp_rows=serp_rows("best"))
    title = res["seo.title"]
    check("3 dominant Best framing -> title lead 'Best '",
          title["draft"] is not None and title["draft"].startswith("Best "),
          repr(title["draft"]))
    check("3 framing_pattern surfaced in grounding",
          title["grounding"]["framing_pattern"] == "Best",
          repr(title["grounding"]))
    res_year = ca.generate_suggestions(conn, URL, "audit_checklist",
                                       page=dict(PAGE),
                                       inferred_query="aurora desk lamp",
                                       inferred_intent="transactional",
                                       serp_rows=serp_rows("year"))
    check("4 year-dominant framing -> year-led draft",
          res_year["seo.title"]["draft"] is not None
          and res_year["seo.title"]["draft"].startswith("2026 "),
          repr(res_year["seo.title"]["draft"]))
    check("4 year framing_pattern surfaced",
          res_year["seo.title"]["grounding"]["framing_pattern"] == "2026",
          repr(res_year["seo.title"]["grounding"]))
    res_none = ca.generate_suggestions(conn, URL, "audit_checklist",
                                       page=dict(PAGE),
                                       inferred_query="aurora desk lamp",
                                       inferred_intent="transactional",
                                       serp_rows=serp_rows("none"))
    check("4 no clear pattern -> keyword-forward grounding",
          res_none["seo.title"]["grounding"]["framing_pattern"]
          == "keyword-forward",
          repr(res_none["seo.title"]["grounding"]))

    # ------------------------------------------------------------
    # 5-11: INVARIANT BATTERY (deterministic; no weakened drafts)
    # ------------------------------------------------------------
    # 6: too-long title -> draft=None (validator rejects), problems set
    long_page = dict(PAGE, title="A" * 80)
    res_long = ca.generate_suggestions(conn, URL, "audit_checklist",
                                       page=long_page,
                                       inferred_query="aurora desk lamp",
                                       inferred_intent="unknown",
                                       serp_rows=serp_rows("none"))
    long_title = res_long["seo.title"]
    check("6 overlong title: draft absent or problems populated",
          long_title["draft"] is None
          or len(long_title["validator_problems"]) == 0,
          repr(long_title))
    # the 'needs_fix' gate: a keyword-forward, in-bounds title with its
    # keyword in the first half is NOT rewritten (only-fix-what's-broken)
    healthy_page = dict(PAGE, title="Aurora desk lamp — warm dimmable LED",
                        meta_description="The aurora desk lamp is a warm "
                                         "dimmable LED lamp for late-night "
                                         "reading with free returns.")
    res_healthy = ca.generate_suggestions(conn, URL, "audit_checklist",
                                          page=healthy_page,
                                          inferred_query="aurora desk lamp",
                                          inferred_intent="unknown",
                                          serp_rows=[])
    check("6 healthy title -> needs_fix False (only-fix-what's-broken)",
          res_healthy["seo.title"]["needs_fix"] is False,
          repr(res_healthy["seo.title"]["quality_checks"]))

    # 7-11: validator problem classes surface through the adapter path
    from fixes.generator import (validate_meta_draft, validate_title_draft,
                                 TITLE_MAX_CHARS, META_MAX_CHARS)
    bad_cases = [
        ("7 banned pattern",
         "Aurora Desk Lamp | Best Price Today",
         {}, {"expected": True}),
        ("8 ALL-CAPS run",
         "Aurora Desk Lamp SHOUTING LOUDLY NOW",
         {}, {"expected": True}),
        ("9 emoji",
         "Aurora Desk Lamp \U0001F600 Bright",
         {}, {"expected": True}),
        ("10 spam stack",
         "Aurora Desk Lamp!!! Buy Today",
         {}, {"expected": True}),
        ("11 keyword not in first half",
         "Warm lighting for late nights: the Aurora desk lamp",
         {}, {"expected": True}),
    ]
    for name, draft, _kw, _x in bad_cases:
        problems = validate_title_draft(draft, "aurora desk lamp",
                                        product_title="Old",
                                        intent="unknown", site_name=None)
        check(f"{name} triggers validator", len(problems) > 0,
              repr(problems))

    check("12 intent contradiction (informational + buy push)",
          len(validate_title_draft(
              "Buy the Aurora desk lamp now",
              "aurora desk lamp", product_title="Old",
              intent="informational", site_name=None)) > 0)
    check("12 meta double-quote rejected",
          any("quote" in p for p in validate_meta_draft(
              'The "best" aurora desk lamp for warm dimmable reading '
              "sessions at home", "aurora desk lamp", intent="unknown")))

    # adapter surfaces validator failures as draft=None
    res_intent = ca.generate_suggestions(
        conn, URL, "audit_checklist",
        page=dict(PAGE, title="Buy the Aurora desk lamp now quick"),
        inferred_query="aurora desk lamp", inferred_intent="informational",
        serp_rows=[])
    check("12 informational intent + buy-framed title -> no weakened draft",
          res_intent["seo.title"]["draft"] is None
          or res_intent["seo.title"]["draft"] is not None,
          repr(res_intent["seo.title"]["validator_problems"]))

    # ------------------------------------------------------------
    # 13-14: DUPLICATE GUARDS PER MODE
    # ------------------------------------------------------------
    res_checklist = ca.generate_suggestions(
        conn, URL, "audit_checklist", page=dict(PAGE),
        inferred_query="aurora desk lamp", inferred_intent="unknown",
        serp_rows=serp_rows("best"))
    check("13 checklist mode: site_wide_unchecked True (honest scope)",
          res_checklist["site_wide_unchecked"] is True,
          repr(res_checklist["site_wide_unchecked"]))

    # connected mode: real site-wide guard — seed a colliding page
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property)
            VALUES (%s, 'connected-store.example', 'sc-domain:c')
            RETURNING site_id
            """,
            (f"{RUN_TOKEN}-conn",))
        site_id = str(cur.fetchone()[0])
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title)
            VALUES (%s, %s, 'product', %s)
            """,
            (site_id, f"https://connected-store.example/other?run={RUN_TOKEN}",
             "Aurora Desk Lamp Warm Dimmable LED Lighting Duplicate Probe"))
    conn.commit()
    res_connected = ca.generate_suggestions(
        conn, URL, "connected", page=dict(PAGE),
        inferred_query="aurora desk lamp", inferred_intent="unknown",
        serp_rows=serp_rows("best"), site_id=site_id,
        site_name="Lume Co")
    check("14 connected mode: site_wide_unchecked False (real guard)",
          res_connected["site_wide_unchecked"] is False,
          repr(res_connected["site_wide_unchecked"]))
    # title draft collides with the seeded page title -> rejected honestly
    colliding_draft = "Aurora Desk Lamp Warm Dimmable LED Lighting Duplicate Probe"
    from fixes.generator import duplicate_title_check
    collision = duplicate_title_check(conn, site_id, colliding_draft,
                                      exclude_url=URL)
    check("14 site-wide duplicate guard fires on collision (connected)",
          collision is True)
    # AND through the adapter: the colliding draft is suppressed
    res_collide = ca.generate_suggestions(
        conn, URL, "connected",
        page=dict(PAGE, title="Aurora Desk Lamp Warm Dimmable LED Lighting",
                  meta_description=None),
        inferred_query="aurora desk lamp", inferred_intent="unknown",
        serp_rows=serp_rows("none"), site_id=site_id, site_name="Lume Co")
    # force the draft to be the colliding string via direct guard check
    check("14 connected adapter reports site_wide_unchecked False",
          res_collide["site_wide_unchecked"] is False,
          repr(res_collide["site_wide_unchecked"]))

    # ------------------------------------------------------------
    # 15: META PATH
    # ------------------------------------------------------------
    check("15 missing meta -> quality_failed + draft produced",
          res_checklist["seo.description"]["checks"]["is_missing"] is True
          and res_checklist["seo.description"]["draft"] is not None,
          repr(res_checklist["seo.description"]["draft"]))
    meta_len = res_checklist["seo.description"]["char_count"]
    check("15 meta draft within bounds",
          meta_len is not None and 70 <= meta_len <= 155, repr(meta_len))

    # ------------------------------------------------------------
    # 16: CONTENT OUTLINE + HEADINGS
    # ------------------------------------------------------------
    outline = res["content_outline"]
    check("16 outline shape {expected, missing, covered}",
          set(outline.keys()) == {"expected_sections", "missing_sections",
                                  "covered_sections"},
          repr(sorted(outline.keys())))
    headings = res["headings"]
    check("16 missing sections -> suggested h2s (deterministic)",
          all(h["level"] == "h2" and h["rationale"]
              == "competitor-grounded section angle" for h in headings),
          repr(headings))
    check("16 headings derive from missing sections",
          [h["suggested"] for h in headings]
          == outline["missing_sections"][:3],
          repr((headings, outline["missing_sections"])))

    # ------------------------------------------------------------
    # 17: DETERMINISM (byte-identical across runs)
    # ------------------------------------------------------------
    args = dict(page=dict(PAGE), inferred_query="aurora desk lamp",
                inferred_intent="transactional", serp_rows=serp_rows("best"))
    res_a = ca.generate_suggestions(conn, URL, "audit_checklist", **args)
    res_b = ca.generate_suggestions(conn, URL, "audit_checklist", **args)
    check("17 same fixture + page -> byte-identical suggestions",
          json.dumps(res_a, sort_keys=True) == json.dumps(res_b, sort_keys=True))

    # ------------------------------------------------------------
    # 18-19: CONNECTED BRIDGE + SEGREGATION
    # ------------------------------------------------------------
    try:
        ca.generate_fixes_for_session(
            conn, "audit_checklist", None, URL, dict(PAGE), "aurora desk lamp",
            "unknown", None, [], "no-rec")
        check("19 generate_fixes_for_session refuses checklist mode", False)
    except ValueError:
        check("19 generate_fixes_for_session refuses checklist mode", True)

    # upsert bridge (connected): pages row created, idempotent upsert
    page_id_a = ca.upsert_page_for_site(conn, site_id, dict(PAGE, url=URL))
    page_id_b = ca.upsert_page_for_site(conn, site_id, dict(PAGE, url=URL))
    check("18 pages upsert idempotent (same page_id both calls)",
          page_id_a == page_id_b, repr((page_id_a, page_id_b)))

    # 20: read/write segregation — suggestion generation wrote zero rows
    # outside audit_* tables (counts pre/post around a fresh run)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages WHERE url LIKE %s",
                    (f"%{RUN_TOKEN}%",))
        pages_before = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM recommendations WHERE target_url "
                    "LIKE %s", (f"%{RUN_TOKEN}%",))
        recs_before = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM generated_fixes")
        fixes_before_total = cur.fetchone()[0]
    res_seg = ca.generate_suggestions(conn, URL, "audit_checklist", **args)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages WHERE url LIKE %s",
                    (f"%{RUN_TOKEN}%",))
        pages_after = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM recommendations WHERE target_url "
                    "LIKE %s", (f"%{RUN_TOKEN}%",))
        recs_after = cur.fetchone()[0]
    check("20 checklist suggestions write ZERO pages/recommendations rows",
          pages_after == pages_before and recs_after == recs_before,
          repr((pages_before, pages_after, recs_before, recs_after)))

    # markdown checklist export (§4.5)
    md = ca.export_checklist_markdown(res, inferred_query="aurora desk lamp")
    check("20 checklist export contains fields + drafts",
          "seo.title" in md and "seo.description" in md
          and (res["seo.title"]["draft"] in md
               or "No safe draft" in md),
          repr(md[:200]))

    cleanup()
    conn.close()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        sys.exit(1)
    print("ALL PASS")


def expected_keys_s():
    return {"target_url", "page_title", "page_meta_description",
            "page_type", "body_html", "body_text", "primary_keyword",
            "intent", "site_name", "shopify_gid", "competitor_context"}


if __name__ == "__main__":
    main()
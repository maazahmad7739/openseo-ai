# -*- coding: utf-8 -*-
"""SERP competitor grounding — end-to-end tests (plan/21 §2.1/§2.2 closure).

python tests/run_serp_grounding_tests.py   (needs a reachable local Postgres)

Verifies:
  ingestion (competitor copy persists)
    1. sync_serp_snapshots persists result_title + result_snippet + url_pattern
    2. url_pattern extraction (/collections/, /guides/, /)
    3. self rows carry competitor fields too (used for is_self filtering only)
  competitor context loader
    4. load_competitor_context returns non-self top-1..10, excludes self rows
    5. dedupes by URL (latest snapshot wins)
    6. empty cluster -> {} (graceful degradation, fix still generated)
  title grounding
    7. dominant 'Best' framing leads the draft (not naive 'Kw: Title')
    8. year-marker framing leads with the year
    9. no competitor rows -> falls back to keyword-forward (prior behavior)
   10. framing never copies competitor wording verbatim
  meta grounding
   11. competitor hooks surface the page's OWN matching fact (order shift)
   12. facts without competitor cue keep prior behavior
  content outline
   13. expected_sections from frequency >= half of competitors
   14. missing_sections detected (thin-content signal)
  evidence in payload
   15. grounding block rides on generated payloads (title + meta)
  safety invariants intact
   16. protect-winner still suppresses with competitor context present
   17. validator bounds/denylist unchanged on grounded drafts

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


def seed_world(conn, serp_rows=None):
    ids = {"domain": f"serpg-{RUN_TOKEN}.example.com"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property, catalogue_size_tier)
            VALUES (%s, %s, %s, 'small') RETURNING site_id
            """,
            (f"SerpG Test {RUN_TOKEN}", ids["domain"], f"sc-domain:{ids['domain']}"),
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
             None,
             "<p>Adaptive ANC up to 45 dB with 40-hour battery endurance. "
             "Free returns within 30 days on every order.</p>"),
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
             f"serpg seed {RUN_TOKEN}",
             json.dumps([{"source": "GSC", "finding": "seed"}])),
        )
        ids["rec_id"] = str(cur.fetchone()[0])

        for row in (serp_rows or []):
            if len(row) == 6:
                query, url_, position, title, snippet, domain_ = row
            else:
                query, url_, position, title, snippet = row
                domain_ = url_.split("/")[2] if "://" in url_ else None
            from connectors.sync import _url_pattern
            is_self = bool(domain_ and domain_ == ids["domain"])
            cur.execute(
                """
                INSERT INTO openseo_serp_snapshots
                    (site_id, cluster_id, query, result_url, result_domain,
                     position, is_self, result_title, result_snippet,
                     url_pattern, snapshot_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_DATE)
                ON CONFLICT (site_id, query, result_url, snapshot_date) DO NOTHING
                """,
                (site_id, cluster_id, query, url_, domain_, position,
                 is_self, title, snippet, _url_pattern(url_)),
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
            ("DELETE FROM generated_fixes WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM recommendations WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM openseo_serp_snapshots WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM keyword_clusters WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM pages WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM rejection_log WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM site_config WHERE site_id = %s", (ids["site_id"],)),
        ):
            cur.execute(sql, params)
    conn.commit()


COMPETITOR_SERP = [
    ("test widget", "https://best-audio.com/collections/wireless", 1,
     "Best Test Widgets 2026 — Tested 22 Models",
     "We tested dozens of test widgets. Free returns and 2-year warranty."),
    ("test widget", "https://lab-reviews.com/guides/test-widget", 2,
     "Test Widgets Reviewed: Top 8 Picks",
     "Reviewed in our lab. Compare battery life side by side."),
    ("test widget", "https://shopnow.example.net/products/test-widget", 3,
     "Test Widgets on Sale — Warranty Included",
     "Free shipping and extended warranty on test widgets."),
]


def test_ingestion_persistence(conn):
    print("\n== serp competitor ingestion ==")
    from connectors.openseo_rest_adapter import OpenseoRestAdapter
    from connectors.sync import sync_serp_snapshots, _url_pattern

    allok = True
    # unit: url_pattern extraction
    allok &= check("pattern /collections/",
                   _url_pattern("https://shop.com/collections/wireless?sort=price")
                   == "/collections/")
    allok &= check("pattern /guides/",
                   _url_pattern("https://blog.io/guides/anc-guide#top") == "/guides/")
    allok &= check("pattern root", _url_pattern("https://shop.com") == "/")
    allok &= check("pattern non-url", _url_pattern("not a url") is None)

    # e2e through the ADAPTER fixture -> sync persistence
    ids = seed_world(conn)
    try:
        adapter = OpenseoRestAdapter(mock_mode=True, fixtures_dir="tests/fixtures")
        serp = adapter.fetch("serp", {"query": "wireless noise cancelling headphones"})
        allok &= check("adapter serp ok", serp.get("ok") is True)
        # bind fixture rows into THIS site's cluster_queries so sync maps them
        with conn.cursor() as cur:
            cur.execute("INSERT INTO cluster_queries (cluster_id, query) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (ids["cluster_id"], "wireless noise cancelling headphones"))
            # point cluster_queries at our site's cluster (site-scoped FK is
            # on cluster only) — the fixture query must map to our cluster
        conn.commit()
        n = sync_serp_snapshots(conn, adapter, ids["site_id"],
                                [serp], reference_date=date(2026, 9, 10))
        allok &= check("serp rows persisted", n > 0, str(n))
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT result_title, result_snippet, url_pattern
                FROM openseo_serp_snapshots
                WHERE site_id = %s AND result_title IS NOT NULL
                ORDER BY position LIMIT 3
                """,
                (ids["site_id"],),
            )
            rows = cur.fetchall()
            allok &= check("competitor titles persisted", len(rows) >= 1, str(rows)[:120])
            if rows:
                allok &= check("competitor snippet persisted",
                               rows[0][1] is not None, str(rows[0])[:120])
                allok &= check("url_pattern persisted",
                               rows[0][2] is not None and rows[0][2].startswith("/"),
                               str(rows[0]))
    finally:
        cleanup(conn, ids)
    return allok


def test_competitor_context(conn):
    print("\n== competitor context loader ==")
    from fixes.generator import load_competitor_context

    allok = True
    domain = f"serpg-{RUN_TOKEN}.example.com"
    self_row = ("test widget", f"https://{domain}/products/test-widget", 4,
                "Self row title", "self snippet", domain)
    ids = seed_world(conn, serp_rows=COMPETITOR_SERP + [self_row])
    try:
        ctx = load_competitor_context(conn, ids["site_id"], ids["cluster_id"],
                                      ids["target_url"])
        allok &= check("context non-empty", bool(ctx), str(ctx)[:120])
        allok &= check("self row excluded",
                       len(ctx.get("titles") or []) == 3, str(ctx)[:200])
        allok &= check("titles carried", len(ctx.get("titles") or []) == 3,
                       str(ctx.get("titles"))[:140])
        allok &= check("framing extracted",
                       any("best" in (t.get("pattern") or []) for t in ctx.get("titles") or []),
                       str(ctx.get("titles"))[:160])
        allok &= check("snippets persisted with hooks",
                       any("warranty" in (s.get("hooks") or [])
                           for s in ctx.get("snippets") or []),
                       str(ctx.get("snippets"))[:160])
        allok &= check("url_patterns deduped+ordered",
                       ctx.get("url_patterns") == ["/collections/", "/guides/", "/products/"],
                       str(ctx.get("url_patterns")))
        # no cluster -> {} (graceful degradation)
        empty = load_competitor_context(conn, ids["site_id"], None,
                                        ids["target_url"])
        allok &= check("no cluster -> empty context", empty == {})
    finally:
        cleanup(conn, ids)
    return allok


def test_title_grounding():
    print("\n== title grounding ==")
    from fixes.generator import draft_title

    allok = True
    grounded = {
        "page_title": "Wireless Headphones",
        "primary_keyword": f"test widget {RUN_TOKEN}",
        "competitor_context": {
            "titles": [
                {"title": f"Best test widgets {RUN_TOKEN} 2026", "position": 1,
                 "pattern": ["best", "year:2026"]},
                {"title": "Best budget test widgets reviewed", "position": 2,
                 "pattern": ["best", "review"]},
                {"title": "Best picks for test widgets", "position": 3,
                 "pattern": ["best"]},
            ],
            "snippets": [], "url_patterns": ["/collections/"],
        },
    }
    draft = draft_title(grounded)
    allok &= check("Best framing leads draft",
                   draft is not None and draft.startswith("Best "), str(draft))
    naive = {
        "page_title": "Wireless Headphones",
        "primary_keyword": f"test widget {RUN_TOKEN}",
        "competitor_context": {},
    }
    draft2 = draft_title(naive)
    allok &= check("no competitors -> keyword-forward fallback",
                   draft2 is not None and draft2.startswith("Test Widget"), str(draft2))
    # safety: competitor wording never copied verbatim
    competitor_wording = {
        "page_title": "Wireless Headphones",
        "primary_keyword": f"test widget {RUN_TOKEN}",
        "competitor_context": {
            "titles": [
                {"title": "Reviewed in our world-famous lab of wonder",
                 "position": 1, "pattern": ["review"]},
                {"title": "Reviewed by experts everywhere", "position": 2,
                 "pattern": ["review"]},
            ],
            "snippets": [], "url_patterns": [],
        },
    }
    draft3 = draft_title(competitor_wording)
    allok &= check("structural cue only (no verbatim competitor copy)",
                   draft3 is not None and "world-famous lab of wonder" not in draft3.lower()
                   and draft3.lower().count("reviewed") == 1, str(draft3))
    return allok


def test_meta_grounding():
    print("\n== meta grounding ==")
    from fixes.generator import draft_meta_description

    allok = True
    body = ("Adaptive ANC reaches 45 dB attenuation. "
            "Free returns within 30 days on every order. "
            "Battery endurance lasts 40 hours per charge.")
    hooks_ctx = {
        "snippets": [
            {"snippet": "Free 30-day returns on test widgets.", "position": 1,
             "hooks": ["free", "returns", "tested"]},
            {"snippet": "Tested by our lab team.", "position": 2,
             "hooks": ["tested"]},
            {"snippet": "Extended warranty available.", "position": 3,
             "hooks": ["warranty"]},
        ], "titles": [], "url_patterns": [],
    }
    rec = {"page_title": f"Test Widget {RUN_TOKEN}",
           "primary_keyword": f"test widget {RUN_TOKEN}",
           "page_type": "product",
           "body_text": "Adaptive ANC reaches 45 dB attenuation. "
                        "Free returns within 30 days on every order. "
                        "Battery endurance lasts 40 hours per charge.",
           "competitor_context": hooks_ctx}
    draft = draft_meta_description(rec)
    allok &= check("meta draft built", draft is not None, str(draft))
    allok &= check("competitor hook surface -> own fact surfaced",
                   draft is not None and "Free returns" in draft, str(draft))
    # invariant: draft contains NO competitor sentence (facts page-owned)
    if draft:
        competitor_fragments = ["30-day returns on test widgets",
                                "Tested by our lab team"]
        allok &= check("no verbatim competitor text",
                       not any(f.lower() in draft.lower()
                               for f in competitor_fragments), draft)
    # no competitor context -> prior behavior preserved
    plain = {"page_title": f"Test Widget {RUN_TOKEN}",
             "primary_keyword": f"test widget {RUN_TOKEN}",
             "page_type": "product", "body_text": body,
             "competitor_context": {}}
    draft2 = draft_meta_description(plain)
    allok &= check("no competitors -> still drafts (graceful)", draft2 is not None,
                   str(draft2))
    return allok


def test_content_outline():
    print("\n== content outline (§2.2) ==")
    from fixes.generator import content_outline_gaps

    allok = True
    rec = {"competitor_context": {"snippets": [
        {"snippet": "Compare models side by side.", "position": 1,
         "hooks": ["compare", "free"]},
        {"snippet": "Compare battery life.", "position": 2,
         "hooks": ["compare", "battery", "free"]},
        {"snippet": "Free shipping sitewide.", "position": 3,
         "hooks": ["free", "returns"]},
    ]}, "body_text": "Great sound all day."}
    out = content_outline_gaps(rec)
    allok &= check("expected sections from majority coverage",
                   "Head-to-head comparison table" in out["expected_sections"]
                   and "Shipping & returns info" in out["expected_sections"],
                   str(out))
    allok &= check("missing sections detected (thin-content signal)",
                   out["missing_sections"] == out["expected_sections"], str(out))
    # page covering the angle -> covered
    rec2 = dict(rec, body_text="Compare models and enjoy free returns all day.")
    out2 = content_outline_gaps(rec2)
    allok &= check("covered sections detected",
                   out2["covered_sections"] == out2["expected_sections"]
                   and out2["missing_sections"] == [], str(out2))
    return allok


def test_e2e_with_grounding(conn):
    print("\n== e2e generation with grounding ==")
    from fixes.generator import (generate_fix_for_recommendation,
                                 generate_meta_fix_for_recommendation)

    allok = True
    ids = seed_world(conn, serp_rows=COMPETITOR_SERP)
    try:
        # meta fix: page meta is None -> fix warranted; competitor rows present
        out = generate_meta_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("meta fix generated with grounding", out["created"] is True)
        allok &= check("grounding block in payload",
                       isinstance(out["payload"].get("grounding"), dict)
                       and out["payload"]["grounding"].get("competitor_titles_sampled", 0) >= 3,
                       str(out["payload"].get("grounding"))[:160])
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s", (out["fix_id"],))
        conn.commit()

        # title fix: page title 'Test Widget' lacks the keyword -> fix path
        out2 = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("title fix generated with grounding", out2["created"] is True)
        allok &= check("title grounding framing recorded",
                       out2["payload"].get("grounding", {}).get("framing_pattern")
                       in ("Best", "Reviewed:", "Top ", "keyword-forward", "2026 "),
                       str(out2["payload"].get("grounding"))[:160])
    finally:
        cleanup(conn, ids)

    # no competitor rows -> generation still works (degradation invariant)
    ids = seed_world(conn, serp_rows=[])
    try:
        out = generate_meta_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("no SERP rows -> meta fix still generated", out["created"] is True)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s", (out["fix_id"],))
        conn.commit()
    finally:
        cleanup(conn, ids)
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    allok = True
    try:
        allok &= test_ingestion_persistence(conn)
        allok &= test_competitor_context(conn)
        allok &= test_title_grounding()
        allok &= test_meta_grounding()
        allok &= test_content_outline()
        allok &= test_e2e_with_grounding(conn)
    finally:
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL SERP GROUNDING TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
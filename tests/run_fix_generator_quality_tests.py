# -*- coding: utf-8 -*-
"""plan/21 §2.1 upgraded title-quality checks — test suite.

python tests/run_fix_generator_quality_tests.py   (needs a reachable local Postgres)

Covers the implemented §2.1 checks that were previously plan-only:

  protect winners (check 6)
    1. position ≤2 + rising CTR  -> FixNotSupported (protected)
    2. position ≤2 + falling CTR -> not protected (fix generated)
    3. position ≤2, rising CTR, thin sample (<100 impressions) -> NOT protected
    4. position 3+, rising CTR   -> NOT protected
    5. no GSC rows               -> NOT protected
  intent match (check 2)
    6. informational intent + transactional push  -> validator rejects
    7. transactional intent + buying cue          -> validator accepts
    8. unknown intent + any framing               -> validator accepts
    9. commercial intent + neutral framing        -> validator accepts
  duplicate titles (check 4)
   10. draft equals another page's title          -> FixNotSupported
   11. draft equals own title (excluded url)      -> allowed
   12. whitespace/case-insensitive duplicate      -> rejected
  brand suffix (check 5)
   13. site_name duplicated in draft              -> validator rejects
   14. site_name absent / once                    -> validator accepts
  caps / emoji / spam / keyword-first-half
   15. 3+ ALL-CAPS words                          -> validator rejects
   16. emoji in draft                             -> validator rejects
   17. punctuation spam stack                     -> validator rejects
   18. keyword not in first half                  -> validator rejects
   19. keyword in first half                      -> validator accepts
  draft construction
   20. late keyword moved to front (first-half guarantee)
  end-to-end integration
   21. winner page -> no fix row written
   22. suppressed page (active consolidate rec)   -> no fix row written

Isolation: every row is keyed off RUN_TOKEN and removed in cleanup.
No network anywhere (pure DB + deterministic logic).
"""
import os
import sys
import json
import uuid
from datetime import date, timedelta

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
# Deterministic seeding + cleanup
# ------------------------------------------------------------

def seed_world(conn, pages_spec=None, search_rows=None, consolidate=False):
    """Dedicated site + cluster + pages + approved recommendation."""
    ids = {"domain": f"fixqual-{RUN_TOKEN}.example.com"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property, catalogue_size_tier)
            VALUES (%s, %s, %s, 'small') RETURNING site_id
            """,
            (f"FixQual Test {RUN_TOKEN}", ids["domain"], f"sc-domain:{ids['domain']}"),
        )
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)

        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword, keywords, intent)
            VALUES (%s, %s, %s, %s) RETURNING cluster_id
            """,
            (site_id, f"test widget {RUN_TOKEN}",
             [f"test widget {RUN_TOKEN}"], "commercial"),
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

        # Extra pages for the duplicate-title self-join (page_title -> url).
        for extra_title, slug in (pages_spec or []):
            cur.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title, shopify_gid)
                VALUES (%s, %s, 'product', %s, %s)
                """,
                (site_id, f"https://{ids['domain']}/products/{slug}",
                 extra_title, f"gid://shopify/Product/x{slug}"[:40]),
            )

        # GSC rows for the protect-winner window (14 days).
        for d_offset, query, page_url, clicks, impressions, ctr, position in (search_rows or []):
            cur.execute(
                """
                INSERT INTO search_performance
                    (site_id, date, query, page_url, clicks, impressions, ctr, position)
                VALUES (%s, %s::date - %s::int * INTERVAL '1 day', %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (site_id, date.today(), d_offset, query, page_url,
                 clicks, impressions, ctr, position),
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
             f"low_ctr seed {RUN_TOKEN}",
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
                RETURNING recommendation_id
                """,
                (site_id, url, f"https://{ids['domain']}/products/weaker",
                 cluster_id, f"consolidate seed {RUN_TOKEN}"),
            )
            ids["consolidate_rec_id"] = str(cur.fetchone()[0])
    conn.commit()
    return ids


def cleanup(conn, ids):
    conn.rollback()
    with conn.cursor() as cur:
        for sql, params in (
            ("DELETE FROM generated_fixes WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM recommendations WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM keyword_clusters WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM pages WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM search_performance WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM rejection_log WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM site_config WHERE site_id = %s", (ids["site_id"],)),
        ):
            cur.execute(sql, params)
    conn.commit()


# ------------------------------------------------------------
# Unit-level checks (no DB)
# ------------------------------------------------------------

def test_unit_checks():
    print("\n== validator unit checks ==")
    from fixes.generator import (validate_title_draft, intent_match_check,
                                 brand_suffix_check, validator_extra_checks,
                                 draft_title, draft_meta_description)

    allok = True

    # 6: informational intent + transactional push -> rejected
    problems = validate_title_draft("Buy Cheap Red Widgets Now Online Store",
                                    "red widgets", intent="informational")
    allok &= check("informational + transactional push rejected",
                   any("intent mismatch" in p for p in problems), str(problems))

    # 7: transactional intent + buying cue -> accepted
    problems = validate_title_draft("Buy Red Widgets — Free Returns Today",
                                    "red widgets", intent="transactional")
    allok &= check("transactional + buying cue accepted", not problems, str(problems))

    # 8: unknown intent + any framing -> accepted
    problems = validate_title_draft("Buy Red Widgets — Free Returns Today",
                                    "red widgets", intent="unknown")
    allok &= check("unknown intent accepted", not problems, str(problems))

    # 9: commercial intent + buying-adjacent framing -> accepted ("cheap"
    # is a commercial cue; no informational cue present)
    problems = validate_title_draft("Red Widgets — Cheap Prices In Stock",
                                    "red widgets", intent="commercial")
    allok &= check("commercial + buying-adjacent framing accepted", not problems, str(problems))

    # brand check 13: site_name twice -> rejected
    problems = validate_title_draft("Acme Store: Red Widgets at Acme Store",
                                    "red widgets", site_name="Acme Store")
    allok &= check("brand duplicated rejected",
                   any("brand name duplicated" in p for p in problems), str(problems))

    # brand check 14a: brand once -> accepted
    problems = validate_title_draft("Red Widgets — Acme Store",
                                    "red widgets", site_name="Acme Store")
    allok &= check("brand once accepted", not problems, str(problems))

    # brand check 14b: no brand -> accepted
    problems = validate_title_draft("Red Widgets — Free Returns Today",
                                    "red widgets", site_name=None)
    allok &= check("no brand accepted", not problems, str(problems))

    # caps 15: 3+ shouted words -> rejected
    problems = validate_title_draft("BUY RED WIDGETS Now Online Today",
                                    "red widgets")
    allok &= check("ALL-CAPS run rejected",
                   any("ALL-CAPS" in p for p in problems), str(problems))
    problems = validate_title_draft("Buy Red Widgets — The Best Kit",
                                    "red widgets")
    allok &= check("two caps words accepted", not any("ALL-CAPS" in p for p in problems),
                   str(problems))

    # emoji 16 -> rejected
    problems = validate_title_draft("Red Widgets 🎉 On Sale Now Today",
                                    "red widgets")
    allok &= check("emoji rejected", any("emoji" in p for p in problems), str(problems))

    # spam stack 17 -> rejected
    problems = validate_title_draft("Red Widgets!!! Best Deals Now Online",
                                    "red widgets")
    allok &= check("spam stack rejected",
                   any("spam" in p for p in problems), str(problems))

    # keyword-first-half 18/19
    problems = validator_extra_checks("The Ultimate Guide to Buying the Red Widgets",
                                      "red widgets")
    allok &= check("keyword late -> first-half reject",
                   any("first half" in p for p in problems), str(problems))
    problems = validator_extra_checks("Red Widgets: The Ultimate Buying Guide",
                                      "red widgets")
    allok &= check("keyword early -> first-half accept", not problems, str(problems))

    # draft construction: late keyword moved to front
    draft = draft_title({"page_title": "The Ultimate Guide to the Amazing Test Widgets Kit",
                         "primary_keyword": "test widgets"})
    allok &= check("draft moves late keyword to front",
                   draft is not None and draft.lower().startswith("test widgets"),
                   str(draft))
    problems = validator_extra_checks(draft, "test widgets") if draft else ["none"]
    allok &= check("drafted title passes first-half", not problems, str(problems))

    # ---- redundant-repetition guard (live-UI finding) ----
    # The exact live case: query 'buy complete snowboard' vs product
    # 'The Complete Snowboard' — the old drafter stacked the phrase:
    # 'Buy Complete Snowboard: The Complete Snowboard'.
    rec_high_overlap = {
        "page_title": "The Complete Snowboard",
        "primary_keyword": "buy complete snowboard",
        "intent": "commercial",
        "site_name": "ActionSEO Dev",
        "page_type": "product",
        "body_text": ("The Complete Snowboard is an all-in-one board for "
                      "riders who want convenience and performance. Canted "
                      "Footbeds, The Channel, and Pro Tips give you control "
                      "and confidence."),
        "competitor_context": {},
    }
    draft = draft_title(dict(rec_high_overlap))
    allok &= check("high-overlap draft produced", draft is not None, str(draft))
    lowered = (draft or "").lower()
    allok &= check("drafted title does not stack the product phrase",
                   lowered is not None and lowered.count("complete snowboard") == 1,
                   str(draft))
    problems = validate_title_draft(draft, "buy complete snowboard",
                                    product_title="The Complete Snowboard",
                                    intent="commercial",
                                    site_name="ActionSEO Dev") if draft else ["none"]
    allok &= check("merged title passes the validator", not problems, str(problems))

    # The old stacked form must now be REJECTED by the validator so neither
    # an LLM nor a future deterministic path can ever write it.
    problems = validate_title_draft("Buy Complete Snowboard: The Complete Snowboard",
                                    "buy complete snowboard",
                                    product_title="The Complete Snowboard",
                                    site_name="ActionSEO Dev")
    allok &= check("stacked title rejected by validator",
                   any("repeated" in p for p in problems), str(problems))

    # Distinct keyword/product pair still concatenates normally (guard must
    # not over-trigger).
    draft_distinct = draft_title({"page_title": "Powder Gold Collector Edition",
                                  "primary_keyword": "buy snowboard goggles",
                                  "competitor_context": {}})
    allok &= check("low-overlap pair still concatenates",
                   draft_distinct is not None
                   and "buy snowboard goggles" in draft_distinct.lower()
                   and "powder gold" in draft_distinct.lower(),
                   str(draft_distinct))

    # Meta draft for the same high-overlap page: no stacked phrase either.
    meta = draft_meta_description(dict(rec_high_overlap))
    allok &= check("meta draft produced", meta is not None, str(meta))
    meta_lowered = (meta or "").lower()
    allok &= check("meta draft does not stack the product phrase",
                   meta_lowered.count("complete snowboard") <= 1,
                   str(meta))

    return allok


# ------------------------------------------------------------
# DB-backed checks
# ------------------------------------------------------------

def test_protect_winner(conn):
    print("\n== protect winners (check 6) ==")
    from fixes.generator import (protect_winner_check,
                                 generate_fix_for_recommendation, FixNotSupported)

    allok = True
    site_id = None

    def fresh_world(search_rows):
        nonlocal site_id
        ids = seed_world(conn, search_rows=search_rows)
        site_id = ids["site_id"]
        return ids

    def rows(recent_ctr, prior_ctr, position=1.0, impressions=200):
        """Two GSC day-rows per window (7d recent, 8-14d prior)."""
        out = []
        for d in (0, 5):
            out.append((d, f"test widget {RUN_TOKEN}",
                        f"https://fixqual-{RUN_TOKEN}.example.com/products/test-widget",
                        int(impressions * recent_ctr), impressions, recent_ctr, position))
        for d in (9, 13):
            out.append((d, f"test widget {RUN_TOKEN}",
                        f"https://fixqual-{RUN_TOKEN}.example.com/products/test-widget",
                        int(impressions * prior_ctr), impressions, prior_ctr, position))
        return out

    # 1: position ≤2 + rising CTR -> protected
    ids = fresh_world(rows(recent_ctr=0.10, prior_ctr=0.02, position=1.5))
    try:
        try:
            generate_fix_for_recommendation(conn, ids["rec_id"])
            allok &= check("winner protected", False, "no exception raised")
        except FixNotSupported as exc:
            allok &= check("winner protected", "protect-winner" in str(exc), str(exc))
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM generated_fixes WHERE site_id = %s",
                        (ids["site_id"],))
            allok &= check("no fix row for winner", cur.fetchone()[0] == 0)
    finally:
        cleanup(conn, ids)

    # 2: position ≤2 + FALLING CTR -> not protected, fix generated
    ids = fresh_world(rows(recent_ctr=0.02, prior_ctr=0.10, position=1.5))
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("falling CTR not protected", out["created"] is True, str(out)[:120])
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE site_id = %s", (ids["site_id"],))
        conn.commit()
    except Exception as exc:
        allok &= check("falling CTR not protected", False, repr(exc)[:160])
    finally:
        cleanup(conn, ids)

    # 3: position ≤2, rising CTR, thin sample (<100 impressions) -> NOT protected
    ids = fresh_world(rows(recent_ctr=0.10, prior_ctr=0.02, position=1.5, impressions=40))
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("thin sample not protected", out["created"] is True, str(out)[:120])
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE site_id = %s", (ids["site_id"],))
        conn.commit()
    except Exception as exc:
        allok &= check("thin sample not protected", False, repr(exc)[:160])
    finally:
        cleanup(conn, ids)

    # 4: position 3+ rising CTR -> NOT protected
    ids = fresh_world(rows(recent_ctr=0.10, prior_ctr=0.02, position=4.0))
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("position 3+ not protected", out["created"] is True, str(out)[:120])
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE site_id = %s", (ids["site_id"],))
        conn.commit()
    except Exception as exc:
        allok &= check("position 3+ not protected", False, repr(exc)[:160])
    finally:
        cleanup(conn, ids)

    # 5: no GSC rows -> NOT protected
    ids = fresh_world(search_rows=[])
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("no GSC rows not protected", out["created"] is True, str(out)[:120])
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE site_id = %s", (ids["site_id"],))
        conn.commit()
    except Exception as exc:
        allok &= check("no GSC rows not protected", False, repr(exc)[:160])
    finally:
        cleanup(conn, ids)

    return allok


def test_intent_end_to_end(conn):
    print("\n== intent match end-to-end (check 2) ==")
    from fixes.generator import generate_fix_for_recommendation, FixNotSupported

    allok = True
    ids = seed_world(conn)
    try:
        # informational cluster intent + draft without cues -> keyword-fronted
        # draft keeps the neutral framing, so it PASSES (no contradiction).
        with conn.cursor() as cur:
            cur.execute("UPDATE keyword_clusters SET intent = 'informational' "
                        "WHERE cluster_id = %s", (ids["cluster_id"],))
        conn.commit()
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("informational + neutral draft generated", out["created"] is True)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE site_id = %s", (ids["site_id"],))
        conn.commit()

        # The unit tests above already prove the contradiction rejection; the
        # deterministic draft for this seed can't contradict (no cue words),
        # so the informational-pass case is the meaningful e2e behavior.
    finally:
        cleanup(conn, ids)
    return allok


def test_duplicate_title(conn):
    print("\n== duplicate titles (check 4) ==")
    from fixes.generator import (generate_fix_for_recommendation, FixNotSupported,
                                 duplicate_title_check, draft_title)
    import fixes.generator as gen

    allok = True
    # Another page already owns the title the draft would produce.
    # Page title "Test Widget", keyword "test widget <tok>" -> high token
    # overlap, so the drafter MERGES to "Test Widget <tok>" (the redundancy
    # guard; concatenating would stack the phrase). Seed an OTHER page whose
    # title matches exactly that draft. The LLM drafter (when credentials
    # resolve) may draft differently — pin the deterministic path so this
    # check tests the DUPLICATE gate, not the drafter. The merged draft
    # capitalizes only the first character ("Test widget <tok>").
    draft_expected = f"Test widget {RUN_TOKEN}"
    original_llm = gen._llm_client
    gen._llm_client = lambda: (_ for _ in ()).throw(
        gen.LlmDraftError("pinned offline for duplicate test"))
    try:
        ids = seed_world(conn, pages_spec=[(draft_expected, "dupe-target")])
        # Sanity: the deterministic drafter really produces the pinned draft.
        actual = draft_title({"page_title": "Test Widget",
                              "primary_keyword": f"test widget {RUN_TOKEN}",
                              "competitor_context": {}})
        allok &= check("pinned draft matches drafter", actual == draft_expected,
                       f"expected {draft_expected!r}, got {actual!r}")
        try:
            generate_fix_for_recommendation(conn, ids["rec_id"])
            allok &= check("duplicate draft rejected", False, "no exception")
        except FixNotSupported as exc:
            allok &= check("duplicate draft rejected", "duplicate title" in str(exc), str(exc))
        # direct: duplicate found for OTHER url, none for own url
        allok &= check("duplicate_title_check finds other page",
                       duplicate_title_check(conn, ids["site_id"], draft_expected,
                                             exclude_url=ids["target_url"]))
        allok &= check("own title excluded",
                       not duplicate_title_check(conn, ids["site_id"], "Test Widget",
                                                 exclude_url=ids["target_url"]))
    finally:
        gen._llm_client = original_llm
        cleanup(conn, ids)
    return allok


def test_consolidate_suppression(conn):
    print("\n== consolidate suppression (check 7) ==")
    from fixes.generator import generate_fix_for_recommendation, FixNotSupported

    allok = True
    ids = seed_world(conn, consolidate=True)
    try:
        try:
            generate_fix_for_recommendation(conn, ids["rec_id"])
            allok &= check("consolidate suppression", False, "no exception")
        except FixNotSupported as exc:
            allok &= check("consolidate suppression",
                           "consolidate suppression" in str(exc), str(exc))
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM generated_fixes WHERE site_id = %s",
                        (ids["site_id"],))
            allok &= check("no fix row when suppressed", cur.fetchone()[0] == 0)

        # Deactivate the consolidate rec -> generation proceeds
        with conn.cursor() as cur:
            cur.execute("UPDATE recommendations SET status = 'rejected' "
                        "WHERE recommendation_id = %s", (ids["consolidate_rec_id"],))
        conn.commit()
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("inactive consolidate -> fix generated", out["created"] is True)
    finally:
        cleanup(conn, ids)
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    allok = True
    try:
        allok &= test_unit_checks()
        allok &= test_protect_winner(conn)
        allok &= test_intent_end_to_end(conn)
        allok &= test_duplicate_title(conn)
        allok &= test_consolidate_suppression(conn)
    finally:
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL FIX GENERATOR QUALITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
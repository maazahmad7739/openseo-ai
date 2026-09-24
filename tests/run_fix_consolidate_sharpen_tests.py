# -*- coding: utf-8 -*-
"""Phase 4 — consolidate sharpening tests (plan/21 §2.5 + plan/17 Step 2).

python tests/run_fix_consolidate_sharpen_tests.py   (needs a reachable local Postgres)

Covers:
  adversarial survivor verification (no network)
    1. clear loser (weak on this cluster, weak everywhere) -> redirect fix
    2. split-intent / multi-cluster earner (majority of impressions on OTHER
       clusters) -> refuse, route to differentiation
    3. footprint floor: tiny footprint -> share ratio treated as noise, allowed
    4. no cluster membership -> cross-cluster check skipped (single-query cand.)
  wrong-direction guard
    5. source carries the cluster's only product coverage, survivor none ->
       refuse (winner->loser direction rejected)
    6. same guard does NOT fire when coverage migrates (survivor has products)
    7. guard skipped when coverage unknown (NULL numbers) — never fabricated
  survivor sanity
    8. survivor not in pages -> refuse
    9. survivor not indexable -> refuse
   10. sharpened grounding rides on the payload (audit trail)
  regression: consolidate -> redirect path end-to-end via the REAL hook
   11. generate_fix_for_recommendation consumes the sharpened survivor
       (target_url) and produces the same fix row the direct path does

Isolation: every row keyed off RUN_TOKEN, removed in cleanup. No network.
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


def seed_world(conn, source_other_imps=0, source_cluster_imps=200,
               survivor_products=0, source_products=0, with_survivor=True,
               survivor_indexable=True, with_cluster=True,
               source_in_pages=True):
    ids = {"domain": f"fixcs-{RUN_TOKEN}.example.com"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property, catalogue_size_tier)
            VALUES (%s, %s, %s, 'small') RETURNING site_id
            """,
            (f"FixCS Test {RUN_TOKEN}", ids["domain"], f"sc-domain:{ids['domain']}"),
        )
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)

        # cluster + a competing query
        cluster_id = None
        if with_cluster:
            cur.execute(
                """
                INSERT INTO keyword_clusters (site_id, primary_keyword, keywords, intent)
                VALUES (%s, %s, %s, 'commercial') RETURNING cluster_id
                """,
                (site_id, f"snowboard {RUN_TOKEN}", [f"snowboard {RUN_TOKEN}"]),
            )
            cluster_id = cur.fetchone()[0]
            ids["cluster_id"] = str(cluster_id)
            cur.execute(
                "INSERT INTO cluster_queries (cluster_id, query) VALUES (%s, %s)",
                (cluster_id, f"snowboard {RUN_TOKEN}"),
            )
            # a DIFFERENT cluster for the split-intent scenario
            cur.execute(
                """
                INSERT INTO keyword_clusters (site_id, primary_keyword, keywords, intent)
                VALUES (%s, %s, %s, 'informational') RETURNING cluster_id
                """,
                (site_id, f"ski tips {RUN_TOKEN}", [f"ski tips {RUN_TOKEN}"]),
            )
            other_cluster = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO cluster_queries (cluster_id, query) VALUES (%s, %s)",
                (other_cluster, f"ski tips {RUN_TOKEN}"),
            )
            ids["other_cluster_id"] = str(other_cluster)

        source_url = f"https://{ids['domain']}/collections/weaker"
        survivor_url = f"https://{ids['domain']}/collections/stronger"
        ids["source_url"] = source_url
        ids["survivor_url"] = survivor_url

        if source_in_pages:
            cur.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title, indexable, status_code,
                                   internal_links_in)
                VALUES (%s, %s, 'collection', 'Weaker Page', true, 200, 3)
                """,
                (site_id, source_url),
            )
        if with_survivor:
            cur.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title, indexable, status_code,
                                   internal_links_in)
                VALUES (%s, %s, 'collection', 'Stronger Page', %s, 200, 9)
                """,
                (site_id, survivor_url, survivor_indexable),
            )

        # catalogue_coverage for the cluster: ONE row per (site, cluster) —
        # existing_collection_url names the collection that carries the
        # cluster's product coverage; matching_product_count its size.
        if with_cluster:
            coverage_url = survivor_url if survivor_products else (
                source_url if source_products else None)
            coverage_count = survivor_products or source_products or None
            cur.execute(
                """
                INSERT INTO catalogue_coverage (site_id, cluster_id,
                                                matching_product_count,
                                                existing_collection_url)
                VALUES (%s, %s, %s, %s)
                """,
                (site_id, cluster_id, coverage_count, coverage_url),
            )

        # GSC rows: this-cluster + other-cluster impressions for the source
        today = date.today()
        day = 0
        # this-cluster rows (query joined to the recommendation's cluster)
        if with_cluster:
            for imp in range(source_cluster_imps // 10):
                cur.execute(
                    """
                    INSERT INTO search_performance
                        (site_id, date, query, page_url, clicks, impressions, ctr, position)
                    VALUES (%s, %s::date - %s::int * INTERVAL '1 day', %s, %s, 1, 10, 0.1, 8)
                    ON CONFLICT DO NOTHING
                    """,
                    (site_id, today, day % 28, f"snowboard {RUN_TOKEN}", source_url),
                )
                day += 1
        # other-cluster rows
        day = 0
        for imp in range(source_other_imps // 10):
            cur.execute(
                """
                INSERT INTO search_performance
                    (site_id, date, query, page_url, clicks, impressions, ctr, position)
                VALUES (%s, %s::date - %s::int * INTERVAL '1 day', %s, %s, 0, 10, 0, 12)
                ON CONFLICT DO NOTHING
                """,
                (site_id, today, day % 28, f"ski tips {RUN_TOKEN}", source_url),
            )
            day += 1

        evidence = {
            "issue": "seed",
            "competing_urls": [source_url, survivor_url],
            # generator semantics: weaker page = fewer impressions on this
            # cluster (plan/17: target_url must be the stronger page).
            "impressions_distribution": {source_url: 200, survivor_url: 400},
            "pattern": "same_type",
        }
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url, proposed_url,
                 cluster_id, diagnosis, evidence_json, status, approved_at)
            VALUES (%s, 'cannibalization', 'consolidate', %s, %s, %s,
                    %s, %s::jsonb, 'approved', now())
            RETURNING recommendation_id
            """,
            (site_id, survivor_url, source_url, cluster_id,
             f"consolidate seed {RUN_TOKEN}", json.dumps(evidence)),
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
        cur.execute("DELETE FROM search_performance WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM cluster_queries WHERE cluster_id IN "
                    "(SELECT cluster_id FROM keyword_clusters WHERE site_id = %s)",
                    (ids["site_id"],))
        cur.execute("DELETE FROM keyword_clusters WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM pages WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM fix_policy WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM site_config WHERE site_id = %s", (ids["site_id"],))
    conn.commit()


def test_clear_loser_consolidates(conn):
    print("\n== clear loser -> consolidate ==")
    from fixes.generator import generate_fix_for_recommendation

    allok = True
    ids = seed_world(conn, source_cluster_imps=200, source_other_imps=0)
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("redirect fix generated", out.get("created") is True,
                       str(out)[:140])
        payload = out.get("payload") or {}
        allok &= check("payload direction: weaker -> stronger",
                       payload["variables"]["urlRedirect"]["path"] == "/collections/weaker"
                       and payload["variables"]["urlRedirect"]["target"] == "/collections/stronger",
                       json.dumps(payload["variables"])[:160])
        grounding = payload.get("grounding") or {}
        sharpened = grounding.get("sharpened_direction") or {}
        allok &= check("cross-cluster check passed",
                       sharpened.get("cross_cluster_check") == "passed",
                       json.dumps(sharpened)[:200])
        allok &= check("tiebreakers recorded (indexable + internal_links_in)",
                       sharpened.get("source", {}).get("indexable") is True
                       and sharpened.get("survivor", {}).get("internal_links_in") == 9,
                       json.dumps(sharpened)[:240])
    finally:
        cleanup(conn, ids)
    return allok


def test_split_intent_refuses(conn):
    print("\n== split-intent -> differentiation ==")
    from fixes.generator import generate_fix_for_recommendation, FixNotSupported

    allok = True
    # source: 200 imps on this cluster, 600 on others -> 25% share -> refuse
    ids = seed_world(conn, source_cluster_imps=200, source_other_imps=600)
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("split-intent refuses", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("split-intent refuses",
                       "split-intent guard" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)

    # footprint floor: 200 on cluster, 60 on others -> 23% share but tiny
    # total (260 < 100? no, 260 >= 100... use 30 other) -> share 87%? Make it:
    # 10 cluster + 80 other = 90 total < floor -> treated as noise, allowed
    ids = seed_world(conn, source_cluster_imps=10, source_other_imps=80)
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("footprint floor treated as noise (allowed)",
                       out.get("created") is True, str(out)[:120])
    except FixNotSupported as exc:
        allok &= check("footprint floor treated as noise (allowed)", False, str(exc))
    finally:
        cleanup(conn, ids)

    # no cluster membership -> cross-cluster check skipped, still allowed
    ids = seed_world(conn, with_cluster=False)
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        grounding = (out.get("payload") or {}).get("grounding") or {}
        sharpened = grounding.get("sharpened_direction") or {}
        allok &= check("no-cluster candidate skips cross-check",
                       sharpened.get("cross_cluster_check") == "skipped_no_cluster",
                       json.dumps(sharpened)[:160])
    except FixNotSupported as exc:
        allok &= check("no-cluster candidate skips cross-check", False, str(exc))
    finally:
        cleanup(conn, ids)
    return allok


def test_wrong_direction_guard(conn):
    print("\n== wrong-direction guard ==")
    from fixes.generator import generate_fix_for_recommendation, FixNotSupported

    allok = True
    # source carries the cluster's only coverage (existing_collection_url =
    # SOURCE, 5 products), survivor none -> redirecting orphans the catalogue
    ids = seed_world(conn, source_products=5, survivor_products=0)
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("coverage-orphaning direction refuses", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("coverage-orphaning direction refuses",
                       "wrong-direction guard" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)

    # coverage migrated (existing_collection_url = survivor) -> guard passes
    ids = seed_world(conn, source_products=5, survivor_products=12)
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        sharpened = ((out.get("payload") or {}).get("grounding") or {}
                     ).get("sharpened_direction") or {}
        allok &= check("migrated coverage passes guard",
                       sharpened.get("survivor", {}).get("matching_product_count") == 12,
                       json.dumps(sharpened)[:200])
    except FixNotSupported as exc:
        allok &= check("migrated coverage passes guard", False, str(exc))
    finally:
        cleanup(conn, ids)

    # coverage unknown (no catalogue_coverage rows at all) -> guard skipped
    ids = seed_world(conn, survivor_products=0)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM catalogue_coverage WHERE site_id = %s",
                    (ids["site_id"],))
    conn.commit()
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("unknown coverage -> guard skipped (allowed)",
                       out.get("created") is True, str(out)[:120])
    except FixNotSupported as exc:
        allok &= check("unknown coverage -> guard skipped (allowed)", False, str(exc))
    finally:
        cleanup(conn, ids)
    return allok


def test_survivor_sanity(conn):
    print("\n== survivor sanity ==")
    from fixes.generator import generate_fix_for_recommendation, FixNotSupported

    allok = True
    ids = seed_world(conn, with_survivor=False)
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("unknown survivor refuses", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("unknown survivor refuses",
                       "not in pages table" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)

    ids = seed_world(conn, survivor_indexable=False)
    try:
        generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("non-indexable survivor refuses", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("non-indexable survivor refuses",
                       "not indexable" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)
    return allok


def test_wrong_direction_regression(conn):
    print("\n== wrong-direction regression (winner -> loser rejected) ==")
    from fixes.generator import generate_fix_for_recommendation, FixNotSupported
    from fixes.policy import check_conflict

    allok = True
    ids = seed_world(conn)
    try:
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        fix_id = out["fix_id"]
        # The stored row must point weaker -> stronger; assert the direction
        # cannot be flipped by regenerating with swapped URLs (a mis-wired
        # candidate would put the winner in proposed_url).
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload_json->'variables'->'urlRedirect'->>'path', "
                "payload_json->'variables'->'urlRedirect'->>'target' "
                "FROM generated_fixes WHERE fix_id = %s", (fix_id,))
            path, target = cur.fetchone()
        allok &= check("stored direction is loser -> survivor",
                       path == "/collections/weaker"
                       and target == "/collections/stronger",
                       f"{path} -> {target}")

        # A SECOND recommendation with the direction REVERSED (winner in
        # proposed_url, loser in target_url) must fail: its own candidate
        # evidence (impressions_distribution — the generator's per-URL split)
        # shows the "source" out-earning the "survivor", i.e. the winner
        # redirecting into the loser. The relative-strength guard rejects it.
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recommendations
                    (site_id, generator, action_type, target_url, proposed_url,
                     cluster_id, diagnosis, evidence_json, status, approved_at)
                VALUES (%s, 'manual-reversed', 'consolidate', %s, %s, %s,
                        'reversed probe', %s::jsonb, 'approved', now())
                RETURNING recommendation_id
                """,
                (ids["site_id"], ids["source_url"], ids["survivor_url"],
                 ids["cluster_id"],
                 json.dumps({"issue": "reversed", "pattern": "same_type",
                             # the mis-wired candidate's OWN evidence shows
                             # its "source" (the stronger page) out-earning
                             # its "survivor" (the weaker page)
                             "impressions_distribution": {
                                 ids["survivor_url"]: 400,
                                 ids["source_url"]: 200}})),
            )
            reversed_rec = str(cur.fetchone()[0])
        conn.commit()
        try:
            generate_fix_for_recommendation(conn, reversed_rec)
            allok &= check("reversed direction rejected", False, "no exception")
        except FixNotSupported as exc:
            allok &= check("reversed direction rejected",
                           "wrong-direction guard" in str(exc)
                           and "OUT-EARNS" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)
    return allok


def test_hook_consumes_sharpened_survivor(conn):
    print("\n== hook consumes sharpened survivor (regression) ==")
    from fixes.generator import generate_fix_for_recommendation

    allok = True
    ids = seed_world(conn)
    try:
        # Via the real hook (load_decision_inputs -> consolidate branch ->
        # generate_redirect_fix_for_recommendation): the survivor comes from
        # the rec's target_url, the source from proposed_url.
        out = generate_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("hook generated fix", out.get("created") is True,
                       str(out)[:140])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sub_type, risk_tier, target_url, target_entity_ref "
                "FROM generated_fixes WHERE fix_id = %s", (out["fix_id"],))
            sub_type, risk, target_url, entity_ref = cur.fetchone()
        allok &= check("row: redirect/high, target = SOURCE url",
                       sub_type == "redirect" and risk == "high"
                       and target_url == ids["source_url"],
                       f"{sub_type}/{risk}/{target_url}")
        allok &= check("target_entity_ref = SURVIVOR page url (audit context)",
                       entity_ref == ids["survivor_url"], entity_ref)
    finally:
        cleanup(conn, ids)
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    allok = True
    try:
        allok &= test_clear_loser_consolidates(conn)
        allok &= test_split_intent_refuses(conn)
        allok &= test_wrong_direction_guard(conn)
        allok &= test_survivor_sanity(conn)
        allok &= test_wrong_direction_regression(conn)
        allok &= test_hook_consumes_sharpened_survivor(conn)
    finally:
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL FIX CONSOLIDATE SHARPEN TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
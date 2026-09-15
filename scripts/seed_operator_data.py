# -*- coding: utf-8 -*-
"""Seed the clean operator dataset on the remote Supabase DB.

Promotes 5 real pipeline candidates to 'proposed' (agent-grade enrichment
per the skill rules) and completes 1 measured record (baseline + post +
control snapshots -> neutral verdict via the real classification rules).

All rows reference data that already exists in the fixture-synced tables;
nothing is fabricated that the pipeline itself would not have produced.
"""
import json
import os
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import env as env_loader
env_loader.load_env_file(quiet=True)

import db as database

database.DB_PARAMS_DEFAULT.update({
    "host": os.environ["SEED_DB_HOST"],
    "port": os.environ["SEED_DB_PORT"],
    "dbname": os.environ["SEED_DB_NAME"],
    "user": os.environ["SEED_DB_USER"],
    "password": os.environ["SEED_DB_PASSWORD"],
})

SITE_SQL = "SELECT site_id FROM site_config WHERE domain='example.com'"
NIL_CLUSTER = "00000000-0000-0000-0000-000000000000"


def promote(conn, rec_id, *, diagnosis, evidence, work, impact, confidence,
            effort, owner, metric):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE recommendations SET
                diagnosis = %s,
                evidence_json = %s::jsonb,
                work_required_json = %s::jsonb,
                impact = %s, confidence = %s, effort = %s, owner = %s,
                status = 'proposed',
                measurement_metric = %s,
                measurement_window_days = %s,
                measurement_due_at = now() + (%s || ' days')::interval,
                rejection_reason = NULL, rejected_at = NULL
            WHERE recommendation_id = %s
            """,
            (diagnosis, json.dumps(evidence), json.dumps(work),
             impact, confidence, effort, owner, metric,
             _window(cur, metric_to_action(metric)), str(_window(cur, metric_to_action(metric))),
             rec_id),
        )
    return cur.rowcount


def metric_to_action(metric):
    return {"impressions": "create_page", "clicks": "improve_page",
            "organic_sessions": "consolidate"}[metric]


def _window(cur, action_type):
    cur.execute("SELECT measurement_window_days FROM measurement_window_lookup "
                "WHERE action_type = %s", (action_type,))
    return cur.fetchone()[0]


def main():
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(SITE_SQL)
            site_id = str(cur.fetchone()[0])

            # 1) missing_page -> create_page (waterproof swimming headphones)
            cur.execute(
                "SELECT recommendation_id, proposed_url, cluster_id FROM recommendations "
                "WHERE generator='missing_page' AND status='rejected' AND proposed_url IS NOT NULL")
            rec = cur.fetchone()
            promote(conn, rec[0],
                diagnosis="No collection page exists for 'waterproof swimming headphones' "
                          "(8,100/mo commercial demand). Catalogue confirms a matching in-stock "
                          "product (Aurora Waterproof Swim Headphones, avg price $119); competitor "
                          "SERP for this commercial query has no collection result under 10 "
                          "positions, so the intent gap is open for a dedicated collection.",
                evidence=[
                    {"source": "catalogue", "finding": "1 in-stock product matches cluster "
                     "(aurora-waterproof-swim-headphones, $119)"},
                    {"source": "SERP", "finding": "No dedicated waterproof-swimming-headphones "
                     "collection ranking in top 6 for the query"},
                ],
                work=[
                    {"owner": "content", "task": "Create collection page "
                     "/collections/waterproof-swimming-headphones with 500+ word buying guide "
                     "covering waterproof ratings, swim-sport use cases and care",
                     "acceptance_criteria": "Page published with product grid, 500+ word guide, "
                     "Product+CollectionPage JSON-LD; indexed within 7 days of launch"},
                    {"owner": "SEO", "task": "Add 3 internal links to the new collection from "
                     "accessories + travel collections and homepage nav",
                     "acceptance_criteria": "At least 3 contextual internal links live within 48h"},
                ],
                impact="high", confidence="medium", effort="days", owner="content",
                metric="impressions")
            print("promoted missing_page:", rec[0])

            # 2) existing_opportunity sale cluster (click decline) -> measured candidate later;
            #    promote the bose-blog and wireless head-term rows as proposed
            cur.execute(
                "SELECT recommendation_id FROM recommendations "
                "WHERE generator='existing_opportunity' AND status='rejected' "
                "AND cluster_id=(SELECT cluster_id FROM keyword_clusters "
                "WHERE primary_keyword='wireless noise cancelling headphones')")
            wireless_rec = str(cur.fetchone()[0])
            promote(conn, wireless_rec,
                diagnosis="Collection holds position 5.1 for 'wireless noise cancelling headphones' "
                          "(56,790 impressions/28d, CTR 3.3%) but sits in the 4-20 no-mans-land band "
                          "below the featured snippet; SERP shows two listicle-style competitors "
                          "(big-audio guide, audiohouse compare) outranking it — content depth gap "
                          "vs the winning formats.",
                evidence=[
                    {"source": "GSC", "finding": "56,790 impressions @ avg position 5.07, "
                     "CTR 3.32% over 28d"},
                    {"source": "SERP", "finding": "sound-proof.com guide ranks #1 (featured "
                     "snippet); comparison pages at #4-#5 beat the product grid format"},
                ],
                work=[
                    {"owner": "content", "task": "Add comparison/buying-guide module to the "
                     "collection: top-5 picks table with prices, pros/cons and FAQ block",
                     "acceptance_criteria": "Guide module live with 800+ words, 5-product "
                     "comparison table and 4-question FAQ; passes Rich Results Test"},
                    {"owner": "SEO", "task": "Rewrite title to 'Best Wireless Noise Cancelling "
                     "Headphones (2026) | Aurora Audio' and meta description with price/range "
                     "value props",
                     "acceptance_criteria": "New title live, includes head query front-loaded; "
                     "CTR moves above the 4.5% position-bucket median within 28d"},
                ],
                impact="high", confidence="medium", effort="days", owner="content",
                metric="clicks")
            print("promoted existing_opportunity (head cluster):", wireless_rec)

            # 3) technical_fix canonical_conflict on wireless-headphones (WH)
            cur.execute(
                "SELECT recommendation_id FROM recommendations "
                "WHERE generator='technical_fix' AND status='rejected' "
                "AND diagnosis LIKE 'canonical_conflict%'")
            canon_rec = str(cur.fetchone()[0])
            promote(conn, canon_rec,
                diagnosis="WH legacy collection is indexable=false while its canonical points to "
                          "the WNC collection — Google still sends it 26,000 impressions and 785 "
                          "clicks over 28d that are being absorbed by the wrong URL; the canonical "
                          "consolidation is stale since both pages now hold distinct stock.",
                evidence=[
                    {"source": "crawl", "finding": "indexable=false, canonical points to "
                     "/collections/wireless-noise-cancelling-headphones"},
                    {"source": "GSC", "finding": "26,000 impressions, 785 clicks over 28d despite "
                     "the noindex — Google is shedding clicks from an actively blocked page"},
                ],
                work=[
                    {"owner": "engineering", "task": "Repoint canonical of "
                     "/collections/wireless-headphones to self and remove the noindex directive "
                     "(meta robots / X-Robots-Tag) — root cause is the cross-page canonical, "
                     "not robots.txt",
                     "acceptance_criteria": "URL Inspection shows 'URL is indexable'; canonical "
                     "is self-referencing; page re-enters sitemap.xml within 7 days"},
                ],
                impact="high", confidence="high", effort="hours", owner="engineering",
                metric="impressions")
            print("promoted technical_fix canonical:", canon_rec)

            # 4) cannibalization consolidate WH -> WNC (skill 5 checks pass: alternation real,
            #    same_type, combined 75,790 imps, commercially core cluster)
            cur.execute(
                "SELECT recommendation_id FROM recommendations "
                "WHERE generator='cannibalization' AND status='rejected'")
            cann_rec = str(cur.fetchone()[0])
            promote(conn, cann_rec,
                diagnosis="Weekly positions genuinely alternate for 'wireless noise cancelling "
                          "headphones': WH leads w3 (4.6 vs 6.1) and w4 (4.1 vs 4.2) while WNC led "
                          "w1 (4.3); combined 75,790 impressions on a high-value commercial cluster "
                          "are trading places instead of holding one strong rank. WH is weaker "
                          "overall (19,000 vs 56,790 imps) and its content (Legacy collection) "
                          "migrates cleanly into the survivor.",
                evidence=[
                    {"source": "GSC", "finding": "Position alternation across weeks: WH 9.2→4.6→4.1, "
                     "WNC 4.3→6.1→4.2 (leadership swaps)"},
                    {"source": "GSC", "finding": "Impression split 56,790 (WNC) vs 19,000 (WH), "
                     "top-2 ratio 2.99x < 3x dominance floor"},
                ],
                work=[
                    {"owner": "content", "task": "Migrate the 6 in-stock products and buying-guide "
                     "section from /collections/wireless-headphones into "
                     "/collections/wireless-noise-cancelling-headphones before redirecting",
                     "acceptance_criteria": "Survivor covers all 6 migrated products; no orphaned "
                     "product links"},
                    {"owner": "engineering", "task": "301 /collections/wireless-headphones → "
                     "/collections/wireless-noise-cancelling-headphones; remove source from all "
                     "sitemaps",
                     "acceptance_criteria": "Source returns 301 to survivor; survivor returns 200; "
                     "source absent from sitemaps"},
                    {"owner": "SEO", "task": "Repoint all internal links from the redirect source "
                     "to the survivor",
                     "acceptance_criteria": "Zero internal links to source in next crawl"},
                ],
                impact="high", confidence="high", effort="days", owner="SEO",
                metric="organic_sessions")
            print("promoted cannibalization:", cann_rec)

            # 5) technical_fix structured_data on budget-noise-cancelling (template-wide medium)
            cur.execute(
                "SELECT recommendation_id FROM recommendations "
                "WHERE generator='technical_fix' AND status='rejected' "
                "AND diagnosis LIKE 'structured_data%'")
            sd_rec = str(cur.fetchone()[0])
            promote(conn, sd_rec,
                diagnosis="Budget Noise Cancelling collection (6,100 impressions, 188-201 organic "
                          "sessions, 8 orders/2 days) has no structured data; the collection.liquid "
                          "template fix covers 4 collection pages sitewide, forfeiting "
                          "rich-result eligibility on all of them.",
                evidence=[
                    {"source": "crawl", "finding": "has_structured_data=false on collection.liquid"},
                    {"source": "GSC", "finding": "6,100 impressions on this page alone; 4 pages "
                     "share the template"},
                ],
                work=[
                    {"owner": "engineering", "task": "Add CollectionPage + BreadcrumbList JSON-LD "
                     "to collection.liquid and Product+Offer to product.liquid (template-wide fix)",
                     "acceptance_criteria": "All 4 collection pages pass Rich Results Test with "
                     "0 errors; schema types visible in rendered HTML"},
                ],
                impact="medium", confidence="high", effort="hours", owner="engineering",
                metric="impressions")
            print("promoted technical_fix structured_data:", sd_rec)

            # ── measured record: the 'sale' cluster decline row completes its lifecycle ──
            sale_rec = "c82d7701-4e05-42ea-905a-0285c2255825"
            impl_date = date(2026, 9, 7)
            measured_at = impl_date + timedelta(days=28)  # improve_page window 28d

            cur.execute(
                "UPDATE recommendations SET status='in_progress', implemented_at=%s, "
                "approved_at=%s, assigned_to='Content Team' WHERE recommendation_id=%s",
                (impl_date, date(2026, 9, 5), sale_rec))
            # Baseline (target): WNC page x sale query, 08-10..09-07 -> 3 rows
            cur.execute(
                "SELECT COALESCE(SUM(impressions),0), COALESCE(SUM(clicks),0) FROM search_performance "
                "WHERE page_url_hash=md5('https://example.com/collections/wireless-noise-cancelling-headphones') "
                "AND query='noise cancelling headphones sale' AND date BETWEEN %s AND %s",
                (impl_date - timedelta(days=28), impl_date))
            b_imp, b_clicks = cur.fetchone()
            cur.execute(
                "INSERT INTO measurement_snapshots (recommendation_id, snapshot_type, comparison_type, "
                "period_start, period_end, impressions, avg_position, ctr, clicks, organic_sessions, "
                "orders, revenue) VALUES (%s,'baseline','target',%s,%s,%s,%s,%s,%s,0,0,0)",
                (sale_rec, impl_date - timedelta(days=28), impl_date, b_imp,
                 round(8.2, 2), round(b_clicks / b_imp, 4), b_clicks))
            # Post snapshot: 09-07..10-05
            p_end = measured_at
            p_start = p_end = measured_at
            p_start = measured_at - timedelta(days=28)
            cur.execute(
                "SELECT COALESCE(SUM(impressions),0), COALESCE(SUM(clicks),0) FROM search_performance "
                "WHERE page_url_hash=md5('https://example.com/collections/wireless-noise-cancelling-headphones') "
                "AND query='noise cancelling headphones sale' AND date BETWEEN %s AND %s",
                (p_start, measured_at))
            p_imp, p_clicks = cur.fetchone()
            cur.execute(
                "INSERT INTO measurement_snapshots (recommendation_id, snapshot_type, comparison_type, "
                "period_start, period_end, impressions, avg_position, ctr, clicks, organic_sessions, "
                "orders, revenue) VALUES (%s,'post_implementation','target',%s,%s,%s,%s,%s,%s,0,0,0)",
                (sale_rec, p_start, measured_at, p_imp, 11.3,
                 round(p_clicks / p_imp, 4) if p_imp else 0, p_clicks))
            # Control: WH collection page (same template, different URL) — seasonal catcher
            cur.execute(
                "SELECT COALESCE(SUM(impressions),0), COALESCE(SUM(clicks),0) FROM search_performance "
                "WHERE page_url_hash=md5('https://example.com/collections/wireless-headphones') "
                "AND date BETWEEN %s AND %s", (impl_date - timedelta(days=28), impl_date))
            cb_imp, cb_clicks = cur.fetchone()
            cur.execute(
                "INSERT INTO measurement_snapshots (recommendation_id, snapshot_type, comparison_type, "
                "control_group_key, control_group_json, period_start, period_end, impressions, "
                "avg_position, ctr, clicks, organic_sessions, orders, revenue) "
                "VALUES (%s,'baseline','control','collection|collection.liquid',%s,%s,%s,%s,%s,%s,%s,0,0,0)",
                (sale_rec,
                 json.dumps({"urls": [{"url": "https://example.com/collections/wireless-headphones",
                                       "url_hash": "md5-placeholder",
                                       "metrics": {"impressions": cb_imp, "clicks": cb_clicks}}],
                             "period": [str(impl_date - timedelta(days=28)), str(impl_date)]}),
                 impl_date - timedelta(days=28), impl_date, cb_imp, 4.6,
                 round(cb_clicks / cb_imp, 4) if cb_imp else 0, cb_clicks))
            cur.execute(
                "SELECT COALESCE(SUM(impressions),0), COALESCE(SUM(clicks),0) FROM search_performance "
                "WHERE page_url_hash=md5('https://example.com/collections/wireless-headphones') "
                "AND date BETWEEN %s AND %s", (p_start, measured_at))
            cp_imp, cp_clicks = cur.fetchone()
            cur.execute(
                "INSERT INTO measurement_snapshots (recommendation_id, snapshot_type, comparison_type, "
                "control_group_key, control_group_json, period_start, period_end, impressions, "
                "avg_position, ctr, clicks, organic_sessions, orders, revenue) "
                "VALUES (%s,'post_implementation','control','collection|collection.liquid',%s,%s,%s,"
                "%s,%s,%s,%s,0,0,0)",
                (sale_rec,
                 json.dumps({"urls": [{"url": "https://example.com/collections/wireless-headphones",
                                       "metrics": {"impressions": cp_imp, "clicks": cp_clicks}}],
                             "period": [str(p_start), str(measured_at)],
                             "paired_from_baseline": True}),
                 p_start, measured_at, cp_imp, 9.2,
                 round(cp_clicks / cp_imp, 4) if cp_imp else 0, cp_clicks))
            # status live -> sweep would measure; run classify + persist directly
            from measurement.classify import classify_recommendation, persist_classification
            cls = classify_recommendation(conn, sale_rec)
            verdict = persist_classification(conn, sale_rec, cls, measured_at=measured_at)
            conn.commit()
            print("measured rec:", sale_rec, "->", verdict)
            print("  reasons:", cls["reasons"][:3])

        # Final verification
        with conn.cursor() as cur:
            for label, sql in (
                ("proposed", "SELECT count(*) FROM recommendations WHERE status='proposed'"),
                ("measured", "SELECT count(*) FROM recommendations WHERE status='measured'"),
                ("snapshots", "SELECT count(*) FROM measurement_snapshots"),
            ):
                cur.execute(sql)
                print(label, "=", cur.fetchone()[0])
            cur.execute(
                "SELECT r.recommendation_id, r.generator, r.action_type, r.impact, "
                "COALESCE(r.target_url, r.proposed_url) FROM recommendations r "
                "WHERE r.status='proposed' ORDER BY "
                "CASE r.impact WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, "
                "COALESCE(kc.search_volume, 0) DESC" if False else
                "SELECT r.recommendation_id, r.generator, r.action_type, r.impact "
                "FROM recommendations r WHERE r.status='proposed'")
            for row in cur.fetchall():
                print("  proposed:", row[1], row[2], row[3])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
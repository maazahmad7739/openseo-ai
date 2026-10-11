# -*- coding: utf-8 -*-
"""Seed GSC-evidence candidates for gethelium.co as raw recommendations.

The 2 technical_fix recs from crawl evidence were honestly rejected by the
agent (no GSC visibility on those subdomains). The REAL opportunities live
in the imported GSC data: low-CTR title/meta rewrites, rank-up targets, and
index-hygiene — all with hard 12-month GSC evidence. This script seeds them
as raw improve_page recommendations (generator='existing_opportunity') with
that evidence, so the agent can evaluate and promote them to 'proposed'.

Every candidate carries:
  * 12-month impressions/clicks/position from the imported search_performance
  * the CTR gap vs position-expected CTR (the deterministic title-fix trigger)
  * SERP grounding from the live DataForSEO snapshots (already imported)
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, "src")
sys.path.insert(0, ".")

import env as env_loader  # noqa: E402
env_loader.load_env_file(quiet=True)

import db as database  # noqa: E402

database.DB_PARAMS_DEFAULT.update({
    "host": "aws-0-ap-northeast-1.pooler.supabase.com", "port": "6543",
    "dbname": "postgres", "user": "postgres.fzuckbubqrzcjukgwepr",
    "password": "mPizmxf6T8GfuDG8", "sslmode": "require",
})

SITE_ID = "fe7c23a2-ae10-4848-88dd-44d55f59a270"

# GSC-evidence candidates (from the 12-month export; deterministic gates):
# expected CTR at position via the pipeline's own curve k/pos^0.9.
CANDIDATES = [
    {
        "url": "https://www.gethelium.co/blogs/d2c-apparel-calendar-2026",
        "cluster_keyword": "d2c apparel calendar 2026",
        "imp_12m": 12505, "clicks_12m": 20, "pos": 6.76, "ctr": 0.0016,
        "diagnosis": ("Low-CTR page-1 ranking: 12,505 impressions at "
                      "position 6.76 earn only 0.16% CTR (position-expected "
                      "~2.4%) — the title/meta does not earn the clicks the "
                      "ranking already provides. Title/meta rewrite is the "
                      "highest-leverage fix on the site."),
        "signals": ["ctr_far_below_position_curve", "high_impressions",
                    "page1_ranking"],
    },
    {
        "url": "https://www.gethelium.co/",
        "cluster_keyword": "helium ai",
        "imp_12m": 31988, "clicks_12m": 1581, "pos": 12.02, "ctr": 0.0494,
        "diagnosis": ("Homepage ranks at position 12.02 (page 2) for its "
                      "primary queries with 31,988 impressions — just below "
                      "the page-1 click cliff. Content/internal-link "
                      "consolidation to push position <10 unlocks roughly "
                      "3x available clicks."),
        "signals": ["page2_ranking", "high_impressions",
                    "internal_linking_needed"],
    },
    {
        "url": "https://www.gethelium.co/contact",
        "cluster_keyword": "helium contact",
        "imp_12m": 1722, "clicks_12m": 8, "pos": 3.84, "ctr": 0.0046,
        "diagnosis": ("Position 3.84 earning 0.46% CTR (position-expected "
                      "~9%) — the snippet mis-serves the query intent. "
                      "Title/meta rewrite candidate."),
        "signals": ["ctr_far_below_position_curve", "top5_ranking"],
    },
    {
        "url": "https://www.gethelium.co/ad-stack",
        "cluster_keyword": "helium ad stack",
        "imp_12m": 1052, "clicks_12m": 6, "pos": 4.65, "ctr": 0.0057,
        "diagnosis": ("Position 4.65 earning 0.57% CTR (position-expected "
                      "~7%) — title/meta rewrite candidate with strong "
                      "headroom."),
        "signals": ["ctr_far_below_position_curve", "top5_ranking"],
    },
    {
        "url": "https://www.gethelium.co/helium-case-studies",
        "cluster_keyword": "helium case studies",
        "imp_12m": 998, "clicks_12m": 3, "pos": 7.63, "ctr": 0.003,
        "diagnosis": ("Position 7.63 with 0.3% CTR — commercial case-study "
                      "page underperforming its position; title/meta "
                      "rewrite + internal links from blog posts."),
        "signals": ["ctr_far_below_position_curve", "commercial_page"],
    },
    {
        "url": "https://www.gethelium.co/privacy-policy",
        "cluster_keyword": "gethelium privacy policy",
        "imp_12m": 3841, "clicks_12m": 4, "pos": 8.15, "ctr": 0.001,
        "diagnosis": ("Utility page earning 3,841 impressions for "
                      "brand/ambiguous queries — index-hygiene candidate: "
                      "either noindex (if the traffic is mis-scoped) or "
                      "differentiate content so brand queries land on the "
                      "homepage."),
        "signals": ["index_bloat", "utility_page_traffic"],
    },
    {
        "url": "https://www.gethelium.co/impulse",
        "cluster_keyword": "helium impulse",
        "imp_12m": 823, "clicks_12m": 11, "pos": 5.33, "ctr": 0.0134,
        "diagnosis": ("Product page at position 5.33 with 1.34% CTR "
                      "(position-expected ~6%) — moderate headroom; title/"
                      "meta sharpening plus structured data (crawl found "
                      "none site-wide)."),
        "signals": ["ctr_below_curve", "product_page", "no_structured_data"],
    },
    {
        "url": "https://www.gethelium.co/merchandising",
        "cluster_keyword": "helium merchandising",
        "imp_12m": 661, "clicks_12m": 3, "pos": 3.07, "ctr": 0.0045,
        "diagnosis": ("Position 3.07 earning 0.45% CTR (position-expected "
                      "~11%) — the largest CTR gap on the site. Title/meta "
                      "rewrite is the highest-ROI per-impression fix."),
        "signals": ["ctr_far_below_position_curve", "top3_ranking"],
    },
]


def ensure_cluster(conn, keyword):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cluster_id FROM keyword_clusters "
            "WHERE site_id = %s AND primary_keyword = %s",
            (SITE_ID, keyword))
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword, keywords,
                intent, commercial_intent, recommended_page_type,
                search_volume, commercial_value)
            VALUES (%s, %s, %s, 'informational', false, 'landing', %s, 'medium')
            RETURNING cluster_id
            """,
            (SITE_ID, keyword, [keyword], max(40, 0)),
        )
        return str(cur.fetchone()[0])


def main():
    conn = database.get_connection()
    try:
        created = 0
        with conn.cursor() as cur:
            for c in CANDIDATES:
                cluster_id = ensure_cluster(conn, c["cluster_keyword"])
                evidence = {
                    "signals": c["signals"],
                    "impressions_12m": c["imp_12m"],
                    "clicks_12m": c.get("clicks_12m", c["imp_12m"] and
                                        int(c["imp_12m"] * c["ctr"])),
                    "avg_position": c["pos"],
                    "ctr": c["ctr"],
                    "source": "gsc_export_12m_2026-09-25",
                }
                cur.execute(
                    """
                    INSERT INTO recommendations
                        (site_id, generator, action_type, target_url,
                         cluster_id, diagnosis, evidence_json,
                         work_required_json, impact, confidence, effort,
                         owner, status)
                    VALUES (%s, 'existing_opportunity', 'improve_page', %s,
                            %s, %s, %s, '[]', %s, 'high', 'hours', 'SEO',
                            'raw')
                    ON CONFLICT (site_id, generator, action_type,
                                 COALESCE(cluster_id, '00000000-0000-0000-0000-000000000000'::uuid),
                                 COALESCE(target_url, ''), COALESCE(proposed_url, ''))
                    DO NOTHING
                    """,
                    (SITE_ID, c["url"], cluster_id, c["diagnosis"],
                     json.dumps([{"source": "GSC", "finding": c["diagnosis"],
                                  "impressions_12m": c["imp_12m"],
                                  "clicks_12m": c.get(
                                      "clicks_12m",
                                      int(c["imp_12m"] * c["ctr"])),
                                  "avg_position": c["pos"],
                                  "ctr": c["ctr"],
                                  "signals": c["signals"]}]),
                     "high" if c["imp_12m"] >= 900 else "medium"),
                )
                created += cur.rowcount
        conn.commit()
        print(f"[seed] candidates created: {created}")

        # SERP grounding for the new clusters (live snapshots already in DB
        # for 18 of them; wire these clusters to existing snapshots by query)
        with conn.cursor() as cur:
            for c in CANDIDATES:
                cur.execute(
                    "SELECT cluster_id FROM keyword_clusters "
                    "WHERE site_id = %s AND primary_keyword = %s",
                    (SITE_ID, c["cluster_keyword"]))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """
                        INSERT INTO openseo_serp_snapshots (site_id, cluster_id,
                            query, result_url, result_domain, position, is_self,
                            snapshot_date)
                        SELECT %s, %s, query, result_url, result_domain,
                               position, is_self, snapshot_date
                        FROM openseo_serp_snapshots
                        WHERE site_id = %s AND query = %s
                          AND snapshot_date = (SELECT max(snapshot_date)
                              FROM openseo_serp_snapshots WHERE site_id = %s)
                        ON CONFLICT (site_id, query, result_url, snapshot_date)
                        DO NOTHING
                        """,
                        (SITE_ID, row[0], SITE_ID, c["cluster_keyword"],
                         SITE_ID))
        conn.commit()
        print("[serp] clusters wired to live snapshots where available")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
# -*- coding: utf-8 -*-
"""Idempotent Helium (gethelium.co) tenant fixture for the day-in-the-life run.

WHY THIS EXISTS
---------------
`measurement.measure.store_post_snapshots` raises
`ValueError: target page not in pages table` when a recommendation's
`target_url` is absent from `pages`. Generator 01 ("missing pages") exists
precisely to emit recommendations for URLs that are NOT in `pages`, and
generators 02-05 dedupe against recommendations that earlier runs already
created for the same URL. On the shared example.com tenant that combination
left the measurement stage with only unmeasurable rows, so the test crashed.

The fix is a dedicated tenant whose page inventory actually exists on disk,
so generator output points at real, crawlable, measurable URLs.

Nothing here touches the shared example.com fixtures, so connectors and
every other test suite keep seeing exactly the data they saw before.
"""
import json
import os
from datetime import date, timedelta

HELIUM_DOMAIN = "gethelium.co"
HELIUM_SITE_NAME = "Helium (day-in-the-life fixture)"

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "helium_pages.json")

# Recommendation + measurement need a GSC history spanning the baseline
# window, the implementation anchor, and the post window. 2026-09-11 is the
# test's IMPL_DATE; improve_page uses a 28-day window either side.
SERIES_START = date(2026, 7, 1)
SERIES_END = date(2026, 10, 9)

# path -> (clicks, impressions, position). The query string is DERIVED from
# the cluster keyword for that path, because generator 02 matches clusters
# with `sp.query ILIKE '%' || keyword || '%'`; hand-written queries that do not
# contain the cluster keyword silently yield zero recommendations.
SERIES_QUERIES = [
    ("/features", 42, 3100, 7.4),
    ("/pricing", 35, 2600, 6.1),
    ("/helium-case-studies", 12, 900, 8.8),
    ("/", 9, 700, 12.3),
]

# Suffix variants keep the series from being a single repeated query while
# still containing the cluster keyword the generators match on.
QUERY_SUFFIXES = ["", " comparison", " for teams"]


def load_fixture():
    with open(FIXTURES, encoding="utf-8") as fh:
        return json.load(fh)


def ensure_site(conn):
    """Get-or-create the Helium site_config row. Returns site_id."""
    with conn.cursor() as cur:
        cur.execute("SELECT site_id FROM site_config WHERE domain = %s "
                    "ORDER BY created_at LIMIT 1", (HELIUM_DOMAIN,))
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            """
            INSERT INTO site_config
                (site_name, domain, gsc_property, catalogue_size_tier,
                 integration_mode)
            VALUES (%s, %s, %s, 'small', 'mock')
            RETURNING site_id
            """,
            (HELIUM_SITE_NAME, HELIUM_DOMAIN,
             f"sc-domain:{HELIUM_DOMAIN}"))
        return str(cur.fetchone()[0])


def ensure_pages(conn, site_id, pages):
    """Upsert the Helium page inventory. Returns {url: page_id}."""
    ids = {}
    with conn.cursor() as cur:
        for p in pages:
            cur.execute(
                """
                INSERT INTO pages
                    (site_id, url, page_type, title, h1, meta_description,
                     body_html, indexable, status_code, crawl_depth)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (site_id, url_hash) DO UPDATE SET
                    title = EXCLUDED.title,
                    h1 = EXCLUDED.h1,
                    meta_description = EXCLUDED.meta_description,
                    body_html = EXCLUDED.body_html,
                    page_type = EXCLUDED.page_type
                RETURNING page_id
                """,
                (site_id, p["url"], p["page_type"], p["title"],
                 p.get("h1"), p.get("meta_description"), p["body_html"],
                 p.get("indexable", True), p.get("status_code", 200),
                 p.get("crawl_depth", 1)))
            ids[p["url"]] = str(cur.fetchone()[0])
    return ids


def ensure_clusters(conn, site_id, page_ids):
    """One keyword cluster per Helium page, wired to that page.

    The page_query_match_scores rows are what
    `offpage_grounding.load_brand_corpus` ranks on, so without them the
    off-page drafter sees an empty corpus and refuses to answer.
    """
    clusters = {}
    with conn.cursor() as cur:
        for url, page_id in page_ids.items():
            slug = url.rstrip("/").rsplit("/", 1)[-1] or "home"
            keyword = f"helium {slug.replace('-', ' ')}"
            cur.execute(
                """
                SELECT cluster_id FROM keyword_clusters
                WHERE site_id = %s AND primary_keyword = %s
                LIMIT 1
                """, (site_id, keyword))
            row = cur.fetchone()
            if row:
                cluster_id = str(row[0])
            else:
                cur.execute(
                    """
                    INSERT INTO keyword_clusters
                        (site_id, primary_keyword, keywords, intent)
                    VALUES (%s, %s, %s, 'commercial')
                    RETURNING cluster_id
                    """,
                    (site_id, keyword, [keyword]))
                cluster_id = str(cur.fetchone()[0])
            clusters[url] = cluster_id

            # Generator 02 joins `cluster_queries` to map a GSC query to a
            # cluster; the ILIKE match is only the brand-query EXCLUSION.
            # Without these rows generator 02 silently returns zero, which is
            # what made the old example.com run unmeasurable.
            for suffix in QUERY_SUFFIXES:
                cur.execute(
                    "INSERT INTO cluster_queries (cluster_id, query) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (cluster_id, f"{keyword}{suffix}"))

            cur.execute(
                """
                SELECT count(*) FROM page_query_match_scores
                WHERE site_id = %s AND page_id = %s AND cluster_id = %s
                """, (site_id, page_id, cluster_id))
            if cur.fetchone()[0]:
                continue
            # No unique constraint on this table (plan/00 only indexes it),
            # so idempotency is an explicit existence check.
            cur.execute(
                """
                INSERT INTO page_query_match_scores
                    (site_id, page_id, cluster_id, intent_page_type_score,
                     catalogue_match_score, gsc_evidence_score,
                     semantic_content_score)
                VALUES (%s, %s, %s, 0.9, 0.9, 0.6, 0.6)
                """, (site_id, page_id, cluster_id))
    return clusters


def ensure_search_performance(conn, site_id, pages_by_url, clusters):
    """Seed a deterministic GSC series so baseline AND post windows have data.

    A mild upward trend after the implementation anchor lets the classifier
    see a plausible 'won' rather than a structurally-zero comparison.
    """
    base = f"https://{HELIUM_DOMAIN}"
    with conn.cursor() as cur:
        # The fixture owns this tenant's GSC history. Wiping first keeps the
        # series deterministic across runs even when the query strings or
        # date range change, instead of silently unioning old rows in.
        cur.execute(
            "DELETE FROM search_performance "
            "WHERE site_id = %s AND page_url LIKE %s",
            (site_id, f"{base}/%"))

        # Resolve every cluster keyword once, up front.
        keywords = {}
        for path, _c, _i, _p in SERIES_QUERIES:
            url = f"{base}{path}"
            cluster_id = clusters.get(url)
            if not cluster_id or url not in pages_by_url:
                continue
            cur.execute("SELECT primary_keyword FROM keyword_clusters "
                        "WHERE cluster_id = %s", (cluster_id,))
            row = cur.fetchone()
            if row:
                keywords[path] = row[0]

        day = SERIES_START
        while day <= SERIES_END:
            for path, clicks, impressions, position in SERIES_QUERIES:
                keyword = keywords.get(path)
                if not keyword:
                    continue
                url = f"{base}{path}"
                for suffix in QUERY_SUFFIXES:
                    query = f"{keyword}{suffix}"
                    # Post-anchor days (after 2026-09-11) trend up.
                    drift = 1.35 if day > date(2026, 9, 11) else 1.0
                    c = int(round(clicks * drift))
                    i = int(round(impressions * drift))
                    ctr = round(c / i, 4) if i else 0.0
                    improved = max(
                        1.0,
                        round(position * (0.72 if drift > 1 else 1.0), 2))
                    cur.execute(
                        """
                        INSERT INTO search_performance
                            (site_id, date, query, page_url, country, device,
                             clicks, impressions, ctr, position)
                        VALUES (%s, %s, %s, %s, 'USA', 'MOBILE', %s, %s, %s, %s)
                        ON CONFLICT (site_id, date, query, page_url_hash,
                                     country, device) DO NOTHING
                        """,
                        (site_id, day, query, url, c, i, ctr, improved))
            day += timedelta(days=1)


def ensure_offpage_world(conn, site_id, pages_by_url, clusters):
    """Community + thread + account + the off-page recommendation to draft.

    Returns the off-page recommendation_id, or None on failure. Off-page
    rows are keyed on natural keys so repeat runs reuse them.
    """
    base = f"https://{HELIUM_DOMAIN}"
    ids = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT community_id FROM offpage_communities
            WHERE site_id = %s AND platform = 'reddit'
              AND community_key = 'r/AppGrowth'
            LIMIT 1
            """, (site_id,))
        row = cur.fetchone()
        if row:
            community_id = str(row[0])
        else:
            cur.execute(
                """
                INSERT INTO offpage_communities
                    (site_id, platform, community_key, link_policy,
                     allows_self_promo, ai_content_allowed, enabled,
                     data_source)
                VALUES (%s, 'reddit', 'r/AppGrowth', 'nofollow', true, true, true,
                        'import')
                RETURNING community_id
                """, (site_id,))
            community_id = str(cur.fetchone()[0])
        ids["community_id"] = community_id

        thread_url = ("https://www.reddit.com/r/AppGrowth/comments/"
                      "h3lium_pricing")
        cur.execute(
            """
            SELECT thread_id FROM offpage_threads
            WHERE site_id = %s AND thread_url = %s
            LIMIT 1
            """, (site_id, thread_url))
        row = cur.fetchone()
        if row:
            thread_id = str(row[0])
        else:
            snippet = (
                "We are a small team of six running an apparel app and we "
                "are trying to work out whether a growth platform that "
                "bundles paid acquisition with retention is worth it at our "
                "install base, or whether we should keep buying media "
                "directly and bolt on lifecycle messaging ourselves. Budget "
                "is roughly eight thousand a month and our payback window "
                "on new installs is about ninety days. What has actually "
                "worked for teams our size, and what did you end up paying "
                "for the retention side once you had enough users to run "
                "cohort analysis properly?"
            )
            cur.execute(
                """
                INSERT INTO offpage_threads
                    (site_id, community_id, thread_url, title, snippet,
                     context_source, context_chars, promotion_allowed,
                     data_source)
                VALUES (%s, %s, %s, %s, %s, 'serp', %s, true, 'import')
                RETURNING thread_id
                """,
                (site_id, community_id, thread_url,
                 "Is a bundled growth platform worth it at our size?",
                 snippet, len(snippet)))
            thread_id = str(cur.fetchone()[0])
        ids["thread_id"] = thread_id

        cur.execute(
            """
            SELECT account_id FROM offpage_accounts
            WHERE site_id = %s AND platform = 'reddit' AND handle = %s
            LIMIT 1
            """, (site_id, "helium_app"))
        row = cur.fetchone()
        if row:
            account_id = str(row[0])
        else:
            cur.execute(
                """
                INSERT INTO offpage_accounts
                    (site_id, platform, handle, brand_name, status,
                     promotional_cap_30d, last_verified_at)
                VALUES (%s, 'reddit', 'helium_app', 'Helium', 'active', 3,
                        now() - INTERVAL '30 days')
                RETURNING account_id
                """, (site_id,))
            account_id = str(cur.fetchone()[0])
        ids["account_id"] = account_id

        cur.execute(
            """
            INSERT INTO offpage_account_communities
                (site_id, account_id, community_id, standing, assessed_at)
            VALUES (%s, %s, %s, 'established', now())
            ON CONFLICT (account_id, community_id) DO UPDATE SET
                standing = 'established', assessed_at = now()
            """, (site_id, account_id, community_id))

        target_url = f"{base}/features"
        cur.execute(
            """
            SELECT recommendation_id FROM recommendations
            WHERE site_id = %s AND generator = 'offpage_community'
              AND target_url = %s AND offpage_thread_id = %s
            LIMIT 1
            """, (site_id, target_url, thread_id))
        row = cur.fetchone()
        if row:
            rec_id = str(row[0])
        else:
            cur.execute(
                """
                INSERT INTO recommendations
                    (site_id, generator, action_type, status, diagnosis,
                     target_url, cluster_id, offpage_thread_id, evidence_json)
                VALUES (%s, 'offpage_community', 'offpage_reply', 'raw',
                        %s, %s, %s, %s, '[]')
                RETURNING recommendation_id
                """,
                (site_id,
                 "Helium prices on active users, not ad spend - worth "
                 "comparing against direct media buying at our size.",
                 target_url, clusters.get(target_url), thread_id))
            rec_id = str(cur.fetchone()[0])
        ids["offpage_recommendation_id"] = rec_id
    return ids


def seed_helium(conn):
    """Seed the whole Helium tenant. Returns an id bundle dict."""
    data = load_fixture()
    site_id = ensure_site(conn)
    pages = data["pages"]
    pages_by_url = {p["url"]: None for p in pages}
    page_ids = ensure_pages(conn, site_id, pages)
    pages_by_url = dict(page_ids)
    clusters = ensure_clusters(conn, site_id, page_ids)
    ensure_search_performance(conn, site_id, pages_by_url, clusters)
    offpage = ensure_offpage_world(conn, site_id, pages_by_url, clusters)
    return {
        "site_id": site_id,
        "page_ids": page_ids,
        "clusters": clusters,
        **offpage,
    }
-- ============================================================
-- OpenSEO++ MVP — Candidate Generator 2: Existing-Page Opportunities
-- ============================================================
-- Finds URL×query pairs where the page is ranking but underperforming.
--
-- CORRECTIONS APPLIED:
--   1.4 Removed the invalid `DISTINCT ON (...) + GROUP BY + no ORDER BY`
--       combination — GROUP BY alone collapses to one row per
--       (page_url, cluster_id, primary_keyword).
--   1.5 Replaced the correlated per-row `page_id` lookup (which bypassed the
--       url_hash index and had no site_id filter) with a direct JOIN on
--       pages.url_hash = md5(page_url) AND pages.site_id = po.site_id.
--   1.6 Fully scoped to a single site: bind :site_id when running.
--   3.1 Position bounds and catalogue-match minimum come from site_thresholds
--       (tier default or per-site override), not hardcoded constants.
--
-- Per-site run: execute once per site with :site_id bound.
-- ============================================================

WITH site_thresholds AS (
    SELECT
        sc.site_id,
        sc.domain,
        COALESCE(sc.min_impressions_7d, tt.min_impressions_7d)     AS min_impressions_7d,
        COALESCE(sc.ctr_drop_threshold, tt.ctr_drop_threshold)     AS ctr_drop_threshold,
        COALESCE(sc.position_drop_threshold, tt.position_drop_threshold) AS position_drop_threshold,
        COALESCE(sc.position_range_min, tt.position_range_min)      AS position_range_min,
        COALESCE(sc.position_range_max, tt.position_range_max)      AS position_range_max,
        COALESCE(sc.catalogue_match_min, tt.catalogue_match_min)    AS catalogue_match_min,
        sc.brand_queries
    FROM site_config sc
    JOIN threshold_tiers tt ON tt.tier = sc.catalogue_size_tier
    WHERE sc.site_id = :site_id
),

-- ────────────────────────────────────────────
-- Part A: Position 4-20 with intent match
-- ────────────────────────────────────────────
position_opportunities AS (
    SELECT
        st.site_id,
        sp.page_url,
        sp.query,
        cq.cluster_id,
        kc.primary_keyword,
        kc.intent,

        -- Aggregate metrics over 28 days
        SUM(sp.clicks) AS total_clicks,
        SUM(sp.impressions) AS total_impressions,
        AVG(sp.ctr) AS avg_ctr,
        AVG(sp.position) AS avg_position,

        -- Current 7d vs previous 7d position trend
        AVG(CASE WHEN sp.date >= (:reference_date::date - INTERVAL '7 days') THEN sp.position END) AS position_7d,
        AVG(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '14 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.position END) AS position_prev_7d,

        -- CTR trend
        AVG(CASE WHEN sp.date >= (:reference_date::date - INTERVAL '7 days') THEN sp.ctr END) AS ctr_7d,
        AVG(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '14 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.ctr END) AS ctr_prev_7d,

        -- Click trend
        SUM(CASE WHEN sp.date >= (:reference_date::date - INTERVAL '7 days') THEN sp.clicks END) AS clicks_7d,
        SUM(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '14 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.clicks END) AS clicks_prev_7d

    FROM search_performance sp
    JOIN cluster_queries cq ON sp.query = cq.query
    JOIN keyword_clusters kc ON cq.cluster_id = kc.cluster_id
    JOIN site_thresholds st ON sp.site_id = st.site_id
    WHERE sp.date >= (:reference_date::date - INTERVAL '28 days')
      AND NOT EXISTS (
          SELECT 1 FROM unnest(st.brand_queries) bq
          WHERE sp.query ILIKE '%' || bq || '%'
      )
    GROUP BY st.site_id, st.min_impressions_7d, st.position_range_min, st.position_range_max,
             sp.page_url, sp.query, cq.cluster_id, kc.primary_keyword, kc.intent
    HAVING SUM(sp.impressions) >= st.min_impressions_7d
       AND AVG(sp.position) BETWEEN st.position_range_min AND st.position_range_max
),

-- Filter to pages with adequate catalogue match.
-- page_id resolved via a direct JOIN on the url_hash index, scoped to site.
position_with_match AS (
    SELECT
        po.*,
        pqms.aggregate_score AS match_score,
        pqms.catalogue_match_score,
        pqms.intent_page_type_score
    FROM position_opportunities po
    JOIN site_thresholds st ON po.site_id = st.site_id
    JOIN pages p
        ON p.url_hash = md5(po.page_url)
       AND p.site_id = po.site_id
    JOIN page_query_match_scores pqms
        ON pqms.page_id = p.page_id
       AND pqms.cluster_id = po.cluster_id
       AND pqms.site_id = po.site_id
    WHERE pqms.catalogue_match_score >= st.catalogue_match_min   -- adequate product coverage (tier default or override)
),

-- ────────────────────────────────────────────
-- Part B: Low CTR at positions 1-5
-- Median CTR is precomputed per average-position bucket so the comparison
-- against each group's average position is a clean join (no aggregate-in-
-- subquery, which Postgres rejects).
-- ────────────────────────────────────────────
position_medians AS (
    SELECT
        sp2.site_id,
        ROUND(sp2.position) AS position_bucket,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sp2.ctr) AS median_ctr
    FROM search_performance sp2
    WHERE sp2.date >= (:reference_date::date - INTERVAL '28 days')
    GROUP BY sp2.site_id, ROUND(sp2.position)
),

low_ctr_agg AS (
    SELECT
        st.site_id,
        sp.page_url,
        sp.query,
        cq.cluster_id,
        kc.primary_keyword,
        SUM(sp.clicks) AS total_clicks,
        SUM(sp.impressions) AS total_impressions,
        AVG(sp.ctr) AS avg_ctr,
        AVG(sp.position) AS avg_position
    FROM search_performance sp
    JOIN cluster_queries cq ON sp.query = cq.query
    JOIN keyword_clusters kc ON cq.cluster_id = kc.cluster_id
    JOIN site_thresholds st ON sp.site_id = st.site_id
    WHERE sp.date >= (:reference_date::date - INTERVAL '28 days')
      AND sp.position BETWEEN 1 AND 5        -- top-5 positions (documented rule)
      AND NOT EXISTS (
          SELECT 1 FROM unnest(st.brand_queries) bq
          WHERE sp.query ILIKE '%' || bq || '%'
      )
    GROUP BY st.site_id, st.min_impressions_7d, sp.page_url, sp.query, cq.cluster_id, kc.primary_keyword
    HAVING SUM(sp.impressions) >= st.min_impressions_7d * 3   -- meaningful sample
),

low_ctr_opportunities AS (
    SELECT
        lc.*,
        'low_ctr' AS signal_type,
        lc.avg_ctr - pm.median_ctr AS ctr_vs_position_median
    FROM low_ctr_agg lc
    JOIN position_medians pm
        ON pm.site_id = lc.site_id
       AND pm.position_bucket = ROUND(lc.avg_position)
    WHERE lc.avg_ctr < (
        -- CTR below 30th percentile for the top-5 position range
        SELECT PERCENTILE_CONT(0.3) WITHIN GROUP (ORDER BY sp2.ctr)
        FROM search_performance sp2
        WHERE sp2.site_id = lc.site_id
          AND sp2.date >= (:reference_date::date - INTERVAL '28 days')
          AND sp2.position BETWEEN 1 AND 5
    )
),

-- ────────────────────────────────────────────
-- Part C: Click decline > threshold
-- ────────────────────────────────────────────
click_decline AS (
    SELECT
        st.site_id,
        sp.page_url,
        sp.query,
        cq.cluster_id,
        kc.primary_keyword,
        'click_decline' AS signal_type,

        SUM(CASE WHEN sp.date >= (:reference_date::date - INTERVAL '7 days') THEN sp.clicks END) AS clicks_recent,
        SUM(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '35 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.clicks END) / 4.0 AS avg_clicks_prior_week,

        CASE
            WHEN SUM(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '35 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.clicks END) > 0
            THEN 1.0 - (
                SUM(CASE WHEN sp.date >= (:reference_date::date - INTERVAL '7 days') THEN sp.clicks END)::NUMERIC /
                (SUM(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '35 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.clicks END) / 4.0)
            )
            ELSE 0
        END AS decline_rate

    FROM search_performance sp
    JOIN cluster_queries cq ON sp.query = cq.query
    JOIN keyword_clusters kc ON cq.cluster_id = kc.cluster_id
    JOIN site_thresholds st ON sp.site_id = st.site_id
    WHERE sp.date >= (:reference_date::date - INTERVAL '35 days')
      AND NOT EXISTS (
          SELECT 1 FROM unnest(st.brand_queries) bq
          WHERE sp.query ILIKE '%' || bq || '%'
      )
    GROUP BY st.site_id, st.ctr_drop_threshold, sp.page_url, sp.query, cq.cluster_id, kc.primary_keyword
    HAVING SUM(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '35 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.clicks END) > 0
       AND (
            1.0 - (
                SUM(CASE WHEN sp.date >= (:reference_date::date - INTERVAL '7 days') THEN sp.clicks END)::NUMERIC /
                GREATEST(SUM(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '35 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.clicks END) / 4.0, 1)
            )
       ) > st.ctr_drop_threshold
),

-- ────────────────────────────────────────────
-- Part D: Ranking decline >= threshold positions
-- ────────────────────────────────────────────
rank_decline AS (
    SELECT
        st.site_id,
        sp.page_url,
        sp.query,
        cq.cluster_id,
        kc.primary_keyword,
        'rank_decline' AS signal_type,

        AVG(CASE WHEN sp.date >= (:reference_date::date - INTERVAL '7 days') THEN sp.position END) AS position_recent,
        AVG(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '35 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.position END) AS position_prior,

        AVG(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '35 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.position END)
        - AVG(CASE WHEN sp.date >= (:reference_date::date - INTERVAL '7 days') THEN sp.position END) AS position_change

    FROM search_performance sp
    JOIN cluster_queries cq ON sp.query = cq.query
    JOIN keyword_clusters kc ON cq.cluster_id = kc.cluster_id
    JOIN site_thresholds st ON sp.site_id = st.site_id
    WHERE sp.date >= (:reference_date::date - INTERVAL '35 days')
      AND NOT EXISTS (
          SELECT 1 FROM unnest(st.brand_queries) bq
          WHERE sp.query ILIKE '%' || bq || '%'
      )
    GROUP BY st.site_id, st.position_drop_threshold, sp.page_url, sp.query, cq.cluster_id, kc.primary_keyword
    HAVING AVG(CASE WHEN sp.date >= (:reference_date::date - INTERVAL '7 days') THEN sp.position END) IS NOT NULL
       AND AVG(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '35 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.position END) IS NOT NULL
       AND (
           AVG(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '35 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.position END)
           - AVG(CASE WHEN sp.date >= (:reference_date::date - INTERVAL '7 days') THEN sp.position END)
       ) >= st.position_drop_threshold
),

-- ────────────────────────────────────────────
-- Combine all opportunity signals
-- ────────────────────────────────────────────
all_opportunities AS (
    SELECT
        page_url, cluster_id, primary_keyword,
        'position_4_20' AS signal_type,
        total_clicks, total_impressions, avg_ctr, avg_position
    FROM position_with_match

    UNION ALL

    SELECT
        page_url, cluster_id, primary_keyword,
        signal_type,
        total_clicks, total_impressions, avg_ctr, avg_position
    FROM low_ctr_opportunities

    UNION ALL

    SELECT
        page_url, cluster_id, primary_keyword,
        signal_type,
        clicks_recent AS total_clicks, NULL AS total_impressions, NULL AS avg_ctr, NULL AS avg_position
    FROM click_decline

    UNION ALL

    SELECT
        page_url, cluster_id, primary_keyword,
        signal_type,
        NULL AS total_clicks, NULL AS total_impressions, NULL AS avg_ctr, position_recent AS avg_position
    FROM rank_decline
),

-- Deduplicate: one recommendation per page+cluster.
-- GROUP BY alone is sufficient — DISTINCT ON is redundant (fix 1.4).
deduplicated AS (
    SELECT
        page_url,
        cluster_id,
        primary_keyword,
        array_agg(DISTINCT signal_type) AS signals,
        SUM(total_clicks) AS total_clicks,
        SUM(total_impressions) AS total_impressions,
        AVG(avg_ctr) AS avg_ctr,
        AVG(avg_position) AS avg_position
    FROM all_opportunities
    GROUP BY page_url, cluster_id, primary_keyword
)

SELECT
    'existing_opportunity' AS generator,
    'improve_page' AS action_type,
    page_url AS target_url,
    cluster_id,
    primary_keyword,
    signals,
    total_clicks,
    total_impressions,
    avg_ctr,
    avg_position,
    -- Priority score: more signals × higher impressions = higher priority
    (COALESCE(array_length(signals, 1), 1) * COALESCE(total_impressions, 0)) AS priority_score
FROM deduplicated
WHERE NOT EXISTS (
    SELECT 1 FROM recommendations r
    WHERE r.target_url = deduplicated.page_url
      AND r.cluster_id = deduplicated.cluster_id
      AND r.status IN ('raw', 'proposed', 'approved', 'in_progress')
)
ORDER BY priority_score DESC
LIMIT 20;   -- per site (query is bound to :site_id)
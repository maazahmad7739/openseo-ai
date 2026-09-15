-- ============================================================
-- OpenSEO++ MVP — Candidate Generator 4: Cannibalization
-- ============================================================
-- Detects when multiple URLs compete for the same keyword cluster
-- with alternating rankings and similar intent.
-- ============================================================

WITH site_thresholds AS (
    SELECT site_id, domain, brand_queries
    FROM site_config
    WHERE site_id = :site_id
),

-- ────────────────────────────────────────────
-- Step 1: Find query×URL pairs with impressions
-- ────────────────────────────────────────────
query_url_performance AS (
    SELECT
        sp.site_id,
        sp.query,
        cq.cluster_id,
        sp.page_url,
        sp.page_url_hash,

        SUM(sp.clicks) AS total_clicks,
        SUM(sp.impressions) AS total_impressions,
        AVG(sp.position) AS avg_position,

        -- Track position variance over time (high variance = switching)
        STDDEV(sp.position) AS position_stddev,

        -- Count how many distinct positions this URL held
        COUNT(DISTINCT ROUND(sp.position)) AS distinct_positions,

        -- Track ranking across 4 weekly windows
        AVG(CASE WHEN sp.date >= (:reference_date::date - INTERVAL '7 days') THEN sp.position END) AS pos_w1,
        AVG(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '14 days') AND (:reference_date::date - INTERVAL '8 days') THEN sp.position END) AS pos_w2,
        AVG(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '21 days') AND (:reference_date::date - INTERVAL '15 days') THEN sp.position END) AS pos_w3,
        AVG(CASE WHEN sp.date BETWEEN (:reference_date::date - INTERVAL '28 days') AND (:reference_date::date - INTERVAL '22 days') THEN sp.position END) AS pos_w4

    FROM search_performance sp
    JOIN cluster_queries cq ON sp.query = cq.query
    JOIN site_thresholds st ON sp.site_id = st.site_id
    WHERE sp.date >= (:reference_date::date - INTERVAL '28 days')
    GROUP BY sp.site_id, sp.query, cq.cluster_id, sp.page_url, sp.page_url_hash
    HAVING SUM(sp.impressions) >= 100  -- minimum signal
),

-- ────────────────────────────────────────────
-- Step 2: For each cluster, find URLs on our own domain
-- ────────────────────────────────────────────
own_domain_pairs AS (
    SELECT
        qup.*
    FROM query_url_performance qup
    JOIN site_thresholds st ON qup.site_id = st.site_id
    WHERE qup.page_url LIKE '%' || st.domain || '%'
),

-- ────────────────────────────────────────────
-- Step 3: Find clusters with 2+ competing URLs
-- ────────────────────────────────────────────
competing_pairs AS (
    SELECT
        cluster_id,
        query,
        array_agg(DISTINCT page_url ORDER BY page_url) AS competing_urls,
        COUNT(DISTINCT page_url) AS url_count,
        SUM(total_impressions) AS combined_impressions,
        SUM(total_clicks) AS combined_clicks
    FROM own_domain_pairs
    GROUP BY cluster_id, query
    HAVING COUNT(DISTINCT page_url) >= 2
),

-- ────────────────────────────────────────────
-- Step 4: Check that neither URL clearly dominates
-- (the leading URL doesn't have 3x+ the impressions of the runner-up)
-- ────────────────────────────────────────────
no_clear_dominator AS (
    SELECT
        cp.cluster_id,
        cp.query,
        cp.competing_urls,
        cp.url_count,
        cp.combined_impressions,
        cp.combined_clicks,

        -- Get the top 2 URLs by impressions
        (
            SELECT jsonb_agg(jsonb_build_object(
                'url', page_url,
                'impressions', total_impressions,
                'clicks', total_clicks,
                'avg_position', avg_position,
                'position_stddev', position_stddev,
                'pos_w1', pos_w1,
                'pos_w2', pos_w2,
                'pos_w3', pos_w3,
                'pos_w4', pos_w4
            ) ORDER BY total_impressions DESC)
            FROM own_domain_pairs op
            WHERE op.cluster_id = cp.cluster_id
              AND op.query = cp.query
              AND op.page_url = ANY(cp.competing_urls)
            LIMIT 2
        ) AS top_two

    FROM competing_pairs cp
),

-- Filter: neither URL has 3x+ the impressions of the other
balanced_competition AS (
    SELECT *
    FROM no_clear_dominator
    WHERE (top_two->0->>'impressions')::NUMERIC < (top_two->1->>'impressions')::NUMERIC * 3
),

-- ────────────────────────────────────────────
-- Step 5: Check ranking alternation across weeks
-- (the ranking URL switches repeatedly)
-- ────────────────────────────────────────────
alternation_check AS (
    SELECT
        bc.*,

        -- Count weeks where each URL is the leader
        (
            SELECT COUNT(*) FILTER (WHERE pos_w1 < pos_w2)
            FROM own_domain_pairs op
            WHERE op.cluster_id = bc.cluster_id
              AND op.query = bc.query
              AND op.page_url = ANY(bc.competing_urls)
        ) AS url1_leads_w1,

        (
            SELECT COUNT(*) FILTER (WHERE pos_w1 > pos_w2)
            FROM own_domain_pairs op
            WHERE op.cluster_id = bc.cluster_id
              AND op.query = bc.query
              AND op.page_url = ANY(bc.competing_urls)
        ) AS url2_leads_w1

    FROM balanced_competition bc
),

-- Step 6: Check that both URLs have materially similar intent
-- (both are collections, or both are products, not collection vs blog)
intent_similarity AS (
    SELECT
        ac.*,

        (
            SELECT jsonb_agg(DISTINCT p.page_type)
            FROM pages p
            WHERE p.url = ANY(ac.competing_urls)
              AND p.site_id = :site_id
        ) AS competing_page_types,

        -- Get titles and H1s for comparison
        (
            SELECT jsonb_agg(jsonb_build_object(
                'url', p.url,
                'title', p.title,
                'h1', p.h1,
                'page_type', p.page_type
            ))
            FROM pages p
            WHERE p.url = ANY(ac.competing_urls)
              AND p.site_id = :site_id
        ) AS page_metadata

    FROM alternation_check ac
),

-- ────────────────────────────────────────────
-- Step 7: Classify cannibalization type and generate recommendation
-- ────────────────────────────────────────────
cannibalization_diagnosis AS (
    SELECT
        ic.*,
        kc.primary_keyword,

        -- Determine if both pages are the same type
        CASE
            WHEN jsonb_array_length(ic.competing_page_types) = 1 THEN 'same_type'
            WHEN jsonb_array_length(ic.competing_page_types) = 2 THEN 'mixed_types'
            ELSE 'multiple_types'
        END AS cannibalization_pattern,

        -- Recommendation type based on pattern
        CASE
            WHEN jsonb_array_length(ic.competing_page_types) = 1 THEN 'consolidate'
            ELSE 'consolidate'  -- both patterns suggest consolidation
        END AS recommended_action,

        -- Extract the weaker page (fewer impressions, worse position)
        (
            SELECT p.url
            FROM pages p
            WHERE p.url = ANY(ic.competing_urls)
              AND p.site_id = :site_id
            ORDER BY (
                SELECT COALESCE(SUM(op.total_impressions), 0)
                FROM own_domain_pairs op
                WHERE op.page_url = p.url AND op.cluster_id = ic.cluster_id
            ) ASC
            LIMIT 1
        ) AS weaker_url,

        -- Extract the stronger page
        (
            SELECT p.url
            FROM pages p
            WHERE p.url = ANY(ic.competing_urls)
              AND p.site_id = :site_id
            ORDER BY (
                SELECT COALESCE(SUM(op.total_impressions), 0)
                FROM own_domain_pairs op
                WHERE op.page_url = p.url AND op.cluster_id = ic.cluster_id
            ) DESC
            LIMIT 1
        ) AS stronger_url

    FROM intent_similarity ic
    JOIN keyword_clusters kc ON ic.cluster_id = kc.cluster_id
    WHERE jsonb_array_length(ic.competing_page_types) <= 2  -- only when both pages are similar type
)

-- FINAL OUTPUT: Cannibalization candidates
SELECT
    'cannibalization' AS generator,
    'consolidate' AS action_type,
    stronger_url AS target_url,  -- the page to keep
    weaker_url AS proposed_url,  -- the page to consolidate/redirect
    cluster_id,
    primary_keyword,
    competing_urls,
    combined_impressions,
    combined_clicks,
    competing_page_types,
    cannibalization_pattern,
    page_metadata,

    jsonb_build_object(
        'issue', 'Cannibalization: two pages competing for the same cluster',
        'competing_urls', competing_urls,
        'impressions_distribution', (
            SELECT jsonb_object_agg(op.page_url, op.total_impressions)
            FROM own_domain_pairs op
            WHERE op.cluster_id = cannibalization_diagnosis.cluster_id
              AND op.query = cannibalization_diagnosis.query
              AND op.page_url = ANY(cannibalization_diagnosis.competing_urls)
        ),
        'pattern', cannibalization_pattern,
        'weekly_positions', (
            SELECT jsonb_agg(jsonb_build_object(
                'url', page_url,
                'pos_w1', pos_w1,
                'pos_w2', pos_w2,
                'pos_w3', pos_w3,
                'pos_w4', pos_w4
            ))
            FROM own_domain_pairs op
            WHERE op.cluster_id = cannibalization_diagnosis.cluster_id
              AND op.query = cannibalization_diagnosis.query
              AND op.page_url = ANY(cannibalization_diagnosis.competing_urls)
        )
    ) AS evidence

FROM cannibalization_diagnosis
-- Exclude if already recommended
WHERE NOT EXISTS (
    SELECT 1 FROM recommendations r
    WHERE r.cluster_id = cannibalization_diagnosis.cluster_id
      AND r.generator = 'cannibalization'
      AND r.status IN ('raw', 'proposed', 'approved', 'in_progress')
)
ORDER BY combined_impressions DESC
LIMIT 10;

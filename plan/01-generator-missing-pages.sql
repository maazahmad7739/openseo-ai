-- ============================================================
-- OpenSEO++ MVP — Candidate Generator 1: Missing Commercial Pages
-- ============================================================
-- Finds keyword clusters with commercial demand but no matching
-- existing page. This is the PRIMARY growth generator.
--
-- CORRECTIONS APPLIED:
--   1.1 unmatched_clusters no longer reads a non-existent
--       cc.page_match_threshold — the threshold is pulled from
--       site_thresholds joined on site_id.
--   1.2 No unscoped `(SELECT domain FROM site_config LIMIT 1)` — every
--       lookup is joined on site_id.
--   1.3 Competitor signals come from openseo_serp_snapshots (OpenSEO
--       connector), NOT search_performance (GSC can never return rows
--       for properties you don't own).
--   1.6 Fully scoped to a single site: bind :site_id when running
--       (function arg, prepared statement, or app-level loop).
--
-- Per-site run: the query is executed once per site with :site_id bound,
-- so the final LIMIT is per-site (one site can't consume another's quota).
-- ============================================================

WITH site_thresholds AS (
    SELECT
        sc.site_id,
        sc.domain,
        sc.catalogue_size_tier,
        sc.in_stock_definition,
        COALESCE(sc.min_search_volume, tt.min_search_volume)         AS min_search_volume,
        COALESCE(sc.min_in_stock_products, tt.min_in_stock_products) AS min_in_stock_products,
        COALESCE(sc.page_match_threshold, tt.page_match_threshold)   AS page_match_threshold,
        sc.brand_queries
    FROM site_config sc
    JOIN threshold_tiers tt ON tt.tier = sc.catalogue_size_tier
    WHERE sc.site_id = :site_id
),

-- Step 1: Filter to commercially-intent clusters with sufficient demand
qualified_clusters AS (
    SELECT
        kc.cluster_id,
        kc.primary_keyword,
        kc.keywords,
        kc.search_volume,
        kc.commercial_value,
        kc.recommended_page_type,
        st.site_id
    FROM keyword_clusters kc
    JOIN site_thresholds st ON kc.site_id = st.site_id
    WHERE kc.commercial_intent = true
      AND kc.search_volume >= st.min_search_volume
      -- Exclude brand queries
      AND NOT EXISTS (
          SELECT 1 FROM unnest(st.brand_queries) bq
          WHERE kc.primary_keyword ILIKE '%' || bq || '%'
      )
),

-- Step 2: Check catalogue coverage — enough in-stock products to build a page.
-- NOTE (Priority 3.7): in_stock_product_count is computed upstream by the
-- catalogue_coverage job using site_config.in_stock_definition:
--   any_variant        = product in stock if >=1 variant has available inventory
--   majority_variants  = product in stock only if >=50% of variants have inventory
covered_clusters AS (
    SELECT
        qc.cluster_id,
        qc.primary_keyword,
        qc.search_volume,
        qc.commercial_value,
        qc.recommended_page_type,
        qc.site_id,
        cc.matching_product_count,
        cc.in_stock_product_count,
        cc.average_price,
        cc.existing_collection_url
    FROM qualified_clusters qc
    JOIN catalogue_coverage cc ON qc.cluster_id = cc.cluster_id
                             AND cc.site_id = qc.site_id
    WHERE cc.in_stock_product_count >= (
        SELECT min_in_stock_products FROM site_thresholds WHERE site_id = qc.site_id
    )
),

-- Step 3: Check that no existing page already strongly matches this cluster.
-- The threshold is resolved per site from site_thresholds (tier default or
-- per-site override), never hardcoded in the SQL.
unmatched_clusters AS (
    SELECT
        cc.*,
        st.page_match_threshold
    FROM covered_clusters cc
    JOIN site_thresholds st ON cc.site_id = st.site_id
    LEFT JOIN page_query_match_scores pqms
        ON pqms.cluster_id = cc.cluster_id
       AND pqms.site_id = cc.site_id
       AND pqms.aggregate_score >= st.page_match_threshold
    WHERE pqms.id IS NULL
      -- Also exclude if we already have a proposed recommendation for this cluster
      AND NOT EXISTS (
          SELECT 1 FROM recommendations r
          WHERE r.cluster_id = cc.cluster_id
            AND r.site_id = cc.site_id
            AND r.status IN ('raw', 'proposed', 'approved', 'in_progress')
      )
),

-- Step 4: Find competitor pages ranking for this cluster.
-- Sourced from OpenSEO SERP snapshots (live SERP data), scoped to this site.
competitor_signals AS (
    SELECT
        oss.cluster_id,
        oss.result_url AS page_url,
        oss.position,
        NULL::INT AS clicks,        -- SERP snapshot has no click data; clicks are GSC-side only
        NULL::INT AS impressions
    FROM openseo_serp_snapshots oss
    JOIN site_thresholds st ON oss.site_id = st.site_id
    WHERE oss.snapshot_date >= (:reference_date::date - INTERVAL '28 days')
      AND oss.is_self = false                 -- competitor results only
      AND oss.position <= 10
      AND oss.cluster_id IS NOT NULL
),

-- Step 5: Generate recommended URL slug
url_proposals AS (
    SELECT
        uc.cluster_id,
        uc.primary_keyword,
        uc.site_id,
        -- Generate a clean URL slug from primary keyword
        '/collections/' || regexp_replace(
            lower(trim(uc.primary_keyword)),
            '[^a-z0-9]+', '-', 'g'
        ) AS proposed_url,
        uc.search_volume,
        uc.in_stock_product_count,
        uc.average_price,
        uc.commercial_value
    FROM unmatched_clusters uc
)

-- FINAL OUTPUT: Missing commercial page candidates (per-site, scoped to :site_id)
SELECT
    'missing_page' AS generator,
    'create_page' AS action_type,
    up.cluster_id,
    up.primary_keyword,
    up.proposed_url,
    up.search_volume,
    up.in_stock_product_count,
    up.average_price,
    up.commercial_value,
    (
        SELECT jsonb_agg(jsonb_build_object(
            'product_id', unnest_id
        ))
        FROM (
            SELECT unnest(matching_product_ids) AS unnest_id
            FROM catalogue_coverage
            WHERE cluster_id = up.cluster_id
            LIMIT 20
        ) prod_sample
    ) AS sample_products,
    (
        SELECT jsonb_agg(jsonb_build_object(
            'url', cs.page_url,
            'position', cs.position,
            'clicks', cs.clicks
        ) ORDER BY cs.position)
        FROM competitor_signals cs
        WHERE cs.cluster_id = up.cluster_id
        LIMIT 5
    ) AS top_competitors,
    up.in_stock_product_count AS matching_products_count
FROM url_proposals up
ORDER BY up.search_volume DESC, up.commercial_value DESC
LIMIT 15;  -- top 15 candidates feed into agent for final selection (per site)
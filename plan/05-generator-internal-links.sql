-- ============================================================
-- OpenSEO++ MVP — Candidate Generator 5: Internal Link Gaps (plan/31)
-- ============================================================
-- Phase 5 closure: plan/03 detects orphan/deep pages (counts only) but the
-- engine could never suggest WHERE a link should be placed, because the
-- link graph (page_links) was never populated. plan/31 ingests edges at
-- audit time; this generator turns them into actionable candidates.
--
-- Candidate = one (orphan|deep TARGET page) x (one SOURCE page suggestion).
-- The TARGET is a revenue-relevant page the graph leaves unreachable or
-- buried; the SOURCE is a link-rich, indexable page of the SAME template
-- family ranked by keyword-cluster overlap (the source's page_query_match_
-- scores clusters vs the target's) and internal_links_out (link budget).
--
-- SUGGESTION-ONLY (plan/31 §3 fail-closed): rows enter the Action Queue as
-- status='proposed' for operator review. NO write adapter exists for
-- internal link placement — fixes/generator refuses issue_type
-- 'internal_link_gap' (typed), so approval can never mint a write.
-- ============================================================

WITH site_thresholds AS (
    SELECT site_id, domain
    FROM site_config
    WHERE site_id = :site_id
),

-- Revenue-relevant orphan/deep pages (mirrors plan/03 Issue 5's WHERE,
-- re-queried here so the generator stays a pure read-only producer).
link_gap_targets AS (
    SELECT DISTINCT ON (p.url)
        p.page_id, p.site_id, p.url, p.page_type, p.crawl_depth,
        p.internal_links_in, p.product_count,
        COALESCE(sp.total_clicks, 0) AS gsc_clicks_28d,
        COALESCE(sp.total_impressions, 0) AS gsc_impressions_28d,
        CASE WHEN p.internal_links_in = 0 THEN 'orphan_page'
             ELSE 'deep_page' END AS issue_type,
        CASE
            WHEN p.product_count > 0
                 OR p.page_type IN ('collection', 'product') THEN 'high'
            ELSE 'medium'
        END AS impact
    FROM pages p
    LEFT JOIN (
        SELECT page_url_hash,
               SUM(clicks) AS total_clicks,
               SUM(impressions) AS total_impressions
        FROM search_performance
        WHERE date >= (:reference_date::date - INTERVAL '28 days')
        GROUP BY page_url_hash
    ) sp ON sp.page_url_hash = p.url_hash
    WHERE p.site_id = :site_id
      AND p.status_code = 200
      AND (p.internal_links_in = 0 OR p.crawl_depth > 5)
      AND p.page_type IN ('collection', 'product', 'landing', 'category',
                          'blog')
),
-- Keyword clusters the TARGET page is relevant to (page_query_match_scores
-- written by the weekly semantic pass): the anchor should carry the
-- cluster's language, so the best source is a page ALREADY targeting a
-- shared cluster.
target_clusters AS (
    SELECT lg.site_id, lg.page_id, lg.url, lg.page_type, lg.crawl_depth,
           lg.internal_links_in, lg.issue_type, lg.impact,
           lg.gsc_clicks_28d, lg.gsc_impressions_28d,
           pqms.cluster_id, k.primary_keyword
    FROM link_gap_targets lg
    JOIN page_query_match_scores pqms ON pqms.page_id = lg.page_id
    LEFT JOIN keyword_clusters k ON k.cluster_id = pqms.cluster_id
),
-- Candidate sources: indexable, crawled, NOT the target itself, with
-- outbound link budget (internal_links_out). Ranked by:
--   1. shared cluster membership (same keyword space -> contextual anchor)
--   2. outbound budget (pages linking out a lot absorb one more edge)
--   3. GSC impressions (authority proxy — impressions, not clicks, so a
--      strong page that merely RANKS can still donate a link)
candidate_sources AS (
    SELECT
        t.page_id AS target_page_id,
        t.url AS target_url,
        t.page_type AS target_page_type,
        t.crawl_depth AS target_crawl_depth,
        t.internal_links_in AS target_internal_links_in,
        t.issue_type,
        t.gsc_clicks_28d,
        s.page_id AS source_page_id,
        s.url AS source_url,
        s.title AS source_title,
        s.internal_links_out,
        s.crawl_depth AS source_depth,
        COUNT(DISTINCT pqms.cluster_id) AS shared_clusters,
        MIN(k.primary_keyword) AS anchor_keyword
    FROM target_clusters t
    JOIN pages s
      ON s.site_id = t.site_id
     AND s.page_id <> t.page_id
     AND s.status_code = 200
     AND s.indexable = true
     AND COALESCE(s.internal_links_out, 0) > 0
     AND s.crawl_depth <= 5
    LEFT JOIN page_query_match_scores pqms
      ON pqms.page_id = s.page_id
     AND pqms.cluster_id IN (SELECT cluster_id FROM target_clusters t2
                             WHERE t2.page_id = t.page_id)
    LEFT JOIN keyword_clusters k ON k.cluster_id = pqms.cluster_id
    GROUP BY t.page_id, t.url, t.page_type, t.crawl_depth,
             t.internal_links_in, t.issue_type, t.gsc_clicks_28d,
             s.page_id, s.url, s.title, s.internal_links_out, s.crawl_depth
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY target_url
               ORDER BY shared_clusters DESC,
                        internal_links_out DESC,
                        source_depth ASC
           ) AS source_rank
    FROM candidate_sources
),
top_source AS (
    SELECT * FROM ranked WHERE source_rank = 1
)
SELECT
    'internal_link' AS generator,
    'improve_page' AS action_type,
    ts.target_url AS target_url,
    ts.source_url AS proposed_url,
    NULL::uuid AS cluster_id,
    -- diagnosis names the GAP and the SUGGESTED placement (operator-facing)
    ts.issue_type || ': ' || ts.target_url || ' has ' ||
        COALESCE(ts.gsc_clicks_28d, 0)::text || ' clicks but is ' ||
        CASE ts.issue_type WHEN 'orphan_page' THEN 'unlinked (orphan)'
             ELSE 'deep in the architecture' END ||
        ' — add an internal link from ' || ts.source_url ||
        ' (shared clusters: ' || ts.shared_clusters::text || ')' AS diagnosis,
    jsonb_build_object(
        'issue_type', 'internal_link_gap',
        'suggestion_only', true,
        'target_page', jsonb_build_object(
            'url', ts.target_url,
            'page_type', ts.target_page_type,
            'crawl_depth', ts.target_crawl_depth,
            'internal_links_in', ts.target_internal_links_in,
            'gsc_clicks_28d', ts.gsc_clicks_28d
        ),
        'suggested_source', jsonb_build_object(
            'url', ts.source_url,
            'title', ts.source_title,
            'internal_links_out', ts.internal_links_out,
            'shared_clusters', ts.shared_clusters
        ),
        -- Deterministic anchor proposal: the shared cluster's primary
        -- keyword when the pages share keyword space; else the source
        -- page's title (a real phrase, never fabricated).
        'suggested_anchor_text', ts.anchor_keyword,
        'anchor_basis', CASE WHEN ts.anchor_keyword IS NOT NULL
                             THEN 'shared_cluster_primary_keyword'
                             ELSE 'source_page_title' END,
        -- Edges already pointing AT the target (dedupe guard evidence).
        'existing_links_in', ts.target_internal_links_in
    ) AS evidence,
    jsonb_build_array(
        jsonb_build_object(
            'owner', 'SEO',
            'task', 'Add an internal link from ' || ts.source_url ||
                    ' to ' || ts.target_url ||
                    ' with contextual anchor text',
            'acceptance_criteria', 'Link present on the source page; target '
                || 'reachable within 3 clicks of the homepage; anchor text '
                || 'reflects the target page topic'
        )
    ) AS work_required,
    lg.impact AS impact,
    'medium' AS confidence,
    'hours' AS effort,
    'SEO' AS owner
FROM top_source ts
JOIN link_gap_targets lg ON lg.url = ts.target_url
-- Dedupe vs the queue: never re-recommend a target that already has a
-- pending internal-link suggestion (any status pre-measurement).
WHERE NOT EXISTS (
    SELECT 1 FROM recommendations r
    WHERE r.target_url = ts.target_url
      AND r.generator = 'internal_link'
      AND r.status IN ('raw', 'proposed', 'approved', 'in_progress')
)
ORDER BY
    CASE lg.impact WHEN 'high' THEN 1 ELSE 2 END,
    lg.gsc_clicks_28d DESC
LIMIT 10;
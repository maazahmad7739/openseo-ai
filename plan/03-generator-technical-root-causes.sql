-- ============================================================
-- OpenSEO++ MVP — Candidate Generator 3: Technical Root Causes
-- ============================================================
-- Groups crawl audit findings by issue_type × page_type × template
-- and generates recommendations only for strategically important issues.
-- ============================================================

WITH site_thresholds AS (
    SELECT site_id, domain
    FROM site_config
    WHERE site_id = :site_id
),

-- Define which page types are strategically important
-- (pages that drive revenue or have significant organic visibility)
important_pages AS (
    SELECT DISTINCT p.page_id, p.site_id, p.url, p.page_type, p.template, p.status_code,
           p.indexable, p.canonical_url, p.crawl_depth,
           p.internal_links_in, p.internal_links_out,
           p.product_count, p.is_orphan, p.has_structured_data,
           p.render_status, p.raw_html_hash, p.rendered_html_hash,
           p.last_crawled_at,

           -- A page is "important" if it has organic traffic or commercial products
           COALESCE(pbp.total_organic_sessions, 0) AS organic_sessions_28d,
           COALESCE(pbp.total_orders, 0) AS orders_28d,
           COALESCE(pbp.total_revenue, 0) AS revenue_28d,
           COALESCE(sp.total_clicks, 0) AS gsc_clicks_28d,
           COALESCE(sp.total_impressions, 0) AS gsc_impressions_28d

    FROM pages p
    LEFT JOIN (
        SELECT page_url_hash,
               SUM(organic_sessions) AS total_organic_sessions,
               SUM(orders) AS total_orders,
               SUM(revenue) AS total_revenue
        FROM page_business_performance
        WHERE date >= (:reference_date::date - INTERVAL '28 days')
        GROUP BY page_url_hash
    ) pbp ON pbp.page_url_hash = p.url_hash
    LEFT JOIN (
        SELECT page_url_hash,
               SUM(clicks) AS total_clicks,
               SUM(impressions) AS total_impressions
        FROM search_performance
        WHERE date >= (:reference_date::date - INTERVAL '28 days')
        GROUP BY page_url_hash
    ) sp ON sp.page_url_hash = p.url_hash
    WHERE p.site_id = :site_id
      AND p.status_code IS NOT NULL  -- must have been crawled
),

-- ────────────────────────────────────────────
-- Issue 1: Important pages not indexable
-- ────────────────────────────────────────────
not_indexable AS (
    SELECT
        'not_indexable' AS issue_type,
        'technical_fix' AS action_type,
        ip.site_id,
        ip.url AS target_url,
        ip.page_type,
        ip.template,
        ip.status_code,
        ip.organic_sessions_28d,
        ip.gsc_clicks_28d,

        jsonb_build_object(
            'issue', 'Page is not indexable but has organic visibility or commercial value',
            'current_status', ip.status_code,
            'indexable', ip.indexable,
            'canonical', ip.canonical_url,
            'organic_sessions_28d', ip.organic_sessions_28d,
            'gsc_clicks_28d', ip.gsc_clicks_28d
        ) AS evidence,

        jsonb_build_array(
            jsonb_build_object(
                'owner', 'engineering',
                'task', 'Investigate why page is not indexable and fix root cause',
                'acceptance_criteria', 'Page passes Google URL Inspection and is included in sitemap'
            )
        ) AS work_required,

        -- Priority: commercial pages with traffic get highest priority
        CASE
            WHEN ip.page_type IN ('collection', 'product', 'landing') AND ip.gsc_clicks_28d > 100 THEN 'high'
            WHEN ip.page_type IN ('collection', 'product', 'landing') THEN 'medium'
            ELSE 'low'
        END AS impact

    FROM important_pages ip
    WHERE ip.indexable = false
      AND ip.page_type IN ('collection', 'product', 'landing', 'category', 'homepage')
),

-- ────────────────────────────────────────────
-- Issue 2: Canonical conflicts
-- (canonical points to different URL or self-referencing mismatch)
-- ────────────────────────────────────────────
canonical_conflicts AS (
    SELECT
        'canonical_conflict' AS issue_type,
        'technical_fix' AS action_type,
        ip.site_id,
        ip.url AS target_url,
        ip.page_type,
        ip.template,
        ip.status_code,
        ip.organic_sessions_28d,
        ip.gsc_clicks_28d,

        jsonb_build_object(
            'issue', 'Canonical URL does not match page URL',
            'page_url', ip.url,
            'canonical_url', ip.canonical_url,
            'status_code', ip.status_code,
            'organic_sessions_28d', ip.organic_sessions_28d
        ) AS evidence,

        jsonb_build_array(
            jsonb_build_object(
                'owner', 'engineering',
                'task', 'Resolve canonical conflict: canonical points to ' || ip.canonical_url,
                'acceptance_criteria', 'Self-referencing canonical OR intentional cross-domain canonical with documented reason'
            )
        ) AS work_required,

        CASE
            WHEN ip.gsc_clicks_28d > 50 THEN 'high'
            WHEN ip.organic_sessions_28d > 0 THEN 'medium'
            ELSE 'low'
        END AS impact

    FROM important_pages ip
    WHERE ip.canonical_url IS NOT NULL
      AND ip.canonical_url != ip.url
      AND ip.status_code = 200
),

-- ────────────────────────────────────────────
-- Issue 3: Status-code failures (4xx/5xx)
-- ────────────────────────────────────────────
status_failures AS (
    SELECT
        'status_failure' AS issue_type,
        'technical_fix' AS action_type,
        ip.site_id,
        ip.url AS target_url,
        ip.page_type,
        ip.template,
        ip.status_code,
        ip.organic_sessions_28d,
        ip.gsc_clicks_28d,

        jsonb_build_object(
            'issue', 'Page returns HTTP ' || ip.status_code,
            'status_code', ip.status_code,
            'gsc_clicks_28d', ip.gsc_clicks_28d,
            'organic_sessions_28d', ip.organic_sessions_28d
        ) AS evidence,

        jsonb_build_array(
            jsonb_build_object(
                'owner', 'engineering',
                'task', 'Fix HTTP ' || ip.status_code || ' error',
                'acceptance_criteria', 'Page returns HTTP 200 and is indexable'
            )
        ) AS work_required,

        CASE
            WHEN ip.status_code = 500 AND ip.gsc_clicks_28d > 0 THEN 'high'
            WHEN ip.status_code = 500 THEN 'medium'
            WHEN ip.status_code = 404 AND ip.gsc_clicks_28d > 10 THEN 'medium'
            ELSE 'low'
        END AS impact

    FROM important_pages ip
    WHERE ip.status_code >= 400
      AND ip.page_type IN ('collection', 'product', 'landing', 'category', 'homepage', 'blog')
),

-- ────────────────────────────────────────────
-- Issue 4: Broken internal links to important pages
-- ────────────────────────────────────────────
-- This requires cross-referencing pages with status codes
-- We check if any important page has internal_links_in > 0
-- but status_code >= 400 (linked but broken)
broken_links AS (
    SELECT
        'broken_internal_link' AS issue_type,
        'technical_fix' AS action_type,
        ip.site_id,
        ip.url AS target_url,
        ip.page_type,
        ip.template,
        ip.status_code,
        ip.organic_sessions_28d,
        ip.gsc_clicks_28d,

        jsonb_build_object(
            'issue', 'Page receives ' || ip.internal_links_in || ' internal links but returns HTTP ' || ip.status_code,
            'status_code', ip.status_code,
            'internal_links_in', ip.internal_links_in
        ) AS evidence,

        jsonb_build_array(
            jsonb_build_object(
                'owner', 'engineering',
                'task', 'Fix broken target or update linking pages to point to correct URL',
                'acceptance_criteria', 'All internal links resolve to HTTP 200 pages'
            )
        ) AS work_required,

        'high' AS impact  -- broken links to important pages are always high

    FROM important_pages ip
    WHERE ip.status_code >= 400
      AND ip.internal_links_in > 2
),

-- ────────────────────────────────────────────
-- Issue 5: Important orphan/deep pages
-- (crawl_depth > 5 OR internal_links_in = 0)
-- ────────────────────────────────────────────
orphan_deep AS (
    SELECT
        CASE
            WHEN ip.internal_links_in = 0 THEN 'orphan_page'
            ELSE 'deep_page'
        END AS issue_type,
        'technical_fix' AS action_type,
        ip.site_id,
        ip.url AS target_url,
        ip.page_type,
        ip.template,
        ip.status_code,
        ip.organic_sessions_28d,
        ip.gsc_clicks_28d,

        jsonb_build_object(
            'issue', CASE
                WHEN ip.internal_links_in = 0 THEN 'Page has zero internal links (orphan)'
                ELSE 'Page is ' || ip.crawl_depth || ' clicks from homepage'
            END,
            'crawl_depth', ip.crawl_depth,
            'internal_links_in', ip.internal_links_in,
            'product_count', ip.product_count
        ) AS evidence,

        jsonb_build_array(
            jsonb_build_object(
                'owner', 'engineering',
                'task', 'Add internal links from relevant pages or adjust site architecture',
                'acceptance_criteria', 'Page is reachable within 3 clicks from homepage'
            )
        ) AS work_required,

        CASE
            WHEN ip.product_count > 0 OR ip.page_type IN ('collection', 'product') THEN 'high'
            ELSE 'medium'
        END AS impact

    FROM important_pages ip
    WHERE (ip.crawl_depth > 5 OR ip.internal_links_in = 0)
      AND ip.status_code = 200
      AND ip.page_type IN ('collection', 'product', 'landing', 'category', 'blog')
),

-- ────────────────────────────────────────────
-- Issue 6: Sitemap/indexability disagreement
-- (page is indexable but not in sitemap, or vice versa)
-- Note: We detect this by checking indexable status vs presence
-- ────────────────────────────────────────────
sitemap_index_mismatch AS (
    SELECT
        'sitemap_index_mismatch' AS issue_type,
        'technical_fix' AS action_type,
        ip.site_id,
        ip.url AS target_url,
        ip.page_type,
        ip.template,
        ip.status_code,
        ip.organic_sessions_28d,
        ip.gsc_clicks_28d,

        jsonb_build_object(
            'issue', 'Page is not indexable but should be (has organic visibility)',
            'indexable', ip.indexable,
            'gsc_clicks_28d', ip.gsc_clicks_28d
        ) AS evidence,

        jsonb_build_array(
            jsonb_build_object(
                'owner', 'engineering',
                'task', 'Add page to sitemap and resolve indexability issue',
                'acceptance_criteria', 'Page is in sitemap.xml and returns indexable status'
            )
        ) AS work_required,

        CASE WHEN ip.gsc_clicks_28d > 100 THEN 'high' ELSE 'medium' END AS impact

    FROM important_pages ip
    WHERE ip.indexable = false
      AND ip.status_code = 200
      AND ip.gsc_clicks_28d > 0  -- Google is clicking it but it's not indexable
),

-- ────────────────────────────────────────────
-- Issue 7: Duplicate faceted URLs being indexed
-- (multiple URLs with same page_type and similar content)
-- ────────────────────────────────────────────
duplicate_faceted AS (
    SELECT
        'duplicate_faceted' AS issue_type,
        'technical_fix' AS action_type,
        ip.site_id,
        ip.url AS target_url,
        ip.page_type,
        ip.template,
        ip.status_code,
        ip.organic_sessions_28d,
        NULL::INT AS gsc_clicks_28d,

        jsonb_build_object(
            'issue', 'Potential duplicate faceted URL — similar content indexed at multiple URLs',
            'page_url', ip.url,
            'status_code', ip.status_code
        ) AS evidence,

        jsonb_build_array(
            jsonb_build_object(
                'owner', 'engineering',
                'task', 'Implement canonical, noindex, or robots directive for faceted duplicate',
                'acceptance_criteria', 'Only canonical version is indexable'
            )
        ) AS work_required,

        'medium' AS impact

    FROM important_pages ip
    WHERE ip.url LIKE '%?%'  -- has query parameters
      AND ip.status_code = 200
      AND ip.indexable = true
      AND ip.page_type = 'collection'
),

-- ────────────────────────────────────────────
-- Issue 8: Structured-data failure on commercial templates
-- ────────────────────────────────────────────
structured_data_failure AS (
    SELECT
        'structured_data_failure' AS issue_type,
        'technical_fix' AS action_type,
        ip.site_id,
        ip.url AS target_url,
        ip.page_type,
        ip.template,
        ip.status_code,
        ip.organic_sessions_28d,
        NULL::INT AS gsc_clicks_28d,

        jsonb_build_object(
            'issue', 'Commercial page missing structured data',
            'has_structured_data', ip.has_structured_data,
            'page_type', ip.page_type,
            'template', ip.template
        ) AS evidence,

        jsonb_build_array(
            jsonb_build_object(
                'owner', 'engineering',
                'task', 'Add appropriate structured data (Product, CollectionPage, BreadcrumbList)',
                'acceptance_criteria', 'Page passes Rich Results Test and has valid JSON-LD'
            )
        ) AS work_required,

        CASE WHEN ip.page_type = 'product' THEN 'high' ELSE 'medium' END AS impact

    FROM important_pages ip
    WHERE ip.has_structured_data = false
      AND ip.page_type IN ('product', 'collection')
      AND ip.status_code = 200
      AND ip.indexable = true
),

-- ────────────────────────────────────────────
-- Issue 9: JavaScript rendering failure
-- (client-side rendered page whose rendered output is missing/stale vs raw HTML)
-- Detected from crawl data: render_status = 'render_failed', or both hashes
-- present but rendered output is empty/missing key content.
-- ────────────────────────────────────────────
js_rendering_failure AS (
    SELECT
        'js_rendering_failure' AS issue_type,
        'technical_fix' AS action_type,
        ip.site_id,
        ip.url AS target_url,
        ip.page_type,
        ip.template,
        ip.status_code,
        ip.organic_sessions_28d,
        ip.gsc_clicks_28d,

        jsonb_build_object(
            'issue', 'Page loads content via JavaScript but rendering is failing or stale',
            'render_status', ip.render_status,
            'raw_html_hash_present', ip.raw_html_hash IS NOT NULL,
            'rendered_html_hash_present', ip.rendered_html_hash IS NOT NULL,
            'rendered_matches_raw', ip.rendered_html_hash IS NOT NULL AND ip.rendered_html_hash = ip.raw_html_hash,
            'gsc_clicks_28d', ip.gsc_clicks_28d
        ) AS evidence,

        jsonb_build_array(
            jsonb_build_object(
                'owner', 'engineering',
                'task', 'Fix JS rendering (SSR/SSG or pre-render critical content); verify rendered HTML contains body content',
                'acceptance_criteria', 'rendered_html_hash differs from raw_html_hash with body content present; page render_status = js_rendered; Google URL Inspection renders page'
            )
        ) AS work_required,

        CASE
            WHEN ip.gsc_clicks_28d > 50 OR ip.organic_sessions_28d > 0 THEN 'high'
            ELSE 'medium'
        END AS impact

    FROM important_pages ip
    WHERE ip.status_code = 200
      AND ip.indexable = true
      AND (
          ip.render_status = 'render_failed'
          OR (
              ip.raw_html_hash IS NOT NULL          -- raw has content we expect to render
              AND ip.rendered_html_hash IS NOT NULL
              AND ip.rendered_html_hash = ip.raw_html_hash   -- rendered == raw: JS body never executed
          )
      )
),

-- ────────────────────────────────────────────
-- Combine all technical issues
-- ────────────────────────────────────────────
all_issues AS (
    SELECT * FROM not_indexable
    UNION ALL
    SELECT * FROM canonical_conflicts
    UNION ALL
    SELECT * FROM status_failures
    UNION ALL
    SELECT * FROM broken_links
    UNION ALL
    SELECT * FROM orphan_deep
    UNION ALL
    SELECT * FROM sitemap_index_mismatch
    UNION ALL
    SELECT * FROM duplicate_faceted
    UNION ALL
    SELECT * FROM structured_data_failure
    UNION ALL
    SELECT * FROM js_rendering_failure
)

SELECT
    'technical_fix' AS generator,
    action_type,
    site_id,
    target_url,
    NULL AS cluster_id,
    issue_type AS primary_keyword,  -- reuse field for grouping
    page_type,
    template,
    status_code,
    organic_sessions_28d,
    gsc_clicks_28d,
    evidence,
    work_required,
    impact,
    -- Confidence is high for technical issues (we can verify them)
    'high' AS confidence,
    'hours' AS effort,
    'engineering' AS owner
FROM all_issues
-- Exclude if already recommended
WHERE NOT EXISTS (
    SELECT 1 FROM recommendations r
    WHERE r.target_url = all_issues.target_url
      AND r.generator = 'technical_fix'
      AND r.status IN ('raw', 'proposed', 'approved', 'in_progress')
)
ORDER BY
    CASE impact
        WHEN 'high' THEN 1
        WHEN 'medium' THEN 2
        WHEN 'low' THEN 3
    END,
    organic_sessions_28d DESC,
    gsc_clicks_28d DESC
LIMIT 15;

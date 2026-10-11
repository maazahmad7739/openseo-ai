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
           p.robots_allowed, p.in_sitemap,
           p.last_crawled_at,
           -- plan/32: needed to prove a crawl actually returned a document.
           -- A 403/404/500 body is real HTML; without its presence AND size
           -- we cannot tell "page has no schema" from "we never loaded it".
           p.body_html,

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
            'robots_allowed', ip.robots_allowed,
            'in_sitemap', ip.in_sitemap,
            'organic_sessions_28d', ip.organic_sessions_28d,
            'gsc_clicks_28d', ip.gsc_clicks_28d,
            -- plan/21 §2.3 generator depth-upgrade: the canonical-vs-robots-vs-
            -- status root-cause chain (plan/16) is derivable from columns this
            -- query already joins — emit the hypothesis deterministically so
            -- Phase 3 fix routing never re-derives it from raw fields.
            'mechanism_hypothesis', CASE
                WHEN ip.canonical_url IS NOT NULL
                     AND ip.canonical_url <> ip.url
                    THEN 'canonical_block'
                WHEN COALESCE(ip.robots_allowed, true) = false
                    THEN 'robots_block'
                WHEN ip.status_code <> 200
                    THEN 'status_block'
                ELSE 'publish_state'
            END
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
            'canonical', ip.canonical_url,
            'robots_allowed', ip.robots_allowed,
            'in_sitemap', ip.in_sitemap,
            'gsc_clicks_28d', ip.gsc_clicks_28d,
            'mechanism_hypothesis', CASE
                WHEN ip.canonical_url IS NOT NULL
                     AND ip.canonical_url <> ip.url
                    THEN 'canonical_block'
                WHEN COALESCE(ip.robots_allowed, true) = false
                    THEN 'robots_block'
                ELSE 'publish_state'
            END
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
--
-- plan/32: grouped by TEMPLATE, not by URL.
--
-- A missing JSON-LD block is a template defect, not a per-page defect: the
-- same layout is rendered by the same Liquid/Next template on every page of
-- that type, so fixing one URL leaves every sibling page broken. Emitting
-- one recommendation per page produced 18 indistinguishable rows for a
-- single JSON-LD gap on kineticgrowth.co.in, which then had to be triaged
-- by hand 18 times for one fix.
--
-- So: one recommendation per template, with the page count and a sample of
-- affected URLs in the evidence, and ONE fix expanded across the whole
-- group at generation time (fixes/generator.py). `target_url` carries a
-- real, deterministic member URL (highest clicks, tie-broken by URL) —
-- it is the recommendation's natural key, so it must be a real page, never
-- a synthetic "template://" pseudo-URL that matches no row in `pages`.
-- ────────────────────────────────────────────

-- Crawl-evidence floor. Below this byte count the response is an error
-- page, an interstitial, or a bare shell — not a rendered document we can
-- judge. Live data showed rows with 0-byte and 47-byte bodies being scored
-- as "no structured data", which is reporting absence on a page that was
-- never actually loaded.
crawl_document_floor AS (
    SELECT 500::INT AS min_body_bytes
),

-- Pages whose crawl genuinely produced a readable document. Every one of
-- these guards is load-bearing; dropping any one re-admits a false positive:
--   status_code = 200      → an error page's body has no schema, by design
--   body_html IS NOT NULL  → never fetched at all
--   length(body_html)      → fetched, but got a stub / redirect / error body
--   last_crawled_at        → ingest upsert that set no crawl timestamp
--   indexable = true       → non-indexable pages are not a priority defect
schema_evidence_pages AS (
    SELECT ip.*
    FROM important_pages ip
    CROSS JOIN crawl_document_floor f
    WHERE ip.has_structured_data = false
      AND ip.page_type IN ('product', 'collection')
      AND ip.status_code = 200
      AND ip.indexable = true
      AND ip.last_crawled_at IS NOT NULL
      AND ip.body_html IS NOT NULL
      AND length(ip.body_html) >= f.min_body_bytes
),

-- One row per (site, template). Grouping includes site_id because template
-- names are only unique within a site — 'product.liquid' exists on every
-- Shopify store — and a recommendation is always per site.
--
-- urls_ordered is sorted by traffic then URL so that element [1] is a
-- stable representative: the same template always yields the same
-- target_url across runs, which matters because that value is the
-- generator's dedup key and would otherwise churn the natural key.
schema_template_groups AS (
    SELECT
        ip.site_id,
        ip.template,
        count(*)::INT AS page_count,
        array_agg(ip.url ORDER BY ip.gsc_clicks_28d DESC NULLS LAST, ip.url ASC)
            AS urls_ordered,
        bool_or(ip.page_type = 'product') AS has_product,
        COALESCE(sum(ip.gsc_clicks_28d), 0)::BIGINT AS total_clicks,
        COALESCE(max(ip.organic_sessions_28d), 0)::BIGINT AS peak_sessions
    FROM schema_evidence_pages ip
    WHERE ip.template IS NOT NULL
    GROUP BY ip.site_id, ip.template
),

structured_data_failure AS (
    -- (a) Template with a known identity → one grouped recommendation.
    SELECT
        'structured_data_failure' AS issue_type,
        'technical_fix' AS action_type,
        g.site_id,
        g.urls_ordered[1] AS target_url,
        CASE WHEN g.has_product THEN 'product' ELSE 'collection' END AS page_type,
        g.template,
        200::INT AS status_code,
        g.peak_sessions AS organic_sessions_28d,
        g.total_clicks AS gsc_clicks_28d,

        jsonb_build_object(
            'issue_type', 'structured_data_failure',
            'issue', 'Commercial template renders without structured data',
            'has_structured_data', false,
            'scope', 'template',
            'template', g.template,
            'template_inferred', g.template LIKE 'urlarchetype:%',
            'page_count', g.page_count,
            'sample_urls', g.urls_ordered[1:5],
            'representative_url', g.urls_ordered[1],
            'page_type', CASE WHEN g.has_product THEN 'product' ELSE 'collection' END
        ) AS evidence,

        jsonb_build_array(
            jsonb_build_object(
                'owner', 'engineering',
                'task', 'Add appropriate structured data (Product, CollectionPage, BreadcrumbList) to the template layout, not to individual pages',
                'acceptance_criteria', 'All pages on this template pass the Rich Results Test and carry valid JSON-LD'
            )
        ) AS work_required,

        CASE WHEN g.has_product THEN 'high' ELSE 'medium' END AS impact

    FROM schema_template_groups g

    UNION ALL

    -- (b) Template unknown → fall back to one row per URL. Grouping pages
    -- together on a NULL template would merge unrelated layouts into a
    -- single bogus finding, and generating one fix from a synthetic target
    -- would rewrite the wrong pages. Per-URL is the honest fallback: it
    -- reproduces the old behaviour only where the old behaviour was valid.
    SELECT
        'structured_data_failure' AS issue_type,
        'technical_fix' AS action_type,
        sp.site_id,
        sp.url AS target_url,
        sp.page_type,
        NULL::TEXT AS template,
        sp.status_code,
        sp.organic_sessions_28d,
        sp.gsc_clicks_28d,

        jsonb_build_object(
            'issue_type', 'structured_data_failure',
            'issue', 'Commercial page missing structured data',
            'has_structured_data', false,
            'scope', 'url',
            'template_known', false,
            'page_count', 1,
            'sample_urls', jsonb_build_array(sp.url),
            'representative_url', sp.url,
            'page_type', sp.page_type
        ) AS evidence,

        jsonb_build_array(
            jsonb_build_object(
                'owner', 'engineering',
                'task', 'Add appropriate structured data (Product, CollectionPage, BreadcrumbList)',
                'acceptance_criteria', 'Page passes Rich Results Test and has valid JSON-LD'
            )
        ) AS work_required,

        CASE WHEN sp.page_type = 'product' THEN 'high' ELSE 'medium' END AS impact

    FROM schema_evidence_pages sp
    WHERE sp.template IS NULL
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

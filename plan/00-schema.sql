-- ============================================================
-- OpenSEO++ MVP — PostgreSQL Schema
-- ============================================================

-- Site-level configuration and thresholds.
-- Threshold columns are NULL-able: a NULL means "use the tier default"
-- from threshold_tiers (small / medium / large catalogue tiers).
CREATE TABLE site_config (
    site_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_name           TEXT NOT NULL,
    domain              TEXT NOT NULL,                -- e.g. "example.com"
    gsc_property        TEXT NOT NULL,                -- e.g. "sc-domain:example.com"
    shopify_domain      TEXT,                         -- e.g. "example.myshopify.com"
    ga4_property_id     TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),

    -- Catalogue-size tier -> resolves default thresholds via threshold_tiers
    catalogue_size_tier     TEXT DEFAULT 'medium'
                            CHECK (catalogue_size_tier IN ('small','medium','large')),

    -- Per-site threshold OVERRIDES. NULL = use tier default from threshold_tiers.
    min_search_volume       INT,
    min_in_stock_products   INT,
    page_match_threshold    NUMERIC(3,2),
    min_impressions_7d      INT,
    ctr_drop_threshold      NUMERIC(5,2),            -- 25%
    position_drop_threshold INT,
    position_range_min      INT,                     -- Generator 2 lower position bound
    position_range_max      INT,                     -- Generator 2 upper position bound
    catalogue_match_min     NUMERIC(3,2),            -- Generator 2 minimum catalogue-match score

    -- "In stock" definition at the variant level (matters for multi-variant products).
    --   any_variant      = product is in stock if >=1 variant has available inventory
    --   majority_variants= product is in stock only if >=50% of variants have inventory
    in_stock_definition     TEXT DEFAULT 'any_variant'
                            CHECK (in_stock_definition IN ('any_variant','majority_variants')),

    -- Agent cost/latency bounds (see Priority 4.1)
    agent_prefetch_limit    INT DEFAULT 25,          -- fetch SERP/candidate context only for top N
    agent_min_valid         INT DEFAULT 5,           -- refill from backup pool if fewer than this pass
    agent_backup_pool       INT DEFAULT 20,          -- candidates kept as backup after the top slice

    -- Rejection-learning context (see Priority 4.3)
    rejection_context_count INT DEFAULT 5,           -- last N rejection reasons injected into agent prompt

    -- OpenSEO is NOT per-brand: it is ONE shared research tool authenticated
    -- as a single company-level account (like Ahrefs/SEMrush). No OpenSEO
    -- credential or capability-list columns live here — the single global
    -- credential + capability list live in system_config (defined after this
    -- table) and are resolved once by get_openseo_adapter(). Only GSC stays
    -- per-site (gsc_property above). See 09-architecture-summary.md OpenSEO
    -- section + assumption flag; full contract in 11-openseo-connector-schema.md.

    brand_queries           TEXT[] DEFAULT '{}'      -- excluded query patterns
);

-- ────────────────────────────────────────────
-- SYSTEM CONFIG — one row per system-global setting.
-- OpenSEO is a single SHARED account for the whole platform, reused for every
-- site's fetch() calls. Only GSC is per-site (site_config.gsc_property).
-- ────────────────────────────────────────────
CREATE TABLE system_config (
    config_key         TEXT PRIMARY KEY,
    config_value       TEXT NOT NULL,
    updated_at         TIMESTAMPTZ DEFAULT now()
);

-- Expected config rows (provisioned at deployment — see
-- 12-migration-openseo-secrets.sql for the secret_ref row):
--   'openseo.base_url'     → REST API base URL (non-sensitive)
--   'openseo.secret_ref'   → reference/ID of the credential in the secrets
--                            store (AWS Secrets Manager / Vault / equiv) —
--                            NEVER a credential value; resolved by
--                            get_openseo_adapter() at load time only.
--   'openseo.capabilities' → explicit capability list the shared account
--                            provides, e.g. '["keyword_volume","serp",...]'.
-- (No seed INSERT: values are deployment/environment-specific.)


-- ────────────────────────────────────────────
-- THRESHOLD TIERS
-- Catalogue-size tiered defaults so fixed universal thresholds are avoided:
-- small catalogues use a lower page-match bar, large catalogues a higher one.
--   small   = <500 URLs        medium = 500-5,000 URLs   large = >5,000 URLs
-- ────────────────────────────────────────────
CREATE TABLE threshold_tiers (
    tier                    TEXT PRIMARY KEY CHECK (tier IN ('small','medium','large')),
    min_search_volume       INT NOT NULL,
    min_in_stock_products   INT NOT NULL,
    page_match_threshold    NUMERIC(3,2) NOT NULL,
    min_impressions_7d      INT NOT NULL,
    ctr_drop_threshold      NUMERIC(5,2) NOT NULL,
    position_drop_threshold INT NOT NULL,
    position_range_min      INT NOT NULL,
    position_range_max      INT NOT NULL,
    catalogue_match_min     NUMERIC(3,2) NOT NULL,
    updated_at              TIMESTAMPTZ DEFAULT now()
);

INSERT INTO threshold_tiers
    (tier, min_search_volume, min_in_stock_products, page_match_threshold,
     min_impressions_7d, ctr_drop_threshold, position_drop_threshold,
     position_range_min, position_range_max, catalogue_match_min)
VALUES
    -- Small: catalogue is thin, relax the match bar so real gaps surface
    ('small',  100,  6, 0.60, 30, 0.25, 3, 4, 20, 0.45),
    -- Medium: default balance
    ('medium', 100,  8, 0.65, 50, 0.25, 3, 4, 20, 0.50),
    -- Large: catalogue is deep, raise the bar so only genuine gaps pass
    ('large',  150, 10, 0.70, 80, 0.20, 3, 4, 20, 0.55);


-- ────────────────────────────────────────────
-- MEASUREMENT WINDOW LOOKUP
-- Per action_type measurement window in days (spec: no fixed 28-day constant).
--   create_page  ≈ 49 days  (Google discovery + crawl + ranking takes longer)
--   improve_page ≈ 28 days  (existing page, faster signal)
--   consolidate  ≈ 28 days
--   technical_fix≈ 21 days  (robots/status fixes surface quickly)
-- ────────────────────────────────────────────
CREATE TABLE measurement_window_lookup (
    action_type             TEXT PRIMARY KEY,
    measurement_window_days INT NOT NULL,
    rationale               TEXT
);

INSERT INTO measurement_window_lookup (action_type, measurement_window_days, rationale) VALUES
    ('create_page',    49, 'New content needs discovery + crawl time before it can rank'),
    ('improve_page',   28, 'Existing page tweaks surface movement faster'),
    ('consolidate',    28, 'Redirect authority transfer + re-crawl'),
    ('technical_fix',  21, 'Indexability/status fixes surface quickly');


-- ────────────────────────────────────────────
-- PAGES TABLE
-- Unified representation of every URL the system knows about
-- ────────────────────────────────────────────
CREATE TABLE pages (
    page_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id         UUID REFERENCES site_config(site_id),
    url             TEXT NOT NULL,
    url_hash        TEXT GENERATED ALWAYS AS (md5(url)) STORED,
    page_type       TEXT NOT NULL DEFAULT 'unknown',  -- collection, product, blog, landing, category, etc.
    template        TEXT,                              -- shopify template name or detected template
    title           TEXT,
    h1              TEXT,
    canonical_url   TEXT,
    indexable        BOOLEAN DEFAULT true,
    status_code     INT,
    crawl_depth     INT,
    internal_links_in  INT DEFAULT 0,
    internal_links_out INT DEFAULT 0,
    product_count   INT DEFAULT 0,                    -- products listed on this page
    is_orphan       BOOLEAN DEFAULT false,
    has_structured_data BOOLEAN DEFAULT false,

    -- JS-rendering signal (populated by OpenSEO crawl/audit — see Priority 2.1 / 4.2)
    -- A render FAILURE = page loads JS to render content but the rendered output
    -- is missing/empty vs the raw HTML's expected content.
    render_status       TEXT DEFAULT 'not_rendered'
                        CHECK (render_status IN ('server_rendered','js_rendered','render_failed','not_rendered')),
    raw_html_hash       TEXT,                          -- hash of un-rendered HTML
    rendered_html_hash  TEXT,                          -- hash of fully-rendered HTML

    last_crawled_at TIMESTAMPTZ,

    UNIQUE (site_id, url_hash)
);

CREATE INDEX idx_pages_site_type ON pages (site_id, page_type);
CREATE INDEX idx_pages_url_hash ON pages (url_hash);


-- ────────────────────────────────────────────
-- SEARCH PERFORMANCE TABLE
-- Daily GSC query-level data
-- ────────────────────────────────────────────
CREATE TABLE search_performance (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id         UUID REFERENCES site_config(site_id),
    date            DATE NOT NULL,
    query           TEXT NOT NULL,
    page_url        TEXT NOT NULL,
    page_url_hash   TEXT GENERATED ALWAYS AS (md5(page_url)) STORED,
    country         TEXT DEFAULT 'all',
    device          TEXT DEFAULT 'all',
    clicks          INT DEFAULT 0,
    impressions     INT DEFAULT 0,
    ctr             NUMERIC(7,4) DEFAULT 0,           -- stored as decimal, e.g. 0.0342 = 3.42%
    position        NUMERIC(6,2),

    UNIQUE (site_id, date, query, page_url_hash, country, device)
);

CREATE INDEX idx_sp_site_date ON search_performance (site_id, date);
CREATE INDEX idx_sp_query ON search_performance (site_id, query);
CREATE INDEX idx_sp_page ON search_performance (site_id, page_url_hash);


-- ────────────────────────────────────────────
-- KEYWORD CLUSTERS TABLE
-- Grouped queries by semantic/intent similarity
-- ────────────────────────────────────────────
CREATE TABLE keyword_clusters (
    cluster_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id                 UUID REFERENCES site_config(site_id),
    primary_keyword         TEXT NOT NULL,
    keywords                TEXT[] NOT NULL DEFAULT '{}',   -- all queries in cluster
    intent                  TEXT NOT NULL DEFAULT 'unknown', -- informational, commercial, transactional, navigational
    commercial_intent       BOOLEAN DEFAULT false,
    recommended_page_type   TEXT,                            -- collection, product, landing, article
    search_volume           INT DEFAULT 0,                   -- aggregate across cluster; populated by OpenSEO connector (Priority 2.1), never by a GSC-derived job
    commercial_value        TEXT DEFAULT 'medium',           -- high, medium, low
    created_at              TIMESTAMPTZ DEFAULT now(),
    updated_at              TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_kc_site_intent ON keyword_clusters (site_id, intent);
CREATE INDEX idx_kc_commercial ON keyword_clusters (site_id, commercial_intent) WHERE commercial_intent = true;


-- ────────────────────────────────────────────
-- OPENSEO SERP SNAPSHOTS
-- Live SERP + competitor ranking data pulled from the OpenSEO connector
-- (NOT GSC — GSC never returns rows for properties you don't own).
-- Used by Generator 1 competitor signals and by the agent's live-SERP validation.
-- ────────────────────────────────────────────
CREATE TABLE openseo_serp_snapshots (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id         UUID REFERENCES site_config(site_id),
    cluster_id      UUID REFERENCES keyword_clusters(cluster_id),
    query           TEXT NOT NULL,
    result_url      TEXT NOT NULL,
    result_domain   TEXT NOT NULL,
    position        INT NOT NULL,
    is_self         BOOLEAN DEFAULT false,           -- true when result_url is on our own domain
    -- Competitor SERP copy (Phase 2 grounding, plan/21 §2.1/§2.2): persisted
    -- verbatim from the connector's organic items; NULL when the provider
    -- omitted the field. Drafters read these for framing/positioning only —
    -- never copy competitor text verbatim into a draft (validator + dedupe
    -- guards enforce that).
    result_title    TEXT,
    result_snippet  TEXT,
    url_pattern     TEXT,                            -- URL archetype: /collections/|/products/|/blog/|/guides/...
    snapshot_date   DATE NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),

    UNIQUE (site_id, query, result_url, snapshot_date)
);

CREATE INDEX idx_oss_cluster_date ON openseo_serp_snapshots (site_id, cluster_id, snapshot_date);
CREATE INDEX idx_oss_query_date ON openseo_serp_snapshots (site_id, query, snapshot_date);


-- ────────────────────────────────────────────
-- CLUSTER-QUERY LINK
-- Maps individual GSC queries to clusters
-- ────────────────────────────────────────────
CREATE TABLE cluster_queries (
    cluster_id  UUID REFERENCES keyword_clusters(cluster_id) ON DELETE CASCADE,
    query       TEXT NOT NULL,
    PRIMARY KEY (cluster_id, query)
);


-- ────────────────────────────────────────────
-- CATALOGUE COVERAGE TABLE
-- Per-cluster product coverage
-- ────────────────────────────────────────────
CREATE TABLE catalogue_coverage (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_id              UUID REFERENCES keyword_clusters(cluster_id) ON DELETE CASCADE,
    site_id                 UUID REFERENCES site_config(site_id),
    matching_product_ids    TEXT[] DEFAULT '{}',         -- shopify product IDs
    matching_product_count  INT DEFAULT 0,
    in_stock_product_count  INT DEFAULT 0,
    average_price           NUMERIC(10,2),
    average_margin          NUMERIC(5,2),
    existing_collection_url TEXT,                        -- if a matching collection already exists

    UNIQUE (site_id, cluster_id)
);

CREATE INDEX idx_cc_cluster ON catalogue_coverage (cluster_id);


-- ────────────────────────────────────────────
-- PAGE BUSINESS PERFORMANCE TABLE
-- GA4-derived organic metrics per page per day
-- ────────────────────────────────────────────
CREATE TABLE page_business_performance (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id         UUID REFERENCES site_config(site_id),
    date            DATE NOT NULL,
    page_url        TEXT NOT NULL,
    page_url_hash   TEXT GENERATED ALWAYS AS (md5(page_url)) STORED,
    organic_sessions    INT DEFAULT 0,
    engaged_sessions    INT DEFAULT 0,
    add_to_carts        INT DEFAULT 0,
    checkouts           INT DEFAULT 0,
    orders              INT DEFAULT 0,
    revenue             NUMERIC(12,2) DEFAULT 0,
    conversion_rate     NUMERIC(7,4) DEFAULT 0,

    UNIQUE (site_id, date, page_url_hash)
);

CREATE INDEX idx_pbp_site_date ON page_business_performance (site_id, date);
CREATE INDEX idx_pbp_page ON page_business_performance (site_id, page_url_hash);


-- ────────────────────────────────────────────
-- RECOMMENDATIONS TABLE
-- Agent output + workflow state
-- ────────────────────────────────────────────
CREATE TABLE recommendations (
    recommendation_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id             UUID REFERENCES site_config(site_id),
    generator           TEXT NOT NULL,                    -- missing_page, existing_opportunity, technical_fix, cannibalization
    action_type         TEXT NOT NULL,                    -- create_page, improve_page, consolidate, technical_fix
    target_url          TEXT,                              -- existing page URL (if applicable)
    proposed_url        TEXT,                              -- new URL (if create_page)
    cluster_id          UUID REFERENCES keyword_clusters(cluster_id),
    diagnosis           TEXT NOT NULL,

    -- Structured evidence and work
    evidence_json       JSONB NOT NULL DEFAULT '[]',      -- [{source, finding}]
    work_required_json  JSONB NOT NULL DEFAULT '[]',      -- [{owner, task, acceptance_criteria}]

    impact              TEXT NOT NULL DEFAULT 'medium',   -- high, medium, low
    confidence          TEXT NOT NULL DEFAULT 'medium',
    effort              TEXT NOT NULL DEFAULT 'days',      -- hours, days
    owner               TEXT NOT NULL DEFAULT 'SEO',       -- content, engineering, SEO
    status              TEXT NOT NULL DEFAULT 'proposed',  -- proposed, approved, in_progress, live, measured
    assigned_to         TEXT,

-- Measurement
    measurement_metric      TEXT,
    measurement_window_days INT,                        -- resolved from measurement_window_lookup by action_type (no fixed 28-day constant)
    measurement_due_at      TIMESTAMPTZ,
    result                  TEXT DEFAULT 'pending',     -- pending, won, neutral, lost, inconclusive (won/lost require significance test — see Skill 3)

    -- Timestamps
    created_at          TIMESTAMPTZ DEFAULT now(),
    approved_at         TIMESTAMPTZ,
    implemented_at      TIMESTAMPTZ,
    rejected_at         TIMESTAMPTZ,
    rejection_reason    TEXT,
    measured_at         TIMESTAMPTZ
);

CREATE INDEX idx_rec_site_status ON recommendations (site_id, status);
CREATE INDEX idx_rec_cluster ON recommendations (cluster_id);
CREATE INDEX idx_rec_generator ON recommendations (site_id, generator);


-- ────────────────────────────────────────────
-- CHANGE LOG TABLE
-- Before/after snapshots for measurement
-- ────────────────────────────────────────────
CREATE TABLE change_log (
    change_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id   UUID REFERENCES recommendations(recommendation_id),
    url                 TEXT NOT NULL,
    before_snapshot     JSONB,    -- {position, ctr, impressions, clicks, sessions, orders, revenue, ...}
    after_snapshot      JSONB,
    implementation_date DATE,
    implemented_by      TEXT,
    rollback_reference  TEXT,     -- URL or commit to revert
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_cl_rec ON change_log (recommendation_id);


-- ────────────────────────────────────────────
-- MEASUREMENT SNAPSHOTS TABLE
-- Baseline + post-implementation metrics, per action_type window
-- (resolved from measurement_window_lookup — no fixed 28-day constant).
-- comparison_type distinguishes target / control group / YoY.
-- ────────────────────────────────────────────
CREATE TABLE measurement_snapshots (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id   UUID REFERENCES recommendations(recommendation_id),
    snapshot_type       TEXT NOT NULL,                   -- baseline, post_implementation
    comparison_type     TEXT NOT NULL DEFAULT 'target'
                        CHECK (comparison_type IN ('target','control','yoy')),
    -- 'target'  = the page that was changed
    -- 'control' = comparable unaffected pages (same site, same period, same
    --             page type/template) — stored so confounds can be caught
    -- 'yoy'     = same period last year (optional bonus signal only)
    control_group_key   TEXT,                            -- key identifying the comparable set (e.g. page-type+template)
    control_group_json  JSONB,                           -- paired control URLs + per-URL metrics for URL-based
                        -- recommendations: {"urls": [{url, url_hash, metrics}, ...]}.
                        -- Stored on the BASELINE control snapshot; the post-window
                        -- capture reuses the identical set so target-vs-control
                        -- before/after (difference-in-differences) is fair.
                        -- NULL for cluster-level control snapshots and legacy rows.
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    impressions         INT DEFAULT 0,
    avg_position        NUMERIC(6,2),
    ctr                 NUMERIC(7,4),
    clicks              INT DEFAULT 0,
    organic_sessions    INT DEFAULT 0,
    orders              INT DEFAULT 0,
    revenue             NUMERIC(12,2) DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_ms_rec ON measurement_snapshots (recommendation_id);

-- ────────────────────────────────────────────
-- MIGRATION: paired control URLs (Comparable Unaffected Pages).
-- Adds control_group_json for existing deployments (no-op on fresh installs
-- where the CREATE TABLE above already carries the column).
-- ────────────────────────────────────────────
ALTER TABLE measurement_snapshots ADD COLUMN IF NOT EXISTS control_group_json JSONB;


-- ────────────────────────────────────────────
-- PAGE-QUERY MATCH SCORE TABLE
-- Cached match scores for candidate generation
-- ────────────────────────────────────────────
CREATE TABLE page_query_match_scores (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id             UUID REFERENCES site_config(site_id),
    page_id             UUID REFERENCES pages(page_id),
    cluster_id          UUID REFERENCES keyword_clusters(cluster_id),

    -- Component scores (each 0-1).
    -- NOTE (Priority 3.2): title/H1 is NOT a separate top-level weight — it is
    -- folded into semantic_content_score as a sub-signal (title/H1 text is
    -- weighted higher inside the embedding/similarity computation). Keeping it
    -- as an independent weight rewarded keyword-stuffed titles over real content.
    intent_page_type_score  NUMERIC(3,2),   -- weight: 0.30
    catalogue_match_score   NUMERIC(3,2),   -- weight: 0.30 (redeployed 5pts from title/H1)
    gsc_evidence_score      NUMERIC(3,2),   -- weight: 0.25 (redeployed 5pts from title/H1)
    semantic_content_score  NUMERIC(3,2),   -- weight: 0.15 (includes title/H1 sub-signal)

    -- Aggregate
    aggregate_score     NUMERIC(3,2) GENERATED ALWAYS AS (
        (intent_page_type_score * 0.30) +
        (catalogue_match_score * 0.30) +
        (gsc_evidence_score * 0.25) +
        (semantic_content_score * 0.15)
    ) STORED,

    calculated_at       TIMESTAMPTZ DEFAULT now(),

    UNIQUE (page_id, cluster_id)
);

CREATE INDEX idx_pqms_cluster ON page_query_match_scores (cluster_id);
CREATE INDEX idx_pqms_page ON page_query_match_scores (page_id);
CREATE INDEX idx_pqms_score ON page_query_match_scores (site_id, aggregate_score DESC);


-- ────────────────────────────────────────────
-- REJECTION LOG
-- Operator rejections feed back into agent learning (Priority 4.3).
-- The last N rejection reasons per site are injected as few-shot context
-- into the next week's agent prompt — explicit context injection, NOT fine-tuning.
-- ────────────────────────────────────────────
CREATE TABLE rejection_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id         UUID REFERENCES site_config(site_id),
    run_id          TEXT,                              -- weekly run identifier
    candidate_id    UUID,
    generator       TEXT,                              -- missing_page, existing_opportunity, technical_fix, cannibalization
    primary_keyword TEXT,
    target_url      TEXT,
    reason          TEXT NOT NULL,
    rejected_by     TEXT,                              -- operator identifier
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_rj_site_created ON rejection_log (site_id, created_at DESC);

-- Per-site job bookkeeping for candidates (which generator feed a given run consumed)
CREATE TABLE candidate_runs (
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id         UUID REFERENCES site_config(site_id),
    run_at          TIMESTAMPTZ DEFAULT now(),
    generator       TEXT NOT NULL,
    candidate_count INT DEFAULT 0,
    mode            TEXT NOT NULL DEFAULT 'primary' CHECK (mode IN ('primary','backup'))
);

CREATE INDEX idx_cr_site_run ON candidate_runs (site_id, run_at DESC);

-- ============================================================
-- 22 — Stage 2 Phase 0: generated_fixes + fix_policy + ingestion columns
-- ============================================================
-- Plan: plan/21-stage2-fix-execution.md §1 (data model).
-- All objects are IF NOT EXISTS / idempotent-safe: re-running is a no-op.
--
-- What this creates:
--   1. generated_fixes      — the fix lifecycle spine (child of recommendations;
--                             recommendation status machine untouched).
--   2. fix_policy           — per-site risk tiers + weekly caps + kill-switch.
--   3. pages.shopify_gid    — GID persistence (collection IDs are currently
--                             discarded by sync_pages_from_shopify; GraphQL
--                             writes need GIDs, not REST numeric IDs).
--   4. pages ingestion columns for Phase 2+ (meta/body/hashes) — created now
--                             so later phases are pure ingestion code, no DDL.
--   5. page_links + site_backlinks_summary — Phase 5 prerequisites (empty until
--                             crawl_links/backlinks syncs land).
--   6. system_config rows   — shopify.api_version + shopify.publication_id
--                             (publication_id provisioned NULL; cached at
--                             first publishable mutation by the executor).
--   7. fix_policy default seeds — ALL disabled (enabled=false): nothing
--                             executes until the owner-gated scope check
--                             (plan §7.1) passes for the real store.
-- ============================================================

-- ------------------------------------------------------------
-- 1. generated_fixes
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS generated_fixes (
    fix_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id   UUID NOT NULL REFERENCES recommendations(recommendation_id),
    site_id             UUID NOT NULL REFERENCES site_config(site_id),
    action_type         TEXT NOT NULL,        -- mirrors recommendations.action_type
    sub_type            TEXT,                 -- issue_type | 'title'|'meta'|'content'|'redirect'|'link_insert'|...
    target_url          TEXT NOT NULL,
    -- plan/25: collection_create mints the GID at EXECUTE time (the entity
    -- does not exist at generation) — NULL until the executor's
    -- snapshot_patch('collection.created_gid') lands.
    target_entity_ref   TEXT,                 -- Shopify GID (gid://shopify/Product/123)

    -- The fix itself
    payload_json        JSONB NOT NULL,       -- exact GraphQL mutation payload
    diff_json           JSONB,                -- [{field, old_value, new_value}] — console diff panel
    generation_source   TEXT NOT NULL DEFAULT 'agent'
                        CHECK (generation_source IN ('agent','deterministic')),

    -- Approval + execution lifecycle (plan/21 §3)
    status              TEXT NOT NULL DEFAULT 'generated'
                        CHECK (status IN ('generated','approved','queued','applied','failed','reverted','expired')),
    risk_tier           TEXT NOT NULL DEFAULT 'medium'
                        CHECK (risk_tier IN ('low','medium','high','plan_only')),

    -- Rollback
    snapshot_json       JSONB,                -- FULL pre-state from FRESH READ at executor pickup
    rollback_of         UUID REFERENCES generated_fixes(fix_id),

    -- Execution audit
    executed_at         TIMESTAMPTZ,
    executed_by         TEXT DEFAULT 'fix_executor',
    adapter_response    JSONB,                -- {ok, userErrors, throttle, error, detail} — never credentials
    verification_status TEXT
                        CHECK (verification_status IN ('unverified','verified','verify_failed')),
    error_detail        TEXT,

    created_at          TIMESTAMPTZ DEFAULT now(),
    approved_at         TIMESTAMPTZ,
    applied_at          TIMESTAMPTZ,
    reverted_at         TIMESTAMPTZ
);

-- One active fix per (site, target_url, field): two executors can never fight
-- over the same field on the same URL (plan/21 §5.2 second guard).
CREATE UNIQUE INDEX IF NOT EXISTS uq_fixes_active_per_target
    ON generated_fixes (site_id, target_url, COALESCE(sub_type, ''))
    WHERE status IN ('generated','approved','queued');

CREATE INDEX IF NOT EXISTS idx_fixes_site_status ON generated_fixes (site_id, status);
CREATE INDEX IF NOT EXISTS idx_fixes_rec ON generated_fixes (recommendation_id);

-- ------------------------------------------------------------
-- 2. fix_policy
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fix_policy (
    site_id             UUID REFERENCES site_config(site_id),
    sub_type            TEXT NOT NULL,
    risk_tier           TEXT NOT NULL CHECK (risk_tier IN ('low','medium','high','plan_only')),
    weekly_cap          INT NOT NULL DEFAULT 0,   -- 0 = generation allowed, execution disabled
    requires_field_verify BOOLEAN NOT NULL DEFAULT true,
    enabled             BOOLEAN NOT NULL DEFAULT false,   -- master kill-switch per sub_type
    PRIMARY KEY (site_id, sub_type)
);

-- Default seeds per plan/21 §5.1 — inserted for sites lacking rows.
-- All disabled by default; the executor refuses any sub_type whose policy
-- row is missing or enabled=false (fail-closed).
INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, requires_field_verify, enabled)
SELECT sc.site_id, v.sub_type, v.risk_tier, v.weekly_cap, v.requires_field_verify, false
FROM site_config sc
CROSS JOIN (VALUES
    ('seo.title',            'low',       10, true),
    ('seo.description',      'medium',     5, true),
    ('collection_description', 'medium',   3, true),
    ('content',              'medium',     5, true),
    ('product_publish',      'medium',     5, true),
    ('product_publish_product', 'medium',  5, true),
    ('collection_publish',   'medium',     3, true),
    ('product_unpublish',    'medium',     3, true),
    ('collection_unpublish', 'medium',     3, true),
    ('article_body_link',    'medium',     5, true),
    ('redirect',             'high',       2, true),
    ('collection_create',    'high',       2, true),
    ('product_create',       'high',       2, true)
) AS v(sub_type, risk_tier, weekly_cap, requires_field_verify)
WHERE NOT EXISTS (
    SELECT 1 FROM fix_policy fp
    WHERE fp.site_id = sc.site_id AND fp.sub_type = v.sub_type
);

-- ------------------------------------------------------------
-- 3. pages ingestion columns (Phase 2+ prerequisites + GID fix)
-- ------------------------------------------------------------
ALTER TABLE pages ADD COLUMN IF NOT EXISTS meta_description TEXT;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS body_html TEXT;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS body_text_hash TEXT;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS heading_outline JSONB;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS shopify_gid TEXT;
CREATE INDEX IF NOT EXISTS idx_pages_shopify_gid ON pages (site_id, shopify_gid);

-- ------------------------------------------------------------
-- 4. page_links (Phase 5 prerequisite — link-graph edges)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS page_links (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id       UUID REFERENCES site_config(site_id),
    source_url    TEXT NOT NULL,
    source_url_hash TEXT GENERATED ALWAYS AS (md5(source_url)) STORED,
    target_url    TEXT NOT NULL,
    target_url_hash TEXT GENERATED ALWAYS AS (md5(target_url)) STORED,
    anchor_text   TEXT,
    context_snippet TEXT,
    first_seen_at DATE NOT NULL,
    last_seen_at  DATE NOT NULL,
    UNIQUE (source_url_hash, target_url_hash)
);
CREATE INDEX IF NOT EXISTS idx_pl_target ON page_links (site_id, target_url_hash);

-- ------------------------------------------------------------
-- 5. site_backlinks_summary (adapter capability + fixture exist; never synced)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS site_backlinks_summary (
    site_id            UUID REFERENCES site_config(site_id),
    url                TEXT,
    referring_domains  INT,
    rank               NUMERIC(8,2),
    fetched_at         DATE NOT NULL,
    UNIQUE (site_id, url)
);

-- ------------------------------------------------------------
-- 6. system_config rows (plan/21 §1.1)
-- ------------------------------------------------------------
INSERT INTO system_config (config_key, config_value)
VALUES ('shopify.api_version', '2026-01')
ON CONFLICT (config_key) DO NOTHING;
-- shopify.publication_id is provisioned NULL at first executor run
-- (cached once via the publications query); no seed row for it.
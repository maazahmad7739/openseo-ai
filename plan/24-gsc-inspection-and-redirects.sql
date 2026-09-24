-- ============================================================
-- 24 — GSC URL Inspection + Shopify redirect execution support
-- ============================================================
-- Tasks 2 + 3 of the last-mile execution work:
--   1. pages.* Google-verdict columns for the URL Inspection sync
--      (gsc_verdict / coverage / robotsTxtState / canonical / sitemap
--      membership / inspection timestamp).
--   2. redirects table — the execution mirror of Shopify urlRedirects,
--      keyed by the created GID so urlRedirectDelete rollback never has
--      to guess which redirect a fix created.
-- All objects are IF NOT EXISTS / idempotent-safe: re-running is a no-op.
-- ============================================================

-- ------------------------------------------------------------
-- 1. URL Inspection verdict columns (Task 3)
-- ------------------------------------------------------------
ALTER TABLE pages ADD COLUMN IF NOT EXISTS gsc_verdict TEXT;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS gsc_coverage_state TEXT;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS gsc_robots_txt_state TEXT;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS gsc_google_canonical TEXT;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS gsc_in_sitemap BOOLEAN;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS gsc_inspected_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_pages_gsc_verdict ON pages (site_id, gsc_verdict);

-- ------------------------------------------------------------
-- 2. pages: verified sitemap membership + robots allowance (Task 5)
--    NULL in_sitemap = sitemap unfetchable (unknown, never fabricated).
-- ------------------------------------------------------------
ALTER TABLE pages ADD COLUMN IF NOT EXISTS in_sitemap BOOLEAN;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS robots_allowed BOOLEAN;
CREATE INDEX IF NOT EXISTS idx_pages_in_sitemap ON pages (site_id, in_sitemap);

-- ------------------------------------------------------------
-- 3. redirects (Task 2 — rollback + audit mirror)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS redirects (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id         UUID NOT NULL REFERENCES site_config(site_id),
    fix_id          UUID REFERENCES generated_fixes(fix_id),
    redirect_gid    TEXT UNIQUE,             -- gid://shopify/UrlRedirect/...
    source_path     TEXT NOT NULL,
    target_path     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'created'
                    CHECK (status IN ('created','deleted')),
    created_at      TIMESTAMPTZ DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_redirects_site ON redirects (site_id, source_path);

-- ------------------------------------------------------------
-- 3. Generated_fixes: created redirect GID as the revert source.
-- The redirect adapter writes redirect.created_id into snapshot_json
-- (snapshot_patch); a dedicated column makes it queryable for audits.
-- ------------------------------------------------------------
ALTER TABLE generated_fixes ADD COLUMN IF NOT EXISTS created_entity_ref TEXT;

-- ------------------------------------------------------------
-- 3b. Budget + cost accounting for the inspection pull
-- ------------------------------------------------------------
INSERT INTO system_config (config_key, config_value)
VALUES ('gsc.url_inspection_daily_limit', '400')
ON CONFLICT (config_key) DO NOTHING;
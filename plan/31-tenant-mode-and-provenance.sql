-- ============================================================
-- plan/31 — Tenant Isolation & Per-Site Integration Mode
-- ============================================================
-- Implements the audit remediation plan:
--   1. site_config.integration_mode — the PER-SITE execution mode
--      ('mock' | 'sandbox' | 'import' | 'live'). The global
--      INTEGRATION_MODE env remains as the fallback for sites whose
--      column is NULL (legacy deployments), then 'mock'.
--   2. data_source provenance columns — every ingest path stamps
--      'live' | 'mock' | 'import' so mock/live provenance is queryable
--      instead of forensic.
--   3. FK constraints for the previously unscoped site_id tables
--      (api_costs, api_costs_archive) with ON DELETE CASCADE.
--
-- Idempotent: safe to re-run. No data is destroyed.
-- ============================================================

-- ── 1. site_config.integration_mode ─────────────────────────
ALTER TABLE site_config
    ADD COLUMN IF NOT EXISTS integration_mode TEXT;

ALTER TABLE site_config
    DROP CONSTRAINT IF EXISTS site_config_integration_mode_check;
ALTER TABLE site_config
    ADD CONSTRAINT site_config_integration_mode_check
    CHECK (integration_mode IN ('mock', 'sandbox', 'import', 'live'));

-- Backfill: NULL -> 'mock' (the historical global default).
UPDATE site_config
SET integration_mode = 'mock'
WHERE integration_mode IS NULL;

-- Per-site mode assignments (idempotent; keyed on domain):
UPDATE site_config SET integration_mode = 'mock'
WHERE domain = 'example.com';                          -- Aurora-Audio
UPDATE site_config SET integration_mode = 'sandbox'
WHERE domain = 'action-seo-test.myshopify.com';        -- ActionSEO Dev
UPDATE site_config SET integration_mode = 'import'
WHERE domain = 'gethelium.co';                         -- Helium (PDF bridge)
UPDATE site_config SET integration_mode = 'live'
WHERE domain = 'kineticgrowth.co.in';                  -- Kinetic Growth

-- Per-site Shopify write credential (plan/31 §6): per-tenant token column.
-- NULL = fall back to the shared SHOPIFY_ACCESS_TOKEN env (legacy behavior).
ALTER TABLE site_config ADD COLUMN IF NOT EXISTS shopify_access_token TEXT;

-- Per-site DataForSEO keyword targets (plan/31 §5): replaces the hardcoded
-- Aurora headphone keyword list. NULL/empty = keyword/SERP sync steps are
-- skipped cleanly for that site.
ALTER TABLE site_config ADD COLUMN IF NOT EXISTS target_keywords TEXT[];

-- ── 2. data_source provenance columns ───────────────────────
-- 'live' = real API call; 'mock' = offline fixture; 'import' = operator
-- file import (GSC PDF/CSV zip). NULL = written before this migration
-- (provenance unknown — historical rows are left untouched).

ALTER TABLE pages             ADD COLUMN IF NOT EXISTS data_source TEXT;
ALTER TABLE recommendations   ADD COLUMN IF NOT EXISTS data_source TEXT;
ALTER TABLE search_performance ADD COLUMN IF NOT EXISTS data_source TEXT;
ALTER TABLE generated_fixes   ADD COLUMN IF NOT EXISTS data_source TEXT;
-- GA4 page_performance upsert stamps provenance too (sync_page_business_
-- performance carries data_source); the column was missing from the
-- original plan/31 list — sync crashed with UndefinedColumn on a fresh DB.
ALTER TABLE page_business_performance ADD COLUMN IF NOT EXISTS data_source TEXT;

ALTER TABLE pages
    DROP CONSTRAINT IF EXISTS pages_data_source_check;
ALTER TABLE pages
    ADD CONSTRAINT pages_data_source_check
    CHECK (data_source IN ('live', 'mock', 'import'));

ALTER TABLE recommendations
    DROP CONSTRAINT IF EXISTS recommendations_data_source_check;
ALTER TABLE recommendations
    ADD CONSTRAINT recommendations_data_source_check
    CHECK (data_source IN ('live', 'mock', 'import'));

ALTER TABLE search_performance
    DROP CONSTRAINT IF EXISTS search_performance_data_source_check;
ALTER TABLE search_performance
    ADD CONSTRAINT search_performance_data_source_check
    CHECK (data_source IN ('live', 'mock', 'import'));

ALTER TABLE generated_fixes
    DROP CONSTRAINT IF EXISTS generated_fixes_data_source_check;
ALTER TABLE generated_fixes
    ADD CONSTRAINT generated_fixes_data_source_check
    CHECK (data_source IN ('live', 'mock', 'import'));

ALTER TABLE page_business_performance
    DROP CONSTRAINT IF EXISTS page_business_performance_data_source_check;
ALTER TABLE page_business_performance
    ADD CONSTRAINT page_business_performance_data_source_check
    CHECK (data_source IN ('live', 'mock', 'import'));

-- ── 3. FK constraints on the previously unscoped tables ─────
-- api_costs: every row carries site_id in practice (costlog.py always
-- writes it). Guard first so the constraint can be added cleanly; any
-- orphan row it flags is a data bug we want surfaced, not hidden.
-- api_costs_archive exists only on DBs that ran 19-cost-tracking.sql
-- (legacy); on fresh DBs the table is absent and the FK is skipped —
-- to_regclass keeps both worlds typed and idempotent.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'api_costs_site_id_fkey'
          AND conrelid = 'api_costs'::regclass
    ) THEN
        -- Surface orphans before constraining (SELECT-only guard).
        IF EXISTS (
            SELECT 1 FROM api_costs c
            LEFT JOIN site_config s ON s.site_id = c.site_id
            WHERE c.site_id IS NOT NULL AND s.site_id IS NULL
        ) THEN
            RAISE NOTICE 'api_costs: orphan site_id rows found; FK skipped';
        ELSE
            ALTER TABLE api_costs
                ADD CONSTRAINT api_costs_site_id_fkey
                FOREIGN KEY (site_id) REFERENCES site_config(site_id)
                ON DELETE CASCADE;
        END IF;
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('api_costs_archive') IS NULL THEN
        RETURN;  -- fresh DB: legacy archive table not present, nothing to scope
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'api_costs_archive_site_id_fkey'
          AND conrelid = 'api_costs_archive'::regclass
    ) THEN
        IF EXISTS (
            SELECT 1 FROM api_costs_archive c
            LEFT JOIN site_config s ON s.site_id = c.site_id
            WHERE c.site_id IS NOT NULL AND s.site_id IS NULL
        ) THEN
            RAISE NOTICE 'api_costs_archive: orphan site_id rows found; FK skipped';
        ELSE
            ALTER TABLE api_costs_archive
                ADD CONSTRAINT api_costs_archive_site_id_fkey
                FOREIGN KEY (site_id) REFERENCES site_config(site_id)
                ON DELETE CASCADE;
        END IF;
    END IF;
END $$;
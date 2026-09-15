-- ============================================================
-- 12 — One-time migration: ONE GLOBAL OpenSEO credential reference
-- ============================================================
-- OpenSEO is a single SHARED company-level account (like Ahrefs/SEMrush), not
-- a per-brand integration. site_config carries NO OpenSEO columns; the single
-- global reference lives in system_config:
--   'openseo.secret_ref'     reference/ID of the ONE credential in the secrets
--                            store — NEVER a credential value
--   'openseo.base_url'       REST API base URL (non-sensitive)
--   'openseo.capabilities'   capability list the shared account provides
-- Earlier drafts stored openseo_secret_ref PER SITE in site_config; this file
-- collapses any such legacy per-site refs into the single global row and drops
-- the per-site column. Verification = exactly one global ref must exist.
--
-- Full runbook: 11-openseo-connector-schema.md §7/§7.1.
-- ============================================================

-- ------------------------------------------------------------
-- STEP 1 — EXTERNAL (app code / runbook, NOT this file):
--   secrets_store.put('openseo/company/credential', <company api key>)
--   # key = the ref you will write into system_config below ("Step-1 key").
--   If no secrets manager is provisioned, use pgcrypto column-level encryption
--   at rest (key outside DB, env/KMS) — flagged as technical debt until a real
--   secrets store is available (11 §7 STORAGE MODEL).
-- ------------------------------------------------------------

-- 1. If a legacy per-site openseo_secret_ref column exists, collapse it to ONE
--    global value before dropping it. Every site used the same shared account,
--    so at most one distinct value may be present across all sites.
DO $$
DECLARE
    legacy_col_exists BOOLEAN;
    distinct_refs     INT;
    legacy_value      TEXT;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'site_config'
           AND column_name = 'openseo_secret_ref'
    ) INTO legacy_col_exists;

    IF legacy_col_exists THEN
        SELECT count(DISTINCT openseo_secret_ref), min(openseo_secret_ref)
          INTO distinct_refs, legacy_value
          FROM site_config
         WHERE openseo_secret_ref IS NOT NULL AND openseo_secret_ref <> '';

        IF distinct_refs > 1 THEN
            RAISE EXCEPTION
                'Aborting: found % distinct legacy openseo_secret_ref value(s); a '
                'single shared account MUST have exactly one. Inspect before collapsing.',
                distinct_refs;
        END IF;

        -- Reuse the single legacy ref if one existed.
        IF distinct_refs = 1 THEN
            INSERT INTO system_config (config_key, config_value)
            VALUES ('openseo.secret_ref', legacy_value)
            ON CONFLICT (config_key) DO UPDATE SET config_value = EXCLUDED.config_value;
        END IF;

        -- Drop the per-site column — no lingering per-brand credential field.
        ALTER TABLE site_config DROP COLUMN openseo_secret_ref;
    END IF;
END $$;

-- 2. If no global ref exists yet (no legacy value, or no legacy column),
--    create it from the Step-1 key. Replace the placeholder with the actual
--    secrets-store key before running.
INSERT INTO system_config (config_key, config_value)
VALUES ('openseo.secret_ref', '<REPLACE_WITH_STEP1_SECRETS_STORE_KEY>')
ON CONFLICT (config_key) DO NOTHING;

-- 3. ALSO PROVISION the two non-sensitive global rows (base URL + capabilities).
INSERT INTO system_config (config_key, config_value)
VALUES
    ('openseo.base_url',      '<REPLACE_WITH_OPENSEO_REST_API_BASE_URL>'),
    ('openseo.capabilities',  '["keyword_volume","serp","competitors","crawl_audit"]')
ON CONFLICT (config_key) DO NOTHING;

-- 4. EXACTLY-ONE VERIFICATION (the "row-count match" for the global model):
--    the system must hold exactly one openseo.secret_ref, not one per site.
DO $$
DECLARE
    n INT;
BEGIN
    SELECT count(*) INTO n
      FROM system_config
     WHERE config_key = 'openseo.secret_ref';

    IF n <> 1 THEN
        RAISE EXCEPTION
            'Aborting: exactly 1 global openseo.secret_ref is required, found %.',
            n;
    END IF;
END $$;
-- ============================================================
-- 26 — Framer onboarding Phase 0: platform seam (plan/26)
-- ============================================================
-- Plan: Framer onboarding, Phase 0 of the approved sequential rollout.
-- All objects are IF NOT EXISTS / idempotent-safe: re-running is a no-op.
--
-- What this creates:
--   1. site_config.platform — the ONE switch the write path dispatches on
--                             ('shopify' default preserves every existing
--                             site's behavior byte-identically).
--   2. site_config.framer_site_ref — the Framer workspace/site reference
--                             (nullable; only used when platform='framer').
--   3. fix_policy seeds     — framer.title / framer.description, ALL
--                             disabled (enabled=false): nothing executes
--                             until the operator explicitly enables.
--   4. system_config rows   — framer.api_base (non-sensitive base URL).
-- ============================================================

-- ------------------------------------------------------------
-- 1. platform seam on site_config
-- ------------------------------------------------------------
ALTER TABLE site_config ADD COLUMN IF NOT EXISTS platform TEXT
    NOT NULL DEFAULT 'shopify'
    CHECK (platform IN ('shopify', 'framer'));

ALTER TABLE site_config ADD COLUMN IF NOT EXISTS framer_site_ref TEXT;

-- ------------------------------------------------------------
-- 2. fix_policy seeds (fail-closed: enabled=false)
-- ------------------------------------------------------------
-- Mirrors the plan/22 §2 seeding pattern: inserted for sites lacking rows.
-- framer.* sub_types ship disabled — the executor refuses any sub_type
-- whose policy row is missing or enabled=false; the operator enables
-- after the Framer write API is verified live.
INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, requires_field_verify, enabled)
SELECT sc.site_id, v.sub_type, v.risk_tier, v.weekly_cap, v.requires_field_verify, false
FROM site_config sc
CROSS JOIN (VALUES
    ('framer.title',         'low',    10, true),
    ('framer.description',   'medium',  5, true)
) AS v(sub_type, risk_tier, weekly_cap, requires_field_verify)
WHERE NOT EXISTS (
    SELECT 1 FROM fix_policy fp
    WHERE fp.site_id = sc.site_id AND fp.sub_type = v.sub_type
);

-- ------------------------------------------------------------
-- 3. system_config rows (non-sensitive mode/base keys only)
-- ------------------------------------------------------------
INSERT INTO system_config (config_key, config_value)
VALUES ('framer.api_base', 'https://api.framer.com')
ON CONFLICT (config_key) DO NOTHING;

-- target_entity_ref semantics update (documentation; the column comment
-- carries no runtime effect — kept in the plan doc, not DDL):
--   shopify:  gid://shopify/Product|Collection/<id>   (unchanged)
--   framer:   canonical URL of the target page (v1); the framer adapter
--             resolves the live page by URL at snapshot time.
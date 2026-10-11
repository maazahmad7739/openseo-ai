-- ============================================================
-- 27 — Netlify / static-site onboarding via GitHub PR writes
-- ============================================================
-- Platform seam extension (plan/26 pattern): platform='netlify' sites are
-- static HTML hosts whose writes are delivered as GIT PULL REQUESTS, never
-- live API overwrites. The credential is a fine-grained GitHub PAT (per
-- site, stored via the secret-ref discipline — the token itself never
-- enters the DB; system_config carries only the non-sensitive base URL).
--
-- All objects are IF NOT EXISTS / idempotent: re-running is a no-op.
--
-- What this creates:
--   1. site_config.platform CHECK widened to include 'netlify'.
--   2. site_config.github_repo + github_branch — the write target
--                             (owner/name + the base branch PRs target).
--   3. site_config.github_root_dir — repo root that maps to the site
--                             origin ('' or '/' = repo root).
--   4. fix_policy seeds     — netlify.title_meta_rewrite /
--                             netlify.structured_data / netlify.noindex,
--                             ALL disabled (enabled=false): nothing
--                             executes until the operator enables.
--   5. system_config rows   — github.api_base (non-sensitive base URL).
-- ============================================================

-- ------------------------------------------------------------
-- 1. platform seam: widen the CHECK to admit 'netlify'
-- ------------------------------------------------------------
-- Idempotent widening: drop the plan/26 CHECK (whatever it currently
-- allows) and re-add with the full value set. Existing rows are
-- untouched; legacy 'shopify' rows keep their byte-identical default.
DO $$
DECLARE
    ck TEXT;
    defn TEXT;
BEGIN
    SELECT conname, pg_get_constraintdef(oid) INTO ck, defn
    FROM pg_constraint
    WHERE conrelid = 'site_config'::regclass
      AND contype = 'c'
      AND conname = 'site_config_platform_check';
    IF ck IS NULL THEN
        ALTER TABLE site_config
            ADD CONSTRAINT site_config_platform_check
            CHECK (platform IN ('shopify', 'framer', 'netlify'));
    ELSIF defn NOT LIKE '%netlify%' THEN
        EXECUTE format('ALTER TABLE site_config DROP CONSTRAINT %I', ck);
        ALTER TABLE site_config
            ADD CONSTRAINT site_config_platform_check
            CHECK (platform IN ('shopify', 'framer', 'netlify'));
    END IF;
END $$;

ALTER TABLE site_config ADD COLUMN IF NOT EXISTS github_repo TEXT;
ALTER TABLE site_config ADD COLUMN IF NOT EXISTS github_branch TEXT
    NOT NULL DEFAULT 'main';
ALTER TABLE site_config ADD COLUMN IF NOT EXISTS github_root_dir TEXT
    NOT NULL DEFAULT '';

-- ------------------------------------------------------------
-- 2. fix_policy seeds (fail-closed: enabled=false)
-- ------------------------------------------------------------
-- Mirrors the plan/26 §2 seeding pattern: inserted for sites lacking rows.
-- netlify.* sub_types ship disabled — the executor refuses any sub_type
-- whose policy row is missing or enabled=false; the operator enables
-- after the GitHub PR write path is verified live.
INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, requires_field_verify, enabled)
SELECT sc.site_id, v.sub_type, v.risk_tier, v.weekly_cap, v.requires_field_verify, false
FROM site_config sc
CROSS JOIN (VALUES
    ('netlify.title_meta_rewrite',  'low',    10, true),
    ('netlify.structured_data',     'medium',  5, true),
    ('netlify.noindex',             'high',    3, true)
) AS v(sub_type, risk_tier, weekly_cap, requires_field_verify)
WHERE NOT EXISTS (
    SELECT 1 FROM fix_policy fp
    WHERE fp.site_id = sc.site_id AND fp.sub_type = v.sub_type
);

-- ------------------------------------------------------------
-- 3. system_config rows (non-sensitive mode/base keys only)
-- ------------------------------------------------------------
INSERT INTO system_config (config_key, config_value)
VALUES ('github.api_base', 'https://api.github.com')
ON CONFLICT (config_key) DO NOTHING;

-- target_entity_ref semantics (documentation; the column comment carries
-- no runtime effect — kept in the plan doc, not DDL):
--   shopify:  gid://shopify/Product|Collection/<id>   (unchanged)
--   framer:   canonical URL of the target page
--   netlify:  repo-relative file path of the target page
--             (e.g. 'index.html', 'contact/index.html', 'blog/post.md')
--             resolved from the page URL + github_root_dir at generation
--             time; the adapter re-resolves via the repo tree at pickup.
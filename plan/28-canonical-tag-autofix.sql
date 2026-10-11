-- ============================================================
-- 28 — Canonical-tag auto-fix on Netlify (plan/28)
-- ============================================================
-- plan/28: adds netlify.canonical_tag to the GitHub-PR engine's executable
-- surface — a self-referencing <link rel="canonical"> injection into the
-- page's <head> (never a duplicate: the generator refuses pages whose
-- crawled canonical_url is already set; the adapter refuses at transform
-- time as a second guard).
--
-- All objects are IF NOT EXISTS / idempotent: re-running is a no-op.
-- ============================================================

-- ------------------------------------------------------------
-- 1. fix_policy seeds (fail-closed: enabled=false) — canonical_tag
-- ------------------------------------------------------------
INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, requires_field_verify, enabled)
SELECT sc.site_id, 'netlify.canonical_tag', 'low', 10, true, false
FROM site_config sc
WHERE NOT EXISTS (
    SELECT 1 FROM fix_policy fp
    WHERE fp.site_id = sc.site_id AND fp.sub_type = 'netlify.canonical_tag'
);

-- target_entity_ref semantics (documentation; the column comment carries
-- no runtime effect — kept in the plan doc, not DDL):
--   netlify canonical_tag: repo-relative file path (same as plan/27);
--   the canonical href is ALWAYS the page's own absolute URL
--   (self-referencing by construction, minted at generation time).
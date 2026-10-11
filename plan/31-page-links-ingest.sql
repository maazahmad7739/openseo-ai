-- ============================================================
-- 31 — Link graph ingestion (Phase 5): page_links rel + edge keys
-- ============================================================
-- page_links (plan/22 §4) was created as a Phase 5 prerequisite but never
-- populated ("empty until Phase 5"). This migration completes the surface
-- for link ingestion (src/audit/link_ingest.py + plan/05 generator):
--
--   1. page_links.rel — the <a rel> attribute (nofollow/sponsored/...),
--      recorded at ingest time alongside anchor_text.
--   2. Per-site edge uniqueness: the v1 UNIQUE (source_url_hash,
--      target_url_hash) ignored site_id — two configured sites sharing a
--      URL string would collide. The table has NEVER been populated, so
--      replacing the constraint is safe (no data migration).
--   3. idx_pl_source — source-side lookups for the internal-link
--      generator (plan/05) and future link-graph queries.
--
-- All statements are IF NOT EXISTS / idempotent-safe: re-running is a no-op.
-- ============================================================

-- ------------------------------------------------------------
-- 1. rel attribute on the edge
-- ------------------------------------------------------------
ALTER TABLE page_links ADD COLUMN IF NOT EXISTS rel TEXT;

-- ------------------------------------------------------------
-- 2. edge uniqueness scoped per site (replace the v1 constraint)
-- ------------------------------------------------------------
DO $$
DECLARE
    ck TEXT;
BEGIN
    SELECT conname INTO ck
    FROM pg_constraint
    WHERE conrelid = 'page_links'::regclass
      AND contype = 'u'
      AND pg_get_constraintdef(oid) LIKE '%source_url_hash%target_url_hash%';
    IF ck IS NOT NULL THEN
        EXECUTE format('ALTER TABLE page_links DROP CONSTRAINT %I', ck);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_page_links_edge
    ON page_links (site_id, source_url_hash, target_url_hash);

-- ------------------------------------------------------------
-- 3. source-side index (plan/05 candidate queries walk OUT of a page)
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_pl_source
    ON page_links (site_id, source_url_hash);
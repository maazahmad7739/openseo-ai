-- ============================================================
-- 15 — IMPACT-TIER SEARCH-VOLUME GATE (Option A, founder decision)
-- The orchestrator's missing-page impact gate (impact='high') reads
-- site_config.min_search_volume. That column already exists (00-schema.sql:22)
-- where it also serves as Generator 1's cluster-qualification override
-- (COALESCE(sc.min_search_volume, tt.min_search_volume)) — this migration is
-- an idempotent no-op guard that documents the dual use:
--   NULL  -> orchestrator impact gate falls back to 5000 (default);
--            Generator 1 qualification falls back to the tier default (100).
--   value -> BOTH the qualification threshold and the impact gate use it.
-- Setting a per-site value therefore affects both behaviors by design.
-- ============================================================

ALTER TABLE site_config
    ADD COLUMN IF NOT EXISTS min_search_volume INT;
-- ============================================================
-- 14 — MIN_SAMPLE_FOR_SIGNIFICANCE site override column
-- plan/05 declares config.measurement.min_sample_for_significance as a
-- per-site config value (agent input config block). The measurement layer
-- resolves it via site_config (same override pattern as min_impressions_7d):
-- NULL = use the documented default (measurement/thresholds.py).
-- ============================================================

ALTER TABLE site_config
    ADD COLUMN IF NOT EXISTS min_sample_for_significance INT;
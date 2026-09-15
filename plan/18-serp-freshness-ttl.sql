-- Phase 2: SERP data-freshness TTL columns on site_config.
-- Adds per-site TTL windows so the daily sync can skip paid DataForSEO
-- fetches when recent snapshots already exist.

ALTER TABLE site_config
    ADD COLUMN IF NOT EXISTS ttl_serp_days            INT NOT NULL DEFAULT 7,
    ADD COLUMN IF NOT EXISTS ttl_keyword_volume_days   INT NOT NULL DEFAULT 30;

-- ============================================================
-- Phase 3: Cost tracking + weekly budget cap
-- Idempotent (ledger migration): safe on fresh and existing DBs.
-- ============================================================

CREATE TABLE IF NOT EXISTS api_costs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ DEFAULT now(),
    site_id UUID,
    service TEXT NOT NULL,          -- 'openseo_serp', 'openseo_keyword_volume', 'ollama_chat'
    call_type TEXT,                 -- capability name or 'chat'
    calls INT DEFAULT 1,
    cost NUMERIC(10,4),            -- $ amount (NULL if unknown, just counting calls)
    tokens_in INT,                 -- for Ollama: prompt tokens
    tokens_out INT,                -- for Ollama: completion tokens
    metadata JSONB                 -- arbitrary (query, model name, etc.)
);

CREATE INDEX IF NOT EXISTS idx_api_costs_service_date ON api_costs (service, timestamp);
CREATE INDEX IF NOT EXISTS idx_api_costs_site ON api_costs (site_id, timestamp);

CREATE TABLE IF NOT EXISTS budget_config (
    service TEXT NOT NULL,          -- '*' for global, or 'openseo_serp', 'ollama_chat'
    weekly_cap NUMERIC(10,2),      -- $ cap per week
    warn_threshold NUMERIC(3,2) DEFAULT 0.80  -- alert at this fraction of cap
);

-- Seed the global weekly budget once (no uniqueness on service; guard via NOT EXISTS).
INSERT INTO budget_config (service, weekly_cap, warn_threshold)
SELECT '*', 100.00, 0.80
WHERE NOT EXISTS (SELECT 1 FROM budget_config WHERE service = '*');
-- ============================================================
-- Phase 3: Cost tracking + weekly budget cap
-- ============================================================

CREATE TABLE api_costs (
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

CREATE INDEX idx_api_costs_service_date ON api_costs (service, timestamp);
CREATE INDEX idx_api_costs_site ON api_costs (site_id, timestamp);

CREATE TABLE budget_config (
    service TEXT NOT NULL,          -- '*' for global, or 'openseo_serp', 'ollama_chat'
    weekly_cap NUMERIC(10,2),      -- $ cap per week
    warn_threshold NUMERIC(3,2) DEFAULT 0.80  -- alert at this fraction of cap
);

INSERT INTO budget_config (service, weekly_cap, warn_threshold) VALUES ('*', 100.00, 0.80);

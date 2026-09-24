-- ============================================================
-- 23 — On-Demand Brand-Agnostic Audit: session tables (Phase A)
-- ============================================================
-- Plan: plan/23-on-demand-brand-agnostic-audit.md §2 (data model).
-- All objects are IF NOT EXISTS / idempotent-safe: re-running is a no-op.
--
-- What this creates:
--   1. audit_sessions         — the persistent spine (one row per audit run;
--                               ad-hoc, brand-agnostic; carries its own
--                               denormalized domain fields — NO ephemeral
--                               site_config rows).
--   2. audit_page_snapshots   — transient page-content snapshot for the
--                               inspected URL (child, cascade-deleted).
--   3. audit_serp_competitors — transient SERP competitor metadata for the
--                               session's inferred query (child,
--                               cascade-deleted; title/snippet/position
--                               mirror openseo_serp_snapshots columns so the
--                               context adapter reads either shape).
--
-- NOT touched: site_config, pages, keyword_clusters, recommendations,
-- generated_fixes, openseo_serp_snapshots (plan/23 §2.4 — read-only reuse).
--
-- Retention: audit_sessions.expires_at (default +7 days) drives the TTL
-- sweep (src/audit/ttl_sweep.py); child tables cascade.
-- ============================================================

-- ------------------------------------------------------------
-- 1. audit_sessions
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_sessions (
    session_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requested_url    TEXT NOT NULL,
    url_hash         TEXT GENERATED ALWAYS AS (md5(requested_url)) STORED,
    normalized_url   TEXT NOT NULL,
    domain           TEXT NOT NULL,           -- registrable domain (e.g. "example.com")
    mode             TEXT NOT NULL DEFAULT 'audit_checklist'
                     CHECK (mode IN ('audit_checklist','connected')),
    site_id          UUID REFERENCES site_config(site_id),  -- NULL unless connected
    inferred_query   TEXT,                    -- primary keyword (plan/23 §1 step 4)
    inferred_intent  TEXT DEFAULT 'unknown',  -- informational|commercial|transactional|navigational|unknown
    fetch_status     TEXT NOT NULL DEFAULT 'pending'
                     CHECK (fetch_status IN ('pending','fetched','page_unreachable','blocked_robots')),
    serp_status      TEXT NOT NULL DEFAULT 'pending'
                     CHECK (serp_status IN ('pending','ok','zero_results','serp_error','budget_exhausted')),
    page_snapshot_id UUID,                    -- filled after page ingest
    created_at       TIMESTAMPTZ DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '7 days'
);
CREATE INDEX IF NOT EXISTS idx_audit_sessions_url ON audit_sessions (url_hash, created_at DESC);

-- ------------------------------------------------------------
-- 2. audit_page_snapshots (transient; cascade-deleted with the session)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_page_snapshots (
    snapshot_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id       UUID NOT NULL REFERENCES audit_sessions(session_id) ON DELETE CASCADE,
    url              TEXT NOT NULL,
    fetched_at       TIMESTAMPTZ DEFAULT now(),
    status_code      INT,
    title            TEXT,
    meta_description TEXT,
    h1               TEXT,
    heading_outline  JSONB,        -- [{level, text}] H1–H3, never fabricated
    body_html        TEXT,         -- normalizer strips tags for body_text (plan/21 §1.4 semantics)
    body_text        TEXT,
    render_status    TEXT DEFAULT 'not_rendered'
                     CHECK (render_status IN ('server_rendered','js_rendered','render_failed','not_rendered')),
    content_hash     TEXT          -- sha256(normalized body_text): cache key + drift detector
);
CREATE INDEX IF NOT EXISTS idx_aps_session ON audit_page_snapshots (session_id);

-- ------------------------------------------------------------
-- 3. audit_serp_competitors (transient; cascade-deleted with the session)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_serp_competitors (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id     UUID NOT NULL REFERENCES audit_sessions(session_id) ON DELETE CASCADE,
    query          TEXT NOT NULL,
    position       INT NOT NULL,
    result_url     TEXT NOT NULL,
    result_domain  TEXT NOT NULL,
    is_self        BOOLEAN DEFAULT false,     -- result on the audited domain
    title          TEXT,
    snippet        TEXT,
    url_pattern    TEXT,                      -- /products/ | /collections/ | /blog/ | ...
    snapshot_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at     TIMESTAMPTZ DEFAULT now(),
    UNIQUE (session_id, result_url)           -- one row per competitor per session
);
CREATE INDEX IF NOT EXISTS idx_asc_session ON audit_serp_competitors (session_id, position);
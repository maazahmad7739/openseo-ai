# On-Demand, Brand-Agnostic URL Search & Audit Engine
# ============================================================
# Version: 1.0 · Date: 2026-09-23 · Status: PLAN (not yet implemented)
#
# Extends the platform from "pre-bound sites on weekly batch cycles" to:
#   paste ANY brand domain / page URL → live page ingest → live SERP
#   competitor pull → SERP-grounded fix drafts → either a copy-paste
#   audit checklist (read-only) or the existing two-gate Shopify
#   execution pipeline (when the domain matches a connected store).
#
# Grounded against main @ plan/21 implementation:
#   - src/fixes/generator.py  (SERP-grounded deterministic drafting + validator)
#   - src/connectors/openseo_rest_adapter.py (live `serp` capability: v3/serp/google/organic/live/regular)
#   - src/connectors/costlog.py (weekly budget caps), src/connectors/mode.py (mock/live switch)
#   - src/api/main.py (FastAPI, /api re-mount, auth middleware)
#   - src/jobs/fix_executor.py (the ONLY queued→applied writer)
#   - plan/00-schema.sql (openseo_serp_snapshots, pages, keyword_clusters, site_config)
#   - plan/22-stage2-phase0-schema.sql (generated_fixes, fix_policy)
#
# NON-NEGOTIABLES (inherited, unchanged):
#   1. NOTHING publishes without human approval — the brand-agnostic path
#      changes the INPUT, never the two-gate approval model.
#   2. Every generated draft passes the SAME deterministic validator gates
#      (length bounds, denylists, anti-spam, duplicate guards, intent
#      alignment) — no bypass path for ad-hoc URLs.
#   3. One generic adapter contract: the new live-SERP use goes through the
#      existing `fetch("serp", params)` seam of OpenseoRestAdapter.
#   4. Read-only audits NEVER write to generated_fixes / recommendations /
#      site_config. Connected-store execution is a separate, gated route.
#   5. Cost governance applies to every DataForSEO live call, including
#      on-demand ones (budget check BEFORE the fetch, cost log AFTER).

---

## 0. Why This Exists (Problem Statement)

Today the pipeline only audits domains that were pre-registered as
`site_config` rows with GSC properties and Shopify bindings, and only on the
weekly Sunday cron. That excludes:

- A prospect evaluating the tool against their own store (no onboarding yet).
- A consultant auditing a third-party brand out of the blue.
- A connected-store operator wanting an on-demand re-audit of a specific URL
  without waiting for Sunday's batch.

This plan adds an **on-demand entry point**: one URL in, a full SERP-grounded
audit out, in seconds — brand-agnostic by construction (the system infers
everything from the URL itself), and split into a read-only mode (default)
and a connected-execution mode (only when the domain matches an
authenticated Shopify store with an enabled fix_policy).

---

## 1. System Architecture & Request Lifecycle

### 1.1 High-level flow

```
                       ┌────────────────────────────────────────────┐
                       │  POST /api/audit/live-url { url }          │
                       └───────────────┬────────────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────┐
                    │ 1. URL normalize + domain resolve    │  src/connectors/url_normalize.py
                    │    (canonicalize_url, registrable    │
                    │     domain extraction)               │
                    └──────────────────┬──────────────────┘
                                       │
          ┌────────────────────────────┼─────────────────────────────┐
          ▼                            ▼                             ▼
┌─────────────────────┐  ┌──────────────────────────┐  ┌──────────────────────────┐
│ 2. LIVE PAGE FETCH  │  │ 3. SITE RESOLUTION       │  │ (cache pre-check — §5.1) │
│ http(s) GET, ~8s    │  │ site_config.domain OR    │  │ identical (url_hash,     │
│ timeout → parse:    │  │ shopify_domain match →   │  │  query) hit < TTL →      │
│ <title>, meta desc, │  │ "connected" mode         │  │ serve cached snapshot,   │
│ H1..H3 outline,     │  │ else "audit_checklist"   │  │ zero SERP spend          │
│ visible body copy   │  │ (read-only)              │  └──────────────────────────┘
│ (plan/21 §1.4 body  │  └────────────┬─────────────┘
│  fields, reused)    │               │
└─────────┬───────────┘               │
          │                           │
          ▼                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 4. KEYWORD-CLUSTER INFERENCE (deterministic, no LLM in v1)           │
│    title + H1 + top body terms → primary keyword candidate(s)        │
│    → optional keyword_volume fetch to pick the highest-volume variant│
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 5. LIVE SERP PULL — fetch("serp", {query, limit: 10..20})            │
│    openseo_rest_adapter._post → v3/serp/google/organic/live/regular  │
│    ~1–5s synchronous. Budget check (§5.2) BEFORE the call;           │
│    log_cost() AFTER (provider-reported cost rides the envelope).     │
│    Persist rows → audit_serp_competitors (§2.3).                     │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 6. FIX GENERATION — SAME deterministic engine as the batch pipeline  │
│    build in-memory `rec` dict (§3.2) → draft_title /                │
│    draft_meta_description → validate_title_draft /                   │
│    validate_meta_draft → content_outline_gaps                        │
│    (all imported from src/fixes/generator.py — zero logic fork)      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
              mode == "audit_checklist"     mode == "connected"
              ┌─────────────┴─────────────┐
              ▼                           ▼
┌───────────────────────────┐ ┌─────────────────────────────────────┐
│ 7a. AUDIT CHECKLIST VIEW  │ │ 7b. CONNECTED-STORE ROUTE           │
│ suggestions + evidence +  │ │ upsert page row → create/improve a  │
│ copy-paste implementation │ │ recommendation → POST /fix (gate 1  │
│ checklist — NO DB writes  │ │ already done by operator) → diff    │
│ beyond the session tables │ │ approve (gate 2) → fix_executor →   │
│                           │ │ Shopify GraphQL (plan/21 §3–§5)     │
└───────────────────────────┘ └─────────────────────────────────────┘
```

### 1.2 Module layout (new code)

```
src/audit/
├── __init__.py
├── page_fetch.py        # live page GET + HTML → {title, meta_description,
│                        #   heading_outline, body_html, body_text, status_code}
│                        #   (reuses fixes.generator._body_to_text for strip)
├── site_resolution.py   # registrable-domain extraction; match against
│                        #   site_config.domain / shopify_domain → mode
├── keyword_infer.py     # deterministic keyword-cluster inference from page copy
├── engine.py            # orchestration: the 7-step lifecycle, session upsert,
│                        #   cache check, budget gate, mode split
└── context_adapter.py   # in-memory rec-dict builder → feeds generator.py
                          #   unchanged (§3.2) — the ONLY new seam into the
                          #   existing fix pipeline

src/api/routes/
└── audit.py             # POST /api/audit/live-url, GET /api/audit/{session_id},
                          #   POST /api/audit/{session_id}/to-store (connected mode)
```

New tables (§2): `audit_sessions`, `audit_page_snapshots`,
`audit_serp_competitors`. **No changes to any existing table**;
`generated_fixes` / `recommendations` / `site_config` are untouched.

### 1.3 Latency expectations (measured targets)

| Stage | Typical | Hard ceiling | Over-ceiling behavior |
|---|---|---|---|
| URL normalize + site resolve | <50ms (in-process) | — | — |
| Page fetch + parse | 0.8–3s | 8s (HTTP_TIMEOUT) | typed `page_unreachable` error |
| Keyword inference | <50ms | — | — |
| keyword_volume (optional) | 1–2s | 4s | skipped, inference-only keyword used |
| Live SERP (`serp` live/regular, depth 10) | 1–5s | 15s | typed `serp_error` / zero-SERP path (§4.4) |
| Fix generation + validation | <100ms (deterministic) | — | — |
| **End-to-end target** | **3–10s** | **30s API hard budget** | 504 with session id for polling |

Deployment note (Vercel, plan/21 §3): the request path is read-only +
deterministic computation + two synchronous provider calls — comfortably
inside the 60s function window. Nothing write-heavy ever runs in-request;
the connected-mode executor remains `jobs/fix_executor.py`, exactly as
today.

### 1.4 What is deliberately NOT in v1

- No headless-browser rendering (fetch is a plain GET; JS-only pages are
  flagged `render_status: 'not_rendered'` honestly, same enum as `pages`).
- No LLM in the inference loop (deterministic keyword + framing extraction
  only — the batch agent's LLM prioritization stays a weekly-batch concern).
- No sitemap-wide crawl (single URL per session; multi-URL batch = v2).
- No re-ranking / rank tracking (SERP is a snapshot for grounding, not
  persisted rank history — TTL applies, §2.3).

---

## 2. Data Models & Schema Adjustments

### 2.1 Dynamic site/domain resolution (no store binding)

The engine never requires a pre-registered site. Resolution order:

1. `site_config.domain = registrable_domain(url)` → connected store candidate.
2. `site_config.shopify_domain = registrable_domain(url)` OR the URL host is
   `*.myshopify.com` → connected store candidate.
3. No match → brand-agnostic audit mode.

`site_resolution.py` returns a typed result:

```python
{"mode": "audit_checklist" | "connected",
 "site_id": UUID | None,           # present iff connected
 "site_name": str | None,          # used for brand-suffix validator check
 "match_basis": "domain" | "shopify_domain" | "myshopify_host" | "none"}
```

**No ephemeral site_config rows.** Inserting throwaway `site_config` rows
would pollute the weekly generators, rejection learning, and GSC joins.
Instead, ad-hoc audits hang off `audit_sessions` (below), which carries its
own denormalized brand/domain fields.

### 2.2 New table `audit_sessions` (persistent spine; one per audit run)

```sql
CREATE TABLE audit_sessions (
    session_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requested_url    TEXT NOT NULL,
    url_hash         TEXT GENERATED ALWAYS AS (md5(requested_url)) STORED,
    normalized_url   TEXT NOT NULL,
    domain           TEXT NOT NULL,           -- registrable domain
    mode             TEXT NOT NULL DEFAULT 'audit_checklist'
                     CHECK (mode IN ('audit_checklist','connected')),
    site_id          UUID REFERENCES site_config(site_id),  -- NULL unless connected
    inferred_query   TEXT,                    -- primary keyword (§1 step 4)
    inferred_intent  TEXT DEFAULT 'unknown',  -- informational|commercial|transactional|navigational|unknown
    fetch_status     TEXT NOT NULL DEFAULT 'pending'
                     CHECK (fetch_status IN ('pending','fetched','page_unreachable','blocked_robots')),
    serp_status      TEXT NOT NULL DEFAULT 'pending'
                     CHECK (serp_status IN ('pending','ok','zero_results','serp_error','budget_exhausted')),
    page_snapshot_id UUID,                    -- FK filled after page ingest
    created_at       TIMESTAMPTZ DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '7 days'
);
CREATE INDEX idx_audit_sessions_url ON audit_sessions (url_hash, created_at DESC);
```

`expires_at` powers the retention/TTL sweep (§5.4): past it, the session is
pruned along with its child snapshots.

### 2.3 Transient vs. persistent snapshot storage

Two new child tables; both are **transient** (TTL-swept), unlike the
persistent weekly `openseo_serp_snapshots` which stays the batch pipeline's
home and is never written by this engine.

```sql
-- Page content snapshot (the inspected target page)
CREATE TABLE audit_page_snapshots (
    snapshot_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id     UUID NOT NULL REFERENCES audit_sessions(session_id) ON DELETE CASCADE,
    url            TEXT NOT NULL,
    fetched_at     TIMESTAMPTZ DEFAULT now(),
    status_code    INT,
    title          TEXT,
    meta_description TEXT,
    h1             TEXT,
    heading_outline JSONB,        -- [{level, text}] H1–H3, never fabricated
    body_html      TEXT,          -- reused by _body_to_text; plan/21 §1.4 column semantics
    body_text      TEXT,
    render_status  TEXT DEFAULT 'not_rendered'
                   CHECK (render_status IN ('server_rendered','js_rendered','render_failed','not_rendered')),
    content_hash   TEXT           -- sha256(normalized body_text): cache key + drift detector
);
CREATE INDEX idx_aps_session ON audit_page_snapshots (session_id);

-- SERP competitor metadata for this session's inferred query
CREATE TABLE audit_serp_competitors (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id     UUID NOT NULL REFERENCES audit_sessions(session_id) ON DELETE CASCADE,
    query          TEXT NOT NULL,
    position       INT NOT NULL,
    result_url     TEXT NOT NULL,
    result_domain  TEXT NOT NULL,
    is_self        BOOLEAN DEFAULT false,     -- result on the audited domain
    title          TEXT,                      -- competitor title (§2.3 schema: title)
    snippet        TEXT,                      -- competitor snippet (§2.3 schema: snippet)
    url_pattern    TEXT,                      -- /products/ | /collections/ | /blog/ | ...
    snapshot_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at     TIMESTAMPTZ DEFAULT now(),
    UNIQUE (session_id, result_url)           -- one row per competitor per session
);
CREATE INDEX idx_asc_session ON audit_serp_competitors (session_id, position);
```

Schema parity note: `title`/`snippet`/`rank(position)` mirror
`openseo_serp_snapshots.result_title/result_snippet/position` so the
`context_adapter` (§3.2) can feed `load_competitor_context`-shaped dicts
from either table without normalization branches.

**Why a separate table instead of reusing `openseo_serp_snapshots`**: that
table's `site_id` is NOT NULL in practice (FK to `site_config`) and its rows
feed Generator 1's 35-day lookback window — ad-hoc one-shot SERP rows would
either violate the FK or poison the weekly competitor context with
off-brand domains. Isolation by table is cheaper and safer than soft
partitioning.

### 2.4 Reused (no schema change)

| Asset | Reuse |
|---|---|
| `site_config` | read-only match (domain, shopify_domain, site_name for brand checks) |
| `fix_policy`, `generated_fixes` | connected mode only; identical two-gate semantics (plan/21 §1.2–1.3, §5.1) |
| `api_costs` + `budget_config` | cost logging + weekly cap enforcement for live SERP calls |
| `keyword_clusters` | NOT written by this engine in v1 (inference is in-session); connected mode may persist a cluster only via the existing sync path |

---

## 3. Competitor Grounding & Fix Generation

### 3.1 Pattern/hook extraction — reuse, don't fork

`src/fixes/generator.py` already contains the full deterministic analysis
stack (verified against plan/21 §2.1/§2.2 gap-closure code):

- `_title_framing(title, position)` → structural tokens
  (`best`, `vs`, `review`, `guide`, `how-to`, `year:YYYY`, `count:N`,
  `split-separator`).
- `_snippet_hooks(snippet)` → value-prop cues (`free`, `tested`, `compare`,
  `returns`, `warranty`, …).
- `_competitor_prefix(rec)` → dominant framing token → title lead fragment.
- `_competitor_snippet_leads(rec)` → frequency-ordered hook ordering for the
  meta draft.
- `content_outline_gaps(rec, body_text)` → expected vs missing vs covered
  section angles (the thin-content / missing-angle signal).

The engine's job is therefore ONLY to produce the two input dicts those
functions read: `rec["competitor_context"] = {titles, snippets, url_patterns}`
and `rec["body_text"]`. No pattern-extraction logic is duplicated.

### 3.2 The in-memory rec adapter (`context_adapter.py`)

`load_decision_inputs()` reads from `recommendations`/`pages`/`keyword_clusters`
— batch-pipeline tables an ad-hoc session doesn't populate. Instead the
adapter builds the same `rec` dict shape **in memory** and calls the
draft/validate functions directly:

```python
rec = {
    "target_url":        normalized_url,
    "page_title":        snapshot.title,
    "page_meta_description": snapshot.meta_description,
    "page_type":         inferred_page_type,      # from URL pattern (/products/ → "product")
    "body_html":         snapshot.body_html,
    "body_text":         _body_to_text(snapshot.body_html),
    "primary_keyword":   inferred_query,
    "intent":            inferred_intent,
    "site_name":         resolved_site_name,      # None in brand-agnostic mode
    "competitor_context": {...},                  # §3.1, from audit_serp_competitors
    "shopify_gid":       gid_or_none,             # present iff connected mode
}
draft_title(rec); validate_title_draft(draft, kw, product_title=..., intent=..., site_name=...)
draft_meta_description(rec); validate_meta_draft(...)
content_outline_gaps(rec, body_text=...)
```

Mode split inside the adapter:

- **audit_checklist**: `shopify_gid=None`, `site_name=None` (no brand-suffix
  check possible — honest: no brand claim is made), drafts are returned to
  the API as diff previews; nothing is inserted into `generated_fixes`.
  All invariants (TITLE_MIN/MAX, META_MIN/MAX, denylists, anti-spam,
  keyword-first-half, intent alignment) still gate every draft — a failing
  draft is surfaced as "no safe draft available for this field" in the UI,
  never as a silently weakened suggestion.
- **connected**: full fidelity. The session upserts/refreshes the page row
  in `pages` (site-scoped, so `duplicate_title_check` and
  `duplicate_meta_check` site-wide self-joins work against real store
  data), and `generate_fix_for_recommendation` /
  `generate_meta_fix_for_recommendation` are called through the normal
  hooks after the recommendation exists.

### 3.3 Protecting the invariants in brand-agnostic mode

The batch pipeline guards that depend on GSC history (`protect_winner_check`,
consolidate suppression) cannot run ad-hoc (no GSC rows for foreign
domains). Two consequences, both explicit:

1. Drafts carry an `unprotected` flag in the response payload — the
   operator sees "no GSC data available; treat this page's current winners
   status as unknown".
2. In connected mode the full guards DO run (the site has history), so
   nothing regresses versus the batch path.

Anti-spam/denylist enforcement is unchanged because the validators are
shared functions — there is no second code path to weaken.

### 3.4 Grounding evidence in the response

Both modes return the same grounding block the batch payload already
carries (plan/21 §2.1 payload["grounding"] shape):

```json
{"framing_pattern": "Best ",
 "competitor_titles_sampled": 9,
 "url_patterns": ["/collections/", "/blog/", "/products/"],
 "content_outline": {"expected_sections": [...], "missing_sections": [...],
                     "covered_sections": [...]},
 "unprotected": true,
 "mode": "audit_checklist"}
```

---

## 4. API Endpoints & Frontend Contract

### 4.1 `POST /api/audit/live-url`

Request:

```json
{"url": "https://example.com/products/aurora-lamp",
 "depth": 10,                     // optional SERP depth, clamp 5..20, default 10
 "location_code": 2840,           // optional, default 2840 (US) — matches adapter default
 "language_code": "en",           // optional, default "en"
 "refresh": false}                // true = bypass snapshot cache (costs a live SERP call)
```

Response `200`:

```json
{"session_id": "uuid",
 "mode": "audit_checklist",
 "normalized_url": "…",
 "domain": "example.com",
 "page": {"status_code": 200, "title": "…", "meta_description": "…",
          "h1": "…", "heading_outline": [...], "body_text_chars": 1840,
          "render_status": "server_rendered"},
 "inferred_query": "aurora desk lamp",
 "inferred_intent": "transactional",
 "serp": {"query": "aurora desk lamp", "results_ingested": 10,
          "self_position": 7, "zero_results": false},
 "suggestions": {
   "seo.title": {"current": "…", "draft": "…", "char_count": 58,
                 "validator_problems": [], "grounding": {...}},
   "seo.description": {"current": null, "draft": "…", "char_count": 149,
                       "validator_problems": [], "grounding": {...}},
   "content_outline": {"expected_sections": [...], "missing_sections": [...],
                       "covered_sections": [...]},
   "headings": [{"level": "h2", "suggested": "Head-to-head comparison table",
                 "rationale": "competitor-grounded section angle"}]
 },
 "quality_checks": {"title": {...}, "meta": {...}},
 "cache": {"hit": false, "snapshot_age_hours": 0},
 "cost": {"serp_usd": 0.002}}
```

Error contract (all typed, all logged to `audit_sessions.fetch_status` /
`serp_status`):

| HTTP | condition | body |
|---|---|---|
| 400 | malformed/non-HTTP URL | `{"error": "invalid_url"}` |
| 422 | robots-disallowed fetch | `{"error": "blocked_robots", "session_id": …}` |
| 502 | fetch failed / timeout / non-200-only page | `{"error": "page_unreachable", "detail": "HTTP 403"}` |
| 200 | SERP returned zero organic rows | full page analysis, empty `suggestions` grounding, `serp.zero_results: true` — degrade gracefully (§4.4) |
| 429 | per-session/IP rate limit | `{"error": "rate_limited", "retry_after": 60}` |
| 402 | DataForSEO weekly budget exhausted | `{"error": "budget_exhausted"}` — page analysis still returned, SERP grounding absent |

### 4.2 `GET /api/audit/{session_id}`

Returns the same shape as above from the persisted session (idempotent
re-fetch of an existing result, no new spend). 404 when expired/pruned.

### 4.3 `POST /api/audit/{session_id}/to-store` (connected mode ONLY)

Bridges a session into the existing execution pipeline:

1. Verifies `mode == "connected"` and `site_id` is set (409 otherwise).
2. Upserts the page snapshot into `pages` (site-scoped; reuses plan/21 §1.4
   `meta_description`/`body_html` columns) and optionally persists a
   `keyword_clusters` row for the inferred query.
3. Creates the `improve_page` recommendation(s) in status `approved`
   **only** if an operator-supplied `approve: true` is in the body — the
   recommendation itself is gate 1, so the bridge may not auto-approve;
   default behavior creates it as `proposed` for the normal operator queue.
4. Calls the existing `POST /recommendations/{id}/fix` → diff →
   `POST /fixes/{id}/approve` → `fix_executor` (plan/21 §3 routes, unchanged).

### 4.4 Zero-SERP and unreachable-page behavior

- **Zero organic results** (very new/niche domains): suggestions degrade to
  the keyword-only behavior exactly as `load_competitor_context` already
  does (`{}` context → naive keyword-forward drafts). The UI labels this
  "no SERP grounding available for this query".
- **Page unreachable**: no session content, but the session row persists
  the failure for debugging; the API returns the typed error above.
- **SERP error / budget exhausted**: page analysis + quality checks are
  still returned (they need no SERP); only grounding-ordered drafting is
  absent. Frontend renders the checklist with an amber banner.

### 4.5 Loading states & frontend

New screen `ui/screens/LiveAudit.js` + one route in `app.js` (same
hand-rolled SPA patterns as `ActionQueue.js`; no framework changes):

- `idle → fetching (~3–10s skeleton with per-stage progress:
  "fetching page → analyzing SERP → drafting suggestions") → result`.
- **Copy-paste checklist view** (audit_checklist mode): field-by-field
  cards — Current | Draft | Character count | Why (grounding chips:
  framing pattern, competitor sample, outline gaps) — with per-field
  "Copy" buttons and an overall "Implementation Checklist" export
  (markdown clipboard).
- **Connected mode**: identical cards PLUS the FixPanel diff flow
  (approve → queued → executor status chip), reusing
  `components/ApprovalWorkflow` patterns; `to-store` button replaces
  copy-paste emphasis.
- Errors render per §4.1 with actionable copy ("Page didn't respond —
  check the URL or try a different page").

---

## 5. Safety, Rate Limiting & Cost Governance

### 5.1 Query-result caching (spend avoidance)

Before any live SERP call, `engine.py` checks:

```sql
SELECT s.session_id FROM audit_sessions s
JOIN audit_page_snapshots p USING (session_id)
WHERE s.url_hash = %s AND s.inferred_query = %s
  AND p.fetched_at > now() - INTERVAL '24 hours'
  AND s.serp_status = 'ok'
ORDER BY s.created_at DESC LIMIT 1
```

Hit → return `GET /api/audit/{existing_session_id}` output with
`cache: {hit: true, snapshot_age_hours: N}`. The 24h TTL matches SERP
volatility expectations while bounding duplicate-query spend. The
`refresh: true` flag bypasses the cache (still rate-limited). Page-body
`content_hash` is re-checked on cache hits against a cheap conditional GET;
a changed page invalidates the cached SERP too (results may no longer
reflect the page's competitive context).

### 5.2 Budget controls (reuse `connectors/costlog.py`)

- Pre-call: `check_budget(conn, site_id=None, service="openseo_serp")` —
  the existing weekly `budget_config` cap (env override
  `WEEKLY_BUDGET_OPENSEO`, plan/19) gates the call; exhausted → 402 path.
- Post-call: `log_cost(conn, site_id, service="openseo_serp",
  call_type="serp_live_audit", cost=result["cost"])` — provider-reported
  per-task cost rides on every successful envelope (adapter `_provider_cost`,
  verified: $0.002/serp live/regular).
- New metadata field on the api_costs row: `{"source": "audit_engine",
  "session_id": …}` so on-demand spend is distinguishable from weekly-batch
  spend in cost reports (extends `jobs/cost_report.py` grouping).
- Depth clamp: `depth` is clamped to 5..20 (cost scales with depth; v1
  default 10 = `COMPETITOR_POSITION_MAX` parity in `generator.py`).

### 5.3 Rate limiting

- Per-IP token bucket: e.g. 10 live audits / hour, 3 concurrent
  (in-process; a `system_config` row makes it tunable without deploy).
- Identical (url, query) cache means accidental double-submits don't
  double-spend.
- SERP pull is strictly one call per session in v1 (no multi-keyword
  fan-out; keyword_volume optional single call only when the inferred
  keyword set is ambiguous).

### 5.4 Read/write permission segregation

| Path | DB writes allowed | Shopify writes |
|---|---|---|
| `POST /api/audit/live-url` + `GET /{id}` | `audit_sessions`, `audit_page_snapshots`, `audit_serp_competitors` ONLY | never |
| `POST /{id}/to-store` | `pages`, `keyword_clusters`, `recommendations` (via existing routes) | never directly |
| diff approve → executor | `generated_fixes` lifecycle (plan/21 §3) | via `fix_executor` only |

The audit router never imports the Shopify connector; execution stays in
`jobs/fix_executor.py` behind `fix_policy` gates (fail-closed, weekly caps,
kill-switch — unchanged, plan/21 §5.1). Enforced as a test invariant (§6
battery: `run_no_socket_tests.py` pattern extended to assert the audit
router performs zero `generated_fixes` writes).

### 5.5 Content & safety guards on the fetch path

- Fetch is GET-only, 8s timeout, 1 redirect hop max, response size cap
  (e.g. 3MB) — no crawler loops.
- robots.txt honored for the *page fetch* (SERP pull is provider-side and
  unaffected).
- SSRF guard: private/loopback/link-local IP literals and `localhost`
  variants rejected before fetch (resolve-and-check the host).
- Competitor text is never copied verbatim into drafts — enforced by the
  existing validators (banned patterns, dedupe vs page's own title, framing
  tokens only), the same grounded-only invariant the batch pipeline
  documents in `openseo_serp_snapshots` comments.

---

## 6. Step-by-Step Implementation Phases & Test Battery

### Phase A — Page ingest + site resolution (read-only, zero external spend)

1. `src/audit/page_fetch.py`: GET + parse (title, meta desc, H1–H3,
   body_html, body_text via `_body_to_text`, render_status='not_rendered').
   SSRF guard, robots check, size cap.
2. `src/audit/site_resolution.py`: registrable-domain extraction + mode
   resolution (3-step order from §2.1).
3. Schema migration: `plan/23-audit-schema.sql` with `audit_sessions`,
   `audit_page_snapshots`, `audit_serp_competitors` (§2).
4. TTL sweep: prune expired sessions + children (scheduler task or
   on-request best-effort delete; pick scheduler for determinism).

**Tests** (`tests/run_audit_page_tests.py`):
- Unit: parser fixtures (static HTML, JS-shell page → not_rendered flag,
  meta-less page, huge page truncation, heading outline ordering).
- Unit: SSRF rejections (localhost, 10.x, 169.254.x, 192.168.x), robots
  disallow → `blocked_robots`.
- Unit: site resolution — bare domain match, myshopify host match,
  www-stripping, port/scheme normalization, no-match → audit_checklist.
- Mock-SERP-free integration: `POST /live-url` with `INTEGRATION_MODE=mock`
  and SERP fixture returning zero rows → quality checks still emitted.

### Phase B — Keyword inference + live SERP ingestion

1. `src/audit/keyword_infer.py`: deterministic candidate extraction
   (title tokens ∩ H1 ∩ body-frequency top terms → ≤3 candidates; pick
   primary by optional `keyword_volume` fetch, fall back to title-led
   candidate). Intent inference from `INTENT_*_CUES` reuse
   (`generator.py` cue sets, imported — no duplicate tables).
2. `engine.py` SERP step: budget pre-check → `fetch("serp", …)` →
   normalize via the adapter's existing `_normalize_serp` shape →
   `audit_serp_competitors` insert (dedupe by URL, cap at depth) →
   `log_cost` with `source=audit_engine` metadata.
3. Cache check + `refresh` bypass (§5.1).

**Tests** (`tests/run_audit_serp_tests.py`, mock-SERP battery):
- Mock-SERP happy path: fixture SERP (10 organic rows) → competitor rows
  ingested with title/snippet/position/url_pattern; `is_self` correctly
  true for rows on the audited domain.
- Zero-results fixture → `serp_status='zero_results'`, no crash, keyword-
  only drafting path.
- Provider error → typed `serp_error`, session still queryable, no partial
  competitor rows (transactional insert).
- Budget: pre-seed `api_costs` over the cap → 402 path, zero SERP network
  call (assert adapter `fetch` not invoked).
- Cache: identical (url, query) twice within TTL → second call performs
  zero adapter fetches (spy/mock counter) and reports `cache.hit=true`;
  `refresh: true` bypasses.
- Rate limit: 11th rapid call from one IP → 429.

### Phase C — Fix generation wiring (the rec adapter)

1. `src/audit/context_adapter.py`: build in-memory `rec`; call
   `draft_title`/`draft_meta_description`/`validate_*`/`content_outline_gaps`
   unchanged from `src/fixes/generator.py`.
2. Headings/outline recommendations: map `missing_sections` via
   `_SECTION_BY_HOOK` to suggested H2s (deterministic, labeled
   competitor-grounded).
3. Grounding payload assembly (§3.4 shape), including `unprotected` flag.

**Tests** (`tests/run_audit_generation_tests.py` — extends the existing
`run_fix_generator_quality_tests.py` conventions):
- Unit: rec adapter produces the exact dict shape the generator functions
  read (contract test pinning the keys).
- Unit: grounded drafting — fixture SERP with dominant "Best" framing →
  title lead "Best "; "year:2026" dominant → year lead; no pattern →
  keyword-forward fallback.
- Invariant battery (deterministic, no LLM): every validator problem class
  triggers (length bounds, banned patterns, caps runs, emoji, spam stacks,
  keyword-not-in-first-half, intent contradiction) → suggestion absent +
  `validator_problems` populated in the response.
- Duplicate guards in brand-agnostic mode: duplicate checks scope to the
  connected site when connected; in checklist mode they run against the
  single ingested page (self-duplicate impossible) and are reported as
  `unchecked_site_wide: true`.
- Mock-SERP → draft determinism: same fixture + same page → byte-identical
  drafts across two runs (deterministic engine invariant).

### Phase D — API surface + frontend

1. `src/api/routes/audit.py`: the three endpoints (§4.1–4.3), typed error
   contract, rate-limit middleware, auth prefix additions
   (`/audit` added to `_AUTH_PROTECTED_PREFIXES` in `main.py`).
2. `ui/screens/LiveAudit.js` + checklist cards + connected-mode FixPanel
   reuse; loading/error states per §4.5.
3. `POST /{session_id}/to-store` bridge with `approve` semantics (§4.3).

**Tests** (`tests/run_audit_api_tests.py` — follows `run_fix_api_tests.py`
patterns: in-process TestClient + mock mode):
- E2E mock: paste URL → 200 with full suggestion payload; assert no rows
  in `generated_fixes`/`recommendations`/`site_config` (read/write
  segregation invariant).
- Error battery: 400 invalid URL; 502 unreachable (mock HTTP failure);
  429 rate limit; 402 budget; 422 robots.
- Session replay: GET after POST returns identical suggestions without a
  second SERP fetch (adapter call counter = 1).
- to-store bridge (connected mock store): creates `proposed`
  recommendation; `approve: true` path goes proposed→approved→fix
  generated→diff approved→queued; executor picks up in mock GraphQL mode
  (full plan/21 two-gate flow preserved).
- Vercel-path parity: `/api/audit/live-url` and bare `/audit/live-url`
  both resolve (re-mount pattern per `main.py`).

### Phase E — End-to-end integration & soak

1. Live smoke (owner-funded, behind `INTEGRATION_MODE=live`): one real
   domain audit; assert latency ≤30s, cost logged, suggestions present.
2. Soak: N=50 varied URLs (products, collections, blogs, JS-heavy,
   non-ecommerce) in mock SERP mode → snapshot corpus retained for
   regression fixtures.
3. TTL sweep test: sessions past `expires_at` pruned, children cascaded.

**Acceptance criteria**:
- p95 end-to-end latency ≤ 10s in mock, ≤ 20s in live (SERP-dominant).
- Zero SERP spend on cache hits (measured by adapter-call counter).
- Zero `generated_fixes` writes from the read-only path (SQL count
  assertion in the E2E test).
- Every suggestion that reaches the UI passed the full validator (no
  draft ever bypasses `validate_title_draft` / `validate_meta_draft`).
- Connected-mode audit reuses the plan/21 two-gate + executor pipeline
  with no new write path (code inspection + graph test: audit modules
  import only `generator` functions, never `fix_executor` internals).

### Rollout order

Phase A → B → C are independently shippable (A alone = page-only audit;
A+B = SERP-grounded checklist; C completes grounding; D ships the UI;
E gates production). Each phase lands behind the existing
`INTEGRATION_MODE` switch with mock fixtures first, live spend last.
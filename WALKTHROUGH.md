# OpenSEO++ — Project Walkthrough

**Version:** 2.0 · **Date:** 2026-09-22 · **Verified against:** `main` @ `16fe1e0` (every claim below checked in code)

Single source of truth for how the system works end to end. Replaces the old `WALKTHROUGH.md`, `KT.md`, and `plan/18-founder-walkthrough.md`.

---

## 1. What the product is

An **SEO operations platform for e-commerce catalogues** running a repeating closed loop:

1. **Ingest** search, catalogue, and analytics data into one PostgreSQL model.
2. **Mine** the data for evidence-backed SEO opportunities (4 SQL generators).
3. **Evaluate** the strongest candidates with an LLM agent that applies documented skill playbooks.
4. **Present** at most 5 enriched recommendations per site to a human operator.
5. **Measure** whether each shipped change moved organic performance (baseline vs post-window, noise-filtered).
6. **Learn** — rejections and measured verdicts feed back into the next agent run.

Human judgment stays in the loop by design: nothing is ever published automatically. Success metric: `MVP Score = Acceptance Rate × Implementation Rate × Improvement Rate` (target > 0.30).

**Non-goals:** no auto-publishing, no content generation (agent writes specs, not prose), no causal inference (significance test is a noise filter), no real-time analysis (weekly batch), no backlink outreach, no proprietary crawler.

---

## 2. Repository map

```
api/index.py                  # Vercel serverless entrypoint (imports FastAPI app)
src/
  db.py                       # Postgres connection + schema apply + insert helper
  env.py                      # .env loader (never prints secrets)
  connectors/                 # Ingestion layer
    mode.py                   #   central mock/live switch (INTEGRATION_MODE)
    gsc.py / shopify.py / ga4.py / openseo.py / openseo_rest_adapter.py
    sync.py                   # per-site sync orchestrator (run_sync)
    costlog.py                # cost ledger + weekly budget caps
    url_normalize.py          # canonical URL helper
  generators/                 # runner.py (SQL loader) + orchestrator.py (dedupe/insert raw)
  agents/
    seo_agent.py              # payload, prompts, validation, persistence, run_agent
    ollama_client.py          # hosted Ollama Cloud JSON-mode client
    prompts/agent_system.md   # base system prompt (rules + schema + skill summaries)
  measurement/                # baseline / measure / classify / significance / thresholds
  api/                        # FastAPI: main, common (TRANSITIONS), routes/, models/
  jobs/                       # scheduler-agnostic CLI entry points + run_multi runner
plan/                         # design docs + schema + generator SQL + skill files
  00-schema.sql               # full PostgreSQL schema (single source of truth)
  01..04-generator-*.sql      # the 4 generators (per-site LIMIT baked in)
  05..09, 10, 13, 16, 17 *.md# agent structure, skills, architecture, validation plans
  12/14/15/16/18/19-*.sql     # incremental migrations (secrets, min-sample, volume, raw status, TTL, costs)
apps/openseo-plus/            # CURRENT operator console (React 19 + Vite)
ui/dist/                      # LEGACY plain-JS SPA (served by local FastAPI only — superseded)
tests/                        # 10 standalone suites + per-provider fixtures
scripts/                      # seed_remote, seed_operator_data, seed_actionseo_gsc, smoke_test_dataforseo
infra/open-seo                # vendor's open-source product — REFERENCE ONLY, not shipped
vercel.json / package.json / .env.example
```

---

## 3. Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, Pydantic v2, psycopg2-binary, uvicorn |
| Database | PostgreSQL (local Docker; production hosted over TLS) |
| Frontend | React 19, Vite 7, TanStack Router + Query, Tailwind 4 + daisyUI, recharts, sonner, lucide-react |
| LLM | Ollama Cloud (OpenAI-compatible `/v1/chat/completions`), JSON mode, `gpt-oss:120b` |
| Deploy | Vercel (static SPA + serverless FastAPI under `/api/*`), Node ≥ 22.12 |
| Data providers | GSC API, Shopify Admin API, GA4 Data API, DataForSEO REST (the "OpenSEO" product) |

Python deps (`requirements.txt`): `fastapi`, `pydantic>=2.7`, `psycopg2-binary`, `uvicorn`. Optional: `google-auth` (GSC service-account token source).

Frontend scripts (`apps/openseo-plus`): `dev` (Vite on **3010**, proxy `/api` → `127.0.0.1:8000`), `build` (`vite build && tsc --noEmit`), `types:check`, `lint` (oxlint), `format:write` (prettier). Root `package.json` just delegates the build for Vercel.

---

## 4. Architecture

Four loosely-coupled layers around **one** PostgreSQL database. Connectors write a normalized model; generators + agent read it and write recommendation rows; the API serves them to the SPA and enforces transitions; measurement closes the loop. Nothing touches the network without the mode layer allowing it.

```
Sources ─┐
GSC        │  connector adapters (mock/live, never-raise contract)
Shopify    ├─► sync.py run_sync (per site) ─► PostgreSQL unified model
GA4        │                                      │
DataForSEO ┘                                      ▼
                  plan/01..04 generator SQL ──► recommendations (status=raw)
                                                      │
                  seo_agent (LLM) ──► promotes raw → proposed (≤5/site)
                                                      ▼
                  FastAPI: /queue /pipeline /recommendations/* /measurements /results
                                                      ▼
                  React console (apps/openseo-plus)
                                                      ▼
                  operator: approve → implement → live
                                                      ▼
                  measurement_clock: post-window → significance → verdict
```

Key rules:

- **Mode switch** (`connectors/mode.py`): resolution order = per-call config → `INTEGRATION_MODE` env → legacy per-connector `*_MOCK_MODE` envs → default **mock**. No code path touches the network unless explicitly switched on. `daily_sync` builds adapters via `_build_adapter()`, which honors env-driven mode and falls back to mock with a loud log line when a live credential is unresolvable — **there are no hardcoded per-connector mock overrides in the sync path** (the only remaining `"shopify.mode": "mock"` literal is the standalone fallback inside `sync_catalogue_coverage` when no adapter is passed).
- **Adapter contract**: every connector exposes `supports(capability)` / `fetch(capability, params)` and **never raises** on provider failure — typed `{"ok": False, ...}` result, callers "skip and log, never crash". GSC and Shopify adapters **do paginate** internally (`max_pages` cap, default 100 pages × page size; a truncation warning is logged if the cap trips).
- **One credential discipline**: DataForSEO is ONE shared company credential resolved once by `get_openseo_adapter()` (`connectors/openseo.py`) — the only code path touching the secret. GSC/Shopify/GA4 are per-site.
- **Everything is idempotent**: sync upserts (`ON CONFLICT … DO UPDATE/NOTHING`), generator re-runs `DO NOTHING`, measurement re-runs delete-before-rewrite of post snapshots only.
- **Injectable clock**: every job takes `--reference_date`; live mode uses the real UTC date, mock falls back to `FIXTURE_ANCHOR_DATE = "2026-09-10"` so the fixture universe moves as one coherent block.

### Status machine (enforced at the API layer)

```
raw ──agent promotes──► proposed ──operator approves──► approved ──implement──► in_progress
 │                          │                               (baseline frozen)        │
 │──agent rejects─────────► rejected (reason logged)                                 ▼
 │                          │──operator rejects──► rejected                     live
 │                                                                           │
 └─ raw rows are never operator-visible (queue filters status='proposed')     ▼
                                                                    measured (verdict)
```

`api/common.py` holds the single `TRANSITIONS` table + `validate_transition()` → **409** on illegal jumps.

---

## 5. Data model (principal tables — `plan/00-schema.sql` + migrations)

| Table | Purpose |
|---|---|
| `site_config` | Per-site identity: domain, GSC property, catalogue tier, per-site threshold overrides, brand queries, agent tuning (`agent_prefetch_limit`, TTL columns, `min_sample_for_significance`) |
| `system_config` | System-global rows (`openseo.base_url`, `openseo.secret_ref`, `openseo.capabilities`) |
| `threshold_tiers` | Default thresholds per catalogue size (small/medium/large) |
| `measurement_window_lookup` | Observation days per action_type — **single source of truth** (create_page=49, improve_page=28, consolidate=28, technical_fix=21) |
| `pages` | Catalogue URLs: type, title, indexability, canonical, crawl depth, link counts, render status, raw/rendered HTML hash pair |
| `search_performance` | Daily GSC rows (date × query × page × country × device): clicks, impressions, CTR, position |
| `page_business_performance` | GA4 organic sessions/engagement/carts/orders/revenue per page/day |
| `keyword_clusters` / `cluster_queries` | Query groupings with volume, intent, commercial flags, recommended page type |
| `openseo_serp_snapshots` | Per-query SERP rows: URL, domain, position, `is_self`, snapshot date |
| `catalogue_coverage` | Per-cluster product match/in-stock counts, average price, existing collection URL |
| `page_query_match_scores` | Weighted match per page–cluster pair: `0.30·type_fit + 0.30·overlap(catalogue) + 0.25·gsc_evidence + 0.15·overlap(semantic)`; below tier `page_match_threshold` = not recorded |
| `recommendations` | **The pipeline spine** — diagnosis, evidence/work JSONB, impact/confidence/effort/owner, status machine, measurement fields, verdict |
| `change_log` / `measurement_snapshots` | Implementation records (before-snapshot) and baseline/post rows (`comparison_type` ∈ target/control/yoy) |
| `rejection_log` | Operator + agent rejections with reasons — the learning-loop source |
| `candidate_runs` | Per-run generator output counts (audit trail) |
| `api_costs` / `budget_config` | Per-call cost ledger + weekly caps (migrations `19-cost-tracking.sql`) |

---

## 6. The end-to-end pipeline

### 6.1 Daily sync — `connectors/sync.py` `run_sync(site, reference_date, force_refresh)`

Per site, in order (all idempotent, all through adapters):
1. **GSC search_analytics** → `search_performance` (last **7 days**; the adapter paginates, page cap 100 — truncation is logged, not silent).
2. **Shopify products + collections** → `pages` (paginated; indexability from published/active state).
3. **GA4 page_performance** → `page_business_performance`.
4. **DataForSEO keyword_volume** — single batch (currently 5 mock keywords) → `keyword_clusters` volumes, guarded by per-site `ttl_keyword_volume_days` (default 30).
5. **DataForSEO SERP** (currently one query, limit 10) → `openseo_serp_snapshots`, guarded by `ttl_serp_days` (default 7).
6. **Crawl audit** → `pages` audit columns (`sync_crawl_audit`; live flow posts an on-page task and polls up to `CRAWL_READY_ATTEMPTS=30` × 5s).
7. **Catalogue coverage** per cluster + **page–query match scores** (O(pages × clusters) full recompute; GSC-evidence component is real: 1.0 only when the page actually ranks for the query in `search_performance`).

Date seam (`_reference_window`): explicit `reference_date` → live mode uses `date.today()` → mock anchors to `FIXTURE_ANCHOR_DATE`. Any single connector being live switches the whole sync to the real clock. Budget pre-flight in `jobs/daily_sync.py` checks `check_budget("openseo_serp")` (falling back to the global cap) and returns `budget_cap_reached` before any paid spend when over cap; warnings fire at 80% (default).

`daily_sync` iterates **all configured sites** from `site_config` (per-site failure isolation, per-site summaries), or falls back to the sandbox mock site when the table is empty. `--reference_date` **is** forwarded into `run_sync`.

### 6.2 Candidate generation (weekly) — `generators/`

- `runner.run_generator()` loads the plan SQL, binds `:site_id` + `:reference_date`, returns candidate dicts. Per-generator quotas (15/20/15/10, combined cap `MAX_PER_SITE = 60`) are enforced by the SQL's own trailing `LIMIT` — Python parses it, never restates it.
- `orchestrator.run_candidate_generation()` builds typed rows per generator, dedupes by (generator, cluster, target/proposed URL), validates `action_type` against `measurement_window_lookup` **at insert time** (fail fast), inserts as `raw` with `DO NOTHING` conflict handling, and writes `candidate_runs` bookkeeping.
- Missing-page impact gating: `search_volume >= _impact_volume_threshold(site)` (site_config override, default **5000**) → `high`, else `medium`.
- Generators:

| File | Generator | Action type | Finds | Cap |
|---|---|---|---|---|
| `plan/01` | missing_page | create_page | Search demand with no strongly-matching page (catalogue depth + SERP competitor signals; excludes clusters with an active raw/proposed/approved/in_progress rec) | 15 |
| `plan/02` | existing_opportunity | improve_page | Underperforming pages (position window, CTR/click declines) | 20 |
| `plan/03` | technical_fix | technical_fix (+typed variants) | Indexability/canonical/status/sitemap/orphan/structured-data/JS-rendering root causes | 15 |
| `plan/04` | cannibalization | consolidate | Two own pages alternating rankings for one cluster | 10 |

Note: the **90-day cooldown** appears in the design docs (`plan/09`, `plan/10`) but the current generator SQL excludes only *active*-status recommendations — rejected rows are not re-excluded by age. This is a doc-vs-code gap, listed in §12.

### 6.3 Agent evaluation (weekly) — `agents/seo_agent.py`

`run_agent(site_id)`:
1. **`build_agent_input()`** — raw candidates (LIMIT `site_config.agent_prefetch_limit` or 25, newest first) each enriched with cluster context, structured generator evidence, live SERP rows, page context (crawl columns + GSC aggregates); plus last **5** `rejection_log` rows as few-shot context; plus site config and `max_recommendations = MAX_RECOMMENDATIONS` (5).
2. **`build_system_prompt()`** — `prompts/agent_system.md` + the full skill file injected verbatim for each generator present (`SKILL_FILES_BY_GENERATOR`: `plan/16-skill-technical-fix.md`, `plan/17-skill-consolidate-cannibalization.md`; missing-page and existing-opportunity rules are carried inline in the base prompt).
3. **Ollama Cloud** JSON-mode call (`ollama_client.py` — raises loudly without credentials, no offline stub; retries once on truncated JSON; captures token usage).
4. **`validate_agent_output()`** — strict schema: ≤5 recommendations, required keys, `action_type` against `measurement_window_lookup`, candidate-id matching (with an action-type + URL-overlap fallback when the model mangles IDs).
5. **`persist_agent_output()`** — accepted rows enriched (diagnosis, evidence, work items, impact/confidence/effort/owner, measurement metric + window, `measurement_due_at = now() + window`) and promoted **raw → proposed**; rejected rows set to `rejected` with reason + `rejection_log`. Only the agent promotes to proposed.

### 6.4 Operator loop — `apps/openseo-plus` + `api/routes/`

- **Action Queue** (`/p/$projectId/action-queue`): proposed cards with impact/effort/owner chips, volume badge, position stat (from evidence `avg_position` or a tolerant diagnosis-text parse — NaN-proof), filters (status, generator, owner, action type, impact, effort), sort (incl. volume), search, **High Impact / Quick Wins** presets, active-filter pills, "N of M" counter respecting filters.
- **Detail drawer** (`components/DetailDrawer.tsx`): full case file — evidence panel, work-required items with acceptance criteria, measurement plan, cluster context, SERP ladder "who outranks you" (own-domain highlighted), catalogue coverage block; approve / reject-with-reason / assign actions.
- **Pipelines** (`/pipelines`): kanban **approved → in progress (incl. live) → measured** (raw column dropped) with `Day X/N` / overdue observation countdowns (`measurement_due_at`); implement + mark-live actions inline.
- **Results** (`/results`): verdicts with Won/Neutral/Lost trend chart, outcome breakdown, incremental clicks/revenue (won only), detail modal.
- **Multi-site**: navbar **SiteSwitcher** (fetches `/queue/sites`) navigates between sites; brand shown as **ActionSEO**.
- Shared data layer (`data/useOperatorData.ts`): one API client with a **hardcoded `/api` fallback** (`API_BASE` always ends in `/api`, so calls can never be bare paths), typed hooks, React Query caching.

### 6.5 Measurement — `measurement/`

- **`baseline.py`**: `store_baseline()` freezes the pre-implementation target snapshot (GSC + GA4 aggregates over a window ending at implement) + paired control group (up to 2 same page_type/template peers with ≥ the minimum-history bar, excluding pages with active changes) + optional YoY. `store_cluster_baseline()` measures create_page at the **cluster level** (GSC footprint of all `cluster_queries` across all pages — an honest zero-footprint "no presence" signal is allowed).
- **`measure.py`**: `store_post_snapshots()` / `store_cluster_post_snapshots()` capture the post window over the **SAME** paired control URLs from baseline (`control_group_json`) — never re-paired (fair DiD). `run_measurement_batch()` handles approved rows with never-crash skip-and-log semantics.
- **`classify.py`**: gates in order — (1) minimum sample (default 50, per-site `min_sample_for_significance`), (2) confound protection (control **or** YoY must exist, else inconclusive), (3) two-proportion z-test (α=0.05), (4) difference-in-differences vs the paired control's own lift. **Won** = improved (>15% clicks/sessions, or commercial >10%, or growth-from-zero) + significant + not contradicted by control trend/DID; **Lost** = decline <−15% + significant + not mirroring control; else Neutral/Inconclusive. All numbers come from `thresholds.py` (single source: `ALPHA=0.05`, `IMPROVE=0.15`, `COMMERCIAL=0.10`, `NEUTRAL_BAND=0.05`, `DECLINE=0.15`, `DID=0.10`, `MIN_SAMPLE=50`).
- **`measurement_clock.py`** (`jobs/measurement_clock.py`): daily sweep — `live` rows where `implemented_at + window_days <= reference_date`; delete-before-rewrite of post snapshots only (baseline preserved); idempotent.

---

## 7. API surface (FastAPI)

Mounted bare (local) and under `/api` (Vercel). Bearer-token auth via `API_AUTH_TOKEN` when set (`api/common.py`); `/health` and static assets stay public. All endpoints site-scoped or id-scoped.

**Queue** (`routes/queue.py`)
- `GET /queue?site_id&generator&action_type&owner&impact&search_volume_min&search_volume_max&limit` — proposed cards (volume from `keyword_clusters` LEFT JOIN), ordered impact → volume → recency, `total_proposed` reflecting the same filters.
- `GET /queue/{id}` — full detail (evidence, work, measurement plan, cluster, catalogue, SERP context with own-domain highlighting).
- `GET /queue/stale-approvals?site_id&stale_days` — approved-but-unimplemented.
- `GET /queue/sites` — site list (login gate + navbar switcher).
- `GET /pipeline?site_id` — approved + in_progress (incl. live) with `measurement_due_at` and window days.

**Decisions** (`routes/decisions.py`)
- `POST /recommendations/{id}/approve` · `/reject` (reason mandatory → `rejection_log`) · `/assign` (owner/assigned_to) · `/live`

**Measurements** (`routes/measurements.py`)
- `POST /recommendations/{id}/implement` — transition + baseline freeze (cluster baseline for create_page)
- `POST /recommendations/{id}/implement-safe` — never-500 variant: transition always commits; baseline best-effort with degraded zeroed fallback; change_log best-effort
- `GET /recommendations/{id}/measurement` — snapshots + classification
- `GET /results?site_id` — dashboard feed
- `POST /measurements/run` — on-demand batch measurement

**Other**: `GET /health` — DB report + mode + agent-credential status (never 500s). `api/main.py` handles CORS, `/api`-prefixed mounts, static SPA hosting, and the auth middleware.

---

## 8. Background jobs (`src/jobs/`)

All `python -m jobs.<name>`, each with `--site_id` / `--reference_date`, each opening its own DB connection.

| Job | Command | Role | Designed cadence |
|---|---|---|---|
| `daily_sync.py` | `python -m jobs.daily_sync [--force-refresh]` | All sites: connector sync + budget pre-flight + webhook summary | Daily 01:00 UTC |
| `measurement_clock.py` | `python -m jobs.measurement_clock` | Measure + classify `live` rows past window | Daily |
| `weekly_candidates.py` | `python -m jobs.weekly_candidates` | Per-site generator run | Weekly |
| `weekly_agent.py` | `python -m jobs.weekly_agent` | Per-site agent pass (Ollama budget gate) | Weekly |
| `weekly_crawl.py` | `python -m jobs.weekly_crawl` | Pull crawl/audit data | Weekly |
| `semantic_scoring.py` | `python -m jobs.semantic_scoring` | Embedding-based semantic scores | Weekly |
| `stale_approvals.py` | `python -m jobs.stale_approvals` | Surface approved-but-unimplemented | Daily/weekly |
| `cost_report.py` | `python -m jobs.cost_report` | Weekly spend by service + budget status | Weekly |
| `measurements.py` | `python -m jobs.measurements` | Measurement sweep wrapper (batch variant) | On demand |

**`run_multi.py`** (helper, not a job): `run_across_sites()` — per-site failure isolation, bounded concurrency (`JOB_MAX_WORKERS`, default **1** = strictly sequential, safe for DataForSEO rate limits), optional `JOB_TIMEOUT_SECONDS` per-site wall-clock cap. Contract: each worker opens its **own** DB connection (psycopg2 is single-threaded). Notifications via `jobs/notify.py` → Slack-compatible `NOTIFICATION_WEBHOOK_URL` (unconfigured = silently skipped, never a crash).

**No scheduler is wired in the repo** (no `.github/workflows`, no Vercel crons). Jobs are CLI-only and cannot run inside Vercel's 60s serverless window — an external runner (GitHub Actions schedule, cron, or worker host) is required. This is unchanged.

---

## 9. External integrations & cost model

| Service | Access | Capabilities | Mock fixture |
|---|---|---|---|
| GSC | Per-site service account (`GSC_SERVICE_ACCOUNT_KEY_FILE`/`_JSON`) | sites, search_analytics (paginated) | `gsc_*.json` |
| Shopify | Per-site admin token | products, collections (paginated) | `shopify_*.json` |
| GA4 | Per-site property credentials | page_performance | `ga4_page_performance.json` |
| DataForSEO | One shared credential (`DATAFORSEO_API_KEY`) | keyword_volume, serp, competitors, backlinks, crawl_audit | `dataforseo_*.json` |
| Ollama Cloud | `OLLAMA_API_BASE` + `OLLAMA_API_KEY` | chat completions (JSON mode) | none — agent always live |

All DataForSEO access flows through `openseo_rest_adapter.py` (typed `supports()`/`fetch()`, per-capability request builders, normalizers). The provider-reported cost is extracted from every raw envelope and logged via the `openseo._on_fetch_complete` callback into `api_costs`. Controls: batch keyword-volume calls, per-capability TTL freshness gates, capability gating (unsupported = typed skip, never a surprise line item), weekly caps per service + global with 80% warning and hard-stop, sequential multi-site execution.

**Observed unit costs** (live-verified, US/EN): keyword volume $0.09/request (per-request minimum; ~$0.018/kw at 5 keywords), SERP organic $0.002/request, on-page crawl task $0.50. Worked weekly envelopes: volumes+SERP only ≈ **$0.10**; +crawl ≈ **$0.60**; larger catalogue (20 kw + 10 SERP + crawl) ≈ **$0.88**; 5 sites minimal ≈ **$0.50**. Crawl is the only meaningful line item; TTL windows are the primary lever.

---

## 10. Deployment

**Vercel** (project `openseo-plus`), monorepo root:
- `vercel.json`: `framework: vite`, `buildCommand: npm run build` (root wrapper installs + builds `apps/openseo-plus`), `outputDirectory: apps/openseo-plus/dist`, rewrites `/api` and `/api/(.*)` → `/api/index.py`, everything else → SPA fallback.
- Backend = FastAPI imported by `api/index.py`, served as a Python serverless function; PostgreSQL over TLS via `POSTGRES_HOST/PORT/DB/USER/PASSWORD`.
- Live: **https://openseo-plus.vercel.app** (canonical domain; Vercel preview URL is auth-gated). Deploys from `main`.
- Prod data seeded via `scripts/seed_remote.py` (remote-only DB binding) / `scripts/seed_operator_data.py`; `.env.vercel-prod` holds prod env (secrets — never commit).

**Local development**:
1. Venv + `pip install -r requirements.txt` (+ `google-auth` only for live GSC).
2. `cp .env.example .env` — mode defaults **mock**, zero credentials needed.
3. Postgres via Docker (`plan/00-schema.sql` applied via `db.apply_schema()`; migrations `12..19-*.sql` manual).
4. Pipeline: `python -m jobs.daily_sync` → `python -m jobs.weekly_candidates` → `python -m jobs.weekly_agent` (needs Ollama creds).
5. API: `python -m uvicorn api.index:app --reload --port 8000`.
6. Frontend: `cd apps/openseo-plus && npm install && npm run dev` → http://localhost:3010.
7. Seed helpers: `scripts/seed_operator_data.py`, `scripts/seed_actionseo_gsc.py` (sandbox seeder: second site_config row + 64 days of deterministic GSC rows + real generator runs), `scripts/smoke_test_dataforseo.py` (live auth/cost smoke test).

The sandbox seeder also powers the **ActionSEO sandbox** flow in the UI's multi-site switcher (commit `19f41dd`).

---

## 11. Tests (`tests/`)

Standalone suites, run directly (`python tests/<name>.py`), need a live local Postgres + `tests/fixtures/`:

| Suite | Covers |
|---|---|
| `run_mock_tests.py` | Adapter contract in mock mode |
| `run_no_socket_tests.py` | No network calls when mock |
| `run_gsc_tests.py` | GSC adapter specifics |
| `run_integration_mock_tests.py` | Sync + generators + agent validation integration |
| `run_agent_tests.py` | Agent validation/persistence |
| `run_api_tests.py` | API surface + transitions |
| `run_api_day_in_the_life.py` | Full API loop against a real DB |
| `run_day_in_the_life.py` | Full pipeline: sync → generate → evaluate → approve → measure |
| `run_measurement_tests.py` | Baseline/post/classify logic |
| `run_live_agent.py` | Live Ollama agent (needs creds) |

Fixtures (`tests/fixtures/`): GSC sites/analytics, Shopify products/collections (Aurora Audio demo storefront: 7 in-stock swim products, published/unpublished collections, draft/out-of-stock variants, indexability/canonical defects, SERP alternation), GA4 page performance, DataForSEO keyword-volume/SERP/competitors/backlinks/crawl-audit. Mock runs record fixture cost fields into the ledger too — the fixture universe is a validation harness, not a parallel demo pipeline.

---

## 12. Known limitations (code-verified, current)

In priority order:

1. **`measurement_due_at` is set at agent promotion, not implement** (`seo_agent.py:374` sets `now() + window` at proposal time; neither `implement` nor `implement-safe` recomputes it). A late implementation can have an already-elapsed due date — the clock then fires immediately, measuring a window that includes pre-live data. The UI's `Day X/N` countdown inherits this skew.
2. **SERP + keyword volume fetches are hardcoded to fixture scope** (`sync.py:667-691`): one fixed 5-keyword batch and a single SERP query. Real multi-cluster sites need iteration over `keyword_clusters` — TTL gates exist but the fetch loop doesn't.
3. **Doc-vs-code: 90-day cooldown** is documented (`plan/09`) but generator SQL only excludes *active* recommendations — rejected/aged ideas can resurface.
4. **GSC data-freshness lag vs exact-window measurement**: the clock measures at exactly `implemented_at + window_days`; GSC's ~3-4 day reporting lag can leave the final days' data incomplete. No settle buffer.
5. **No data retention**: the only `DELETE` in `src/` is the measurement clock's snapshot rewrite. `search_performance`, `openseo_serp_snapshots`, `rejection_log` grow unbounded.
6. **No scheduler/CI**: no `.github/workflows`, no Vercel crons — jobs run only when invoked manually.
7. **`page_query_match_scores` is O(pages × clusters)** recomputed from scratch per site — slow at real scale.
8. **No migration runner**: `apply_schema()` runs only `plan/00-schema.sql`; incremental migrations are manual → schema-drift risk.
9. **No concurrency guards** between overlapping job runs (no advisory locks) — safe only while jobs run sequentially.
10. **Rejection log writes are in-transaction with status updates** (`seo_agent.py:407-416`) — a log failure rolls back the whole agent run.
11. **Volume chips are cluster-level, not page-level**: the UI shows the cluster's `search_volume` on every card sharing it (known UX confusion).
12. **LLM impact/confidence unvalidated post-agent**: Phase 5 validation tables (`plan/10`) are designed but not built.

**Resolved since the old audit** (was listed as P0, now fixed in code): GSC pagination exists (`max_pages` loop with truncation warnings); sync dates advance in live mode (`_reference_window` uses the real clock; anchor only in mock); `daily_sync` honors `INTEGRATION_MODE` for all connectors via `_build_adapter` (mode is env-driven with a loud mock fallback on missing credentials) and forwards `--reference_date`; mock-mode entry points load `.env` with `include_secrets=False`.

---

## 13. Code map

| "Where do I look for…" | File |
|---|---|
| How a recommendation gets created | `generators/orchestrator.py` + `plan/01..04-*.sql` |
| How the agent decides | `agents/seo_agent.py`, `agents/prompts/agent_system.md`, `plan/05/06/07/08/16/17` |
| Why a card shows position/volume/effort | `api/routes/queue.py` + `apps/openseo-plus/src/features/operator/data/useOperatorData.ts` |
| How measurement works | `measurement/baseline.py`, `measure.py`, `classify.py`, `thresholds.py`, `significance.py` |
| How the DB is accessed | `src/db.py` (`get_connection`, `insert_rows`, `apply_schema`) |
| Status transitions | `api/common.py` `TRANSITIONS` |
| Site config surfaces | `plan/00-schema.sql` `site_config`, `threshold_tiers`, `measurement_window_lookup` |
| How a connector behaves | `connectors/mode.py`, `connectors/<name>.py`, fixtures in `tests/fixtures/` |
| How money is spent/guarded | `connectors/costlog.py`, `jobs/daily_sync.py` pre-flight, `plan/19-cost-tracking.sql` |
| Mode switching | `connectors/mode.py` (`resolve_mode`) — config value → `INTEGRATION_MODE` → legacy envs → mock |
| The demo flow end-to-end | `tests/run_api_day_in_the_life.py`, `scripts/seed_operator_data.py`, `scripts/seed_actionseo_gsc.py` |

---

## 14. Quick reference — env vars

| Var | Default | Meaning |
|---|---|---|
| `INTEGRATION_MODE` | `mock` | Central mock/live switch for all connectors |
| `OPENSEO_MOCK_MODE` / `GSC_MOCK_MODE` / `SHOPIFY_MOCK_MODE` / `GA4_MOCK_MODE` | unset | Legacy per-connector overrides (still honored) |
| `POSTGRES_HOST/PORT/DB/USER/PASSWORD` | localhost:5432 `openseo`/`openseo`/`openseo_local_only` | DB connection (prod: hosted TLS) |
| `DATAFORSEO_API_KEY` | — | Shared OpenSEO credential (live) |
| `GSC_SERVICE_ACCOUNT_KEY_FILE` / `_JSON`, `GSC_SITE_URL` | — | Per-site GSC credential + property |
| `SHOPIFY_DOMAIN`, `SHOPIFY_ACCESS_TOKEN` | — | Per-site Shopify (live) |
| `GA4_PROPERTY_ID`, `GA4_ACCESS_TOKEN` | — | Per-site GA4 (live) |
| `OLLAMA_API_BASE` / `OLLAMA_API_KEY` / `OLLAMA_MODEL` | `https://ollama.com/v1` / — / `gpt-oss:120b` | Agent LLM (required, fails loudly) |
| `API_AUTH_TOKEN` | unset (auth off) | Shared bearer token for `/api` data routes |
| `NOTIFICATION_WEBHOOK_URL` | — | Slack-compatible summary webhook |
| `WEEKLY_BUDGET_*` / `WEEKLY_BUDGET_GLOBAL` | unset | Weekly spend caps per service / global |
| `JOB_MAX_WORKERS` | `1` | Multi-site concurrency (keep 1 for DataForSEO rate limits) |
| `JOB_TIMEOUT_SECONDS` | unset | Per-site wall-clock timeout |
| `GSC_MOCK_FIXTURES_DIR` | `tests/fixtures` | Mock fixture location |
| `VITE_API_BASE_URL` / `VITE_API_BASE` | same-origin | API base override; `/api` fallback hardcoded |

**Security rules:** never log/echo credential values; `.env` and `.env.vercel-prod` are git-ignored and must never be committed; mock-mode entry points load `.env` with `include_secrets=False` so live keys never enter a mock process; DataForSEO secrets resolve via one facade path (`get_openseo_adapter`), config references secrets (`plan/12` secret-ref pattern), never values.

**Production status:** full loop runs end-to-end on fixture data (mock mode) against the live deployment; DataForSEO is live-verified (auth, normalization, cost capture, budget enforcement); real-store cutover is configuration + credentials, not code.
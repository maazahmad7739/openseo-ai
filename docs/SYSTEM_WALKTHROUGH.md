# OpenSEO AI — Complete System Walkthrough

**Date:** 2026-09-30
**Verified against:** working tree at `b7fac32` + ~45 uncommitted files (the batch that added collection fixes, Framer/Netlify routing, canonical autofix, link ingest, redirect graph, webhook hardening, tenant provenance)
**Method:** every claim below was read out of source, not out of `docs/`. Where documentation disagrees with code, the doc is called out explicitly.

### How to read the status tags

| Tag | Meaning |
|---|---|
| **IMPLEMENTED** | Code exists, wired into a path, and does what this says. |
| **PARTIAL** | Code exists but is stubbed, unwired, or its output is discarded. |
| **PLANNED** | Described in `plan/*.sql` or docs, has no code behind it. |
| **NOT IMPLEMENTED** | Absent from the codebase entirely. |

### Critical starting facts

1. **Out of the box, this system changes nothing on any website.** Every `fix_policy` row is seeded `enabled=false` (`plan/22:96-117`, and the same pattern for framer/netlify in `plan/26:35-45`, `plan/27:66-77`, `plan/28:16-22`). Writes are blocked fail-closed until an operator flips the policy per site.
2. **Nothing runs the scheduler in any deployed environment.** There is a CI workflow (`.github/workflows/ci.yml`) but it is `on: push` + `pull_request` only — **no `schedule:` trigger**. No Vercel cron. Jobs run only when a human starts `python -m jobs.scheduler` or invokes `--once`. This is the single largest gap between "the code works" and "the product runs."
3. **No audit score exists.** There is no 0–100 health metric anywhere in the codebase. Findings are pass/fail checks plus a `high/medium/low` impact label and a `high/medium/low` risk tier on fixes. Do not look for one.

---

## 0. What changed most recently

The last two weeks of work (commits `8f869a4` → `b7fac32`, plus a large uncommitted batch) moved the system from *"analytical engine with no last pipes"* to *"a system that actually writes to live stores and can undo it."* This section is the changelog; the rest of the document is the steady-state explanation.

### 0.1 Committed (2026-09-24 → 2026-09-25)

| Commit | What it changed | Why it mattered |
|---|---|---|
| `8f869a4` | Lazy-draft auto-fix diffs from the detail drawer; removed a dead queue link | The operator no longer has to guess which fix types exist for a recommendation — the drawer drafts every supported one on open. Per-field failures no longer 500 the endpoint. |
| `1fb537b` | Surface fix approve/reject API errors instead of failing silently | Previously an operator could click Approve and get no feedback on a 409/422. |
| `d36e922` | Empty previous-meta placeholder; task text scoped to engine capabilities | Stops the UI from promising work the engine cannot actually perform. |
| `fa12aac` | **"Apply now" button for queued fixes** → `POST /fixes/{id}/execute` | A manual execution path that runs the *same* `_execute_one` under the *same* per-site advisory lock as the scheduled job. This is a deliberate escape hatch, not a bypass of the gates. |
| `2fe5a0e` | Allow fix drafting for `in_progress` recommendations | Previously a recommendation that had already been implemented could not have a fix drafted — a real workflow dead-end. |
| `781ae28` | Dedupe keyword/product token overlap in title and meta drafts | Fixed drafts like "Yoga Mat — Yoga Mat Premium Eco" when the product title already contained the keyword. 245 lines added including tests. |
| `27b60d6` | **Collection description + collection create auto-fix paths** (plan/24, plan/25) | The largest single change in the batch: **+3,620 lines across 12 files.** Two new fix sub_types, 1,210 lines of new tests, two new design docs. |
| `138e697` | Create-path drafters survive real-store data | The collection create/description drafters were validated against fixtures and failed on real store data. 131 lines of hardening. |
| `36373cb` | `FixOut.target_entity_ref` made optional | Create-fixes legitimately carry a NULL GID until the executor mints the entity — the response schema was rejecting its own valid output. |
| `c215935` | Editorial collection descriptions + live-verified `collectionCreate` input arg | The `input:` argument name was verified against the live API, not assumed. |
| `b26ec19` | Resolve publication id via the shared resolver on the create path | Removed a second, divergent way of resolving publications. |
| `f2986a7` | **Idempotent self-heal routing for `collection_create`** | If a prior run created the collection but died before publishing, the next run resolves the handle, finds it, skips creation, and routes to publish-only. +108 lines. This is the fix that makes an interrupted run safe to retry. |
| `1ec713c` | `Publication.name`, not `.title` | A field-name bug caught by a live call, not by reading docs. |
| `b7fac32` | Publishable read-back fields | `Publishable` has no `id` field; presence must be detected via `publishedOnPublication`, and stores with no publications reject any `publicationId` at all. |

The pattern across these commits is worth naming: **most of them are corrections found by calling the live API, not by reading documentation.** `input:` vs positional args, `name` vs `title`, `Publishable` having no `id`, `publicationId` rejection on publication-less stores, and the whole-object `productUpdate.seo` replacement hazard are all real API behaviors now encoded in the adapters with `LIVE-VERIFIED` comments.

### 0.2 Uncommitted — new capabilities (9 migrations, 8 new source modules, 12 new test suites, 2 new fixtures)

**Migrations added** (all idempotent, ledger-tracked by `scripts/migrate.py`):

| File | What it enables |
|---|---|
| `plan/24-collection-description-autofix.sql` | `collection_description` fix sub_type + policy seed |
| `plan/25-collection-create-autofix.sql` | `collection_create` fix sub_type + policy seed |
| `plan/26-framer-onboarding.sql` | `site_config.platform` seam + `framer_site_ref` + framer policy seeds |
| `plan/27-netlify-github-pr.sql` | `platform='netlify'`, `github_repo`/`github_branch`/`github_root_dir` + 3 policy seeds |
| `plan/28-canonical-tag-autofix.sql` | `netlify.canonical_tag` policy seed |
| `plan/29-webhook-hardening.sql` | Partial expression index `idx_fixes_pr_url` on `snapshot_json->>'pr.url'` so webhook fan-out is O(fixes-per-PR) instead of a full scan |
| `plan/30-schema-business-profile.sql` | Per-site `business_service_types`, `business_areas_served`, `business_offer_name` so JSON-LD grounds on the *client's* business instead of hardcoded agency copy |
| `plan/31-tenant-mode-and-provenance.sql` | `integration_mode` per site, `data_source` provenance columns, FK + cascade on previously unscoped `api_costs`, per-tenant `shopify_access_token`, per-site `target_keywords` |
| `plan/31-page-links-ingest.sql` | `page_links.rel`, per-site edge uniqueness (the v1 key ignored `site_id`), `idx_pl_source` |

**New source modules:**

| File | Purpose |
|---|---|
| `src/connectors/framer.py` | Framer write client — **contract only, live path returns a typed `live_writes_disabled` refusal** |
| `src/connectors/github_static.py` | GitHub REST v3 client: read file, branch, commit, open PR, close PR. **Live path is fully implemented**, flag-gated behind `NETLIFY_LIVE_WRITES=1` |
| `src/connectors/write_credentials.py` | Single per-platform credential dispatcher returning typed `{ok, platform, config, outcome, detail}`; never logs values |
| `src/audit/link_ingest.py` | Internal link edge extraction — internal-only, relative/protocol-relative resolution, fragment stripping, query strings kept, self-loop and `mailto`/`tel`/`javascript` skipping, dedupe per page, fail-soft |
| `src/audit/url_pattern.py` | URL → page_type archetype mapping (`/products/`, `/collections/`, `/blog/`, `/guides/`, `/articles/`, `/pages/`) |
| `src/audit/structured_data.py` | **Fixes a real false-positive bug** — see below |
| `src/fixes/redirect_graph.py` | Pure redirect-chain logic: flatten A→B, B→C into A→C; refuse cycles, self-loops, and chains over 10 hops |
| `src/api/routes/github_sync.py` | PR merge sync + HMAC-verified webhook receiver |

**The `structured_data.py` fix deserves specific attention.** `pages.has_structured_data` is `BOOLEAN DEFAULT false`. Every ingest path that fetched a page but never wrote that column stored a *default*, not an observation — and generator 3 (`plan/03`, `structured_data_failure`) read that default as "verified: no schema", so **uncrawled and untagged pages were reported as missing structured data**. The new module detects all three syntaxes (JSON-LD, microdata, RDFa) and, critically, returns `None` for "we did not look" so callers can distinguish *absent* from *unobserved*. It is wired into all three ingest paths (`site_fetch.py:453`, `sync.py:486`, `context_adapter.py:352`), and the agent now carries a `structured_data_verifiable` trust flag (`seo_agent.py:278`) to suppress the finding when the page was never actually inspected.

**New tests:** `run_link_graph_tests.py`, `run_link_ingest_tests.py`, `run_p2_auth_hardening_tests.py`, `run_phase0_credentials_tests.py`, `run_phase1_sitemap_ingest_tests.py`, `run_phase2_platform_routing_tests.py`, `run_phase3_framer_adapter_tests.py`, `run_phase4_api_dispatch_tests.py`, `run_phase5_netlify_pr_tests.py`, `run_phase6_canonical_tag_tests.py`, `run_redirect_chain_tests.py`, `run_schema_base_url_tests.py` — organized as numbered phases mirroring the plan rollout.

**Uncommitted source growth:** `src/fixes/adapters.py` +1,004 lines, `src/fixes/generator.py` +803, `src/connectors/sync.py` +450, `src/api/routes/queue.py` +305, `src/api/routes/fixes.py` +220, `src/db.py` +151, `src/jobs/fix_executor.py` +162, `src/site_fetch.py` +210. Total across the batch: **+4,231 / −544 lines across 41 tracked files.**

**Also new:** `.github/workflows/ci.yml` (backend + frontend jobs, Postgres 16 service on 5433, 600 s per-suite timeout). It runs on `push`/`pull_request` only — **no `schedule:` trigger**, see §8. And two onboarding runbooks: `docs/FRAMER_ONBOARDING_RUNBOOK.md`, `docs/NETLIFY_STATIC_SITE_ONBOARDING_RUNBOOK.md`.

### 0.3 Housekeeping debt introduced by this batch

- **`tmp_canonical_e2e.py`, `tmp_e2e2.py`, `tmp_e2e3.py`, `tmp_e2e_final.py`, `tmp_fail2.py`, `tmp_prod_debug.py`** are untracked scratch scripts at the repo root. They should be deleted or moved into `scripts/` before this is committed.
- **`gethelium.co-Performance-on-Search-2026-09-25.zip`** is untracked at the repo root — a real customer GSC export. **Must not be committed.** Confirm `.gitignore` covers `*.zip` and raw GSC exports.
- `src/db.py` defines `get_connection()` twice (`db.py:62` and `db.py:175`); the second shadows the first. Behaviorally identical, but it should be deleted.
- `CONTROL_GROUP_LIMIT = 20` in `measurement/baseline.py:28` is defined and imported but never used — the actual pairing cap is `CONTROL_URL_COUNT = 2`. Dead constant; delete it or use it.
- `plan/31` had a real ordering bug: provenance columns were referenced before the migration ran on fresh DBs, producing `UndefinedColumn`. Documented in `docs/category-2-blueprint.md:144-146` and fixed by adding `page_business_performance.data_source` to the SQL. Worth remembering if you add more provenance columns.

---

## 1. Overall SEO coverage

### 1.1 IMPLEMENTED

| SEO area | What the system actually does | Code |
|---|---|---|
| **Titles** | Detects blank / outside 20–60 chars / missing target keyword. Drafts replacements via LLM with a deterministic fallback, then runs a validator gauntlet. Auto-fixes on Shopify. Auto-fixes on Netlify via GitHub PR. | `src/fixes/generator.py:892-899` (detect), `:1062-1107` (validate), `:1252-1308` (draft) |
| **Meta descriptions** | Detects blank / outside 70–155 chars / missing keyword. Same draft+validate pipeline. Shopify + Netlify write surfaces. | `generator.py:1318-1529` |
| **Collection page descriptions** | Detects missing/short/thin collection copy. Drafts 100–120 word HTML bodies through a sanitizer allowlist and a sentence-level overlap tripwire. Shopify write. | `generator.py:3272-3548`, `:3722-3908` |
| **Content gaps vs competitors** | Scans the top 3 competitor URLs' H1–H3 outlines, maps them to section archetypes, and reports sections competitors have that the page lacks (max 3 suggested H2s). | `src/audit/competitor_headings.py`, `generator.py:553-592` |
| **Internal linking (analysis)** | Ingests internal link edges per audited page, produces orphan / deep-page / weak-anchor suggestions. | `src/audit/link_ingest.py`, `plan/05-generator-internal-links.sql` |
| **Indexing signals** | GSC URL Inspection pulls verdict, coverage state, robots.txt state, Google-selected canonical, and sitemap membership into `pages.gsc_*`. 7-day TTL, 400 URLs/run, 2000/day quota. | `src/connectors/sync.py:760-870`, `plan/24` |
| **Sitemap / robots** | Own fetcher parses `robots.txt`, follows `Sitemap:` directives, walks sitemap indexes (5000 entries, depth 2, 10 children, 2.5 MB body cap) and records real membership + robots allowance per page. | `src/site_fetch.py` |
| **Canonicals** | Detects via GSC inspection + crawl columns. **Auto-fix exists only on Netlify** (self-referencing canonical injected as a PR). | `plan/28`, `generator.py:2670-2718` |
| **Redirects** | Detects 404/410 pages with residual traffic and consolidation targets. Auto-creates Shopify 301s with full chain flattening and loop refusal. | `src/fixes/redirect_graph.py`, `generator.py:2112-2274`, `:3194-3254` |
| **Structured data / JSON-LD** | **Detection is now real** (new `src/audit/structured_data.py`): all three syntaxes — JSON-LD, microdata, RDFa — detected from raw HTML at ingest, with a `None` "did not look" state that no longer reads as "verified absent" (§0.2). Generation and injection are Netlify-only, grounded on the client's own business profile per `plan/30`. | `src/audit/structured_data.py`, `sync.py:486`, `context_adapter.py:352`, `generator.py:2368-2667` |
| **Crawl / technical root causes** | SQL generator surfaces status-code failures, orphans, JS-rendering problems, faceted URL proliferation, sitemap mismatches. | `plan/03-generator-technical-root-causes.sql` |
| **Cannibalization** | Detects two own pages alternating for one cluster; produces consolidation recommendations. | `plan/04` |
| **Publish state** | Detects unpublished products/collections that should be live; auto-fixes the publish state. | `generator.py:2992-3122` |
| **Keyword / SERP analysis** | Deterministic keyword inference (no LLM), keyword clusters with volume + intent, weekly top-10 SERP snapshots per target keyword, competitor ladder per query. | `src/audit/keyword_infer.py`, `src/connectors/openseo*.py` |
| **Measurement** | Baseline + paired control + YoY arms, two-proportion z-test, difference-in-differences, won/lost/neutral verdicts. | `src/measurement/` |

### 1.2 PARTIAL

| Area | What is actually true |
|---|---|
| **Framer** | The connector, adapter, generator, credential resolver, policy seeds, and operator UI all exist. `src/connectors/framer.py:158` returns a typed `live_writes_disabled` refusal unconditionally — **no live HTTP call is implemented.** `probe_auth` also never reaches the network (`framer.py:129-145`). Framer can be onboarded and audited but cannot be written to. |
| **Semantic scoring** | `src/jobs/semantic_scoring.py:32-34` raises `NotImplementedError` ("embedding call is not implemented yet"). It is on the weekly schedule, so it runs and fails every Saturday. The 0.15 semantic weight in the match-score formula is therefore never populated with real embeddings. |
| **GSC URL Inspection pacing** | `URL_INSPECTION_MIN_INTERVAL_SECONDS = 0.12` is defined (`src/connectors/gsc.py:44`) but never applied in the sync loop. The 400/run cap is what actually protects the quota. |
| **Collection-description LLM drafting** | The LLM call is wired, then its output is discarded — the deterministic corpus-grounded drafter produces the body. Comment at `generator.py:3794-3800` says a body-scale prompt "lands later." |
| **Keyword disambiguation** | `keyword_infer.infer()` returns an `ambiguous()` result that no caller acts on (`keyword_infer.py:182-188`). The intended "spend one keyword-volume call to disambiguate" path is unwired. |
| **Content-change SERP cache invalidation** | `docs/SEO_RULES_AND_POLICIES.md` §3.9 claims a changed page invalidates its cached SERP via `content_hash`. `content_hash` is computed and stored but `find_cached_session` (`src/audit/engine.py:172-198`) never compares it. **Doc is wrong; the cache is 24h and URL+query keyed regardless of edits.** |
| **Netlify/GitHub live writes** | Fully implemented and flag-gated behind `NETLIFY_LIVE_WRITES=1`. Real REST calls, real branch/commit/PR creation. Unlike Framer, this path is real. |

### 1.3 NOT IMPLEMENTED

- **JavaScript rendering.** The on-demand audit fetch always records `render_status='not_rendered'` (`src/audit/page_fetch.py:480`). There is no headless browser anywhere. Any client-rendered site produces false "blank title" findings and near-empty grounding.
- **Page speed / Core Web Vitals.** Nothing.
- **Backlinks.** `site_backlinks_summary` table exists, DataForSEO `backlinks` capability is wired, fixture exists — **it is never synced** (its own schema comment says so). No backlink analysis at all.
- **Broken-link detection**, link-equity / PageRank scoring, anchor-text optimization scoring from the link graph. `page_links.internal_links_in/out` counts used by orphan rules come from the provider crawl, not from our own graph.
- **Image alt text, hreflang, robots meta / X-Robots-Tag parsing of the audited page**, thin-content scoring.
- **E-E-A-T**, competitor content comparison beyond headings/snippets.
- **Soft-404 detection.**
- **Rank tracking over time** (no position history table).
- **Any content generation** (blog posts, body copy) — only titles, metas, and collection descriptions are drafted.

### 1.4 PLANNED but not coded

- 90-day recommendation cooldown, `agent_min_valid=5`, `agent_backup_pool=20` pre-agent validation — described in `docs/SEO_RULES_AND_POLICIES.md` §2.5 / Appendix A #29–31, **grep finds zero occurrences in `src/`**. Only `agent_prefetch_limit` (25) is real.
- Risk-tier conflict resolution ("lower-tier fix expires") — §1.2.4 of the policies doc. The executor does order claims by risk tier (`fix_executor.py:789`) but conflicts raise `PolicyBlocked`/409 instead of expiring.
- "3 concurrent live audits" — the 10/hour/IP rate limit is real, the concurrency cap is not.

---

## 2. How the system works under the hood

### 2.1 The loop, end to end

```
      INGEST (daily 01:00 UTC, per site)
      ┌──────────────────────────────────────────────┐
      │ GSC search analytics (7d) ──┐                │
      │ Shopify products/collections ├─► pages       │
      │ GA4 organic sessions ───────┘   search_performance
      │ DataForSEO keyword volume ──► keyword_clusters
      │ DataForSEO SERP ────────────► openseo_serp_snapshots
      │ GSC URL Inspection ────────► pages.gsc_*     │
      │ Provider crawl (weekly) ───► pages crawl cols│
      └──────────────────────────────┬───────────────┘
                                     ▼
      MINE (weekly Sat 04:00) — 5 deterministic SQL generators
      candidates → recommendations(status='raw'), cap 60/site
                                     ▼
      EVALUATE (weekly Sat 05:00) — LLM agent
      raw → proposed (≤5) or rejected(reason)
                                     ▼
      QUEUE — active window capped at 5 proposed cards
                                     ▼
      GATE 1 — operator approves the recommendation
                                     ▼
      DRAFT — LLM copy + hallucination guard + validators
      → generated_fixes(status='generated', diff_json ready)
                                     ▼
      GATE 2 — operator reviews the diff, approves
      → generated_fixes(status='queued')
                                     ▼
      EXECUTE (daily 03:00) — fix_executor, the ONLY writer
      policy gate → fresh snapshot → stale-diff guard → write
      → read-back verify → auto-revert on failure
                                     ▼
      MEASURE (daily 02:00) — baseline/control/YoY → z-test
      → won / lost / neutral / inconclusive
```

### 2.2 Ingestion, concretely

`src/connectors/sync.py::run_sync` runs a fixed per-site sequence, and every connector is wrapped in a **never-raise adapter contract**: a provider failure becomes `{"ok": False, ...}`, the step is skipped and logged, and the sync continues. One dead provider never kills a sync.

1. **GSC search analytics**, two passes — by page, then by query — over a 7-day window, `dataState='all'`. Page-level rows carry the literal query `"(not provided/none)"` (`sync.py:345-352`). Upserted on the 6-column natural key. Pagination: 1000 rows/page, 100 pages, with a logged truncation warning.
2. **Shopify products + collections** into `pages`. The REST path merges `custom_collections.json` + `smart_collections.json` (the combined `collections.json` 403s against live stores). Mints `shopify_gid` as `gid://shopify/Product/<id>`, which is what unblocked the entire write path. Upsert uses COALESCE so richer existing values survive.
3. **GA4 page performance**, 1-day window, organic-search filtered, 10 000-row report cap. Auth is a static OAuth access token — **there is no token refresh flow**, so a GA4 token expiring silently ends GA4 ingestion.
4. **DataForSEO keyword volume** → cluster volumes, gated on `ttl_keyword_volume_days` (default 30).
5. **DataForSEO SERP** per target keyword, `limit=10, geo=us` → `openseo_serp_snapshots`, gated on `ttl_serp_days` (default 7). Upsert is `DO NOTHING`, so a re-fetch on the same date never overwrites.
6. **URL Inspection** → `pages.gsc_*`. Priority order is `indexable=false with clicks` first.
7. **Catalogue coverage** and **page↔cluster match scores**, computed as set-based SQL in chunks of 500 with a 120 s statement timeout, pre-gated on 0.5 minimum term overlap. Full recompute only happens when no scores exist; otherwise only rewritten clusters are rescored.

Every write is stamped with a `data_source` provenance column (`live` / `mock` / `import`) added in `plan/31`.

### 2.3 Candidate mining — 100% deterministic SQL

Five generators, each a SQL file whose trailing `LIMIT` is the quota (Python parses the number rather than restating it):

| Generator | File | Quota | Produces |
|---|---|---|---|
| missing_page | `plan/01` | 15 | `create_page` — search demand with no strongly matching page |
| existing_opportunity | `plan/02` | 20 | `improved_page` — underperformers in the position window with CTR/click declines |
| technical_fix | `plan/03` | 15 | `technical_fix` — indexability, canonical, status, sitemap, orphan, structured data, JS rendering |
| cannibalization | `plan/04` | 10 | `consolidate` — own pages alternating for one cluster |
| internal_link | `plan/05` | 10 | link suggestions, inserted directly as `proposed` (no agent pass) |

Combined cap 60/site (`MAX_PER_SITE`), dedupe on `(generator, cluster_id, target_url, proposed_url)`, and an idempotent natural-key index `uq_recommendations_candidate` so re-running the generator never duplicates a row.

Scoring is deliberately simple and inspectable: opportunity priority is `signals × impressions`; page-cluster match is a stored generated column `0.30·intent + 0.30·catalogue + 0.25·gsc + 0.15·semantic`.

### 2.4 The LLM gatekeeper — fails loud

`src/agents/seo_agent.py::run_agent` loads up to 25 `raw` candidates, enriches each with cluster context, SERP rows (capped at 10), page context (render status, indexability, crawl depth, orphan flag, 28-day GSC aggregates), and the last 5 rejections as few-shot context. It injects the full skill playbook for `technical_fix` and `cannibalization` batches verbatim.

Output validation is strict: ≤5 recommendations, required keys present, `impact`/`confidence` ∈ {high,medium,low}, effort normalized ("4 hours" → "hours"), `owner` ∈ {SEO, content, engineering}, and every rejection must carry a candidate id and a reason.

**If the LLM fails, the run produces nothing.** Config error, API error, or schema violation all roll back and re-raise. There is deliberately no offline stub. A broken LLM means no new recommendations, not bad ones.

### 2.5 Fix drafting — fails soft, with three deterministic layers

`src/fixes/generator.py` turns an approved recommendation into a `generated_fixes` row. The value pipeline is LLM-first with a hard deterministic floor:

1. **LLM drafts** ≤3 candidates per field from a grounded prompt (competitor text is explicitly labelled a style reference only, `generator.py:105-110`).
2. **Hallucination guard** (`verify_grounded_claims`, `:235-269`) — every claim cue from a ~50-term vocabulary (materials, certifications, commercial promises, superlatives) and every numeric claim must appear in the page's own title/body, with inflection tolerance ("30-day" ↔ "30 days"). Ungrounded candidates are dropped.
3. **Validator gauntlet** — length bounds, keyword presence and first-half placement, banned patterns (`free shipping`, `best price`, `click here`, `buy now`, `limited time`, `|`, `»`, `★`, `→`), emoji ranges, ALL-CAPS runs ≥3 words, spam punctuation, double quotes in metas, brand name ≤1 occurrence, intent contradiction, and site-wide duplicate title/meta self-joins.
4. **First fully-passing candidate wins**, and `title_source` / `meta_source` records whether the value came from `llm` or `deterministic`.
5. **If the LLM is unavailable or everything is rejected, deterministic baseline drafters produce the value.** A fix is never lost to a provider outage. If even the deterministic draft fails validation, no fix row is created at all — the honest outcome.

### 2.6 Cross-cutting decision rules you should know

- **Protect-winner** (`generator.py:906-943`): a page at position ≤2 with rising 7-day CTR over ≥100 impressions is **never** rewritten. Existing success is not risked.
- **Consolidate suppression** (`:2770-2791` and siblings): an active consolidate recommendation on a cluster suppresses title, meta, description, and create fixes on that cluster, to avoid fighting itself.
- **Redirect chain flattening** (`src/fixes/redirect_graph.py`): A→B and B→C means a new A→C, not A→B. Cycles, self-loops, and chains over 10 hops are refused with `manual_review=True`.
- **Payload allowlists**: every adapter validates its payload against a strict field allowlist *before* any network call, so an accidental extra key can never reach the platform.
- **SERP grounding** rides on every fix payload — the framing pattern, competitor sample size, outline gaps, draft source, and the rejected LLM candidates are all persisted so a reviewer can see *why* a value was chosen.

---

## 3. Reliability

### 3.1 Deterministic vs AI-dependent

| Layer | Nature | On failure |
|---|---|---|
| Ingestion, crawl, sitemap, robots | Deterministic + external APIs | Step skipped, logged, sync continues |
| Candidate generation | 100% deterministic SQL | Run fails; no partial recommendations |
| Agent evaluation | LLM | **Run fails loud, nothing published** |
| Fix value drafting | LLM with deterministic floor | Falls back to templates; worst case no fix |
| Fact-checking, validators, dedupe | 100% deterministic | Fix refused, never written |
| Policy, caps, locks, snapshots, verification, revert | 100% deterministic | Write blocked |
| Measurement + z-test | 100% deterministic | Row skipped with a typed reason |
| SERP / keyword volume / crawl | External DataForSEO | Typed `serp_error` / `budget_exhausted`; page analysis still completes |

### 3.2 Fail-closed mechanisms that genuinely exist

- **Policy gate checked twice** — at queue time and again at pickup (`fix_executor.py:205-212`). A missing `fix_policy` row means blocked, not allowed.
- **Fresh snapshot at pickup** — generation-time values are never trusted for the write. A missing snapshot is a typed `no_snapshot` refusal, not a guessed write.
- **Stale-diff guard** (`fix_executor.py:253-270`) — if the live value no longer matches the `old_value` the diff was built against, the fix auto-expires and will be regenerated next cycle.
- **Read-back verification** — every adapter re-reads the platform after writing and reports `verified` / `verify_failed` / `verify_pending`. Auto-revert fires **only** on a failed read-back.
- **Async-aware verification** — an unsettled Shopify smart-collection job stays `verify_pending` rather than being treated as failure, so eventual consistency never triggers a spurious revert.
- **3-strike kill-switch** — three `access_denied` outcomes for a (site, sub_type) pair flip `fix_policy.enabled = false` (`fix_executor.py:551-579`).
- **Volume caps** — weekly caps per sub_type (titles 10, metas 5, collection descriptions 3, redirects 2, collection_create 2, product_create 2, product_publish 5, framer.title 10, netlify.title_meta_rewrite 10, netlify.structured_data 5, netlify.noindex 3, netlify.canonical_tag 10), a hard 5-applies-per-run ceiling, and one fix per URL per run.
- **Budget gates** — $100/week global seed, warn at 80%, hard-stop above. Over-budget SERP pulls return HTTP 402 without touching the network.
- **SSRF guard** on every live fetch — loopback, private, link-local, reserved ranges, and DNS-resolving-into-protected-range hosts are all rejected, re-checked on each redirect hop.
- **`import`-mode sites get zero connector traffic** (`fix_executor.py:821-827`).

### 3.3 Where false positives and false negatives come from

**False positives (we flag something that is fine):**
- **No JS rendering** — a client-rendered title reads as blank. This is the single largest source of bogus findings.
- **Keyword inference is pure heuristics** — the title∩H1∩body waterfall can pick a semantically wrong target keyword, which then makes a perfectly good title look keyword-missing.
- **Provider crawl defaults to 20 pages** (`openseo_rest_adapter.py:27`, `max_crawl_pages` defaults to `CRAWL_PAGE_LIMIT`) — the crawl covers a tiny fraction of a real site, so "orphan" and "no canonical" findings extrapolate from a 20-page sample.
- **SERP cache ignores content changes** — a day-old SERP can ground a draft for a page that has since changed.
- **Link graph is only populated by on-demand audits** — the weekly batch crawl does not feed `page_links`, so link suggestions are only as fresh as the last manual audit.
- **The `fix_policy` seeds ship all-disabled**, so an operator who enables a fix type on a site with a stale or wrong page snapshot can get a write based on outdated grounding.

**False negatives (we miss something real):**
- **Anything outside the crawl sample** is invisible. At 20 pages/run against a 100K-URL site, coverage is effectively zero.
- **No backlinks, no speed, no alt text, no hreflang** — not merely un-flagged, not measured at all.
- **Framer** produces no findings from live data because no live read path exists.
- **The agent caps at 5 recommendations per site per week** — a site with 40 good opportunities surfaces 5.
- **Generator quotas** (15/20/15/10) cap what is even considered.
- **Consolidation requires ≥100 impressions and ≥50% cluster share** on the loser, which correctly protects ambiguous cases but also correctly refuses most of them.

### 3.4 Missing, stale, conflicting, or failing data

| Situation | Behavior |
|---|---|
| Page unreachable | Typed `page_unreachable`; audit session still records the attempt |
| robots.txt blocks | Typed `blocked_robots`; HTTP 422; not treated as a technical defect |
| Sitemap unfetchable | `in_sitemap` set to NULL — explicitly "unknown", never fabricated |
| SERP provider error | `serp_status='serp_error'`, HTTP 502, page analysis still returned |
| Zero organic results | `zero_results` — a real signal, not an error |
| Over budget | `budget_exhausted`, HTTP 402, zero network spend |
| Two fixes target the same URL+sub_type | Partial unique index blocks it; the regenerator returns the existing row (idempotent) or raises `PolicyBlocked` → HTTP 409 |
| Fix conflicts across sub_types | 409, never a silent overwrite |
| Site config missing/invalid | `site_not_found` / `unknown_platform` / `no_credentials` / `shopify_domain_missing` — all fail closed; the executor refuses the write |
| Unknown site_id in a request | Fails loudly (`no_sites`); the old "fall back to the mock site" behavior was removed by `plan/31` |
| Missing baseline page row | Zeroed baseline + `degraded` flag; control and YoY arms skipped; never a crash |
| Legacy DB missing a column | Caught, rolled back, retried as a legacy query |

---

## 4. Auto-fixes vs manual fixes

### 4.1 Fully supported automatic write paths

Every one of these goes through both approval gates. "Automatic" means the system writes it correctly and can roll it back — not that it writes without a human.

| Sub-type | Platform | What it writes | Rollback |
|---|---|---|---|
| `seo.title` | Shopify | `productUpdate(seo:{title, description})` — **both fields always together** | Restores both from snapshot |
| `seo.description` | Shopify | Same call, description changed | Restores both |
| `collection_description` | Shopify | `collectionUpdate({id, descriptionHtml})` | Writes back the snapshot's HTML |
| `collection_create` | Shopify | `collectionCreate` + `publishablePublish` | **`publishableUnpublish` only — never `collectionDelete`** |
| `redirect` | Shopify | `urlRedirectCreate` | `urlRedirectDelete` using the id the fix itself created |
| `product_publish_product` | Shopify | `productUpdate(status: ACTIVE)` | Restores prior status |
| `collection_publish` | Shopify | `publishablePublish(publicationId)` | Unpublish / republish per snapshot |
| `product_unpublish` / `collection_unpublish` | Shopify | Status/membership flip, only as a sequenced follow-up to an applied redirect | Restores prior state |
| `netlify.title_meta_rewrite` | GitHub PR | HTML transform, one PR per fix | Closes the recorded PR |
| `netlify.structured_data` | GitHub PR | Injects JSON-LD | Closes the PR |
| `netlify.canonical_tag` | GitHub PR | Self-referencing canonical | Closes the PR |
| `netlify.noindex` | GitHub PR | robots meta, **high risk, requires an explicit `noindex_intentional` evidence flag** | Closes the PR |
| `framer.title` / `framer.description` | Framer | Whole-object `updatePageMeta` with both fields | Restores both — **but the live path is a stub** |

Two engineering details worth knowing because they were learned from real stores, not from docs:

- **Shopify `productUpdate.seo` replaces the entire object.** Writing only the title would wipe the description. Every seo adapter refuses when the sibling value isn't present in the fresh snapshot. This was live-verified 2026-09-23.
- **`collection_create` is idempotently self-healing.** If a prior run was interrupted after creating the collection but before publishing it, the next run resolves the handle first, finds it exists, skips creation, and routes to publish-only with the resolved GID.

### 4.2 Recommendation-only, and exactly why

This is the important part: **manual-only here is a design decision with a typed in-code rationale, not an unfinished feature.**

| Manual category | Root cause | Why | What would unblock it |
|---|---|---|---|
| **Internal link placement** | No write surface exists on any connected platform | `generator.py:675-682` hard-refuses with the comment "link placement needs a human/editorial decision — no auto-fix surface exists". Approving one can *never* mint a fix; it always raises `FixNotSupported`. This is **deliberately fail-closed**, not a gap. | A CMS/blog write API plus an editorial safety model. Even then it should stay advisory — link placement is a content judgment. |
| **Shopify theme/template issues** (canonical or robots implemented in theme code, Liquid edits, sitemap config) | Every store's theme is bespoke | Wrong edits brick a storefront. Routed to `plan_only` — the recommendation ships with the work described, no write attempted. | Per-theme adapters with template-specific validation, and a preview/rollback surface. High effort, high risk. |
| **5xx / 403 responses** | Not an SEO-shaped defect | "Real defects with a DIFFERENT fix, not a 301" (`generator.py:3221-3224`). Redirecting an outage just hides it. | Hosting/platform-level integration, not an SEO tool's job. |
| **404 redirect destination** | Destination must be provable, not inferred | The resolver walks product_type → `catalogue_coverage` and **refuses** if no confident target exists. Never guesses. | Better destination inference (semantic match above 0.5 overlap) — already partially in place, needs to be good enough to be safe. |
| **JSON-LD Offer/Service fields** | Facts, not copy | The generator omits a field rather than invent a claim (`generator.py:2512-2544`). | Real structured business-profile data — `plan/30` adds the schema; tenant adoption is the blocker, not code. |
| **Content/body rewrites** (H2 sections, blog posts) | Task text only | Queued as operator context (`queue.py:127-143`); no content write connector exists. | A content platform write connector with its own approval model. |
| **Cross-platform Shopify concepts** | Structural | A collection GID, `CollectionInput`, or `urlRedirectCreate` has no meaning on Framer/Netlify. `_require_shopify_platform` refuses with a typed `FixNotSupported` rather than mis-targeting. | Nothing — this refusal is correct forever. |
| **Publication channel choice** | Ambiguous | No resolved publication id → typed refusal. The system will not pick a sales channel. | Operator resolves the publication once; it's cached in `system_config` afterwards. |

### 4.3 Structural automation that still respects the gates

- **Unpublish-after-redirect sequencing**: when a redirect applies *and* verifies, the executor mints a follow-up `product_unpublish`/`collection_unpublish` row. It enters as `generated`, so **gate 2 still applies** — but note the strict reverse-order revert: undoing a redirect while its source is still unpublished would leave the source with no redirect and no listing, so the unpublish is reverted first, always.
- **PR merge sync** (`src/api/routes/github_sync.py`): when a Netlify PR merges, the webhook (HMAC-verified) or the sync endpoint annotates the fix and advances the recommendation to `live`. Automated *observation*, never a write.

---

## 5. What the system does today for a real website

**Onboarding**
1. Insert a `site_config` row: domain, GSC property, platform, catalogue tier, threshold overrides, `target_keywords`.
2. Set `integration_mode` (`mock` / `sandbox` / `import` / `live` per `plan/31`) and supply per-site credentials — Shopify token is now a per-tenant column, not just an env var.
3. Create the `fix_policy` rows (seeded disabled) and deliberately enable the sub_types you want.

**Daily (01:00 UTC)** — GSC 7-day window, Shopify catalogue, GA4 organic, keyword volumes (30-day TTL), SERP snapshots (7-day TTL), URL Inspection (400/run), catalogue coverage, match scores. Every row stamped with provenance.

**Weekly (Sat)** — provider crawl + robots/sitemap verification + sitemap page ingest (non-Shopify only); then the 5 SQL generators; then the LLM agent filters to ≤5 enriched recommendations.

**Operator loop** — up to 5 proposed cards at a time in the Action Queue. Each has evidence, a work breakdown, a measurement plan, and the competitor SERP ladder. Approve → drafts generate lazily on drawer open (per-field failures don't kill the endpoint) → review the diff → approve → it queues. The drawer also labels each work item **`automated` or `manual`** from the engine's actual capabilities (`queue.py:109-201`), so a card never implies automation that doesn't exist, and every automated item is linked to the specific fix rows that implement it. A queued fix can be applied immediately via **Apply now** (`POST /fixes/{id}/execute`), which runs the same executor code under the same per-site lock as the scheduled job — an escape hatch, not a gate bypass. Approve/reject API errors are surfaced in the UI rather than swallowed.

**Execution (03:00 UTC)** — the executor claims up to 5 fixes, highest risk tier first, one per URL. For each: policy re-check, fresh snapshot, stale-diff check, write, read-back verify, change_log, baseline freeze. Verified failure auto-reverts.

**Measurement (02:00 UTC)** — after the per-action window (49/28/28/21 days by action type) plus a 4-day GSC settle buffer, a z-test against paired controls yields won/lost/neutral/inconclusive.

**Ad hoc** — `POST /audit/live-url` runs a single-URL audit with competitor SERP analysis, rate-limited to 10/hour/IP, SERP results cached 24h. `POST /audit/{session_id}/to-store` can push findings into the queue as `proposed` — it can never auto-approve, and the audit router is structurally read-only with respect to pipeline tables.

**How it protects the site**
Every write is: gated twice, capped, snapshotted fresh, diff-checked, read-back verified, auto-revertible, kill-switched after three credential failures, and recorded in `change_log` with a `rollback_reference`. Nothing executes inline in an API request. The audit path cannot reach the write connectors at all.

---

## 6. Limitations and the path to a mature platform

### Biggest gaps, ranked
1. **No autonomous scheduling** — no `schedule:` in CI, no hosted cron. The system is idle unless a human runs it.
2. **No job retry policy, no backoff, no dead-letter table.** A failing job re-dispatches every 60 s tick forever, re-alerting each time.
3. **No job timeouts.** `JOB_TIMEOUT_SECONDS` defaults unset; a hung HTTP call can wedge a site's slot indefinitely (Python cannot kill a thread).
4. **No observability** — no metrics export, no error tracking, no structured logging. `/health` is a DB ping.
5. **Crawl coverage of 20 pages/run** against real sites of any size.
6. **No JS rendering, no speed, no backlinks** — a third of modern SEO is simply absent.
7. **No retention anywhere** except the 7-day audit TTL.
8. **Single shared bearer token** for all tenants; webhook HMAC passes silently when the secret env is unset.

### Short-term (weeks)
- Add `schedule:` to `.github/workflows/ci.yml` (or a worker host) invoking `python -m jobs.scheduler --once`. **Cheapest possible path to a running product.**
- Wire `content_hash` into `find_cached_session` — the doc already claims this behavior; make it true.
- Apply `URL_INSPECTION_MIN_INTERVAL_SECONDS` in the sync loop.
- Either implement `semantic_scoring` or remove it from the schedule — right now it fails every Saturday.
- Build the Framer live HTTP path, or remove Framer from onboarding docs so nobody believes they can write.
- Set `GITHUB_WEBHOOK_SECRET` in every deployed environment; make the unset case fail closed.
- Add a `job_failures` table with attempt counts and backoff.

### Medium-term (a quarter)
- Real job queue with per-tenant lanes (Celery / RQ / managed worker) plus per-site timeouts and bounded concurrency.
- Retention and batching: partition `search_performance` by month, prune `openseo_serp_snapshots` and `api_costs`, batch the sitemap-membership updates.
- A prioritized crawler driven by GSC impressions instead of a flat 20-page sample.
- Extend the page-audit rule set: on-page canonical, JSON-LD detection, alt text, hreflang.
- Per-tenant auth (not one shared token) and Postgres row-level security so isolation stops depending on 98 correct `WHERE` clauses.
- GA4 OAuth token refresh.

### Long-term (enterprise)
- Theme-file auto-fix with per-theme validation and preview.
- Content generation with its own approval model.
- Rank tracking history, PageRank-style link equity, E-E-A-T signals.
- Per-tenant quota and billing on top of the existing `api_costs` ledger.
- Rendered-page support (headless fetch) for SPAs.

---

## 7. Database and large-site scalability

This is where the current design is most honestly bounded. The system is built for small-to-medium catalogues, and the caps are explicit in code rather than incidental.

### 7.1 What happens at 100K / 500K / 1M URLs today

**It silently does almost nothing.** The provider crawl defaults to `max_crawl_pages = CRAWL_PAGE_LIMIT = 20` (`src/connectors/openseo_rest_adapter.py:27, 216`). At 100 000 URLs that is **0.02% coverage per weekly run**. The crawl-derived signals that drive orphan detection, canonical checks, render status, and structured-data detection are all computed from a 20-page sample and then treated as site-level facts. That is a correctness problem before it is a performance problem.

Sitemap ingest caps at 5 000 entries, depth 2, 10 children, 2.5 MB body, and **200 pages actually fetched per run** (`site_fetch.py:294`) for non-Shopify sites. The Shopify catalogue pull is theoretically bounded at 25 000 (100 pages × 250).

So: a 1M-URL site is not slow. It is **un-analyzed**, while the system reports findings derived from 20 pages.

### 7.2 Current DB constraints and bottlenecks

- **`sync_sitemap_membership` updates `in_sitemap` and `robots_allowed` row-by-row in a Python loop** (`site_fetch.py:222-276`). At 100K pages that is 100K sequential `UPDATE`s in one job, holding a connection. This is the first thing that will hurt.
- **Duplicate title/meta checks are case-insensitive self-joins on `pages`** (`generator.py:972-990`, `:1344-1361`). Fine at thousands of rows, degrading at hundreds of thousands without expression indexes on normalized values.
- **Match scores are O(pages × clusters)**. Already mitigated (500-row chunks, 120 s statement timeout, 0.5 overlap pre-gate, incremental rescope), but a genuinely large catalogue will still be slow.
- **`page_query_match_scores`** is fully recomputed when any score is missing.
- **Advisory locks are session-scoped with no timeout.** The blocking variant is explicitly unbounded in code with a comment noting the caller should bound it — it isn't bounded.
- **API connection pool** is 8 connections by default (`DB_POOL_MIN=0`, `DB_POOL_MAX=8`). The Vercel serverless path creates a pool per warm instance, and the rate limiter is in-process, so it resets on every cold start.

### 7.3 Which tables grow fastest

| Table | Growth driver | Retention today |
|---|---|---|
| `search_performance` | daily GSC rows × queries × pages × country × device, 7-day window re-upserted each day; the query pass multiplies rows | **none** |
| `api_costs` | one row per paid DataForSEO call **plus one per URL inspection** (up to 400/site/day), each carrying JSONB request metadata | **none** (a legacy `api_costs_archive` table is referenced but unused) |
| `openseo_serp_snapshots` | top-10 organic × N target keywords, re-fetched on TTL expiry (7d) | **none** |
| `page_links` | one row per internal edge per audited page | **none** |
| `audit_page_snapshots` | **full `body_html` TEXT per session** | ✅ 7-day TTL with cascade — the only real retention in the system |
| `pages` | bounded by catalogue/sitemap size | n/a |

### 7.4 Are we storing too much data?

Mostly **yes, in the cost ledger**. `api_costs.metadata` stores full request parameters on every call, and URL inspection alone writes up to 400 rows/site/day that are only ever summed into a quota counter. That table is a cost *accounting* table being used as a cost *journal*.

`audit_page_snapshots.body_html` is defensible at 7 days and a liability beyond it.

`search_performance` is the valuable one and should be kept longest — it's the substrate for every measurement and every GSC-evidence signal. It needs partitioning, not pruning.

### 7.5 What scale actually requires

- **Partition `search_performance` by month**, 13–26 month retention, with the indexes rebuilt per partition.
- **Roll up `openseo_serp_snapshots`** into a monthly competitor-position table after 90 days; daily SERP detail is only useful for recent windows.
- **Move crawl history out of `pages` columns** into a dated `crawl_runs` table so a page's current state isn't a 20-column dumping ground.
- **Batch the sitemap-membership updates** — one `UPDATE ... FROM unnest()` or a temp table instead of 100K round-trips.
- **Replace the 20-page flat sample with a crawl budget** prioritized by GSC impressions, clicks, and last-change age, so 20 pages are the *right* 20.
- **Queue and shard crawls per tenant**; a single 1M-URL site should never hold a worker.
- **Add expression indexes on normalized `lower(trim(title))` for the duplicate checks.**

---

## 8. 24/7 production operation

### 8.1 What exists today

- **Scheduler** (`src/jobs/scheduler.py`): a registry-driven tick loop. `SCHEDULE` holds 10 jobs — 5 daily (sync 01:00, measurements 02:00, fix_executor 03:00, stale_approvals 05:00, audit_ttl_sweep 06:00 UTC) and 5 weekly (candidates Sat 04:00, agent Sat 05:00, crawl Sat 06:00, semantic Sat 07:00, cost_report Sun 08:00). Due-logic is deterministic: past the hour, not already run this period. Last-run state persists in `system_config` under `scheduler.lastrun.<job>`, so restarts don't double-run.
- **Two run modes**: `run_forever()` (a plain blocking `while True` with a 60 s sleep) and `--once` (evaluate due slots and exit — the contract for an external cron).
- **Advisory locks** (`src/jobs/locks.py`): Postgres session-scoped `pg_try_advisory_lock`, keys minted only through a central `LOCK_KEYS` registry as `openseo:job:<job>[:<site_id>]`. Per-site granularity for per-site jobs, so site A never blocks site B. Try-lock never queues — a busy lock returns `already_running` and the caller exits. Locks release on connection close, so a crashed process cannot strand one.
- **Per-site fault isolation** (`src/jobs/run_multi.py`): one site's exception is captured as `{"status": "failed"}` while every other site continues.
- **Budget pre-flight** before paid sync; **webhook notifier** for failures (no-op with a log line when unset).

### 8.2 What is missing — the honest production gap list

1. **No autonomous trigger.** `.github/workflows/ci.yml` triggers on `push` to main and `pull_request` only. **There is no `schedule:` block.** No Vercel cron. Nothing runs the system unless a human starts the loop or wires an external cron themselves. The old `WALKTHROUGH.md` line "no `.github/workflows`" is itself now stale — the workflow exists, it just never fires on a timer.
2. **No retry policy and no dead-letter queue.** A failing job stays "due" and re-dispatches every 60 s tick, indefinitely, with no backoff and no attempt cap. Failures exist only in stdout and an optional webhook. There is no `job_failures` table anywhere in the schema.
3. **No enforced timeouts.** `JOB_TIMEOUT_SECONDS` defaults to unset, and even when set, `ThreadPoolExecutor` timeouts cannot kill a running thread — a timed-out site is marked failed while its thread keeps going and still blocks process exit. No connection-level `statement_timeout` on jobs; the only `SET LOCAL statement_timeout` in the codebase is inside the match-score query.
4. **No observability.** No metrics export, no error tracking, no structured logging, no dashboards. `/health` returns a DB report and mode flags.
5. **Rate limiting is per-process.** The audit limiter's 10/hour/IP bucket is in-memory, so across Vercel serverless instances it is effectively unenforced.
6. **Sequential multi-site execution** (`JOB_MAX_WORKERS=1` by default). One slow site delays every other site, and combined with no timeout, one hung site stalls the whole nightly run.
7. **The scheduler itself is single-instance-safe but not HA.** Overlapping processes are lock-guarded, which prevents duplicate work, but there is no leader election, no health check, and no restart-on-crash.

### 8.3 The minimum for real 24/7

1. **External trigger.** Add a `schedule:` block to the CI workflow (cron → `python -m jobs.scheduler --once`) or run the loop on a Railway/Fly worker. `--once` already exists and is already lock-guarded; this is a config change, not an engineering project.
2. **`job_failures` table**: `(job, site_id, slot_period, attempts, last_error, first_failed_at, dead_at)`. Backoff = `2^attempts` capped at the slot period. Dead at N attempts → alert and skip.
3. **Timeouts everywhere**: set `JOB_TIMEOUT_SECONDS`, and set `statement_timeout` on job connections so a runaway query can't wedge a worker.
4. **Bounded per-tenant concurrency** with a real queue so a large site gets its own lane and its own rate limit.
5. **Alerts on the things that matter**: dead-letter entries, fix-executor verify failures, budget cap hits, scheduler ticks that produced nothing, and a heartbeat when the scheduler hasn't run in its expected window.
6. **Move limiter state to Redis** if staying serverless.

### 8.4 Long-running crawls

The DataForSEO crawl already uses the right shape — `task_post` then poll `tasks_ready` every 5 s for up to 30 attempts (~150 s), which is asynchronous at the provider. The problems are at our end: the whole job holds a per-site advisory lock for its duration, is sequential, and has no timeout. A crawl on a large site should be split into chunked jobs that release the lock between chunks and record progress durably so they can resume.

### 8.5 Preventing one tenant from starving others

Today: per-site advisory locks (good — a lock on site A does not block site B's job) combined with strictly sequential `run_across_sites` (bad — a slow site A delays site B in wall-clock terms even though it doesn't block it logically). The fix is bounded concurrency plus per-site timeouts plus a real queue with per-tenant lanes, and per-service rate limits so one tenant's DataForSEO usage cannot exhaust the shared account quota for everyone.

---

## 9. Multi-tenant reliability

### 9.1 How sites are separated

The tenant key is `site_id UUID` on `site_config`. Every domain table carries `site_id` with an FK — `pages`, `search_performance`, `keyword_clusters`, `recommendations`, `rejection_log`, `measurement_snapshots`, `generated_fixes`, `fix_policy`, `page_links`, `redirects`, `audit_sessions`, `catalogue_coverage`, `page_business_performance`, `candidate_runs`.

**There are no tenant or user tables, and there is no Postgres row-level security.** Isolation is enforced entirely by `WHERE site_id = ...` in application code — roughly 98 such clauses across the route layer. A single missing clause in a new endpoint is a cross-tenant data leak. This is the most important structural weakness in multi-tenancy, and it is invisible until someone adds an endpoint.

`plan/31-tenant-mode-and-provenance.sql` materially hardened this:
- Per-site `integration_mode` with a CHECK constraint (`mock|sandbox|import|live`), replacing a global env switch as the primary control. **`import` sites receive zero connector traffic** — the strongest guarantee in the system.
- `data_source` provenance (`live|mock|import`) on `pages`, `recommendations`, `search_performance`, `generated_fixes`, `page_business_performance`, so no one can confuse fixture data with production data.
- FK + `ON DELETE CASCADE` on `api_costs`, which had previously been unscoped and could not be attributed to a tenant.
- Per-tenant `shopify_access_token` column, with env fallback for legacy single-tenant installs.
- Per-site `target_keywords TEXT[]`.

### 9.2 How leaks are prevented between tenants

- **Credentials**: resolved per site through `src/connectors/write_credentials.py::resolve_write_credentials`, returning a typed `{ok, platform, config, outcome, detail}`. A per-site Shopify token takes precedence over the shared env var. **Credential values are never logged** — failures name the missing env key only.
- **Cost attribution**: every DataForSEO and GSC inspection call is written to `api_costs` with its `site_id`, and the inspection quota is counted live per site against a global ceiling.
- **Queue visibility**: `GET /queue` returns only `status='proposed'` rows for the requested site. Raw candidates are never operator-visible.
- **Execution**: the executor holds a per-site lock, resolves per-site credentials, and refuses the whole platform when the credential check fails — fail-closed per site, not per process.
- **Write scoping**: adapter payloads carry a site-scoped entity GID, and `_require_shopify_platform` refuses concepts that don't exist on a site's platform rather than mis-targeting.

### 9.3 Missing or incorrect site configuration

| Condition | Result |
|---|---|
| `site_config` row absent | `CredentialResolutionError` / `site_not_found` → executor refuses the write |
| `platform` NULL or unrecognized | `unknown_platform` → refused (legacy rows coerce to `shopify`) |
| Credential env/column missing | `no_credentials` → refused, naming only the missing key |
| `domain` missing for Shopify | `shopify_domain_missing` → refused |
| `integration_mode` invalid | `ModeError`, no silent fallback |
| Request with unknown/empty site | `no_sites` — the historical "fall back to the sandbox mock site" behavior was deliberately removed by `plan/31` |
| Legacy DB missing a new column | Exception → rollback → legacy-shape query retry |

Resolution order for mode: explicit per-call config → `site_config.integration_mode` → `INTEGRATION_MODE` env → legacy per-connector env → `'mock'`. **The final fallback is mock, so a misconfigured site degrades to fixture data rather than making live writes** — combined with the provenance columns, that's detectable rather than silent.

### 9.4 Multi-tenant weaknesses to fix

- **One shared API bearer token** for every tenant and every operator. No per-user identity, no per-tenant authz, no rotation, no expiry. If the token leaks, every site is exposed.
- **Auth is disabled entirely when `API_AUTH_TOKEN` is unset** — which is the local-dev default and a plausible production misconfiguration.
- **No row-level security** in the database as a backstop.
- **One shared DataForSEO account** across all tenants; one tenant's crawl volume can consume the shared quota.
- **The GitHub webhook HMAC passes silently when `GITHUB_WEBHOOK_SECRET` is unset** (documented dev bootstrap, a real production footgun).
- **No per-tenant rate limiting or quota** on writes.

---

## 10. Final assessment

**What the system is genuinely capable of today.** For a Shopify or Netlify/GitHub-static site with server-rendered HTML, it is a real closed loop: daily multi-source ingestion with provenance, weekly deterministic candidate mining, LLM-screened recommendations capped at 5 with full evidence, two human approval gates, safe auto-writes for titles, meta descriptions, collection descriptions, collection creation and publishing, redirects, and publish-state flips on Shopify plus title/meta/JSON-LD/canonical/noindex via GitHub PR on Netlify, read-back verification with automatic revert, measurement against paired control groups with a real significance test, and win/loss verdicts that feed operator decisions.

**What is production-ready.** The fix-execution safety chain is the strongest asset here and is clearly hardened against real stores — the sibling-field discipline on Shopify's whole-object `seo` update, publication-free status reads for stores with no sales channels, idempotent self-heal on interrupted collection creation, "unpublish, never delete" rollback, "close the PR, never force-push," async-aware verification, and the 3-strike kill-switch. Those are not theoretical safeguards; they carry live-verified comments against the 2026-01 Shopify API. The deterministic rule layer — thresholds, validators, hallucination guard, redirect graph, measurement math — is equally trustworthy.

**What still needs human review.** Everything, by design. No write skips the gates. Internal links, theme edits, content rewrites, and hosting defects are manual-only with explicit typed refusals, which is the correct posture rather than a backlog.

**Biggest reliability risks.** Nothing runs the scheduler (no `schedule:` trigger anywhere). No retry policy, backoff, or dead-letter store. No enforced job timeouts. The webhook HMAC fails open when unset. Framer looks supported in the UI but has no live HTTP path. One shared bearer token protects every tenant.

**Biggest scalability risks.** A 20-page-per-run crawl cap that makes large sites un-analyzed rather than slow. Row-by-row sitemap-membership updates. No retention on `search_performance`, `api_costs`, `openseo_serp_snapshots`, or `page_links`. Strictly sequential per-site job execution with no timeouts. Duplicate-detection self-joins degrading with catalogue size.

**Biggest missing SEO capabilities.** JavaScript rendering. Page speed and Core Web Vitals. Backlinks (schema and capability exist, never synced). On-page canonical, JSON-LD detection, alt text, and hreflang in the single-URL audit. Internal-link *execution*. Rank tracking over time.

**What to focus on next, in order.**
1. **Make it run.** Add a cron trigger for `python -m jobs.scheduler --once`, add a `job_failures` table with backoff, and set real timeouts. Without this, everything else is a demo. This is configuration plus one migration.
2. **Fix the cheap correctness claims.** Wire `content_hash` into the SERP cache lookup; apply the 0.12 s inspection spacer; either implement or unschedule `semantic_scoring`; make the webhook fail closed.
3. **Decide about Framer** — build the live path or remove it from the docs and UI. Right now it implies a capability that does not exist.
4. **Add retention and batching before onboarding a large site** — partition `search_performance`, prune the SERP snapshots and cost journal, batch the sitemap updates.
5. **Then** expand audit coverage (rendering, speed, alt text, on-page canonical) and replace the flat 20-page crawl with an impression-prioritized crawl budget. Those are the two changes that most increase real SEO value.
6. **Then** per-tenant auth and row-level security, before onboarding a second real customer.

---

## Appendix A — Doc-vs-code discrepancies found during this audit

| # | Documentation claim | Reality |
|---|---|---|
| 1 | `SEO_RULES_AND_POLICIES.md` §3.9: a changed page invalidates its cached SERP via `content_hash` | **Not implemented.** `find_cached_session` (`audit/engine.py:172-198`) keys on url_hash + query + TTL only |
| 2 | `SEO_RULES_AND_POLICIES.md` §2.5 / App. A #29–31: 90-day cooldown, `agent_min_valid=5`, `agent_backup_pool=20`, pre-ranking by priority | **Not implemented.** No occurrences in `src/`. Only `agent_prefetch_limit` is real |
| 3 | `SEO_RULES_AND_POLICIES.md` §1.2.4: risk tier wins cross-type conflicts, lower-tier fix expires | **Partially.** The executor orders claims by risk tier but conflicts raise 409 rather than expiring the loser |
| 4 | `SEO_RULES_AND_POLICIES.md` App. A #54: 3 concurrent live audits | **Not implemented.** The 10/hour/IP limit is real; concurrency is not |
| 5 | `WALKTHROUGH.md` §8/§12.6: "no `.github/workflows`" | **Stale.** `.github/workflows/ci.yml` exists — but only on `push`/`pull_request`, with **no `schedule:` trigger** |
| 6 | `WALKTHROUGH.md` §12.9: "no concurrency guards between overlapping job runs (no advisory locks)" | **Stale.** `src/jobs/locks.py` implements session-scoped advisory locks with per-site granularity |
| 7 | `WALKTHROUGH.md` §12.8: "no migration runner" | **Stale.** `scripts/migrate.py` exists with a `schema_migrations` ledger |
| 8 | `WALKTHROUGH.md` §1.20 and §1.22: "nothing is ever published automatically", "no auto-publishing" | **Stale.** The fix executor writes to live stores daily at 03:00 (policy-gated) |
| 9 | `WALKTHROUGH.md` §6.2: 4 generators | **Stale.** There are 5 (`plan/01`–`plan/05`) |
| 10 | `WALKTHROUGH.md` §12.4: "no settle buffer" for GSC lag | **Stale.** `GSC_SETTLE_DAYS = 4` gates the measurement batch |
| 11 | `WALKTHROUGH.md` §5: `page_query_match_scores` weights `0.30 type_fit + 0.30 catalogue + 0.25 gsc + 0.15 semantic` | Component names differ — the actual generated column is `0.30·intent + 0.30·catalogue + 0.25·gsc + 0.15·semantic` |
| 12 | Old `WALKTHROUGH.md` §12.6 claim: daily_sync "falls back to the sandbox mock site when the table is empty" | **Stale.** `plan/31` removed it; `daily_sync` now returns `no_sites` |

## Appendix B — Verification method

This document was produced by reading source directly, not by trusting documentation. Specifically: the full schema was read from `plan/00-schema.sql` plus every migration, cross-checked against the applied/not-applied split enforced by `scripts/migrate.py::NON_MIGRATIONS`; the audit engine, fix generator, adapters, executor, and API routers were read end to end; the LLM/deterministic boundary was traced through both failure paths; and every documentation claim in `docs/SEO_RULES_AND_POLICIES.md` and the previous `WALKTHROUGH.md` was checked against the code, producing the discrepancy list in Appendix A. Claims that could not be verified against code are marked as planned or doc-only rather than asserted.

Two facts were spot-checked directly for this document: `CRAWL_PAGE_LIMIT = 20` in `src/connectors/openseo_rest_adapter.py:27`, and the `enabled=false` seeding of `fix_policy` in `plan/22-stage2-phase0-schema.sql`.

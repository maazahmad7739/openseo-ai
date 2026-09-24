# Stage 2 — Fix Generation, Approval & Execution Framework
# ============================================================
# Version: 1.0 · Date: 2026-09-22 · Status: PLAN (not yet implemented)
#
# Extends the pipeline from "describing fixes" (Stage 1) to:
#   auto-generate concrete fix → human approves the diff → auto-execute
#   write to Shopify → flows into the UNCHANGED measurement pipeline.
#
# Grounded against main @ Phase-1 discovery (every file/claim verified in
# code). Write targets verified against the Shopify GraphQL Admin 2026-01
# schema directly (shopify.dev/docs/api/admin-graphql/2026-01), NOT from
# memory — see §4 mutation registry rule.
#
# NON-NEGOTIABLES (unchanged by this stage):
#   1. NOTHING publishes without human approval — ever, for any type.
#   2. One generic framework; only decision-logic + write-adapter vary per type.
#   3. Grounding rule: every capability names its data source + schema home.
#   4. Backlink automation = internal linking only (no external outreach).
#   5. improve_page pilot first (v1 = seo.title fixes only).
# ============================================================

---

## 0. Framework Architecture

One new module (`src/fixes/`) with three pluggable seams, one new table,
one new job, one new router. Everything else is reused.

- **generator-hook** — turns an approved recommendation into a concrete fix
  payload (deterministic decision logic picks WHAT; LLM drafts the VALUE;
  deterministic validator gates the draft).
- **write-adapter** — one per write target; `supports()/execute()/restore()`
  mirroring the existing connector never-raise contract.
- **policy** — risk tier, weekly caps, kill-switch per sub_type (`fix_policy`).

Invariant: `recommendations` keeps its existing status machine untouched
(`src/api/common.py TRANSITIONS`). The fix lifecycle hangs off a new child
table so measurement, rejection learning, and the operator queue never change
shape.

Pillar decisions (owner-delegated, recorded per decision-authority rules):
- GraphQL-only writes, pinned `2026-01` (REST `2024-10` in the current
  connector is unsupported; REST product endpoints are deprecated past the
  Apr 2025 custom-app deadline; no store is <100-variants-verified, and
  Stage 2 outlives any REST grace period).
- Pilot writes `seo.title` only (verified field on ProductUpdateInput) —
  zero customer-visible storefront surface, no handle-regeneration risk.
  Safe default if owner doesn't answer the business question by pilot start.
- Two approval gates for every executable fix (recommendation + diff).
  `trust_mode` knob ships OFF.
- Auto-revert on failed read-back verification ONLY; measurement verdicts
  never trigger automatic reverts (operators decide from verdicts).
- Snapshot = fresh read at executor pickup, never trusted from generation.
- Scope-miss = typed skip → fix `failed` (`execution_blocked_scope`),
  3-strike auto-disable of the sub_type.

---

## 1. Data Model & Schema DDL

### 1.1 Reused (no new tables)
| Asset | Reuse |
|---|---|
| `recommendations` | Spine unchanged. One display-only column `fix_status TEXT` (projection; never read by logic — plan/20 pattern) |
| `change_log` | Execution audit; `rollback_reference` finally populated (= fix_id). Same best-effort pattern as `src/api/routes/measurements.py:136-153` |
| `measurement_snapshots`, `measurement_window_lookup` | Untouched. Applied fixes → existing `implement` path → baseline → live → window+GSC_SETTLE_DAYS → classify |
| `rejection_log` | Fix-generation rejections (`rejected_by='agent'`) — same learning loop |
| `src/jobs/locks.py` | Executor concurrency (new LOCK_KEYS: `fix_executor` global + per-site) |
| `system_config` | `shopify.publication_id` (cached once via `publications` query; needed by publishablePublish), `shopify.api_version` ('2026-01') |

### 1.2 New table `generated_fixes`
```sql
CREATE TABLE generated_fixes (
    fix_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id   UUID NOT NULL REFERENCES recommendations(recommendation_id),
    site_id             UUID NOT NULL REFERENCES site_config(site_id),
    action_type         TEXT NOT NULL,
    sub_type            TEXT,          -- issue_type | 'title'|'meta'|'content'|'redirect'|'link_insert'|...
    target_url          TEXT NOT NULL,
    target_entity_ref   TEXT NOT NULL, -- Shopify GID (gid://shopify/Product/123)
    payload_json        JSONB NOT NULL,   -- exact GraphQL mutation payload
    diff_json           JSONB,            -- [{field, old_value, new_value}] — console renders
    generation_source   TEXT NOT NULL DEFAULT 'agent',  -- 'agent' | 'deterministic'
    status              TEXT NOT NULL DEFAULT 'generated'
                        CHECK (status IN ('generated','approved','queued','applied','failed','reverted','expired')),
    risk_tier           TEXT NOT NULL DEFAULT 'medium' CHECK (risk_tier IN ('low','medium','high','plan_only')),
    snapshot_json       JSONB,            -- FULL pre-state from fresh read at pickup (rollback source)
    rollback_of         UUID REFERENCES generated_fixes(fix_id),
    executed_at         TIMESTAMPTZ,
    executed_by         TEXT DEFAULT 'fix_executor',
    adapter_response    JSONB,            -- {ok, userErrors, throttle, error, detail}
    verification_status TEXT CHECK (verification_status IN ('unverified','verified','verify_failed')),
    error_detail        TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    approved_at         TIMESTAMPTZ,
    applied_at          TIMESTAMPTZ,
    reverted_at         TIMESTAMPTZ
);
-- One active fix per (site, target_url, field): executors can never fight.
CREATE UNIQUE INDEX uq_fixes_active_per_target
    ON generated_fixes (site_id, target_url, COALESCE(sub_type, ''))
    WHERE status IN ('generated','approved','queued');
CREATE INDEX idx_fixes_site_status ON generated_fixes (site_id, status);
CREATE INDEX idx_fixes_rec ON generated_fixes (recommendation_id);
```

### 1.3 New table `fix_policy` (risk tiers + weekly caps per site)
```sql
CREATE TABLE fix_policy (
    site_id             UUID REFERENCES site_config(site_id),
    sub_type            TEXT NOT NULL,
    risk_tier           TEXT NOT NULL CHECK (risk_tier IN ('low','medium','high','plan_only')),
    weekly_cap          INT NOT NULL DEFAULT 0,   -- 0 = no execution route
    requires_field_verify BOOLEAN NOT NULL DEFAULT true,
    enabled             BOOLEAN NOT NULL DEFAULT false,   -- master kill-switch
    PRIMARY KEY (site_id, sub_type)
);
```
Enforced at queue time AND executor pickup (7-day `executed_at` COUNT —
same weekly-cap pattern as `plan/19` budget caps).

### 1.4 Ingestion-side prerequisites (own tasks, not pilot scope)
```sql
ALTER TABLE pages ADD COLUMN IF NOT EXISTS meta_description TEXT;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS body_html TEXT;        -- normalizer keeps what it drops today
ALTER TABLE pages ADD COLUMN IF NOT EXISTS body_text_hash TEXT;   -- near-duplicate detection at scale
ALTER TABLE pages ADD COLUMN IF NOT EXISTS heading_outline JSONB; -- only if crawl exposes; never fabricated
ALTER TABLE pages ADD COLUMN IF NOT EXISTS shopify_gid TEXT;      -- GID persistence (collections currently discarded by sync_pages_from_shopify)

CREATE TABLE page_links (              -- link-graph edges (today: only in/out COUNTS on pages)
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id       UUID REFERENCES site_config(site_id),
    source_url    TEXT NOT NULL,
    source_url_hash TEXT GENERATED ALWAYS AS (md5(source_url)) STORED,
    target_url    TEXT NOT NULL,
    target_url_hash TEXT GENERATED ALWAYS AS (md5(target_url)) STORED,
    anchor_text   TEXT,
    context_snippet TEXT,
    first_seen_at DATE NOT NULL,
    last_seen_at  DATE NOT NULL,
    UNIQUE (source_url_hash, target_url_hash)
);
CREATE INDEX idx_pl_target ON page_links (site_id, target_url_hash);

CREATE TABLE site_backlinks_summary (  -- adapter capability + fixture exist; never synced
    site_id UUID REFERENCES site_config(site_id),
    url TEXT,
    referring_domains INT,
    rank NUMERIC(8,2),
    fetched_at DATE NOT NULL,
    UNIQUE (site_id, url)
);
```
`page_links` source: new `crawl_links` capability → DataForSEO OnPage Links
(`v3/on_page/links`) in `openseo_rest_adapter.py` ENDPOINTS — endpoint
shape/cost to verify before Phase 5 (owner-funded, §7).

---

## 2. Action-Type Decision Logic & Write Targets

Pattern per fix: deterministic decision logic picks WHAT → LLM drafts the
VALUE → deterministic validator gates → human approves diff → adapter
executes → field-verify read-back → applied.

### 2.1 improve_page v1 — seo.title fixes only (PILOT)
Decision logic (SEO-reasoned, grounded in `plan/02` signals):
- **Target only `low_ctr`** (position ≤5, CTR < bucket median + site 30th pct)
  = snippet problem → title-fixable. Position 4–20 = relevance/authority
  ceiling (title marginal; skip unless title-quality checks fail).
  click_decline/rank_decline = root-cause investigation, NOT title (v1 skips
  unless CTR-vs-bucket also fails).
- Title-quality checks (data → home):
  1. Keyword coverage: `pages.title` vs `keyword_clusters.keywords[]` + top
     GSC query (`search_performance` by page_url_hash).
  2. Intent match: `keyword_clusters.intent` vs title framing.
  3. Length/truncation (>60 / <20 chars) — deterministic.
  4. Duplicate titles site-wide — `pages.title` self-join.
  5. Brand suffix boilerplate — `site_config.site_name`.
  6. Protect winners: position ≤2 + rising CTR → never rewrite.
  7. Cannibalization interaction: active consolidate rec on cluster → suppress.
- Value generation: agent drafts ≤3 candidates under hard constraints
  (≤60 chars, keyword in first half, no banned patterns: ALL-CAPS runs,
  emoji, spam stacks, brand duplication — denylist IS in v1 validator);
  deterministic gate; fail → no fix row.
- **Write target (VERIFIED 2026-01):**
  `mutation productUpdate(product: ProductUpdateInput!)` — field
  **`seo: SEOInput {title}`** (first-class; verified on ProductUpdateInput).
  Falls back to `title` only if theme renders <title> from product title
  (theme-fact-dependent; owner question). Collections:
  `mutation collectionUpdate(input: CollectionInput!)` (title/description;
  note: rule-set changes return async `job`; store must not be Starter/Retail).
  Scope: `write_products`.
- Entity ref: product GID from `catalogue_coverage.matching_product_ids`;
  collection GID via §1.4 `shopify_gid` patch (collection IDs are currently
  discarded by `sync_pages_from_shopify` — verified).

### 2.2 improve_page v2 (designed now; blocked on §1.4 ingestion)
Checks and grounding verdicts:
| Check | Feasible | Data |
|---|---|---|
| Meta missing/dup/truncated | yes | `pages.meta_description` (new) |
| Semantic coverage vs ranking competitors | PARTIAL — competitor content not ingested; DataForSEO SERP normalizes URLs/positions only; OnPage competitor crawl = new capability (owner-funded) | gap unless added |
| Content structure | partial | `body_html` headings (products/collections); blog/theme content = GAP |
| Near-duplicate | yes | `body_text_hash` |
| Keyword stuffing | yes | token stats (deterministic) |
| E-E-A-T | GAP — no data source in system; out of scope | — |
| Thin content | yes but NOT word-count alone: (low entropy/empty body) AND impressions>0 AND commercial intent | `body_*` + `search_performance` |
Write: `productUpdate(seo: {description})` (VERIFIED first-class on
ProductUpdateInput); `metafieldsSet` (CAS `compareDigest`, max 25/call,
atomic) only as fallback. Scope: `write_products`.

### 2.3 technical_fix — all 9 real sub-types (plan/03 `primary_keyword`)
Prioritized by ranking impact (plan/16 Step 2 ordering):

| # | sub_type | Fix decision logic | Write target (2026-01 VERIFIED) | Auto-executable? |
|---|---|---|---|---|
| 1 | sitemap_index_mismatch (indexable=false AND 200 AND clicks>0 — misnamed; real sitemap membership NOT ingested) | Root-cause chain (plan/16): canonical → meta robots → status. If block = Shopify publish-state (collection published_at null / product status != active — exactly how sync derives indexable) → publish restore | Products: `productUpdate(status: ACTIVE)` (`write_products`). Collections: `publishablePublish(id, input:{publicationId})` — scope **`write_publications`** (VERIFIED; publication id cached in system_config) | **Yes (v1.5)** — cleanest high-impact auto-write |
| 2 | canonical_conflict | plan/16 rules; faceted-intentional reject; dead-canonical verify. Theme-controlled on Shopify | none — plan_only | No |
| 3 | not_indexable | Same chain; publish-state subset = #1; else plan_only | per #1 | Partial |
| 4 | status_failure (404 w/ residual clicks) | Discontinued+traffic → 301 map: nearest equivalent (same product_type collection, else `catalogue_coverage.existing_collection_url`) | `urlRedirectCreate(urlRedirect:{path,target})` | Yes (medium) — rollback `urlRedirectDelete(id)` |
| 5 | broken_internal_link | Dedupe first (plan/16 Step 0); repair-target branch → #1/#4; repoint branch → §2.6 | inherits | Partial |
| 6 | orphan_page/deep_page | Named linking plan (plan/16); crawl-artifact re-check (JS nav) | §2.6 | v3 |
| 7 | duplicate_faceted | Weakest signal (plan/16): require GSC split-proofs; canonical = theme | plan_only | No |
| 8 | structured_data_failure | JSON-LD = theme section; v2+ could populate product metafields feeding theme schema | plan_only now | No |
| 9 | js_rendering_failure | plan/16 Branch A/B: usually crawler-setup or theme/SSR | plan_only | No |

Honest summary: **5 of 9 are theme-controlled → plan_only.** Auto-executable
technical surface = publish-state + redirects + link-derived branches.
Sitemap: native Shopify auto-regenerates on publish/unpublish (no API write,
none needed) — pending owner confirmation of native vs app-generated.
Generator depth-upgrade (grounded, zero new ingestion): `plan/03` emits
`mechanism_hypothesis` in evidence (canonical-vs-robots-vs-status derivable
from columns already joined in `important_pages`).

### 2.4 create_page — real page-type inference (new generator work, confirmed)
- Current defect (verified): `plan/01` hardcodes `/collections/` slug;
  `sync_upsert_keyword_clusters` (sync.py:400-446) hardcodes
  `recommended_page_type='collection'`. Neither is inference.
- New logic:
  1. SERP-type inference from `openseo_serp_snapshots.result_url` patterns
     (`/collections/`|`/products/`|`/blog/`|`/pages/`) among top-5 non-self
     results → archetype. `/products/`-dominated + single matching in-stock
     product (high `page_query_match_scores.catalogue_match_score`) → product;
     multi-product → collection; informational intent → blog.
  2. Gap (flagged): competitor page CONTENT/type beyond URL patterns — needs
     OnPage competitor crawl (owner-funded). v1 ships URL-pattern inference,
     confidence='medium', honestly.
  3. Proposed URL by type: `/collections/<slug>`; product → draft
     (`productCreate` — VERIFIED creates unpublished by default; operator
     approves draft then publishes separately); blog →
     `articleCreate`/`articleUpdate` family (scope `write_content` OR
     `write_online_store_pages` — articleUpdate VERIFIED any-of); static
     page → `pageCreate` (VERIFIED any-of same two scopes).
- Measurement unchanged: cluster baseline (`store_cluster_baseline`,
  measurements.py:75-77), action_type stays `create_page`.

### 2.5 consolidate — redirect/canonical plan
- Winner logic sharpened beyond impressions-only (generator's current pick):
  cluster impressions + full-cluster GSC footprint of the OTHER cluster each
  page serves (plan/17's "weaker across ALL clusters" check) + `pages.indexable`
  + `internal_links_in` + `catalogue_coverage.matching_product_count`.
  Weaker page primary-earner elsewhere → differentiation, not consolidation.
- Fix payload: 301 weaker→stronger + sitemap note + internal-link repoint list
  (from `page_links` once built; until then from `work_required_json`).
- Write (VERIFIED 2026-01): `urlRedirectCreate(urlRedirect: UrlRedirectInput!)`
  → `{urlRedirect{id,path,target}, userErrors}`; update = `urlRedirectUpdate`;
  rollback = `urlRedirectDelete(id)` → `deletedUrlRedirectId`.
  **Scope: `write_online_store_navigation`** (corrected from earlier draft —
  the REST-era `write_content`/`write_redirects` pair is wrong).
  Content retirement: unpublish only AFTER redirect verified live.
  Risk tier: high (capped 2/wk), always field-verified.

### 2.6 internal_linking (only backlink capability; external outreach OUT)
- Target: weak page = `position_4_20`/orphan/`internal_links_in=0`.
- Sources: high-GSC-click pages (28d) with high
  `page_query_match_scores.aggregate_score` on target's cluster, not already
  linking (needs `page_links` — verified gap).
- Anchors: cluster `primary_keyword` + top queries; anchor-diversity guard
  (no identical anchors >3 inserts); insertion = article `body` + collection
  descriptions only (writable records; theme nav = plan_only).
- Write (VERIFIED 2026-01): `articleUpdate(id, article: ArticleUpdateInput!)`
  — body field is **`body`** (resolved from earlier "unverified" flag).
  Scope: `write_content` OR `write_online_store_pages`.
  Collections: `collectionUpdate(descriptionHtml:)` (`write_products`).
- Authority enrichment: `site_backlinks_summary` (adapter capability +
  fixture exist; never synced — small task, owner cost sign-off).

### 2.7 Not-yet-actioned, worth considering (all grounded)
1. Draft/out-of-stock products with live demand (`pages.indexable` from
   Shopify status + `catalogue_coverage.in_stock_product_count` + GSC
   impressions) — no generator acts today; fix = same publish-state write.
2. Conversion-weak pages — `page_business_performance.add_to_carts/checkouts/
   conversion_rate` ingested, never read by any generator (verified: plan/01–04
   don't join it beyond traffic aggregates). v2 improve_page sub-type.
3. Backlink authority data — capability+fixture exist, unsynced; input for
   §2.6 only (not an action type).
4. `competitors` capability — unsynced, no table; create_page depth later.
5. `pages.h1` — column exists, never populated; would feed title≠h1 checks
   (needs crawl field — flagged, not assumed).
6. SERP position volatility over time (`openseo_serp_snapshots` accumulates
   it) — diagnostic generator candidate; data exists, decision logic TBD.

---

## 3. State Machine & Console Integration

```
recommendation: proposed ─approve─► approved ──────────► in_progress ─► live ─► measured
                                 │                  (TRANSITIONS unchanged)
generated_fixes:            (fix generated on approval)
                            generated ─operator diff-approves─► queued ─executor─► applied
                                 │                                  │               │
                                 ├─rejected──► expired (reason→rejection_log)        ├─► failed (retry = new row)
                                 │                                                  └─► reverted (new row, rollback_of)
```
- Generation happens at recommendation approval; operator then reviews the
  DIFF as the second gate (both gates required; `trust_mode` ships OFF).
- `queued→applied` ONLY by `jobs/fix_executor.py` — never inline in an API
  request (Vercel 60s window can't own a write transaction).
- Measurement wiring unchanged: on applied, executor runs the SAME logic as
  `/implement` (`_transition_to_in_progress` + baseline freeze),
  `implemented_at` = execution time; clock's due condition
  (`implemented_at + window + GSC_SETTLE_DAYS`) identical to today.
- New routes (`src/api/routes/fixes.py`, mounted bare + `/api` like existing):
  `POST /recommendations/{id}/fix` · `GET /recommendations/{id}/fix` ·
  `POST /fixes/{id}/approve` · `POST /fixes/{id}/reject` ·
  `POST /fixes/{id}/execute` (dev/manual, guarded) ·
  `POST /fixes/{id}/revert` · `GET /fixes/{id}`.
- Console: FixPanel inside DetailDrawer — field-level diff table (old|new),
  exact payload preview, risk-tier badge, weekly-cap status. Approve = queued;
  Reject → rejection_log. Pipelines kanban gains fix-status chip. One
  component + `useFixData` hook; no new screens.

---

## 4. Shopify GraphQL Write Targets (ALL verified against 2026-01 docs)

Endpoint: `POST https://{shop}.myshopify.com/admin/api/2026-01/graphql.json`,
header `X-Shopify-Access-Token` (unchanged). Version rationale: `2026-01`
stable until 2026-01-16; `2025-10`/`2026-04`/`2026-07` also stable.

**MUTATION REGISTRY (build rule: every pair re-verified against pinned docs
at build time; doc URL recorded in adapter):**

| Write | Mutation (verified) | Scope (verified) |
|---|---|---|
| Product title/SEO/status/description | `productUpdate(product: ProductUpdateInput!)` — `seo` field VERIFIED on input | `write_products` |
| Collection title/description | `collectionUpdate(input: CollectionInput!)` — note `input:` arg; async `job` on rule-set | `write_products` (non-Starter/Retail) |
| Collection publish/unpublish | `publishablePublish` / `publishableUnpublish` | **`write_publications`** |
| Product meta desc (v2) | `productUpdate(seo:)`; `metafieldsSet` fallback (max 25, atomic, CAS `compareDigest`) | `write_products` |
| Content/body | `productUpdate(descriptionHtml:)` / `collectionUpdate(descriptionHtml:)` | `write_products` |
| Redirects | `urlRedirectCreate/Update/Delete` (VERIFIED — corrected names) | **`write_online_store_navigation`** |
| Article body/links | `articleUpdate` — body field **`body`** VERIFIED | any of `write_content`, `write_online_store_pages` |
| Draft product create | `productCreate` (unpublished by default — VERIFIED) | `write_products` |
| Static page create | `pageCreate` | any of `write_content`, `write_online_store_pages` |
| Publish-state (products) | `productUpdate(status: ACTIVE)` (productStatusUpdate deprecated) | `write_products` |
| Scope introspection | `GET /admin/oauth/access_scopes.json` — OAuth is UNVERSIONED per Shopify docs | token-valid only |

Read-back verification: `product(id)`/`collection(id)`/`urlRedirect(id)`
queries. Snapshot capture + verify both GraphQL; snapshot-freshness guard
unchanged. Executor startup logs scope introspection + response
`X-Shopify-API-Version` (warn ≠ pinned).

Adapter changes (all `src/connectors/shopify.py`, existing contract):
- New `_graphql()` caller: cost-bucket throttle tracker
  (`extensions.cost.throttleStatus`, restore ~50 pts/s, updates ~10 pts),
  pre-check `currentlyAvailable`, sleep-until-restore, THROTTLED backoff.
- Error classifier: `userErrors` → failed (typed field-level detail);
  `ACCESS_DENIED` → execution_blocked_scope (3-strike disable);
  HTTP/network → typed skip (never-raise).
- Fix existing bug: `_get_raw` discards error bodies (HTTP 403/429 detail lost).
- Cursor pagination (`PageInfo{hasNextPage,endCursor}`) for reads; mock
  fixtures reworked to GraphQL envelopes (Phase 0 — run_mock_tests depends).
- `resource throttle` note: productCreate/Update extra 50k-variant/day cap —
  surfaced if ever hit.

Consolidated scope set to verify on the real store: `write_products`,
`write_publications`, `write_online_store_navigation`, plus
`write_content` OR `write_online_store_pages` (Phases 5–6 only).

---

## 5. Risk Tiers, Guardrails, Rollback, Production Resilience

### 5.1 Tiers + weekly caps (`fix_policy` defaults)
| Tier | sub_types | Cap/site | Notes |
|---|---|---|---|
| low | seo.title | 10 | single-field, snapshot = old value |
| medium | seo.description, content, product_publish, article body links | 5 | publish can deindex → field-verify before applied |
| high | redirect, product/collection-body link inserts, creates | 2 | traffic-destructive if wrong |
| plan_only | canonical/theme, structured data, js-rendering, faceted | 0 | exported as work items (recommendation-shaped) |

Caps enforced at queue time AND executor pickup; `MAX_APPLIES_PER_RUN=5`.

### 5.2 Concurrency
- `LOCK_KEYS` += `fix_executor` (global) + per-site executor lock
  (`src/jobs/locks.py` session advisory locks, try-mode).
- `uq_fixes_active_per_target` partial unique index = second guard.
- Strictly one fix per (site,url) per run; second active fix on same
  target+field → 409 from API; cross-type conflict resolves by risk tier
  (high wins; low expires w/ logged reason) — deterministic, no LLM.
- GraphQL cost-bucket replaces req/sec model: pre-check
  `currentlyAvailable`, sleep-until-restore, THROTTLED backoff; sequential
  per site (JOB_MAX_WORKERS=1 discipline preserved).
- `metafieldsSet` CAS wherever metafields apply (verified atomicity).
- `collectionUpdate` async `job{done=false}` → verification stays
  `unverified` until re-read (not failure).

### 5.3 Rollback
- `snapshot_json` = FULL pre-state from FRESH READ at executor pickup (never
  trusted from generation); live value ≠ diff old_value → auto-expire
  (`verify_failed`) + regenerate next cycle (stale-diff protection).
- Restore: `POST /fixes/{id}/revert` → adapter with snapshot values → new
  row w/ `rollback_of` (reverts are audited rows).
- `change_log.rollback_reference` (exists, never populated — verified) used:
  `= fix_id`; `after_snapshot` = read-back result. Best-effort write pattern.
- Auto-revert on failed read-back verification ONLY. Lost/Neutral verdicts
  never auto-revert (operator decisions) — recorded decision.

### 5.4 Production resilience (real-store gaps; dummy ~20-product validation
does NOT generalize)
- Reads: skip-and-log on malformed/missing (never-raise contract).
- Scale: real pagination (Shopify cursor loops w/ max_pages cap + truncation
  warning — existing pattern); metafield reads batched (n+1 GETs unacceptable
  at real catalogue scale).
- Executor: payload JSON schema-validated per sub_type before ANY network
  call; per-run ceiling; 403→scope-block, 429→backoff paths intentionally
  exercised in tests.
- Title/fix decision minimums (e.g. ≥500 impressions/28d/query) so sparse
  real-GSC data never triggers rewrites.
- **Pre-production battery vs YOUR real store before any enabled=true:**
  1. Read-verify metafield/GID ingestion on ≥100 real products (nulls,
     drafts, archived, non-English).
  2. DRY-RUN mode: executor logs payloads end-to-end, writes nothing;
     diff accuracy checked vs live store.
  3. Scope probe + GraphQL write-auth probe (harmless self-rename on a
     test product) — proves scopes + exercises error paths.
  4. Single real apply → read-back → revert cycle on one low-traffic
     product (full rollback proof).
  5. Concurrency drill: two queued fixes same URL+field → expiry behavior.
- Startup diagnostics: scope introspection (unversioned endpoint) + version
  drift warning.

---

## 6. Implementation Sequence

| Phase | Scope | Depends on |
|---|---|---|
| 0 | Schema (§1); GraphQL adapter shell (throttle tracker + error classifier); fixture rework to GraphQL envelopes; scope introspection; fixes API routes; state-machine wiring | owner store credentials |
| 1 PILOT | improve_page v1 seo.title only (§2.1); productUpdate/collectionUpdate adapters; executor job + locks + caps; FixPanel diff UI; change_log/rollback path | `write_products` verified; collection GIDs ingested |
| 1.5 | Real-store validation battery (§5.4) on YOUR store; pilot enabled `title` only | Phase 1 green |
| 2 | Ingestion: body_html (normalizer change), meta desc (seo/metafield read); content/meta decision logic (§2.2); adapters reuse Phase-1 write path | Phase 1 stable |
| 3 | technical_fix auto-executable subset: publish-state (product_publish, medium tier); 404→301 via urlRedirect* (`write_online_store_navigation` — VERIFY); generator emits mechanism_hypothesis | Phase 2 |
| 4 | consolidate: sharpened win/lose logic; redirect execution reuses Phase-3 capability; unpublish-after-redirect sequencing (needs `write_publications` for collections) | Phase 3 |
| 5 | internal_linking prerequisites: `crawl_links` capability (OnPage Links — verify endpoint/cost FIRST); `site_backlinks` sync; edge backfill; link-insert logic; `articleUpdate` adapter | Phase 3 + owner cost approval |
| 6 | create_page rebuild: URL-pattern page-type inference replacing hardcoded `/collections/` (§2.4); new generator SQL; draft-entity writes (productCreate unpublished-by-default, articleCreate/pageCreate per verified scopes); competitor-content enrichment only if OnPage added | Phase 5 |
| continuous | plan_only exports (canonical, structured data, js-rendering, faceted) ride along from Phase 1 — draft generation only, no write path | — |

Sequencing logic: each phase adds exactly ONE write-adapter capability and
reuses everything prior; create_page (largest blast radius, only genuinely
new generator work) is deliberately last, benefiting from accumulated
measurement history.

---

## 7. Owner-Gated Items (decision-authority: final list)

1. **Store access & scopes** (BLOCKING for real writes): real-store
   credentials + verify token carries `write_products`, `write_publications`
   (collection publish/unpublish), `write_online_store_navigation`
   (redirects), `write_content` OR `write_online_store_pages` (Phases 5–6).
   Executor introspects and reports granted-vs-required at startup.
2. **Store facts**: theme architecture (record-controlled vs
   theme-template-controlled copy); native vs app-generated sitemap.
3. **Money**: DataForSEO OnPage Links + competitor-page crawl + backlinks
   sync funding (~$0.02–0.50/task per verified cost data). Phases 0–4 need
   NO new spend.
4. **Business call — pilot surface**: v1 writes `seo.title` only (SERP
   presentation; zero storefront-customer-visible surface) vs also changing
   product `title` (customer-visible + handle-regeneration risk).
   RECOMMENDATION: seo.title-only. If unanswered by pilot start, PROCEEDS
   with seo.title-only (safe default).

Everything else is decided by the executor plan itself (decision log §0).
The four items above gate ENABLING real writes, not building the framework,
dry-run mode, or mock tests.

---

## 8. Verification history (audit trail)

- Phase 1 discovery: schema/generators/connector/status machine verified in
  code; Shopify connector READ-ONLY (2 GET capabilities), scopes
  unverifiable from repo, meta/body fields dropped by normalizers,
  collection IDs discarded, `pages.h1` unpopulated, change_log
  rollback_reference unused, backlinks/competitors capabilities orphaned.
- REST→GraphQL decision: connector pinned to dead `2024-10` REST; platform
  REST product deprecations past (Feb/Apr 2025); GraphQL-only policy adopted
  with cost-bucket executor redesign.
- Mutation/scope re-verification round (2026-01 docs fetched live):
  corrected `redirectCreate→urlRedirectCreate` family, redirect scope
  `write_online_store_navigation`, publish scope `write_publications`,
  `collectionUpdate(input:)` arg, `articleUpdate.body` field, `pageCreate`
  any-of scopes; discovered `ProductUpdateInput.seo` (pilot upgrade).
- Phase 1 scope-gate cleanup (2026-09-22, owner-reviewed; fix-now rationale:
  the flat-union gate already caused real confusion in the Phase 1.5 live
  battery — it reported `write_content` missing on the dev store while the
  pilot scopes were fully satisfied, a false alarm a strict superset
  consumer would have turned into a hard block):
  1. `REQUIRED_WRITE_SCOPES` flat union replaced by
     `granted_covers_required()` with `ANY_OF_SCOPE_GROUPS` modeling the
     verified `write_content | write_online_store_pages` any-of pair
     explicitly; registry-derived, no hand-maintained second list.
  2. `required_scopes_for_sub_types()` now DERIVES every scope from
     `MUTATION_REGISTRY` by mutation name (single source of truth — the two
     lists cannot drift; a sub_type referencing an unregistered mutation
     raises `KeyError`). Unknown sub_types return `[]` and the executor
     probe reports `unknown_sub_type` loudly instead of passing silently.
  3. `page_create` mapping added (was silently empty → no scope diagnostic).
  4. Publish-state split for Phase 3: `product_publish` remains the union
     (products via `productUpdate`/`write_products`, collections via
     `publishablePublish`/`write_publications`); `product_publish_product`
     (products-only, `write_products`) and new `collection_publish`
     (collections-only, `write_publications`) are the precise keys. Rename
     lands at the adapter boundary when Phase 3 wires the adapters.
  5. `collectionCreate` verified against pinned 2026-01 docs (2026-09-22):
     requires `write_products`; `input: CollectionInput!`; created
     unpublished by default (publishablePublish after, same semantics as
     `productCreate`); Starter/Retail excluded (same as collectionUpdate).
     Registered in `MUTATION_REGISTRY`; `collection_create` scope mapping
     now registry-derived. Phase 6 still owns the adapter wiring.
- Decision-authority note: fix-now-vs-phase-boundary timing judged by the
  standing executor-plan rule (cheap cleanup beats registry/policy drift;
  the gate had already produced a misleading live diagnostic), recorded here
  per decision-authority rules — not owner-routed.
- Phase 3 build (2026-09-23, publish-state write path — the §2.3 table's only
  OTHER auto-executable surface):
  1. Mutation re-verified against pinned 2026-01 docs live (2026-09-23):
     `publishablePublish`/`publishableUnpublish` (args `id` +
     `input:[PublicationInput!]!`, scope `write_publications`),
     `ProductUpdateInput.status` (ProductStatus enum), `Publishable`
     interface (`publishedOnPublication(publicationId:)` read-back).
     Registry comment updated with the re-verification date.
  2. plan/03 emits `mechanism_hypothesis` deterministically in
     not_indexable/sitemap_index_mismatch evidence (canonical_block →
     robots_block → status_block → publish_state, from columns the CTE
     already joins; `robots_allowed`/`in_sitemap` now projected from
     plan/24 columns). `orchestrator._technical_fix_row` persists `issue_type`
     explicitly into evidence (was only reachable via the SQL's
     primary_keyword slot, which the orchestrator dropped).
  3. New adapters: `product_publish_product`
     (`productUpdate(status)` — writes ONLY id+status, no seo drift) and
     `collection_publish` (`publishablePublish` + `publishableUnpublish`
     rollback). Snapshots capture full publish pre-state (product.status /
     publication membership); restore refuses to guess without one. Fixed a
     double-nesting bug caught by the revert test (restore wrapped
     `{"product": {"product": {...}}}`).
  4. Publication-id resolution: `resolve_publication_id()` in the connector
     (system_config cache `shopify.publication_id` → one `publications`
     query → cache-back). Generation refuses a collection publish fix when
     nothing resolvable (typed, never a guessed channel).
  5. `load_decision_inputs` accepts `technical_fix` (issue_type from
     evidence_json; page row supplies GID); `generate_fix_for_recommendation`
     routes it to `generate_publish_fix_for_recommendation`, which gates on
     issue_type ∈ {sitemap_index_mismatch, not_indexable} AND
     `mechanism_hypothesis == 'publish_state'` — canonical/robots/status
     blocks stay plan_only.
  6. Route `POST /recommendations/{id}/publish-fix`; fix_policy seeds now
     include `product_publish_product` (medium/5) + `collection_publish`
     (medium/3).
  7. Tests: tests/run_fix_publish_tests.py (26 checks: routing, plan_only
     gates, scope mapping, adapter validation, product e2e incl.
     verify_failed→auto-revert, collection e2e incl. unpublish rollback,
     3-strike disable). Full fix suite green (executor, api, meta,
     generator-quality, llm-drafting, graphql-shell, last-mile, scheduler,
     serp-grounding, mock, api-smoke, integration-mock).
  8. Remaining Phase-3 row (§2.3 #4, status_failure 404→301) deliberately
     deferred to its own pass: the redirect ADAPTER is already built and
     exercised (Phase "last-mile"), only the technical_fix → redirect
     generation branch remains.
- Phase 3 CLOSED (2026-09-23): technical_fix status_failure (404) → redirect.
  1. Routing inside `generate_fix_for_recommendation` by
     `evidence.issue_type` (action_type stays `technical_fix` — no new
     measurement_window_lookup row, measurement pipeline untouched). A page
     row is NOT required for the dead URL (it 404s; its row may be gone).
  2. Deterministic gates (plan/16): status_code ∈ {404, 410} only (5xx/403
     are a different fix); residual-traffic gate (gsc_clicks_28d ≥ 10, or
     clicks > 0 AND impressions ≥ 100 — no residual visibility = plan_only
     hygiene); destination = the dead page's product_type collection
     (`/collections/<slug>` from pages, indexable) else
     `catalogue_coverage.existing_collection_url` else refuse — never a
     guessed target.
  3. Contract inheritance: the branch REUSES
     `generate_redirect_fix_for_recommendation` — sub_type='redirect',
     risk 'high' (weekly cap 2), conflict keyed on the SOURCE url via
     `check_conflict`, `payload.grounding.route='status_failure'` carries
     the dead-URL evidence for console/audit, and execution/rollback run the
     EXISTING redirect adapter (pre-state snapshot at pickup, revert deletes
     the created redirect via snapshot `redirect.created_id`, audited
     rollback_of row).
  4. Tests: tests/run_fix_status_redirect_tests.py (16 checks: gates, payload
     shape, conflict, executor apply, revert, audit row). Full 13-suite fix
     battery green.
- Phase 4 items 1–2 (2026-09-23): consolidate survivor sharpening + direction
  guards. Fix-generation gates (fixes/generator.py), NOT plan/04 SQL changes:
  the generator SQL's impressions-only pick stays as the CANDIDATE producer;
  the deterministic re-verification runs where fixes are minted (data -> home
  at fix generation, mirroring the title/meta gate pattern).
  1. `verify_consolidation_direction` — split-intent guard: the redirect
     source's 28d GSC footprint is split by cluster membership
     (cluster_queries join); if the majority of its impressions come from
     OTHER clusters (share < 0.5 with total >= 100), it is a primary earner
     elsewhere -> refuse, differentiation is the right fix. Footprint floor
     treats tiny samples as noise (never a fabricated conflict); no cluster
     membership -> check skipped (single-query candidate).
  2. `verify_survivor_strength` — relative-strength guard: the candidate's
     OWN evidence.impressions_distribution must show source < survivor; a
     winner→loser redirect is refused regardless of how the candidate was
     seeded (skipped when the distribution is missing — never fabricated).
     Survivor sanity (exists in pages + indexable) verified early with a
     survivor-named error. Coverage-orphaning guard: catalogue_coverage
     naming the SOURCE as the cluster's coverage owner refuses the 301
     (merge/migrate first or reverse); skipped when coverage is unknown.
     Tiebreakers (indexable, internal_links_in, coverage owner + count)
     recorded in payload.grounding.sharpened_direction for the audit trail.
  3. status_failure route skips the split-intent half (the source is already
     dead — nothing healthy to protect); survivor sanity still applies.
  4. load_decision_inputs consolidate branch now loads evidence_json (the
     guards read impressions_distribution from it).
  5. Tests: tests/run_fix_consolidate_sharpen_tests.py (15 checks: clear-loser
     consolidate, split-intent refusal + footprint floor + no-cluster skip,
     coverage orphan refusal + migrated pass + unknown skip, survivor sanity,
     reversed-direction rejection, hook regression). Full 13-suite battery
     green.
- Phase 4 item 3 (2026-09-23): unpublish-after-redirect sequencing, CLOSED.
  Design: a SEQUENCED FOLLOW-UP FIX ROW, not a post-apply hook — the executor
  is already the single writer with per-row policy gates, fresh snapshots,
  verification, and revert semantics; a new row reuses ALL of it (and keeps
  the two-gate approval intact for the unpublish itself).
  1. Sequencing contract (structural): when a consolidate redirect fix is
     APPLIED with verification_status='verified', `_mint_unpublish_followup`
     inserts a `product_unpublish`/`collection_unpublish` row (status
     'generated', risk 'medium', rollback_of = redirect fix id). The row
     CANNOT exist on any failure path: verify_failed auto-reverts mint
     nothing; adapter failures mint nothing; a second verified run is
     idempotent (rollback_of lookup). `payload.grounding` carries the
     sequencing proof: route=unpublish_after_redirect, redirect_fix_id,
     redirect_verified=true.
  2. Mutation mapping (VERIFIED 2026-01 surfaces, reusing Phase-3 read-backs):
     products -> `productUpdate(status: ARCHIVED)` (write_products; ARCHIVED
     not DRAFT — retired, revertable); collections -> `publishableUnpublish`
     against the system_config publication id (write_publications). Entity
     resolution from the SOURCE page row (pages.page_type + shopify_gid) —
     unknown page_type or missing GID skips (typed, logged). New adapters
     with full execute/snapshot/restore contracts: pre-execution snapshot
     captures product.status + publication membership; restore returns the
     entity to its pickup state (no-op restore recorded when pickup found it
     already unpublished — never a fabricated publish).
  3. Rollback lineage (strict reverse order): the redirect revert endpoint
     now runs `revert_unpublish_before_redirect` FIRST — an APPLIED unpublish
     is undone via its own audited revert (entity reactivated) BEFORE the
     redirect is deleted (undoing in the other order would leave the source
     unpublished with no redirect live). A NOT-YET-APPLIED follow-up is
     expired instead (never executed after its redirect's undo). Failures
     are typed and reported, never raised; the redirect revert proceeds.
  4. Executor claim path: `_claim_queued` now admits sequenced rows
     (`rollback_of IS NULL OR sub_type IN unpublish`); revert-audit rows stay
     excluded. Scope mapping: product_unpublish -> write_products,
     collection_unpublish -> write_publications (registry-derived).
  5. Policy: fix_policy seeds add product_unpublish + collection_unpublish
     (medium, cap 3/site, field-verify on, disabled by default).
  6. Tests: tests/run_fix_unpublish_sequence_tests.py (24 checks: product e2e
     sequencing + snapshot + revert; failure safety both paths (verify_failed,
     userErrors); revert lineage incl. not-yet-applied expiry + no-op guard;
     collection path incl. republish revert). Full 15-suite battery green.
- LIVE verification battery on action-seo-test (2026-09-23, owner-directed
  controlled protocol — sync → title/meta diff gate → executor apply → live
  read-back, NO rollback; plus a create-and-delete redirect check). Findings:
  1. `collections.json` REST endpoint returns HTTP 403 on the live store
     (verified; custom_collections.json + smart_collections.json work) —
     connector ENDPOINTS split the collections capability into the working
     pair, _get() merges both. Mock fixtures unaffected.
  2. **productUpdate(seo:) REPLACES the whole seo object** (live-verified:
     writing seo:{title} wipes seo.description and vice versa). All seo
     adapters now carry BOTH fields from the fresh pickup snapshot in one
     write (`_sibling_seo_field`); a missing snapshot refuses (typed
     no_snapshot) rather than destroying data. Restore paths likewise
     restore both fields. fix_executor passes the in-memory snapshot to the
     adapter call (claimed row predates the snapshot UPDATE).
  3. The one-fix-per-(site,url)-per-run guard correctly skipped the second
     queued fix on the same target within a single run (duplicate_target_in_run);
     a second executor run applied it — behavior per §5.2.
  4. Redirect path exercised live end-to-end: pre-state snapshot,
     urlRedirectCreate → independent read-back (gid
     gid://shopify/UrlRedirect/592365322538) → urlRedirectDelete → gone.
  5. Test coverage added: sibling-title ride-along assertion in
     run_fix_meta_tests; full fix suite re-ran green post-patch.
- §5.4 pre-production battery #4 (2026-09-24, action-seo-test): the FULL
  Phase-4-item-3 cycle executed LIVE with two dummy products created via
  productCreate (loser gid://shopify/Product/10326374514986, survivor
  gid://shopify/Product/10326374547754) and deleted afterwards (12/12
  checks green, LIFO restore verified on the store):
  1. Scope probe: write_products + write_online_store_navigation granted;
     write_publications NOT present on the token but unneeded for the
     product path (registry mapping only demands it for collection rows).
  2. Redirect cycle live: consolidate rec (dummy GIDs seeded locally) →
     generate → approve → executor → urlRedirectCreate applied+verified;
     live read-back confirmed /products/bat54-loser-… → /products/bat54-survivor-….
  3. Sequencing fired live: `product_unpublish` follow-up minted
     ('generated'/medium, rollback_of=redirect fix), approved, executed —
     loser product ARCHIVED, read-back verified.
  4. LIFO revert verified live: unpublish revert restored loser ACTIVE
     (pickup-status restore, "restored": "ACTIVE"), THEN redirect revert
     deleted the 301 (urlRedirect read-back → null). Dummy products deleted.
  5. **LIVE BUG FOUND + FIXED**: stores with NO sales-channel publications
     (action-seo-test returns `publications: []`) reject ANY publicationId
     on `publishedOnPublication` with NOT_FOUND — nulling the WHOLE product
     read (one bad field poisons the object). Product publish/unpublish
     snapshot+verify+restore now use a publication-free status read
     (`PRODUCT_STATUS_READ_QUERY` + `_product_status_read`); the
     publication-scoped read stays on the collection path, which already
     refuses upstream when no publication id exists. Without this fix the
     unpublish verify would ALWAYS fail on such stores (write lands, verify
     reads None, auto-revert refuses no_snapshot → stuck ARCHIVED).
     Regression battery re-ran green (16 suites).
  6. Battery-harness residue (run 1 crashed pre-cleanup) removed; the
     shared-fixture API smoke test depends on the consolidate-evidence
     invariant, so live-battery runs MUST clean their local rows even on
     failure paths (harness now does; run-1 orphans purged by hand).
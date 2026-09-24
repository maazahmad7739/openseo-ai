# OpenSEO AI — Rules & Policy Engine
# ============================================================
# Definitive engineering reference for every detection rule, threshold,
# guardrail, decision formula, and safety mechanism in the system.
#
# Sources: plan/00–23, src/fixes/generator.py, src/fixes/policy.py,
# src/fixes/adapters.py, src/jobs/fix_executor.py, src/measurement/*,
# src/generators/orchestrator.py, src/audit/*, src/connectors/shopify.py.
# Every constant below is quoted verbatim from code/SQL with file citations.
# ============================================================

**Version:** 1.0 · **Date:** 2026-09-23 · **Status:** Production reference
**Grounding:** every value below is extracted from the repository — plan SQL files (`plan/00`–`plan/23`), Python decision modules (`src/fixes/`, `src/measurement/`, `src/jobs/`, `src/audit/`), and connectors. Where a concept is *planned but not implemented*, it is explicitly marked **PLAN**.

---

## Document Map

| Section | Covers |
|---|---|
| [1. Technical SEO Policies & Detection Rules](#1-technical-seo-policies--detection-rules) | Indexation, crawlability, status codes, link health, canonicals, redirects |
| [2. On-Page & Opportunity Detection Rules](#2-on-page--opportunity-detection-rules) | Striking distance, low CTR, declines, cannibalization, consolidation |
| [3. Generative & Content Optimization Rules](#3-generative--content-optimization-rules) | Title/meta specs, anti-hallucination, SERP grounding, outline gaps |
| [4. Execution Guardrails & Rollback Safeguards](#4-execution-guardrails--rollback-safeguards) | Protect-winner, fix policy, executor, verification, auto-revert |
| [Appendix A. Master Threshold Table](#appendix-a-master-threshold-table) | Every numeric constant in one place |
| [Appendix B. Honest Gaps](#appendix-b-honest-gaps) | Concepts requested but NOT implemented (do not assume them) |

---

## 1. Technical SEO Policies & Detection Rules

### 1.1 Indexation & Crawlability

#### 1.1.1 The `indexable` signal and its limits

Indexability is a single crawl-derived boolean on the `pages` table (`plan/00-schema.sql:148`):

| Column | Values | Source |
|---|---|---|
| `pages.indexable` | `true`/`false` (default `true`) | OpenSEO crawl pull (`weekly_crawl.py` → `fetch("crawl_audit")`) |
| `pages.status_code` | `INT` | same crawl |
| `pages.canonical_url` | `TEXT` | same crawl |
| `pages.render_status` | `server_rendered` \| `js_rendered` \| `render_failed` \| `not_rendered` | `plan/00-schema.sql:160-161` |

**Hard rule:** `indexable = false` says *THAT* the page is blocked, never *HOW* (`plan/16-skill-technical-fix.md:66-70`). Before any recommendation is approved, the blocking mechanism must be named — the fix differs completely:

```
1. canonical_url is non-self-referencing AND differs from page URL
   → root cause is the CANONICAL (diagnose as canonical_conflict;
     do not propose touching meta robots).

2. status_code = 200 AND canonical self-referencing
   → blocking factor is meta robots, X-Robots-Tag, or robots.txt.
     Verify via fetch: check <meta name="robots"> / response header /
     robots.txt and NAME it in the diagnosis.

3. status_code >= 400 with indexable = false
   → this is a STATUS FAILURE, not an indexability defect.
     Reject as duplicate of the status candidate.
```
— `plan/16-skill-technical-fix.md:70-81` (root-cause chain)

#### 1.1.2 GSC inspection categories (`crawled_not_indexed` vs `discovered_not_indexed`) — **PLAN**

GSC URL-Inspection coverage classes are **not ingested** as discrete fields anywhere in the repo. The system's proxy for "Google is clicking a page the site declares non-indexable" is:

- **`sitemap_index_mismatch` (Generator 3, `plan/03-generator-technical-root-causes.sql:275-307`)**
  - Trigger: `indexable = false AND status_code = 200 AND gsc_clicks_28d > 0`
  - Interpretation (Skill 4, `plan/16`): *"Google has NOT dropped the page, so the block is recent/reversible"* — the sharpest defect signal in Generator 3.
  - Impact: `high` when `gsc_clicks_28d > 100`, else `medium`.
  - Root-cause order (Skill 4): **canonical → meta robots → status**; sitemap membership is treated as a *symptom*, never the cause (`plan/16-skill-technical-fix.md:205-225`).
  - De-dupe: this URL also appears under `not_indexable` — keep the `sitemap_index_mismatch` diagnosis when both exist (it proves ongoing traffic loss).

The "not-indexed" class split (Crawled vs Discovered) would require GSC URL Inspection ingestion; it is **not built** (see [Appendix B](#appendix-b-honest-gaps)).

#### 1.1.3 `not_indexable` (Generator 3, Issue 1 — `plan/03:56-95`)

| Rule | Value |
|---|---|
| Trigger | `ip.indexable = false` AND `ip.page_type IN ('collection','product','landing','category','homepage')` |
| Prerequisite | page must have been crawled (`status_code IS NOT NULL`) |
| Impact `high` | `page_type IN ('collection','product','landing')` AND `gsc_clicks_28d > 100` |
| Impact `medium` | same page types, any clicks |
| Impact `low` | everything else |
| Acceptance criteria | "Page passes Google URL Inspection and is included in sitemap" |

**Intentional-noindex false positives to reject** (`plan/16-skill-technical-fix.md:83-90`):
- Paid-campaign landing pages, internal search results, tag archives with `noindex`.
- Explicit rule: `page_type = 'landing' AND organic_sessions_28d = 0 AND gsc_clicks_28d = 0` → **REJECT**: *"Noindex appears intentional (landing page with zero organic visibility)."*
- Geo/consent interstitials returning 200 flagged non-indexable only in a stale crawl snapshot → re-crawl before asserting.

**Accept only if** (`plan/16:92-95`):
- Page has organic visibility (`gsc_clicks_28d > 0` OR `organic_sessions_28d > 0`), AND
- Mechanism identified or verifiable in one fetch, AND
- `page_type` is revenue-bearing (`collection/product/landing/category/homepage`).

#### 1.1.4 robots.txt, meta robots & header enforcement

Two distinct enforcement surfaces exist:

| Surface | Where enforced | Rule |
|---|---|---|
| **robots.txt for OUR fetcher** (audit engine page fetch) | `src/audit/page_fetch.py:143-170` | robots.txt honored before fetching; disallow → typed `blocked_robots` (HTTP 422, `plan/23 §4.1`). robots.txt *unreachable* = allowed (standard practice). Agent matched by bare product token `ROBOTS_AGENT = "OpenSEOAuditBot"` (`page_fetch.py:45`). |
| **robots.txt / meta robots / X-Robots-Tag on the audited SITE** | `plan/16-skill-technical-fix.md:74-79`, `plan/07-skill-diagnose-existing-page.md:17-43` | These are *diagnosis inputs only* — verified via a fetch and named in the diagnosis; the system never writes robots directives automatically (all theme-controlled paths are `plan_only`, `plan/21 §2.3`). |

Diagnostic checklist order from Skill 2 (`plan/07` Step 1): status 200 → no noindex meta → not robots.txt-blocked → canonical self-referencing → not X-Robots-Tag blocked → renderable → in sitemap.

#### 1.1.5 Sitemap membership & orphan classification

- **Real sitemap membership is NOT ingested.** `plan/21 §2.3` row 1 states the sub_type is *"misnamed; real sitemap membership NOT ingested."* Shopify's native sitemap auto-regenerates on publish/unpublish — no API write needed (pending owner confirmation, `plan/21 §2.3`).
- **Orphan vs deep page** (Generator 3, Issue 5, `plan/03:226-268`):

| Issue type | Trigger | Additional guards | Acceptance criterion |
|---|---|---|---|
| `orphan_page` | `internal_links_in = 0` | `status_code = 200`, page_type in (collection, product, landing, category, blog) | "Page is reachable within 3 clicks from homepage" |
| `deep_page` | `crawl_depth > 5` | same | same |

Impact: `high` when `product_count > 0` OR page_type in (collection, product), else `medium`.

**Crawl-artifact false positives** (`plan/16-skill-technical-fix.md:183-199`):
1. JS-rendered navigation: if the site's nav is client-rendered and the crawl recorded `render_status='render_failed'` (or rendered==raw) on TEMPLATES, `internal_links_in = 0` may mean the crawler never saw the nav. Cross-check: does the URL share its template with pages that DO have `internal_links_in > 0`? If yes → crawl artifact, REJECT (or downgrade to "re-crawl first").
2. Intentionally deep paginated/archived pages with no search demand (`gsc_clicks_28d = 0`) → REJECT.
3. Pages published < 14 days ago → premature, REJECT; the next crawl re-flags if still orphaned.

#### 1.1.6 JS rendering failure (Generator 3, Issue 9 — `plan/03:393-438`)

Two detection branches (`plan/16-skill-technical-fix.md:289-315`):

| Branch | Condition | Verdict rule |
|---|---|---|
| A | `render_status = 'render_failed'` | Crawler failed, not necessarily Google. If `gsc_clicks_28d > 0` AND page is indexed → Google renders it fine; REJECT as site defect (crawl-infrastructure note). |
| B | `raw_html_hash IS NOT NULL AND rendered_html_hash IS NOT NULL AND rendered_html_hash = raw_html_hash` | AMBIGUOUS by construction: for a server-rendered page rendered==raw is the *expected* outcome. Accept only when raw HTML is confirmed a content-empty JS shell (empty `<body>`/app-shell markup). |

Accept only when: raw HTML confirmed content-empty shell AND (clicks present OR commercially important) AND `render_status IN ('render_failed', missing/stale rendered capture)`.
Impact: `high` when `gsc_clicks_28d > 50 OR organic_sessions_28d > 0`, else `medium`.

### 1.2 Crawl Errors & Link Health

#### 1.2.1 Status-code failure classification (Generator 3, Issue 3 — `plan/03:144-181`)

Trigger: `status_code >= 400` AND page_type in (`collection, product, landing, category, homepage, blog`).

| Condition | Impact |
|---|---|
| `status_code = 500 AND gsc_clicks_28d > 0` | **high** |
| `status_code = 500` (any) | **medium** |
| `status_code = 404 AND gsc_clicks_28d > 10` | **medium** |
| all other failures | **low** |

**Soft-404 / intentional-404 adjudication** (`plan/16-skill-technical-fix.md:136-160`):
- 5xx → real defect (server instability). HIGH if the page carries traffic.
- 404 on product/collection:
  - Product discontinued AND delisted everywhere (nav, sitemap, no links) → intentional; REJECT **unless** `gsc_clicks_28d` shows Google still sending traffic — then the fix is a **301 to nearest equivalent**, not "fix 404".
  - Product in catalogue (still purchasable) but page 404s → REAL defect, high priority.
- 403/401 on public commerce pages → real defect (bot management or auth wrongly gating Googlebot — verify user-agent treatment).
- REJECT if `status_code >= 400 AND gsc_clicks_28d = 0 AND organic_sessions_28d = 0` AND internal link context unknown → no visibility at risk.
- REJECT if the URL is a pagination/facet variant Google is supposed to forget.

#### 1.2.2 Broken internal links (Generator 3, Issue 4 — `plan/03:189-220`)

| Rule | Value |
|---|---|
| Trigger | `status_code >= 400 AND internal_links_in > 2` (navigation-level breakage, not a single stale blog link) |
| Generator impact | `'high'` unconditionally |
| Skill-4 override | `internal_links_in` high BUT `gsc_clicks_28d = 0` → downgrade to **medium** (link hygiene, not yet traffic loss) |
| Fix branches | "repair target" (URL should exist — collection with traffic history) OR "repoint the linking pages" (target gone for good) |
| Acceptance | "All internal links resolve to HTTP 200 pages" |

De-dupe note (`plan/16` Step 0): `broken_internal_link` on URL A and `status_failure` on URL B are separate targets — keep both; `broken_internal_link` ⊃ `status_failure` on the SAME URL → keep the root-cause diagnosis only.

#### 1.2.3 Canonical conflict resolution (Generator 3, Issue 2 — `plan/03:101-139`)

Detection predicate:

```sql
WHERE ip.canonical_url IS NOT NULL
  AND ip.canonical_url != ip.url     -- STRING comparison (weak evidence alone)
  AND ip.status_code = 200
```

Impact: `high` when `gsc_clicks_28d > 50`; `medium` when `organic_sessions_28d > 0`; else `low`.

**Intentional patterns that look like defects — reject or downgrade** (`plan/16-skill-technical-fix.md:100-134`):

| Pattern | Verdict |
|---|---|
| Faceted/parameterized URL canonicals to clean URL (`page_url` contains `?`, canonical strips parameters) | **WORKING AS DESIGNED** — REJECT: "Canonical is intentionally consolidating faceted variant — correct behaviour, not a defect." |
| Cross-domain canonical (syndicated/licensed content) | Legitimate only with documented reason; target domain ≠ site domain AND syndicated → REJECT or demand operator confirmation. |
| Pagination canonicaling to page 1, or hreflang'd country/language variants | Intentional consolidation → downgrade to LOW; approve only if target holds meaningful impressions it is losing. |

**Real defects — approve** (`plan/16:123-130`):
- Canonical points to a URL that 404s/redirects (**dead canonical** — verify the canonical target's status via fetch).
- Canonical points to an unrelated page (different template/intent).
- Canonical points to itself with wrong scheme/host (http vs https, www).

**String-comparison caveat (verbatim):** *"the generator compares canonical_url != url as STRINGS — trailing slashes, query strings, or scheme differences alone are weak evidence. Confirm the canonical target actually differs in content/behaviour."* (`plan/16:127-130`)

Impact override rule: *"A canonical conflict on a page with 0 sessions/0 clicks is LOW regardless of generator default."* (`plan/16:132-133`)

#### 1.2.4 Redirect chain & loop detection — **NOT IMPLEMENTED**

There is no redirect-chain walker or loop detector in the repo. What exists instead:

- Redirect **writes** only: `urlRedirectCreate` / `urlRedirectUpdate` / `urlRedirectDelete` (Shopify GraphQL, scope `write_online_store_navigation` — `src/connectors/shopify.py:312-323`).
- Loop prevention is structural, not detection-based: one active fix per (site, target_url, field) via `uq_fixes_active_per_target` (`plan/22-stage2-phase0-schema.sql:70-72`), plus one fix per (site, url) per executor run (`src/jobs/fix_executor.py:527-531`).
- Cross-type conflict resolution on the same target: **risk tier wins** (high > medium > low); the lower-tier fix expires with a logged reason — deterministic, no LLM (`plan/21 §5.2`).

See [Appendix B](#appendix-b-honest-gaps) for the honest gap statement.

#### 1.2.5 Duplicate faceted URLs (Generator 3, Issue 7 — `plan/03:313-346`)

| Rule | Value |
|---|---|
| Detection predicate | `url LIKE '%?%' AND status_code = 200 AND indexable = true AND page_type = 'collection'` |
| Traffic signal | `gsc_clicks_28d IS NULL` — the generator has NO traffic signal for this issue |
| Evidence class | Weakest in Generator 3 (Skill 4, `plan/16:227-255`) |

Approve (medium) only with harm proven via GSC:
- Faceted URL earning impressions/clicks the clean URL also earns (split signals on same queries — cite both rows), OR
- Site-wide crawl shows MANY parameterized collection URLs indexable (template-level defect → one fix, cite the count), OR
- Parameters are tracking/commerce junk (`?gclid`, `?utm_`, `?session`) → cheap noindex/canonical fix, impact LOW.

Reject when:
- The parameter carries genuine search demand and is intentionally indexable (check whether the faceted URL's queries intersect the clean URL's; if they don't, both may legitimately coexist).
- The URL is already canonicalized to the clean version (candidate contradicts its own acceptance criterion).

### 1.3 Automated 301 Redirect Policy

#### 1.3.1 Who qualifies for an automated redirect

Redirect execution is restricted to **two exact cases** (`plan/21 §2.3-2.5`):

| Case | Qualification criteria | Source |
|---|---|---|
| **404-with-residual-traffic → 301 map** | `status_code = 404` (or discontinued) AND the product is discontinued AND Google still sends traffic (`gsc_clicks_28d` > 0) AND delisted from nav/sitemap | `plan/21 §2.3` row 4; `plan/16:144-146` |
| **Cannibalization consolidation** | Generator 4 candidate that passed all Skill-5 checks (§2.4 of this doc) — 301 weaker → stronger | `plan/21 §2.5` |

Destination selection for the 404 case: *"nearest equivalent (same product_type collection, else `catalogue_coverage.existing_collection_url`)"* (`plan/21 §2.3`).

Everything else touching redirects is **human-review** (not auto-executable):
- Redirect payloads generated for consolidation carry `risk_tier='high'`, weekly cap **2/site**, `requires_field_verify=true` (`plan/22:102`).
- Broken-link repair, orphan linking plans, canonical changes, theme edits → `plan_only` (cap 0) or operator-driven work items.

#### 1.3.2 Cannibalization redirect-source assignment rules

From `plan/17-skill-consolidate-cannibalization.md` (Skill 5) — all checks must pass before the redirect is confirmed:

| Check | Rule | Verdict |
|---|---|---|
| Sub-intent distinctness | Titles/H1s target recognizably different sub-intents (e.g. listicle vs collection) AND live SERP shows different result types | REJECT — "Pages serve distinct sub-intents" |
| Alternation reality | Leader must SWAP across weeks; stable separated positions (A always ~4, B always ~9, low stddev) | Downgrade or REJECT — consolidation would sacrifice the #2 listing |
| Click split | One URL takes nearly all clicks despite balanced impressions (e.g. 60/40 impressions but 95/5 clicks) | Google already decided → optional tidy-up; downgrade LOW or reject when combined clicks < 10 over 28d |
| Query-level materiality | Only 1 low-volume long-tail query shows competition | REJECT unless `combined_impressions >= ~500` or the query is commercially core |
| Weaker page's other-cluster primacy | Weaker page is PRIMARY earner for other clusters | **DO NOT redirect** — differentiation (improve_page), not consolidation |
| Asset asymmetry | Weaker page holds the cluster's only product coverage/buying guide/in-stock products | Merge content into survivor FIRST; add migration work item |
| Survivor sanity | "Stronger" survivor has non-200 / non-indexable / canonical-elsewhere defect | REJECT consolidate; route as technical_fix on the survivor |

#### 1.3.3 Redirect execution sequencing & safeguards

- **Migration before redirect:** content/product migration from `proposed_url` into `target_url` is work item 1; the redirect is work item 2 (`plan/17` Step 5).
- **Unpublish only AFTER redirect verified live** (`plan/21 §2.5`).
- **Interim canonical holding pattern:** when immediate redirect is risky (seasonal page, pending content merge), canonical weaker → survivor first, redirect after migration; the interim plan must be stated explicitly (`plan/17` Step 5 item 4).
- **Sitemap + internal links:** source removed from all sitemaps; all internal links repointed; acceptance = "Zero internal links to source in next crawl."
- **Rollback:** `urlRedirectDelete(id)` → `deletedUrlRedirectId` (`plan/21 §2.5`).
- **Weekly throttle:** redirect sub_type capped at **2/week/site** (`fix_policy` seed, `plan/22:102`).
- **Never auto-revert on measurement verdicts:** a `lost` verdict never triggers rollback — operators decide from verdicts (`plan/21 §0` pillar; `plan/17-lost-result-sop.md` is an open draft confirming "no automatic next step is triggered").

---

## 2. On-Page & Opportunity Detection Rules

### 2.1 Threshold Resolution Model (applies to every generator)

All numeric thresholds resolve per site with a two-level override chain (`plan/00-schema.sql:20-35`, `plan/02-generator-existing-opportunities.sql:20-34`):

```sql
COALESCE(sc.<column>, tt.<column>)   -- per-site override, else tier default
```

Tier defaults (`plan/00-schema.sql:100-110`):

| Tier | Catalogue size | min_search_volume | min_in_stock_products | page_match_threshold | min_impressions_7d | ctr_drop_threshold | position_drop_threshold | position_range | catalogue_match_min |
|---|---|---|---|---|---|---|---|---|---|
| `small` | <500 URLs | 100 | 6 | 0.60 | 30 | 0.25 | 3 | 4–20 | 0.45 |
| `medium` | 500–5,000 | 100 | 8 | 0.65 | 50 | 0.25 | 3 | 4–20 | 0.50 |
| `large` | >5,000 | 150 | 10 | 0.70 | 80 | 0.20 | 3 | 4–20 | 0.55 |

Rationale: *"Small: catalogue is thin, relax the match bar so real gaps surface. Large: catalogue is deep, raise the bar so only genuine gaps pass."* (`plan/00:105-110`)

### 2.2 Striking Distance / Existing-Page Opportunities (SQL Gen 02 — `plan/02-generator-existing-opportunities.sql`)

#### 2.2.1 Part A — Position band + intent match (`position_opportunities`)

| Rule | SQL / constant | Citation |
|---|---|---|
| Window | `sp.date >= (:reference_date::date - INTERVAL '28 days')` | plan/02:70 |
| Position band | `AVG(sp.position) BETWEEN st.position_range_min AND st.position_range_max` → tier default **4–20** | plan/02:78 |
| Impression floor | `HAVING SUM(sp.impressions) >= st.min_impressions_7d` (30/50/80 by tier) | plan/02:77 |
| Catalogue match | `pqms.catalogue_match_score >= st.catalogue_match_min` (0.45/0.50/0.55) | plan/02:98 |
| Brand exclusion | `NOT EXISTS (SELECT 1 FROM unnest(st.brand_queries) bq WHERE sp.query ILIKE '%' || bq || '%')` | plan/02:71-74 |
| Signal type | `position_4_20` | plan/02:246 |

#### 2.2.2 Part B — Low CTR at positions 1–5 (`low_ctr_opportunities`)

| Rule | SQL / constant | Citation |
|---|---|---|
| Position band | `sp.position BETWEEN 1 AND 5` — "top-5 positions (documented rule)" | plan/02:133 |
| Sample floor | `HAVING SUM(sp.impressions) >= st.min_impressions_7d * 3` — "meaningful sample" (3× the tier base: 90/150/240) | plan/02:139 |
| CTR benchmark (median) | `PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sp2.ctr)` per `ROUND(position)` bucket over 28d (`position_medians` CTE) | plan/02:107-115 |
| CTR benchmark (30th pct) | `avg_ctr < PERCENTILE_CONT(0.3) … WHERE sp2.position BETWEEN 1 AND 5` (site-level, 28d) | plan/02:151-158 |
| Derived metric | `ctr_vs_position_median = lc.avg_ctr - pm.median_ctr` | plan/02:146 |
| Signal type | `low_ctr` | plan/02:143 |

Both benchmarks are computed per site over the same 28-day window — the page must beat the **site's** 30th-percentile CTR in the top-5 band, not a global constant.

#### 2.2.3 Part C — Click decline (`click_decline`)

Formula (`plan/02:164-202`):

```
clicks_recent      = SUM(clicks) for date >= ref - 7 days
avg_clicks_prior   = SUM(clicks) for 35d..8d window / 4.0   (weekly average of prior 4 weeks)
decline_rate       = 1.0 - (clicks_recent / avg_clicks_prior)   [prior > 0]
TRIGGER            = prior > 0 AND decline_rate > st.ctr_drop_threshold   (0.25 / 0.20 by tier)
```

Window: `sp.date >= (:reference_date::date - INTERVAL '35 days')` — a **7-day recent window vs a 28-day prior baseline**. Brand exclusion applies.

#### 2.2.4 Part D — Ranking decline (`rank_decline`)

```
position_recent = AVG(position) for ref - 7 days
position_prior  = AVG(position) for 35d..8d
TRIGGER         = (position_prior - position_recent) >= st.position_drop_threshold   (3 positions, all tiers)
```
Both windows must be non-NULL (`plan/02:232-237`).

#### 2.2.5 Combination, dedupe, quota

| Rule | Value |
|---|---|
| Dedupe key | `GROUP BY page_url, cluster_id, primary_keyword` → one recommendation per page+cluster; signals merged via `array_agg(DISTINCT signal_type)` | plan/02:277-289 |
| Active-rec exclusion | `NOT EXISTS (… r.target_url = page_url AND r.cluster_id = … AND r.status IN ('raw','proposed','approved','in_progress'))` | plan/02:305-310 |
| Priority score | `(COALESCE(array_length(signals,1),1) * COALESCE(total_impressions,0))` — more signals × higher impressions = higher priority | plan/02:303 |
| Per-site quota | `LIMIT 20` (parsed from the SQL itself as the single source of truth — `src/generators/orchestrator.py:59-69`) | plan/02:312 |
| Inserted status | `raw` (agent is the only raw→proposed promoter) | orchestrator.py:9-16 |

#### 2.2.6 Branded query exclusion (global rule)

`site_config.brand_queries TEXT[] DEFAULT '{}'` (`plan/00:54`). Every generator applies the same NOT EXISTS ILIKE pattern; Skill 1 adds: *"IF primary keyword contains brand name → SKIP (brand exclusion)"* (`plan/06:37-40`). The agent config block also carries `excluded_patterns = list(brand_queries or [])` (`src/agents/seo_agent.py:297`).

#### 2.2.7 Cannibalization de-duplication between opportunities

- Generator 2 dedupes per (page, cluster) — one rec per pair regardless of how many signals fired.
- The orchestrator adds a second dedupe pass keyed on `(generator, cluster_id, target_url, proposed_url)` and caps the combined set at **60 per site** (`MAX_PER_SITE = 60`, `src/generators/orchestrator.py:34, 262-272`).
- A DB-level natural-key unique index makes re-generation idempotent: `uq_recommendations_candidate` on `(site_id, generator, action_type, COALESCE(cluster_id, NIL), COALESCE(target_url,''), COALESCE(proposed_url,''))` (`plan/16-raw-candidate-status.sql:78-83`). Documented rule: *"a row the agent already promoted/rejected keeps its status — re-generation never knocks enriched work back to 'raw' and never resurrects a rejected candidate."* (`plan/16:16-17`)

### 2.3 Missing Commercial Pages (SQL Gen 01 — `plan/01-generator-missing-pages.sql`)

| Step | Rule | Constant |
|---|---|---|
| 1. Qualified clusters | `commercial_intent = true AND search_volume >= st.min_search_volume` AND brand exclusion | 100/100/150 by tier |
| 2. Catalogue coverage | `cc.in_stock_product_count >= st.min_in_stock_products` | 6/8/10 by tier |
| 3. Unmatched | no `page_query_match_scores.aggregate_score >= st.page_match_threshold` row exists | 0.60/0.65/0.70 by tier |
| 4. Competitor signals | `openseo_serp_snapshots` only (never GSC — *"GSC can never return rows for properties you don't own"*, plan/01:13-15); `snapshot_date >= ref - 28 days`, `is_self = false`, `position <= 10`, `cluster_id IS NOT NULL` | plan/01:109-122 |
| 5. URL proposal | `'/collections/' \|\| regexp_replace(lower(trim(primary_keyword)), '[^a-z0-9]+', '-', 'g')` | plan/01:131-134 |
| Quota | `LIMIT 15` per site, ordered by `search_volume DESC, commercial_value DESC` | plan/01:176-177 |

**In-stock definition** (`plan/00:32-36`): `any_variant` (default; ≥1 variant has inventory) vs `majority_variants` (≥50% of variants have inventory). Applied in the `catalogue_coverage` job — *"never guessed"* (`plan/07:97-99`).

**Impact gate** (orchestrator, `src/generators/orchestrator.py:40-56, 103`):
- `search_volume >= site_config.min_search_volume` → `impact='high'`, else `medium`.
- When the column is NULL: fallback constant `MISSING_PAGE_HIGH_IMPACT_VOLUME_DEFAULT = 5000` (founder decision, Option A — `plan/15-impact-volume-threshold.sql`).
- Dual-use semantics documented: a per-site value affects BOTH Generator-1 qualification AND the impact gate, by design.

### 2.4 Cannibalization & Consolidation Engine (SQL Gen 04 + Skill 17)

#### 2.4.1 Competing-URL criteria (`plan/04-generator-cannibalization.sql`)

| Step | Rule | Constant |
|---|---|---|
| 1. Query×URL pairs | 28-day window; `HAVING SUM(sp.impressions) >= 100` ("minimum signal") | plan/04:46 |
| 2. Own domain | `page_url LIKE '%' \|\| st.domain \|\| '%'` | plan/04:57 |
| 3. Competition | `COUNT(DISTINCT page_url) >= 2` per (cluster, query) | plan/04:73 |
| 4. No clear dominator | top-2 by impressions: `(top_two->0->>'impressions')::NUMERIC < (top_two->1->>'impressions')::NUMERIC * 3` — the **3× clear-dominator rule** | plan/04:116 |
| 5. Alternation windows | 4 weekly buckets: `pos_w1` (0–7d), `pos_w2` (8–14d), `pos_w3` (15–21d), `pos_w4` (22–28d), plus `STDDEV(sp.position)` and `COUNT(DISTINCT ROUND(sp.position))` | plan/04:30-39 |
| 6. Type similarity | `jsonb_array_length(competing_page_types) <= 2` — only when both pages are similar type | plan/04:227 |
| 7. Pattern label | 1 distinct type → `same_type`; 2 → `mixed_types`; >2 → `multiple_types`; **both actionable patterns → `'consolidate'`** | plan/04:186-195 |

Winner/loser assignment: `stronger_url` = most impressions for the cluster; `weaker_url` = fewest impressions (`ORDER BY … total_impressions DESC/ASC LIMIT 1`, plan/04:199-223). Output semantics: `target_url = survivor (keep)`, `proposed_url = redirect source`, `LIMIT 10`, ordered by `combined_impressions DESC` (plan/04:231-280).

#### 2.4.2 Skill-5 validation overlay (`plan/17-skill-consolidate-cannibalization.md`)

The SQL proves the *mechanics*; the agent confirms the *conflict is real*:

**Impact / confidence / effort calibration** (`plan/17:152-183`):

| Tier | Condition |
|---|---|
| Impact HIGH | `combined_impressions >= 1000` AND weekly alternation is real AND `cluster.commercial_value = 'high'` |
| Impact MEDIUM | conflict confirmed, `combined_impressions` 100–1000, or clicks concentrated on one URL already |
| Impact LOW | `combined_clicks < 10` over 28d, or conflict resolved in practice — prefer REJECT |
| Confidence HIGH | alternation in `weekly_positions` AND SERP confirms one dominant result type AND weaker page has no other-cluster primacy |
| Confidence LOW | single-query evidence, samples barely above the 100 floor, or `mixed_types` without a SERP check |
| Effort | **always `days`** — "never accept the generator default of hours for consolidation work" |

**Pattern handling** (`plan/17:123-147`):
- `same_type` (both collections or both products): cleanest case → consolidate. Collections: confirm survivor's product coverage can absorb the weaker page's products. Products: prefer merging into the better-converting page; two DIFFERENT products ranking for one query usually need canonical/differentiation, not deletion.
- `mixed_types` (collection vs product/blog): frequently SERP fluidity, not a site defect → verify live SERP; if the intent supports both types appearing → REJECT or downgrade; prefer differentiation (improve_page).
- `multiple_types`: generator only emits ≤2, so this label is not expected; if seen, treat as mixed_types with extra caution.

#### 2.4.3 Intent classification for stronger vs weaker

The generator's intent work is type-based (`pages.page_type`), but the *stronger/weaker* determination used by the fix layer follows `plan/21 §2.5`'s sharpened winner logic (beyond impressions-only):

> Winner = cluster impressions + full-cluster GSC footprint of the OTHER cluster each page serves (plan/17's "weaker across ALL clusters" check) + `pages.indexable` + `internal_links_in` + `catalogue_coverage.matching_product_count`. Weaker page primary-earner elsewhere → differentiation, not consolidation. (`plan/21-stage2-fix-execution.md:259-263`)

Rule of record: *"redirect the weaker page only when it is weaker overall, not merely weaker for this single query."* (`plan/17:118-119`)

### 2.5 Pre-Agent Validation Rules (`plan/05-agent-execution-structure.md:240-252`)

Applied automatically before the agent sees any candidate (thresholds tier-resolved):

1. **Deduplication** — active recommendation for the cluster → skip.
2. **Brand exclusion** — brand-pattern match → skip.
3. **Minimum signal** — candidate must have **≥100 impressions OR ≥5 organic sessions**.
4. **Threshold check** — generator-specific tier-resolved thresholds must be met.
5. **Recent exclusion** — same page recommended in the last **90 days** → flag for manual review (`recommendation_cooldown_days: 90`, `plan/09:307`).
6. **Pre-ranking** — rank by `priority_score/search_volume`; keep top ~25 (`site_config.agent_prefetch_limit = 25`) for the fetch stage; backup pool of 20 (`agent_backup_pool`); refill from backup only if fewer than 5 (`agent_min_valid`) valid recommendations emerge.

### 2.6 Measurement Classification (verdict rules — applies to every action)

Windows per action type (`measurement_window_lookup`, `plan/00:127-131`) — *"no fixed 28-day constant"*:

| action_type | Window (days) | Metric (plan/16:87-90) | Rationale |
|---|---|---|---|
| `create_page` | 49 | `impressions` | "New content needs discovery + crawl time before it can rank" |
| `improve_page` | 28 | `clicks` | "Existing page tweaks surface movement faster" |
| `consolidate` | 28 | `organic_sessions` | "Redirect authority transfer + re-crawl" |
| `technical_fix` | 21 | `impressions` | "Indexability/status fixes surface quickly" |

**GSC settle buffer:** `GSC_SETTLE_DAYS = 4` (`src/measurement/thresholds.py:43`) — a window that closes within the settle buffer is not yet measurable (GSC's final days would be incomplete). Due condition: `implemented_at::date + measurement_window_days + GSC_SETTLE_DAYS <= reference_date` (`src/jobs/measurement_clock.py:45-48`). The settle days shift *when* the sweep fires, never *what* the post window covers (`measurement_clock.py:58-62`).

**Classification gates** (`src/measurement/classify.py:164-329`, constants in `thresholds.py`):

| Gate | Rule | Constant |
|---|---|---|
| 1. Sample sufficiency | Impressions below the bar in BOTH baseline and post → `inconclusive`, never Won/Lost | `MIN_SAMPLE_FOR_SIGNIFICANCE = 50` (per-site override `site_config.min_sample_for_significance`, plan/14) |
| 2. Confound protection | No control group AND no YoY snapshot → `inconclusive` (plan/08 mandates it) | — |
| 3. Significance | Two-proportion z-test on click deltas; not significant → downgrade | `ALPHA = 0.05` |
| WON | clicks +15% OR sessions +15% OR commercial (orders up OR revenue +10%) OR zero-baseline growth — AND significant AND not contradicted by control AND not DID-blocked | `IMPROVE_THRESHOLD = 0.15`, `COMMERCIAL_IMPROVE = 0.10` |
| LOST | clicks −15%+ AND beyond the control group's trend AND significant | `DECLINE_THRESHOLD = 0.15` |
| NEUTRAL | movement within ±5%, failed significance, or contradicted by control | `NEUTRAL_BAND = 0.05` |
| DID block | target lift must beat the control lift by >10 pts when controls moved beyond the neutral band — else seasonality/algorithm noise → neutral, never won | `DID_IMPROVE_THRESHOLD = 0.10` |

Control-group construction (`src/measurement/baseline.py:28-38`): `CONTROL_GROUP_LIMIT = 20`, `CONTROL_URL_COUNT = 2` paired peers, `CONTROL_MIN_IMPRESSIONS = 50` per peer in the baseline period. Peers must have NO active changes (no non-rejected recommendation targeting them, no change_log entries).

---

## 3. Generative & Content Optimization Rules

### 3.1 Title Tag Specifications (`src/fixes/generator.py`)

| Rule | Constant / check | Citation |
|---|---|---|
| Min length | `TITLE_MIN_CHARS = 20` | generator.py:18 |
| Max length | `TITLE_MAX_CHARS = 60` | generator.py:19 |
| Primary keyword required | `primary_keyword.lower() not in stripped.lower()` → reject | generator.py:826-827 |
| Keyword position | Keyword must START within the first half: `half = max(1, len(draft) // 2)`; `start == -1 or start >= half` → reject ("its end may spill over — a long keyword can never fully fit in half of a short title") | generator.py:804-811 |
| Banned patterns | `BANNED_TITLE_PATTERNS = ("|", "»", "«", "★", "→", "free shipping", "best price")` | generator.py:20 |
| ALL-CAPS shouting | `_caps_runs(draft) >= 3` → "ALL-CAPS run (3+ shouted words)" (words = `[A-Za-z']+`, length ≥ 3, `isupper()`) | generator.py:772-775, 798-799 |
| Emoji | Any codepoint in `(0x1F000,0x1FAFF)`, `(0x2600,0x27BF)`, `(0x1F1E6,0x1F1FF)` → "emoji in title" | generator.py:42-43, 778-784 |
| Spam punctuation | Repeated char runs (`(\W)\1{2,}`) or `[!$]{4,}` → "spam punctuation stack" | generator.py:787-791 |
| Whitespace artifacts | Leading/trailing space or double space → reject | generator.py:832-833 |
| Brand rule | `brand_suffix_check`: brand name at most once per draft; mid-title duplication ("Brand X — Brand Y") fails | generator.py:761-769 |
| Duplicate title (site-wide) | `duplicate_title_check`: case/whitespace-insensitive self-join on `pages.title` — a match → draft rejected | generator.py:740-758 |
| Product-title no-op | Draft equal to `product_title` → reject: *"Shopify stores that as null (no-op write)"* — Phase 1.5 live finding | generator.py:834-840 |
| Intent match | `intent_match_check` — only CONTRADICTION fails (see 3.1.1) | generator.py:714-737 |
| Only-fix-what's-broken | A fix row is generated only when at least one quality check fails (`is_blank` OR `not length_ok` OR `not has_primary_kw`); all-pass → `FixNotSupported` | generator.py:1326-1333 |

#### 3.1.1 Intent-match semantics (deterministic cues)

Cue sets (`generator.py:32-38`):

```python
INTENT_TRANSACTIONAL_CUES = ("buy", "shop", "order", "price", "sale",
                             "discount", "cart", "checkout", "for sale")
INTENT_COMMERCIAL_CUES    = ("best", "top", "review", "reviews", "vs",
                             "compare", "comparison", "cheap", "affordable",
                             "deal", "deals")
INTENT_INFORMATIONAL_CUES = ("guide", "how to", "what is", "learn", "tips",
                             "tutorial", "ideas", "meaning", "examples")
```

Verdict matrix (`intent_match_check`, generator.py:714-737):

| Cluster intent | Title framing | Verdict |
|---|---|---|
| `informational` | contains any transactional cue | **FAIL** (hard buying push on informational intent) |
| `transactional`/`commercial` | purely informational framing with no buy/commercial cue | **FAIL** |
| `transactional`/`commercial` | has txn OR commercial cue, or is not informational-only | pass |
| `unknown` / empty intent | anything | pass ("no grounded verdict possible") |
| neutral title | anything | pass — "a neutral title is never a mismatch" |

### 3.2 Meta Description Specifications (`src/fixes/generator.py`)

| Rule | Constant / check | Citation |
|---|---|---|
| Min length | `META_MIN_CHARS = 70` — "floor avoids one-liner snippets" | generator.py:895-896 |
| Max length | `META_MAX_CHARS = 155` — "Google truncates ~155-160 chars" | generator.py:895-896 |
| Primary keyword required | same rule as title | generator.py:1047-1048 |
| Banned patterns | `BANNED_META_PATTERNS = ("free shipping", "best price", "click here", "buy now", "limited time")` | generator.py:898-899 |
| Double quotes | `'"' in draft` → reject — "Quotes break Google's snippet rendering" | generator.py:953-954 |
| Emoji / caps runs / spam stacks | same detectors as title, applied via `_meta_denylint` | generator.py:941-955 |
| Brand rule | brand ≤ 1 occurrence | generator.py:1056-1057 |
| Duplicate meta (site-wide) | `duplicate_meta_check` — self-join on `pages.meta_description`, normalized | generator.py:921-938 |
| Quality gate | `meta_quality_checks`: fix only when `is_missing OR not length_ok OR not has_primary_kw` | generator.py:902-918 |
| Truncation marker | Over-length deterministic drafts are truncated to `MAX-1` chars + `…` (both title and meta drafters); below-minimum → `None` (no draft) | generator.py:881-884, 1029-1032 |

### 3.3 Competitor Grounding & SERP Snapshot Freshness

#### 3.3.1 Batch-pipeline competitor context (`load_competitor_context`, generator.py:358-410)

| Rule | Constant |
|---|---|
| Competitor positions | `COMPETITOR_POSITION_MAX = 10` — "top-1..10 organic results feed the patterns" |
| Row cap | `COMPETITOR_LIMIT = 10` — "cap rows read per cluster (payload bound)" |
| Freshness window | `snapshot_date >= %s::date - INTERVAL '35 days'` (35-day TTL) |
| Self-exclusion | `is_self = false` |
| URL dedupe | one row per competitor URL — "latest snapshot wins" (`seen_urls` guard) |
| Degrade behavior | Empty dict when no rows → "grounding degrades gracefully to the prior keyword-only behavior — never blocks a fix" |

#### 3.3.2 SERP freshness TTLs (per surface)

| Surface | TTL | Source |
|---|---|---|
| Batch SERP snapshots (drafting lookback) | 35 days | generator.py:383 |
| Daily-sync SERP refetch gate | `ttl_serp_days` default **7** | `plan/18-serp-freshness-ttl.sql:6`; `src/connectors/sync.py:793, 813-824` |
| Daily-sync keyword-volume refetch gate | `ttl_keyword_volume_days` default **30** | `plan/18:7`; `sync.py:792, 795` |
| On-demand audit SERP cache | `CACHE_TTL_HOURS = 24` (identical `(url_hash, inferred_query)` + successful SERP) | `src/audit/engine.py:45, 172-198` |
| Audit session retention | `expires_at = now() + INTERVAL '7 days'`; daily TTL sweep prunes sessions + cascaded children | `plan/23:221-222`; `src/audit/ttl_sweep.py:21-33` |

#### 3.3.3 On-demand live SERP pull (`src/audit/engine.py`)

| Rule | Constant |
|---|---|
| Depth clamp | `DEPTH_MIN = 5`, `DEPTH_MAX = 20`, `DEPTH_DEFAULT = 10` ("cost scales with depth; default 10 = COMPETITOR_POSITION_MAX parity", plan/23 §5.2) |
| Budget gate | `check_budget(service="openseo_serp")` **BEFORE** the fetch — exhausted → `serp_status='budget_exhausted'`, HTTP 402, zero network call |
| Cost log | `log_cost(call_type="serp_live_audit")` AFTER the call with `{"source": "audit_engine", "session_id": ...}` so on-demand spend is distinguishable from weekly-batch spend |
| Failure semantics | provider error → `serp_error`; zero organic rows → `zero_results` (grounding degrades to keyword-only); page analysis unaffected in all three cases |

### 3.4 Anti-Hallucination: `verify_grounded_claims` (`src/fixes/generator.py:176-267`)

Every LLM-drafted candidate is fact-checked against the page's OWN verified facts before validation. Purely deterministic, never touches the network.

**Fact base:** `_fact_base(rec)` = page title + the page's own body text (`body_text`/`body_text_excerpt`), lowercased, whitespace-normalized. *"The ONLY text a candidate's claims may draw from."*

**Claim-cue vocabulary** (`CLAIM_CUES`, generator.py:182-196) — each must be substantiated:

| Category | Cues |
|---|---|
| Materials & attributes | `organic, leather, cotton, vegan, recycled, sustainable, waterproof, water-resistant, wireless, bluetooth, noise cancelling, handmade, handcrafted, premium, luxury` |
| Certifications & standards | `certified, fda, gmp, iso, fair trade` |
| Commercial promises | `free shipping, free delivery, free returns, money-back, lifetime warranty, warranty, guarantee, 30-day, 60-day, same-day, next-day, cash on delivery, installments` |
| Fact-asserting superlatives | `award-winning, best-selling, number one, #1, clinically proven, doctor recommended, eco-friendly, non-toxic, bpa free` |

**Numeric-claim rule** (`_CLAIM_NUMBER`, generator.py:200-203): claims like `40-hour`, `45 dB`, `22 pairs`, `50% off` must exist in the fact base **verbatim** (whitespace-normalized), with flexible inflections: `30-day` ↔ `30 days` ↔ `30 day`; `40-hour` ↔ `40 hours` (variants generated in `verify_grounded_claims:248-254` and `_claim_number_inflections:260-267`).

**Verdict:** every cue or number not grounded → the candidate is **REJECTED** with problems like `"ungrounded claim: 'warranty' not in page facts"`. Rejected candidates are recorded in the fix payload under `llm_candidates_rejected` for the audit trail.

### 3.5 LLM Drafting Pipeline & Fallback Triggers (`draft_candidates_with_fallback`, generator.py:274-347)

Pipeline (every stage fails SOFT — an LLM outage can never lose a fix, and a bad LLM draft can never be written):

1. **LLM drafts ≤ 3 candidates per field** (`LLM_CANDIDATE_COUNT = 3`) under a grounded prompt (`DRAFT_SYSTEM_PROMPT`, generator.py:133-150): *"Use ONLY facts present in page.title or page.body_text_excerpt. Never claim specs, materials, certifications, shipping, warranties, or numbers that are not in the page facts. Borrow competitor FRAMING … but never copy competitor wording."*
2. **Grounding prompt constraints** (`build_draft_prompt`, generator.py:111-123): char bounds, keyword-first-half, meta keyword required, no banned patterns, no emoji/ALL-CAPS/spam punctuation, no double quotes in metas, `brand_name_at_most_once`, `invent_no_facts`.
3. **Competitor data is labeled a style reference ONLY:** `"Structural/framing reference ONLY. Never copy wording or claim any fact that is not in page facts."` (generator.py:108-109)
4. **`verify_grounded_claims`** rejects hallucinated candidates (§3.4).
5. **Survivors run the deterministic validators** (`validate_title_draft` / `validate_meta_draft`).
6. **First fully-passing candidate wins per field**; provenance recorded as `title_source`/`meta_source` ∈ `llm|deterministic` and persisted as `generation_source ∈ {'agent','deterministic'}` (DB CHECK constraint, `plan/22:41-42`).

**Fallback triggers (LLM output discarded in favor of deterministic drafting):**

| Trigger | Behavior |
|---|---|
| No credentials / client construction failure (`LlmDraftError`) | Deterministic baseline drafter, no error surfaced |
| Network/API/schema failure (`answer` not a JSON object, titles/metas not lists) | Same |
| All LLM candidates fail fact-check or validator | Deterministic baseline drafter |
| Deterministic draft also fails validation | **NO fix row** — `FixNotSupported` ("a failing draft means NO fix row") |

The deterministic drafters are themselves SERP-grounded but fact-owned:
- `draft_title` (generator.py:853-885): mirrors the DOMINANT competitor framing's *shape* (`_competitor_prefix`: highest-frequency framing token across top-3 competitor titles; ties → lower position wins → alphabetical). `"Best "` / `year:2026` / `"Reviewed: "` / `"Top N "` leads. **"Only the STRUCTURE is borrowed — every word stays from the page's own title and the cluster's keyword."** Guide/how-to framing is deliberately NOT used as a title lead ("meta/snippet territory").
- `draft_meta_description` (generator.py:958-1033): FACTS strictly page-owned (title + own body copy + page type); the SERP grounds only the ORDERING — page sentences matching dominant competitor hook cues (`_competitor_snippet_leads`, frequency-ordered) surface first.

### 3.6 SERP Competitor Pattern Extraction

Framing tokens (`_title_framing`, generator.py:413-443) — deterministic, ordered by specificity:

| Token | Regex / cue |
|---|---|
| `year:YYYY` | `\b(20\d{2})\b` |
| `count:N` | `\b(\d{1,3})\s+(best\|top\|things\|tips\|ways\|pairs\|reasons)\b` |
| `guide`, `vs`, `compare`, `review`, `best`, `how-to`, `worth-it` | substring hooks |
| `split-separator` | `\|`, `—`, or ` - ` in title (punctuation — excluded from prefix scoring) |

Snippet value-prop hooks (`_snippet_hooks`, generator.py:446-454):

```python
_SNIPPET_VALUE_CUES = ("free", "tested", "compare", "returns", "warranty",
                       "battery", "hours", "dB", "waterproof", "ship",
                       "guide", "checklist", "verified")
```

### 3.7 Content Outline & Gap Analysis

#### 3.7.1 Heading extraction standards (`src/audit/page_fetch.py`)

| Rule | Value |
|---|---|
| Levels captured | H1–H3 only (`tag in ("h1", "h2", "h3")`) |
| Ordering | Document order preserved; text whitespace-collapsed |
| Fabrication | *"Parser never fabricates: a field absent from the HTML stays None / empty; heading_outline holds only H1–H3 actually present"* (module docstring) |
| Dropped content | `script, style, noscript, svg, template` (`SKIP_CONTENT`) |
| `h1` derivation | First `h1` entry of `heading_outline` |
| Fallback | Regex extractor (`_HEADING_RE`) for parser-hostile HTML |
| Persistence | `pages.heading_outline JSONB` (`plan/22:117`) — *"only if crawl exposes; never fabricated"* (`plan/21 §1.4`) |

#### 3.7.2 Missing-section scoring (`content_outline_gaps`, generator.py:508-580)

Section archetypes (`_SECTION_BY_HOOK`) map competitor hook cues to expected sections:

| Hook cue | Expected section |
|---|---|
| `review`, `tested` | Test results / hands-on findings |
| `guide`, `how-to` | Step-by-step walkthrough |
| `vs`, `compare` | Head-to-head comparison table |
| `best` | Top-picks shortlist |
| `worth-it` | Buying-advice / verdict section |
| `free`, `returns` | Shipping & returns info |
| `warranty` | Warranty & support info |
| `battery` | Battery & specs detail |

Priority formula for "expected": a section is expected when its hook appears in **`count >= max(2, n_snippets // 2)`** of the observed competitor snippets — i.e. frequency ≥ 2 AND at least half the competitor sample covers it ("A section is 'expected' when >= half of the observed competitors cover it").

Coverage check (`_SECTION_CUES`, generator.py:529-538): a section counts as COVERED when the page's OWN body text contains any cue word (e.g. `test|tested|hands-on|lab` for test sections). **"Competitor text is never inserted"** — grounded-only invariant.

Outputs: `expected_sections` (ordered), `missing_sections` (the thin-content / missing-angle signal), `covered_sections`. Missing sections feed:
- The fix payload's `grounding.content_outline_missing` (audit trail, generator.py:1174-1177).
- Suggested H2 headings in the on-demand audit — max 3, each labeled `rationale: "competitor-grounded section angle"` (`src/audit/context_adapter.py:189-194`).

### 3.8 Thin-Content & Content-Structure Checks (v2 design — `plan/21 §2.2`)

| Check | Feasible | Data source |
|---|---|---|
| Meta missing/dup/truncated | yes | `pages.meta_description` |
| Semantic coverage vs ranking competitors | PARTIAL — competitor content not ingested | gap unless OnPage competitor crawl added |
| Content structure | partial | `body_html` headings (products/collections); blog/theme content = GAP |
| Near-duplicate | yes | `body_text_hash` |
| Keyword stuffing | yes | token stats (deterministic) |
| E-E-A-T | GAP — no data source in system; out of scope | — |
| Thin content | yes but **NOT word-count alone**: `(low entropy/empty body) AND impressions > 0 AND commercial intent` | `body_*` + `search_performance` |

### 3.9 On-Demand Audit Safety Gates (`src/audit/page_fetch.py`)

| Guard | Constant / rule |
|---|---|
| Method | GET-only, single fetch |
| Timeout | `HTTP_TIMEOUT_SECONDS = 8` |
| Redirect hops | `MAX_REDIRECT_HOPS = 1`; every hop target is SSRF-re-checked |
| Body size cap | `MAX_BODY_BYTES = 3MB` — exceeded → `page_unreachable` |
| SSRF guard | loopback/private/link-local/reserved/multicast IP literals AND hosts that *resolve* into protected ranges are rejected BEFORE any connection (`ssrf_check_host`); `localhost` variants rejected |
| Scheme allowlist | `ALLOWED_SCHEMES = ("http", "https")` |
| robots.txt | honored for the page fetch (SERP pull is provider-side); unreachable robots = allowed |
| Render honesty | `render_status` ALWAYS `'not_rendered'` in v1 — plain GET, no headless browser |
| Content drift | `content_hash` = sha256 of normalized body text — cache key + drift detector; a changed page invalidates the cached SERP (`plan/23 §5.1`) |
| Site resolution | `site_config.domain` exact registrable match → `connected`; `shopify_domain` or `*.myshopify.com` host → `connected`; else `audit_checklist` (read-only). Subdomain matching is explicitly NOT supported in v1 (`src/audit/site_resolution.py:21-25`) |
| Brand-agnostic mode | `site_name=None` (no brand-suffix claim), drafts are previews, `unprotected: true` flag ("no GSC data available"), duplicate guards reported `site_wide_unchecked: true` |

---

## 4. Execution Guardrails & Rollback Safeguards

### 4.1 The "Protect Winner" Rule (`plan/21 §2.1 check 6`; `src/fixes/generator.py:22-26, 674-711`)

**Conditions under which a page is completely locked from title/meta modification:**

```
protected = (position_recent <= PROTECT_POSITION_MAX)          # avg position ≤ 2.0 over last 7 days
            AND (imps_recent >= PROTECT_MIN_IMPRESSIONS)       # ≥ 100 impressions in the 7-day window
            AND (ctr_recent > ctr_prior)                       # 7-day avg CTR strictly above prior-7-day avg
```

| Constant | Value | Meaning |
|---|---|---|
| `PROTECT_POSITION_MAX` | `2.0` | Position ≤ 2 |
| `PROTECT_MIN_IMPRESSIONS` | `100` | *"Protection only engages when the GSC sample is meaningful, so sparse data can never fabricate a 'winner'"* |

Window mechanics: recent = last 7 days; prior = days 8–14 (`BETWEEN ref - 14 days AND ref - 8 days`), computed on `search_performance` for the exact (site, page_url). No GSC rows for the target → **not** a winner (nothing to protect); missing prior window → not protected.

Enforcement: both the title hook (`generate_fix_for_recommendation`, generator.py:1287-1290) and the meta hook (`generate_meta_fix_for_recommendation`, generator.py:1077-1080) raise `FixNotSupported("protect-winner rule: position ≤2 with rising CTR — … (never rewrite what works)")`.

**Caveat carried into the on-demand audit** (`plan/23 §3.3`): the brand-agnostic path has no GSC rows, so `protect_winner_check` cannot run ad-hoc; drafts carry `unprotected: true` and the operator sees *"treat this page's current winners status as unknown."* Connected mode runs the full guard.

### 4.2 Consolidate Suppression (`plan/21 §2.1 check 7`)

An active `consolidate` recommendation on the same cluster (`status IN ('raw','proposed','approved','in_progress')`) suppresses both title and meta fixes for that cluster's pages — *"the target page may soon be redirected away; a title fix would be wasted/conflicting"* (generator.py:1292-1313, 1081-1099).

### 4.3 Two-Gate Approval Model (non-negotiable)

> "NOTHING publishes without human approval — ever, for any type." (`plan/21 §0`)

```
recommendation: proposed ─approve─► approved ──────────► in_progress ─► live ─► measured
                                  │                  (TRANSITIONS unchanged)
generated_fixes:            (fix generated on approval)
                            generated ─operator diff-approves─► queued ─executor─► applied
                                 │                                  │               │
                                 ├─rejected──► expired (reason→rejection_log)        ├─► failed (retry = new row)
                                 │                                                  └─► reverted (new row, rollback_of)
```
(`plan/21 §3`)

- **Gate 1** = recommendation approval (operator/API). Generation happens *at* approval.
- **Gate 2** = the operator reviews the **DIFF** (`POST /fixes/{id}/approve` — approve requires status `generated`; concurrent transition → 409).
- `trust_mode` ships **OFF**; autonomous execution is not enabled anywhere in the codebase.
- Generation is allowed while execution is disabled (`weekly_cap = 0` semantics: "generation allowed, execution disabled" — `plan/22:84`); policy is NOT enforced at generation time by design.
- `queued → applied` ONLY by `jobs/fix_executor.py` — *"never inline in an API request (Vercel 60s window can't own a write transaction)"* (`plan/21 §3`). The `POST /fixes/{id}/execute` endpoint is a dev/manual trigger running the SAME job path under the same advisory lock (`src/api/routes/fixes.py:394-430`).
- Diff rejection: fix → `expired`, reason → `rejection_log` (`rejected_by='operator'`), no `approved_at` stamp ("the fix never was approved").

### 4.4 Fix Policy — Risk Tiers, Weekly Caps, Kill-Switch (`fix_policy`)

Schema (`plan/22-stage2-phase0-schema.sql:80-88`): PK `(site_id, sub_type)`; `risk_tier ∈ ('low','medium','high','plan_only')`; `weekly_cap INT NOT NULL DEFAULT 0` (0 = no execution route); `requires_field_verify BOOLEAN DEFAULT true`; `enabled BOOLEAN DEFAULT false` — **master kill-switch, fail-closed**.

Default seed (`plan/22:93-109`, all rows inserted with `enabled = false`):

| sub_type | risk_tier | weekly_cap/site | field verify |
|---|---|---|---|
| `seo.title` | low | 10 | true |
| `seo.description` | medium | 5 | true |
| `content` | medium | 5 | true |
| `product_publish` | medium | 5 | true |
| `article_body_link` | medium | 5 | true |
| `redirect` | high | **2** | true |
| `collection_create` | high | 2 | true |
| `product_create` | high | 2 | true |
| (plan_only types: canonical/theme, structured data, js-rendering, faceted) | plan_only | 0 | n/a — exported as work items |

**Gate mechanics** (`src/fixes/policy.py:52-72`) — consulted at BOTH queue time and executor pickup (a cap can never be bypassed by taking the other path):

| Typed reason | Trigger |
|---|---|
| `no_policy_row` | missing `fix_policy` row → fail-closed |
| `policy_disabled` | `enabled = false` |
| `no_execution_route` | `weekly_cap <= 0` |
| `weekly_cap_reached` | `applied >= weekly_cap` over the 7-day window (`applied_at::date > ref - 7 days`, count of `status='applied'` rows — `policy.py:38-49`) |

**Per-run and per-target caps:**
- `MAX_APPLIES_PER_RUN = 5` (executor per-run ceiling, `src/jobs/fix_executor.py:475`); excess queued fixes → `deferred` (`reason: "run_ceiling"`).
- Strictly **one fix per (site, url) per run** (`duplicate_target_in_run` skip, fix_executor.py:527-531).
- `uq_fixes_active_per_target` partial unique index: one active fix per (site, target_url, field) across statuses `('generated','approved','queued')` (`plan/22:70-72`).
- Conflict guard (`check_conflict`, policy.py:75-93): same-recommendation conflict = idempotent regeneration; a *different* recommendation's active fix → `PolicyBlocked("active_fix_conflict")` → HTTP 409.
- Claim order: highest risk tier first (`ORDER BY CASE risk_tier WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, created_at`).

**3-strike scope disable** (`_record_scope_strike`, fix_executor.py:304-332): an `ACCESS_DENIED` outcome bumps `system_config['fix.scopemiss.<site>.<sub_type>']`; the **third strike flips `fix_policy.enabled = false`** (kill-switch, fail-closed).

**Concurrency:** session-scoped Postgres advisory locks (`jobs/locks.py`) — `fix_executor` global key + per-site key; try-mode (never queue); overlapping runs are impossible per (job, site); connection close releases locks leak-proof. The API execute path shares the same lock ("a request and a scheduled run can never double-execute the same site").

**Rate limiting (Shopify GraphQL cost bucket)** (`src/connectors/shopify.py:283-287, 392-433`):

| Constant | Value | Meaning |
|---|---|---|
| `THROTTLE_DEFAULT_BUDGET` | 1000 | `maximumAvailable` points |
| `THROTTLE_RESTORE_RATE` | 50.0 | points restored per second |
| `DEFAULT_MUTATION_COST` | 10 | typical update mutation cost |
| `THROTTLE_SLEEP_CAP_SECONDS` | 20 | never sleep unbounded inside one call |
| `max_throttle_retries` | 3 | exponential backoff (1s → 2s → 4s, capped at 20s) on `throttled` |

Pre-check `currentlyAvailable`, sleep-until-restore, THROTTLED backoff; pinned API version `2026-01` with served-version drift warning (`X-Shopify-API-Version ≠ pinned` → log warning, shopify.py:520-522).

### 4.5 Executor Write Path — Fresh Snapshot, Stale-Diff, Read-Back Verification

`_execute_one` sequence (`src/jobs/fix_executor.py:105-263`), per plan/21 §5.3:

1. **Policy gate AT PICKUP** (caps can be consumed between queue and run; cheapest check first).
2. **Adapter resolution** — unknown sub_type in a queued row is a data anomaly → `failed` + `no_adapter`, never a crash that poisons the rest of the run.
3. **Fresh-read snapshot** (`snapshot_json`): *"captured from a live read NOW — generation-time old_values are never trusted"* (`plan/21 §5.3`). Dry-run writes nothing anywhere (§5.4) — the fresh read feeds only the in-memory stale-diff guard on dry runs. Snapshot fields captured: `seo.title`, `seo.description`, `product.title`, `status`, `read_at_api_version` (adapters.py:173-183). Snapshot read failure → `failed` (`snapshot_failed`), the fix is never guessed.
4. **Stale-diff guard:** for every diff entry, `live_value != old_value` (both non-NULL) → fix **auto-expires** (`status='expired'`, `verification_status='verify_failed'`) with detail *"fix expired, will regenerate next cycle"*. This is the stale-diff expiry mechanism: value drift between generation and pickup voids the write.
5. **Payload validation BEFORE any network call** (`_payload_for` / `_meta_payload_for`): mutation must match the adapter; required fields present; the seo adapter writes ONLY the `seo` field (`"product title is customer-visible; out of pilot scope"`), the meta adapter writes ONLY `seo.description` (`"no title/status drift through the meta path"`). Invalid → `payload_invalid`, no network touched.
6. **Write** (unless dry-run) → **read-back verify:** trust only a fresh read. `verified = verify.ok AND live_value == new_value` (exact equality). Outcomes: `verification_status ∈ ('unverified','verified','verify_failed')`.
7. **change_log audit row:** `rollback_reference = fix_id` (finally populated), `before_snapshot` = the fresh snapshot, `after_snapshot` = {verified, live_value}. Best-effort write.
8. **Measurement wiring** (on verified applies only): same logic as `POST /implement` — `_transition_to_in_progress` + `store_baseline`; `implemented_at = execution time`; the clock's due condition is unchanged (`plan/21 §3`).

### 4.6 Auto-Revert Protocol

**Trigger rule (singular):** *"Auto-revert on failed read-back verification ONLY. Lost/Neutral verdicts never auto-revert (operator decisions)."* (`plan/21 §0`, `§5.3`; fix_executor.py:249-261)

| Failure condition | Immediate action |
|---|---|
| `verification_status = 'verify_failed'` after write (read-back mismatch, verify query failed, or live value NULL when a value was written) | `revert_fix(conn, fix_row, config, reason="verify_failed")` — restore from the fresh-read snapshot |
| Snapshot missing the touched field at restore time | refuse to guess: `{"ok": false, "outcome": "no_snapshot", "detail": "snapshot_json missing seo.title — refusing to guess a restore value"}` (adapters.py:137-138) |
| Revert write itself fails | fix row gets `error_detail = "revert failed: …"`, stays auditable; no silent overwrite |

**Revert bookkeeping** (`revert_fix`, fix_executor.py:380-442):
- Restores ONLY the fields the fix touched (e.g. `seo.title`), from the fresh-read snapshot stored at pickup.
- The original row → `status='reverted'`, `reverted_at=now()`, `verification_status='verified'`.
- A **new audited row** is inserted with `rollback_of = original fix_id`, `generation_source='deterministic'`, `status='applied'` — *"reverts are audited rows, never a silent overwrite"*; its snapshot is cleared ("the restore source must never be reused").
- Operator reverts (`POST /fixes/{id}/revert`) require `status='applied'` + a stored snapshot + the executor advisory lock (`reason="operator"`).

### 4.7 Failure Classification & Retry Semantics

| Outcome | Fix status | Follow-up |
|---|---|---|
| write ok + verify ok | `applied` | measurement wiring |
| write ok + verify failed | (transient) | **auto-revert** → status ends `reverted` |
| `userErrors` (field-level) | `failed` | typed detail persisted; retry = a NEW row |
| `access_denied` | `failed` | scope strike (+1); 3 strikes → sub_type disabled |
| `throttled` after retries | `failed` | backoff already applied inside the adapter |
| http/network error | `failed` | never-raise adapter contract; typed outcome |
| stale diff at pickup | `expired` | regenerate next cycle |
| operator reject | `expired` | reason → `rejection_log` |

`adapter_response` persisted verbatim, stripped to persistable keys (`ok, outcome, userErrors, errors, http_status, retry_after, api_version, adapter`, detail ≤ 500 chars) — **never credentials** (adapters.py:186-198).

### 4.8 Production Resilience Gates (`plan/21 §5.4`)

- Reads: skip-and-log on malformed/missing (never-raise).
- Real pagination with `max_pages` cap + truncation warning; metafield reads batched (no n+1).
- **Decision minimums:** title/fix decisions require meaningful GSC samples — *"e.g. ≥500 impressions/28d/query so sparse real-GSC data never triggers rewrites"* (documented minimum in the plan; the protect-winner floor of 100 impressions/7d is the enforced floor in code).
- **Pre-production battery required before any `enabled=true`:** read-verify ingestion on ≥100 real products; dry-run mode end-to-end; scope probe + harmless write-auth probe; single real apply → read-back → revert cycle on one low-traffic product; concurrency drill (two queued fixes same URL+field → expiry behavior).
- Startup diagnostics: scope introspection (granted vs required, with any-of group modeling for `write_content | write_online_store_pages`) + version-drift warning; unknown sub_type reported `unknown_sub_type` loudly, never silently passed.

### 4.9 Cost Governance (applies to every paid call)

| Control | Value | Source |
|---|---|---|
| Global weekly cap | `budget_config('*', weekly_cap=100.00, warn_threshold=0.80)` | `plan/19-cost-tracking.sql:27` |
| Warn | alert at 80% of cap | `costlog.check_budget` |
| Hard stop | `spend >= cap` → paid calls refuse (audit SERP → HTTP 402, `budget_exhausted`) | `engine.py:389-398` |
| Env overrides | `WEEKLY_BUDGET_OPENSEO`, `WEEKLY_BUDGET_OLLAMA`, `WEEKLY_BUDGET_GLOBAL` | `costlog.py:19-30` |
| Week window | Monday 00:00 UTC of the current ISO week | `costlog._week_start` |
| Observed unit costs | keyword volume $0.09/request; SERP organic $0.002/request; on-page crawl task $0.50 | WALKTHROUGH.md:271 |
| Agent budget gate | weekly_agent checks the Ollama budget before the LLM run | scheduler cadence note |

### 4.10 Operational Cadences & Hygiene Jobs

Scheduler registry (`src/jobs/scheduler.py:58-68`; single source of truth):

| Job | Cadence | Time (UTC) |
|---|---|---|
| `daily_sync` | daily | 01:00 |
| `measurements` (measurement_clock sweep) | daily | 02:00 |
| `fix_executor` | daily | 03:00 |
| `stale_approvals` | daily | 05:00 |
| `weekly_candidates` | weekly (Sat) | 04:00 |
| `weekly_agent` | weekly (Sat) | 05:00 |
| `weekly_crawl` | weekly (Sat) | 06:00 |
| `semantic_scoring` | weekly (Sat) | 07:00 |
| `cost_report` | weekly (Sun) | 08:00 |
| audit TTL sweep | daily (scheduler-registered) | prunes sessions past `expires_at` |

- Safety: a failed job does NOT record its last-run — the slot stays due and the next tick retries naturally; per-job advisory locks make retries safe (`scheduler.py:159-199`).
- **Stale-approval sweep:** approved recommendations unimplemented for `STALE_APPROVAL_DAYS = 7` (`src/api/routes/queue.py:37`) are surfaced — detection is read-only, the human decides (`jobs/stale_approvals.py`).
- **Agent output caps:** `MAX_RECOMMENDATIONS = 5` per weekly run (enforced in `validate_agent_output`); raw→proposed promotion is the agent's exclusive power; rejections must carry `candidate_id` + `reason` and feed the next run's few-shot `rejection_context` (last 5 per site).

---

## Appendix A. Master Threshold Table

| # | Constant | Value | Where enforced |
|---|---|---|---|
| 1 | `TITLE_MIN_CHARS` / `TITLE_MAX_CHARS` | 20 / 60 | `fixes/generator.py:18-19` |
| 2 | `META_MIN_CHARS` / `META_MAX_CHARS` | 70 / 155 | `fixes/generator.py:895-896` |
| 3 | `LLM_CANDIDATE_COUNT` | 3 | `fixes/generator.py:71` |
| 4 | `PROTECT_POSITION_MAX` | 2.0 | `fixes/generator.py:25` |
| 5 | `PROTECT_MIN_IMPRESSIONS` | 100 | `fixes/generator.py:26` |
| 6 | `COMPETITOR_POSITION_MAX` / `COMPETITOR_LIMIT` | 10 / 10 | `fixes/generator.py:354-355` |
| 7 | SERP drafting lookback | 35 days | `fixes/generator.py:383` |
| 8 | `ttl_serp_days` / `ttl_keyword_volume_days` | 7 / 30 | `plan/18` |
| 9 | `CACHE_TTL_HOURS` (audit SERP cache) | 24 | `audit/engine.py:45` |
| 10 | Audit session retention | 7 days | `plan/23:221` |
| 11 | Position band (Gen 2 Part A) | 4–20 (tier-resolved) | `plan/00:106-110` |
| 12 | Top-5 band (Gen 2 Part B) | 1–5 | `plan/02:133` |
| 13 | `min_impressions_7d` | 30 / 50 / 80 (S/M/L) | `plan/00:106-110` |
| 14 | Low-CTR sample floor | `min_impressions_7d × 3` | `plan/02:139` |
| 15 | Low-CTR CTR bar | < 30th percentile, top-5, site-level 28d | `plan/02:151-158` |
| 16 | `ctr_drop_threshold` | 0.25 / 0.25 / 0.20 (S/M/L) | `plan/00:106-110` |
| 17 | `position_drop_threshold` | 3 (all tiers) | `plan/00:106-110` |
| 18 | Cannibalization impression floor | 100 per query×URL | `plan/04:46` |
| 19 | Clear-dominator ratio | 3× | `plan/04:116` |
| 20 | Cannibalization HIGH impact | combined_impressions ≥ 1000 | `plan/17:157` |
| 21 | Cannibalization LOW/reject | combined_clicks < 10 (28d) | `plan/17:164` |
| 22 | Query materiality floor (Skill 5) | ~500 combined impressions | `plan/17:80` |
| 23 | Gen 1 volume floor | 100 / 100 / 150 (S/M/L) | `plan/00:106-110` |
| 24 | Gen 1 in-stock floor | 6 / 8 / 10 (S/M/L) | `plan/00:106-110` |
| 25 | `page_match_threshold` | 0.60 / 0.65 / 0.70 (S/M/L) | `plan/00:106-110` |
| 26 | `catalogue_match_min` | 0.45 / 0.50 / 0.55 (S/M/L) | `plan/00:106-110` |
| 27 | Missing-page HIGH impact volume | `min_search_volume` (fallback 5000) | `orchestrator.py:40` |
| 28 | Page-match weights | intent 0.30 / catalogue 0.30 / GSC 0.25 / semantic 0.15 | `plan/00:436-447` |
| 29 | Pre-agent minimum signal | ≥100 impressions OR ≥5 sessions | `plan/05:249` |
| 30 | Recommendation cooldown | 90 days | `plan/09:307` |
| 31 | `agent_prefetch_limit` / `agent_min_valid` / `agent_backup_pool` | 25 / 5 / 20 | `plan/00:39-41` |
| 32 | `MAX_PER_SITE` (candidates) | 60 | `orchestrator.py:34` |
| 33 | Generator quotas | 15 / 20 / 15 / 10 (G1/G2/G3/G4) | SQL LIMITs (single source) |
| 34 | `MAX_RECOMMENDATIONS` (agent) | 5 | `seo_agent.py:36` |
| 35 | `rejection_context_count` | 5 | `plan/00:44` |
| 36 | Measurement windows | 49 / 28 / 28 / 21 days | `measurement_window_lookup` |
| 37 | `GSC_SETTLE_DAYS` | 4 | `measurement/thresholds.py:43` |
| 38 | `ALPHA` | 0.05 | `thresholds.py:9` |
| 39 | `IMPROVE_THRESHOLD` / `DECLINE_THRESHOLD` | 0.15 / 0.15 | `thresholds.py:12,21` |
| 40 | `COMMERCIAL_IMPROVE` | 0.10 | `thresholds.py:15` |
| 41 | `NEUTRAL_BAND` | 0.05 | `thresholds.py:18` |
| 42 | `DID_IMPROVE_THRESHOLD` | 0.10 | `thresholds.py:27` |
| 43 | `MIN_SAMPLE_FOR_SIGNIFICANCE` | 50 (site-overridable) | `thresholds.py:34` |
| 44 | `CONTROL_GROUP_LIMIT` / `CONTROL_URL_COUNT` / `CONTROL_MIN_IMPRESSIONS` | 20 / 2 / 50 | `measurement/baseline.py:28-38` |
| 45 | fix_policy caps | title 10; desc/content/publish/link 5; redirect/creates 2; plan_only 0 | `plan/22:93-109` |
| 46 | `MAX_APPLIES_PER_RUN` | 5 | `fix_executor.py:475` |
| 47 | Scope-miss strike limit | 3 → auto-disable | `fix_executor.py:322` |
| 48 | Shopify throttle | budget 1000 pts; restore 50/s; mutation cost 10; sleep cap 20s; 3 retries | `connectors/shopify.py:283-287` |
| 49 | Shopify API pin | 2026-01 (drift-warned) | `shopify.py:281` |
| 50 | Global weekly budget | $100.00, warn at 80% | `plan/19:27` |
| 51 | `STALE_APPROVAL_DAYS` | 7 | `api/routes/queue.py:37` |
| 52 | Audit fetch timeout / size / hops | 8 s / 3 MB / 1 hop | `audit/page_fetch.py:37-39` |
| 53 | Audit SERP depth clamp | 5..20, default 10 | `audit/engine.py:41-43` |
| 54 | On-demand rate limit (PLAN) | 10 live audits/hour, 3 concurrent | `plan/23:541-543` |
| 55 | Crawl depth / orphan thresholds | deep > 5; orphan = 0 links-in; target ≤ 3 clicks | `plan/03:265` |
| 56 | 404 medium-impact clicks | > 10 clicks/28d | `plan/03:174` |
| 57 | Canonical HIGH impact | > 50 clicks/28d | `plan/03:130` |
| 58 | Sitemap-mismatch HIGH impact | > 100 clicks/28d | `plan/16:223-225` |

## Appendix B. Honest Gaps

The following concepts are commonly assumed in SEO tooling but are **NOT implemented** in this repository. Do not write code or documentation that assumes them:

| Concept | Status | What exists instead |
|---|---|---|
| `crawled_not_indexed` vs `discovered_not_indexed` GSC inspection classes | Not ingested | `sitemap_index_mismatch` proxy: `indexable=false AND status=200 AND clicks>0` (`plan/03:275-307`) |
| Real sitemap membership data | Not ingested (`plan/21 §2.3`: "misnamed") | Publish-state write (`productUpdate(status: ACTIVE)` / `publishablePublish`); Shopify native sitemap auto-regenerates |
| Redirect chain & loop detection | Not built | Structural loop prevention (unique active-fix index, one-fix-per-URL-per-run); redirect rollback via `urlRedirectDelete` |
| Soft-404 as a labeled class | Not labeled | Adjudicated by Skill 4 rules: discontinued+delisted = intentional; in-catalogue-but-404 = real defect; residual clicks → 301 |
| Auto-rollback on measurement `lost` verdicts | Explicitly forbidden | "Lost/Neutral verdicts never auto-revert (operator decisions)" (`plan/21 §0`); SOP for lost results is an open draft (`plan/17-lost-result-sop.md`) |
| Autonomous execution mode (`trust_mode`) | Ships OFF | Two-gate human approval for every executable fix |
| External backlink outreach | Out of scope by non-negotiable #4 | Internal linking only (`articleUpdate` body + collection descriptions) |
| Competitor page-content ingestion | Owner-funded gap | SERP title/snippet/URL-archetype grounding only; competitor text never copied |
| E-E-A-T scoring | GAP — no data source in system | Out of scope (`plan/21 §2.2`) |

---

*End of document. This file is the single reference for SEO rules & policies; when a threshold changes in code, change it here in the same commit.*
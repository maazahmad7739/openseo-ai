# ============================================================
# OpenSEO++ MVP — Architecture Summary
# ============================================================

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    DATA LAYER (Daily Sync)                    │
├──────────────┬──────────────┬──────────────┬────────────────┤
│ Google Search│   Shopify    │ GA4 / Helium │    OpenSEO     │
│    Console   │   Catalogue  │ (Organic)    │  (kw volume,   │
│              │              │              │  SERP, crawl,  │
│              │              │              │  competitors,  │
│              │              │              │  backlinks)    │
└──────┬───────┴──────┬───────┴──────┬───────┴──────┬─────────┘
       │              │              │              │
       ▼              ▼              ▼              ▼
┌─────────────────────────────────────────────────────────────┐
│              UNIFIED DATA MODEL (PostgreSQL)                 │
├─────────────────────────────────────────────────────────────┤
│  pages │ search_performance │ keyword_clusters │            │
│  catalogue_coverage │ page_business_performance │            │
│  page_query_match_scores │ site_config │                    │
│  openseo_serp_snapshots │ threshold_tiers │                │
└──────────────────────────┬──────────────────────────────────┘
                           │
                    Weekly Cron (Sunday 02:00 UTC)
                           │
       ┌───────────────────┼───────────────────┐
       ▼                   ▼                   ▼
┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│ Generator 1 │  │ Generator 2 │  │ Generator 3 │  │ Generator 4 │
│ Missing     │  │ Existing    │  │ Technical   │  │ Cannibal-   │
│ Pages       │  │ Opportunities│ │ Root Causes │  │ ization     │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │                │
       └────────────────┴────────────────┴────────────────┘
                           │
              (per-site execution, tier-resolved
              thresholds — no global LIMIT pool)
                           │
                    Validation Rules
                    (dedup, brand, thresholds)
                           │
                     Pre-ranking (top ~25)
                    + backup pool (SV, priority)
                           │
                            ▼
                     ┌──────────────┐
                     │  SEO Agent   │
                     │  (1 agent)   │
                     │  + 3 skills  │
                     │  + rejection │
                     │  context     │
                     └──────┬───────┘
                            │
                     ≤5 Recommendations
                            │
                            ▼
               ┌────────────────────────┐
               │    ACTION QUEUE UI     │
               │  Screen 1: Queue       │
               │  Screen 2: Detail      │
               │  Screen 3: Results     │
               └────────────┬───────────┘
                            │
                    Operator Workflow
                    (approve → implement → measure)
                            │
                            ▼
               ┌────────────────────────┐
               │  Action-Type-Specific  │
               │  Measurement Window    │
               │  baseline + control    │
               │  group + YoY (opt.)    │
               │  → significance test   │
               │  → classify → learn    │
               └────────────────────────┘
```

---

## File Structure

```
openseo-mvp/
├── plan/
│   ├── 00-schema.sql                        # Full PostgreSQL schema
│   ├── 01-generator-missing-pages.sql       # Generator 1: Missing commercial pages
│   ├── 02-generator-existing-opportunities.sql  # Generator 2: Existing-page opportunities
│   ├── 03-generator-technical-root-causes.sql   # Generator 3: Technical root causes
│   ├── 04-generator-cannibalization.sql      # Generator 4: Cannibalization
│   ├── 05-agent-execution-structure.md       # Agent input/output schema & invocation
│   ├── 06-skill-validate-opportunity.md      # Skill 1: Validate opportunity
│   ├── 07-skill-diagnose-existing-page.md    # Skill 2: Diagnose existing page
│   ├── 08-skill-implementation-spec.md       # Skill 3: Write implementation brief
│   ├── 10-phase5-validation-plan.md          # Phase 5 validation (new)
│   └── 11-openseo-connector-schema.md        # OpenSEO adapter + capability schemas (new)
│
├── src/
│   ├── connectors/                           # Data ingestion scripts
│   │   ├── gsc.py                           # Google Search Console API
│   │   ├── shopify.py                       # Shopify Admin API
│   │   ├── ga4.py                           # GA4 Data API
│   │   ├── openseo.py                       # OpenSEO FACADE (NEW): get_openseo_adapter()
│   │   │                                    #   — resolves the ONE global credential once;
│   │   │                                    #     sole code path that touches the secrets store
│   │   ├── openseo_rest_adapter.py          # OpenSEO REST adapter — supports()/fetch().
│   │   │                                    #   REST is the only access mode; generic contract
│   │   │                                    #   kept ready for a future second mode
│   │   └── sync.py                          # Orchestrator for daily sync
│   │
│   ├── generators/                           # SQL execution wrappers
│   │   ├── missing_pages.py                 # Runs Generator 1 SQL (per site)
│   │   ├── existing_opportunities.py        # Runs Generator 2 SQL (per site)
│   │   ├── technical_fixes.py               # Runs Generator 3 SQL (per site)
│   │   ├── cannibalization.py               # Runs Generator 4 SQL (per site)
│   │   └── orchestrator.py                  # Combines candidates; per-site LIMITs
│   │
│   ├── agents/
│   │   ├── seo_agent.py                     # Main agent logic
│   │   ├── skills/
│   │   │   ├── validate_opportunity.md      # Skill 1 prompt file
│   │   │   ├── diagnose_page.md             # Skill 2 prompt file
│   │   │   └── implementation_spec.md       # Skill 3 prompt file
│   │   ├── prompts/
│   │   │   └── agent_system.md              # System prompt (+ rejection context)
│   │   └── rejection_context.py             # NEW: inject last N rejection reasons
│   │                                        #   per site as few-shot context
│   │
│   ├── measurement/
│   │   ├── baseline.py                      # Store baseline + control group + YoY (optional)
│   │   ├── measure.py                       # Action-type measurement windows
│   │   ├── classify.py                      # Won/Neutral/Lost/Inconclusive + significance test
│   │   └── significance.py                  # NEW: two-proportion/binomial test on deltas
│   │
│   ├── api/
│   │   ├── main.py                          # FastAPI application
│   │   ├── routes/
│   │   │   ├── recommendations.py           # CRUD for recommendations
│   │   │   ├── queue.py                     # Action queue endpoints
│   │   │   └── measurements.py              # Measurement results
│   │   └── models/
│   │       └── schemas.py                   # Pydantic models
│   │
│   └── jobs/
│       ├── daily_sync.py                    # Cron: data synchronization
│       ├── weekly_crawl.py                  # Cron: pull crawl/audit FROM OPENSEO
│       ├── semantic_scoring.py              # NEW: embed clusters + pages → cosine
│       │                                    #   similarity → semantic_content_score
│       ├── weekly_candidates.py             # Cron: candidate generation (per site)
│       └── weekly_agent.py                  # Cron: agent prioritization
│
├── ui/
│   ├── screens/
│   │   ├── ActionQueue.tsx                  # Screen 1: Queue
│   │   ├── RecommendationDetail.tsx         # Screen 2: Detail
│   │   └── ResultsDashboard.tsx             # Screen 3: Results
│   └── components/
│       ├── EvidencePanel.tsx
│       ├── WorkRequired.tsx
│       ├── MeasurementPlan.tsx
│       └── ApprovalWorkflow.tsx
│
└── config/
    ├── site_config.json                     # Default thresholds + tier + in-stock rule
    ├── brand_queries.txt                    # Brand exclusion patterns
    └── measurement_windows.json             # Per action_type windows
```

---

## Key Data Flows

### 1. Daily Data Sync

```
Schedule: Every day at 01:00 UTC
Duration: ~15 minutes for typical Shopify store

Steps:
1. GSC API: Pull last 7 days of query×page×device×country data
2. Shopify API: Pull product catalogue, collections, inventory
3. GA4 API: Pull organic sessions, conversions, revenue by page
4. OpenSEO connector (ONE shared company account): resolve the single adapter
   via get_openseo_adapter() (no site_id — one global credential), check
   supports() per capability, then fetch with the per-site target passed in
   params: keyword volumes (→ keyword_clusters.search_volume) via
   fetch("keyword_volume"), SERP snapshots (→ openseo_serp_snapshots) via
   fetch("serp"), competitor/domain research via fetch("competitors"), plus
   backlinks / rank history / crawl-audit when the global openseo.capabilities
   list includes them. Unsupported capabilities are skipped and logged, never
   fatal.
5. Update pages table with crawl/audit data pulled from OpenSEO
   (incl. render_status / raw vs rendered HTML hashes for JS-rendering checks)
6. Store daily snapshots for historical comparison
```

### 2. Weekly Candidate Generation

```
Schedule: Every Sunday at 02:00 UTC
Duration: ~5 minutes (SQL queries on cached data)
NOTE: Generators run ONCE PER SITE with :site_id bound — each site gets its own
LIMIT quota; one site can never consume another's.

Per site:
1. Run Generator 1 SQL → up to 15 missing-page candidates
2. Run Generator 2 SQL → up to 20 existing-opportunity candidates
3. Run Generator 3 SQL → up to 15 technical-fix candidates (incl. JS rendering)
4. Run Generator 4 SQL → up to 10 cannibalization candidates
5. Combine all candidates (max 60 per site)
6. Apply validation rules:
   - Deduplicate by cluster_id (keep highest priority)
   - Exclude brand queries
   - Apply site thresholds resolved from threshold_tiers by catalogue_size_tier
     (per-site overrides win; no hardcoded constants in SQL)
   - Exclude recent recommendations (90-day cooldown)
7. Pre-rank candidates by priority_score/search_volume; select top
   ~25 (site_config.agent_prefetch_limit) for the expensive fetch stage;
   keep the remainder as a backup pool (site_config.agent_backup_pool),
   pulled in only if the top slice yields fewer than 5 valid recommendations.
8. Store valid candidates for agent evaluation
```

### 3. Agent Evaluation

```
Schedule: Every Sunday at 03:00 UTC (after candidate generation)
Duration: ~5-10 minutes (bounded by pre-ranking — fetch volume is capped)

Steps:
1. Load pre-ranked candidates (top ~25 + backup pool)
2. Load the last N rejection reasons for this site from rejection_log
   and inject them as few-shot context into the agent prompt (Priority 4.3)
3. For each candidate in the top slice:
   a. Fetch live SERP for primary keyword (via OpenSEO connector, top 10 results)
   b. Fetch target page content and metadata
   c. Fetch top 3 competitor pages
   d. Prepare candidate context JSON
4. If fewer than 5 candidates pass validation, draw from the backup pool
5. Send batch to agent (with skill files loaded)
6. Agent evaluates:
   - Validates intent against live SERP
   - Rejects weak candidates with documented reason (stored to rejection_log)
   - Diagnoses constraint for accepted candidates
   - Specifies exact implementation requirements
7. Store top 5 recommendations in recommendations table
8. Send notification with summary
```

### 4. Operator Workflow

```
After agent produces recommendations:

1. QUEUE DISPLAY: Show 5 recommendations with filters
2. REVIEW: Operator views evidence, diagnosis, work required
3. APPROVE/REJECT:
   - Approve → status changes to "approved"
   - Reject → must provide reason (stored to rejection_log; last N per site are
     injected as few-shot context into the next week's agent prompt)
4. ASSIGN: Assign to team member (content, engineering, SEO)
5. IMPLEMENT: Team executes work, marks "in_progress" → "live"
   - Baseline stored at implementation: target page delta + comparable
     unaffected control pages + YoY (optional) in measurement_snapshots
6. MEASURE: System auto-measures after the action_type-specific window
   (measurement_window_lookup; no fixed 28-day constant)
7. CLASSIFY:
   - Won/Lost require a two-proportion/binomial significance test on the
     deltas, scaled by sample size (noise filter, not causal inference)
   - Neutral/Inconclusive when movement fails the bar or YoY/control
     baseline shows a confounding trend; YoY is a bonus signal only —
     low-traffic sites fall back cleanly to the period-vs-period comparison
8. LEARN: Capture learnings for future recommendations
```

---

## Critical Configuration

### Site Config Defaults

Threshold columns in site_config are per-site OVERRIDES. When NULL, values are
resolved from `threshold_tiers` by `catalogue_size_tier`:

```json
{
  "catalogue_size_tier": "medium",
  "min_search_volume": null,
  "min_in_stock_products": null,
  "page_match_threshold": null,
  "min_impressions_7d": null,
  "ctr_drop_threshold": null,
  "position_drop_threshold": null,
  "position_range_min": null,
  "position_range_max": null,
  "catalogue_match_min": null,
  "in_stock_definition": "any_variant",
  "agent_prefetch_limit": 25,
  "agent_min_valid": 5,
  "agent_backup_pool": 20,
  "rejection_context_count": 5,
  "max_recommendations_per_week": 5,
  "measurement_window_days": null,
  "recommendation_cooldown_days": 90,
  "brand_queries": []
}
```

### Threshold Tiers (catalogue size)

| tier   | URLs          | page_match_threshold | catalogue_match_min | min_in_stock_products | min_impressions_7d |
|--------|---------------|----------------------|---------------------|-----------------------|--------------------|
| small  | <500          | 0.60                 | 0.45                | 6                     | 30                 |
| medium | 500–5,000     | 0.65                 | 0.50                | 8                     | 50                 |
| large  | >5,000        | 0.70                 | 0.55                | 10                    | 80                 |

### Measurement Windows (per action_type)

| action_type | window (days) | rationale                                      |
|-------------|---------------|------------------------------------------------|
| create_page | 49            | Discovery + crawl + ranking takes longer       |
| improve_page| 28            | Existing page tweaks surface faster            |
| consolidate | 28            | Redirect authority transfer + re-crawl         |
| technical_fix| 21           | Indexability/status fixes surface quickly      |

### In-Stock Definition (Priority 3.7)

`in_stock_definition` in site_config:
- `any_variant` (default) — product counts as in stock if ≥1 variant has available inventory
- `majority_variants` — product counts as in stock only if ≥50% of variants have inventory

Applied in the `catalogue_coverage` computation job, never left implicit.

### Page-Match Score Weights

Title/H1 is folded into `semantic_content_score` as a sub-signal (Priority 3.2);
the freed 10% is redistributed to catalogue-match and GSC-evidence (both are
harder to game than a title tag).

```json
{
  "intent_page_type": 0.30,
  "catalogue_match": 0.30,
  "gsc_evidence": 0.25,
  "semantic_content": 0.15
}
```

---

## MVP Constraints (What We're NOT Building)

1. **No automatic publishing** — all changes require human approval
2. **No backlink outreach** — out of scope for v1 (documented as secondary constraint in Skill 2)
3. **No multiple agents** — single principal agent only
4. **No causal inference** — before/after measurement with a statistical noise
   filter (two-proportion significance test) and a control-group baseline; we
   never claim causality from movement alone
5. **No content generation** — agent writes specs, not content
6. **No real-time analysis** — weekly batch processing only
7. **No complex attribution** — last-touch organic for MVP
8. **No freestanding competitor crawl** — competitor/SERP data comes solely from
   the OpenSEO connector (openseo_serp_snapshots); never from GSC-derived tables
9. **No fine-tuning** — rejection learning is explicit few-shot context injection only

## OpenSEO Connector — Shared Single-Account Adapter (Priority 2.1)

OpenSEO is ONE shared research tool (like Ahrefs/SEMrush) authenticated as a
**single company-level account**, not a per-brand integration — only GSC stays
per-site (each brand has its own Search Console property and authorized login;
that connector design is untouched). OpenSEO exposes only a documented REST
API — a separate MCP access mode does not exist externally. The connector
layer uses one generic interface and a single REST adapter. Full contract +
capability schemas: **`11-openseo-connector-schema.md`**. Summary:

- **One generic call shape** — every job/generator talks to OpenSEO only via
  `fetch(capability: str, params: dict) -> response`. Adding a capability
  (e.g. `content_gaps`) requires a new capability string + a response-schema
  entry — never a new function or a change to the calling code.
- **Adapter contract** — `openseo_rest_adapter.py` implements exactly two
  methods: `supports(capability)` and `fetch(capability, params)`, returning
  normalized responses (schema doc §4). There is NO MCP adapter — the generic
  `fetch()`/`supports()` contract is kept so a second access mode could be
  added later without touching callers, but it is not built until a real second
  mode exists. An unsupported capability returns a typed "unsupported" result —
  never a raw crash bubbling into a job.
- **Global config, one credential** — OpenSEO config is system-global, stored
  in the new `system_config` table (single rows: `openseo.base_url`,
  `openseo.secret_ref`, `openseo.capabilities`). `site_config` carries NO
  OpenSEO credential or capability-list columns — only GSC and
  domain/business fields. `get_openseo_adapter()` takes no `site_id`: it loads
  the single credential once and reuses it for every site's `fetch()` calls.
  Each call passes the target (site domain / keyword set, from
  `site_config.domain`) in `params`, never as a credential selector.
- **Credential security** — exactly one global credential, referenced by
  `openseo.secret_ref`, resolved by `get_openseo_adapter()` against the
  secrets store at load time and never logged/cached/persisted beyond the
  adapter instance's in-memory lifetime. It is the only code path that touches
  the secrets store; the one-time migration creates/collapses to the single
  global ref (12-migration-openseo-secrets.sql; §7/§7.1 of 11).
- **Wiring** — `openseo_serp_snapshots` (Generator 1 competitor signals) is
  populated via `fetch("serp")` / `fetch("competitors")`;
  `keyword_clusters.search_volume` via `fetch("keyword_volume")`;
  `weekly_crawl.py`'s OpenSEO audit pull via `fetch("crawl_audit")`. All
  callers already go through the adapter, so the correction is invisible to
  Generator 1 and the jobs. Missing capabilities are skipped + logged, never
  fatal.
- In all cases the platform never runs its own crawler — crawl/audit results
  come from OpenSEO through the adapter layer.

> **ASSUMPTION FLAG**: the global-credential model above rests on the team's
> current understanding that OpenSEO is one shared account for the whole
> system (verified against the products; no per-brand integration exists today).
> If a future brand requires its own separate OpenSEO account, the
> global-credential model in §1 above would need to revert to a per-site model
> — confirm this doesn't change before relying on it long-term.

---

## Success Metric

```
MVP Score = Acceptance Rate × Implementation Rate × Improvement Rate

Where:
  Acceptance Rate = Recommendations approved / Total recommendations
  Implementation Rate = Approved recommendations implemented / Approved recommendations
  Improvement Rate = Recommendations classified "Won" / Implemented recommendations

Target: MVP Score > 0.30 (30%)

If this works → justify automation and deeper intelligence
If this doesn't → adding more tools scales weak advice
```

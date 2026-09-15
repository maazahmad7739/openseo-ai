# OpenSEO AI — Implementation Plan: 5 Fixes (Phase-by-Phase)

**Audience:** Opencode (implementing engineer/agent). This document defines the *problem* and the *required outcome* for each fix, grounded in the actual codebase. **Technical approach, data structures, and code patterns are Opencode's call** — engineering freedom is intentional here. Where a decision genuinely needs product/founder input rather than an engineering judgment call, that is called out explicitly.

**Codebase reviewed:** `OpenSEO AI/src/**`, `OpenSEO AI/plan/**` (as of this zip).

---

## 0. How to run this (parallelization instructions for Opencode)

These 5 fixes touch mostly non-overlapping files, so they can be assigned to **5 parallel sub-agents**, one per phase below. Overlap risk is low but not zero:

| Phase | Primary files touched | Shares files with |
|---|---|---|
| 1. Cross-week duplicates | `plan/01-04-generator-*.sql`, `src/generators/orchestrator.py` | — |
| 2. SERP/data freshness TTL | `src/connectors/sync.py`, possibly `plan/00-schema.sql` | 3 (if cost log sits in same sync path) |
| 3. Cost/budget tracking | `src/connectors/openseo_rest_adapter.py`, `src/agents/ollama_client.py`, `src/connectors/sync.py`, `src/jobs/weekly_agent.py`, new schema | 2 |
| 4. Rollback SOP + visibility hook | `plan/*.md` (new doc), `src/api/routes/*`, `ui/*` (minor) | — |
| 5. Multi-site job isolation/scheduling | `src/generators/orchestrator.py`, `src/jobs/weekly_candidates.py`, `src/jobs/daily_sync.py`, `src/connectors/sync.py` | 1 (orchestrator.py, 3 (cost caps interact with scheduling) |

Because Phase 1 and Phase 5 both touch `orchestrator.py`, and Phase 2/3 both touch `sync.py`, whichever sub-agent finishes first on a shared file should note the exact functions it touched so the second agent can merge cleanly (or run those two pairs sequentially instead of in parallel — Opencode's call).

**Required output per phase** (from each sub-agent, at the end of its work): a short report containing —
1. What was broken, in the sub-agent's own words, with file:line references.
2. What approach was chosen and *why* (especially why over alternatives, if the phase below lists more than one option).
3. Exact files changed / added, with a one-line summary of each change.
4. How this was verified (test added, manual trace, migration run, etc.).
5. Any decision that was deferred to product/founder (see "needs product input" flags below) instead of resolved unilaterally.

---

## Phase 1 — Cross-week duplicate candidate suppression

### Confirmed root cause (not a hypothesis — verified in code)

There is already a **natural-key unique index** on `recommendations` (`plan/16-raw-candidate-status.sql`, step 4) and the orchestrator inserts with `ON CONFLICT ... DO NOTHING` (`orchestrator.py`, `_insert_recommendations`). So exact duplicate **rows** cannot land in the DB for the same `(site_id, generator, action_type, cluster_id, target_url, proposed_url)`. That part already works.

The real gap is **upstream**, in the generator SQL itself. All four generators exclude a cluster/URL from re-candidacy only when an existing recommendation already has status `proposed`, `approved`, or `in_progress`:

- `plan/01-generator-missing-pages.sql` (~line 99): `AND r.status IN ('proposed', 'approved', 'in_progress')`
- `plan/02-generator-existing-opportunities.sql` (~line 309): same three statuses
- `plan/03-generator-technical-root-causes.sql` (~line 488): same three statuses
- `plan/04-generator-cannibalization.sql` (~line 277): same three statuses

**None of them include `raw`.** Recommendations are inserted as `raw` and only promoted to `proposed` by the agent stage (`src/agents/seo_agent.py`). If a `raw` row sits unprocessed for a week (agent didn't get to it, errored on it, or the weekly agent run didn't cover it), the generator does **not** see it as "already recommended," so it re-derives the same candidate from source data next week. The DB insert itself is silently absorbed by `ON CONFLICT DO NOTHING` (no new row), but:
- the generator wastes a work cycle re-deriving something it already found,
- if any input signal changed slightly week-to-week (e.g. `target_url` resolution, `proposed_url` slug), the natural key can differ just enough to *not* collide, and a genuine second row is created for what is operationally the same problem,
- this is exactly the mechanism that produces what the operator perceives as repeat/duplicate recommendations.

### Required outcome

A cluster/URL that already has an **unresolved** candidate (any non-terminal status — at minimum `raw` needs to join the existing three) must not be re-proposed by the same generator until that candidate is resolved (approved → implemented → measured, or rejected).

### Needs product input (do not resolve unilaterally)

- Should a **rejected** candidate ever be eligible to resurface (e.g., after N weeks, or if underlying signals materially changed — like search volume doubling)? Current behavior (per `plan/16`'s documented intent) is "never resurrect a rejected candidate." Confirm this is still the desired behavior before hard-coding it further.
- Should a candidate whose `result = 'lost'` be eligible to resurface as a *new* candidate (this connects to Phase 4)? Flag it, don't decide it here.

### Acceptance criteria

- Re-running the weekly generator job twice in a row (same reference date, or one week apart with no operator action in between) produces **zero new rows** for clusters/URLs that already have an unresolved candidate, and the generator's own candidate count reflects this (i.e. it shouldn't even count them as "found" if that count is used for reporting).
- A regression test exists that: inserts a `raw` recommendation for a cluster, re-runs the relevant generator query, and asserts no new candidate is produced for that cluster.
- No change to how `proposed`/`approved`/`in_progress` exclusion already works.

---

## Phase 2 — SERP data freshness / TTL

### Confirmed root cause

`src/connectors/sync.py` calls `openseo.fetch("serp", ...)` (and the other capabilities — `keyword_volume`, `competitors`, `backlinks`, `crawl_audit`) unconditionally on every sync run, with no check against existing `openseo_serp_snapshots` rows for recency. `src/connectors/openseo_rest_adapter.py` maps `"serp"` straight to DataForSEO's live SERP endpoint (`v3/serp/google/organic/live/regular`) — a paid, per-call endpoint. There is no TTL, no "skip if snapshot from last N days exists" logic anywhere in the sync path.

### Required outcome

Before making a paid call for a given (site, query/keyword, capability), check whether sufficiently fresh data already exists and reuse it if so. "Sufficiently fresh" should be configurable (not hardcoded as a magic number buried in logic) — per-capability, since SERP volatility differs from keyword-volume or backlink volatility.

### Design freedom for Opencode — things to decide

- Where TTL config lives: env var, `site_config` column(s), or a small config table. Consider that different capabilities (`serp`, `keyword_volume`, `competitors`, `backlinks`, `crawl_audit`) plausibly want different TTLs.
- Whether freshness is checked at query granularity (per keyword) or per sync-run granularity (skip the whole capability if *any* recent data exists) — the former is more correct but more code; make the call and justify it in the report.
- Whether to add an explicit `--force-refresh` escape hatch for manual operator-triggered re-pulls (useful for debugging/demos; not required but consider it low cost).

### Acceptance criteria

- Running `daily_sync` (or whichever job triggers `sync_serp_snapshots`/equivalent) twice within the TTL window results in the second run making zero (or measurably fewer) live SERP calls, verifiable via whatever mechanism Phase 3 builds for call counting — coordinate with that phase or log a simple call counter as a stopgap if Phase 3 isn't merged yet.
- After the TTL window expires, the next sync does make a fresh call.
- This must not silently go stale forever — if `site_config` doesn't set an override, a documented sane default TTL applies (mirror the existing pattern used for `min_search_volume` in `orchestrator.py`'s `_impact_volume_threshold` — per-site override with a documented fallback constant — for consistency with how this codebase already does per-site config resolution).

---

## Phase 3 — Cost/budget tracking and weekly cap

### Confirmed root cause

Zero cost-tracking exists in the codebase today. Specifically verified absent:
- No per-call cost logging for DataForSEO calls in `openseo_rest_adapter.py` or `sync.py`.
- `src/agents/ollama_client.py`'s `chat()` method does not capture or log token usage from the response at all (no `usage`, `eval_count`, or `prompt_eval_count` handling), so even if Ollama Cloud returns token counts, they're currently discarded.
- No budget/cap table in `plan/00-schema.sql`, no budget check anywhere in `src/jobs/` or `src/generators/`.

### Required outcome

1. Every paid call (DataForSEO via the OpenSEO connector; Ollama Cloud via the agent) is logged with enough detail to compute cost: at minimum, timestamp, site_id (where applicable), call type/capability, and a cost or usage figure (call count is a valid stand-in if per-call pricing is flat; token counts if Ollama pricing is per-token).
2. A running weekly total is computable per service (and ideally per-site, since multi-brand billing/attribution will matter per issue #5 in the original report).
3. A configurable weekly budget cap exists. When the running total crosses the cap (or a warn-threshold below it, e.g. 80%), the system alerts (reuse `src/jobs/notify.py`'s existing summary/alert mechanism if suitable) and — this is the part that needs a decision, see below — either hard-stops further paid calls for the rest of the week or just alerts loudly. 

### Needs product input (do not resolve unilaterally)

- **Hard stop vs. alert-only.** A hard stop protects spend but risks silently starving the pipeline (no new candidates generated) if nobody reads the alert promptly. Opencode should implement whichever default is safer to ship first (alert + soft-stop with an explicit override is a reasonable default) but this should be flagged to Shrey/team as a decision, not assumed.
- Exact budget numbers (₹ or $ per week, per service) — not an engineering decision; use a clearly-named placeholder/env var so the number is a one-line config change once the team decides it.

### Acceptance criteria

- A dry-run/report command exists to show "spend so far this week, by service" without needing to query the DB by hand.
- Artificially setting the cap very low in a test and running a sync/generation job demonstrably triggers the alert/stop path.
- Ollama token usage, once captured, is visible in whatever log/table this phase creates — even before real pricing is wired up, so the data exists retroactively.

---

## Phase 4 — "Result = Lost" handling: SOP + minimum technical hook

### Confirmed root cause

`recommendations.result` supports `pending | won | neutral | lost | inconclusive` (`plan/00-schema.sql`), and `src/measurement/` computes significance to assign this. But there is no defined next step anywhere in the code once a row lands at `result = 'lost'` — no rollback table, no re-open workflow, no flag distinguishing "lost and reviewed" from "lost and nobody has looked at it yet."

### This is primarily a process problem, not a code problem — treat it as such

Opencode's job here is **not** to invent a rollback mechanism unilaterally (reverting a live redirect, unpublishing a page, etc. are real-world actions with consequences outside this codebase — a wrong automated "rollback" could do more damage than the original bad recommendation). Two concrete deliverables instead:

1. **A short SOP draft document** (`plan/17-lost-result-sop.md` or similar) laying out the *questions* the team needs to answer — not answering them. E.g.: Who gets notified when a result is `lost`? Is there a required human sign-off before any reversal action? Does "rollback" mean literally reverting the change, or just deprioritizing that cluster/approach going forward? This draft is explicitly for Shrey/team discussion, and should say so at the top.
2. **A minimal, safe technical hook**: make sure `lost` results are impossible to miss operationally — confirm (and fix if not already true) that the operator queue/dashboard (`src/api/routes/queue.py`, `ui/dist/screens/ResultsDashboard.js`) surfaces `lost` results distinctly, rather than them quietly sitting in a `measured` bucket indistinguishable from `won`/`neutral`. Add a `reviewed_at`/`reviewed_by` style field only if it doesn't already exist and the team's SOP (once drafted) would clearly need it — otherwise leave schema changes for after the SOP conversation.

### Acceptance criteria

- The SOP draft exists as a reviewable doc, explicitly marked as a discussion input, not a finished policy.
- `lost` results are visibly distinguishable from other terminal results in whatever surfaces recommendations today (queue/dashboard), verified by checking the actual current dashboard behavior, not assumed.
- No automated content/redirect reversal logic is built in this phase.

---

## Phase 5 — Multi-brand/site job execution: isolation and scheduling

### Confirmed root cause

Two separate issues, both verified in code:

1. **No fault isolation (more urgent than sequential-vs-parallel).** `src/generators/orchestrator.py`'s `main()` loops `for site_id, domain in sites: counts = run_candidate_generation(site_id, conn)` with **no try/except around the per-site call**. `src/jobs/weekly_candidates.py`'s `run()` has the identical pattern (`for sid in sites: out[str(sid)] = run_candidate_generation(sid, conn, ...)`), also with no per-site exception handling. As written today, **one site throwing an unhandled exception (bad data, connector timeout, whatever) kills the entire job for every remaining site in the loop**, not just delays them. This is worse than the "just slow" scenario originally flagged and should be fixed regardless of the sequential-vs-parallel decision below.
2. **Sequential execution with a shared connection.** All sites currently share one `conn` object and run strictly one-after-another within a single process. This is the "does a slow brand delay everyone" concern from the original report.

### Required outcome

- A failure or hang on one site must never prevent other sites' jobs from running or being attempted, and must be clearly logged/reported per-site (not just a stack trace that kills the whole run).
- A slow site should not indefinitely block others (some form of timeout and/or concurrency, bounded appropriately).

### Design freedom for Opencode — things to decide

- Per-site try/except + continue is the minimum fix and should ship even if concurrency work takes longer — treat this as the non-negotiable floor, concurrency as the stretch goal.
- Whether to move to real concurrency (thread pool, async, or a proper job queue) — and if so, how to bound it against DataForSEO/Ollama rate limits, since naive full-parallelism across 10 brands could itself trigger the rate-limit problem Phase 2/3 are trying to avoid. A bounded worker pool (e.g. N concurrent sites, N tuned to known rate limits) is a reasonable middle ground — Opencode's call on the exact mechanism.
- Whether each site needs its own DB connection when running concurrently (sharing one `conn` across threads is unsafe with most DB drivers) — this is a hard constraint if concurrency is added, not optional.

### Acceptance criteria

- A test/simulation where one site's generator run is forced to raise an exception demonstrates that other sites in the same job still complete and their results are recorded/reported.
- The per-site failure is visible in the job's output/notification (via `src/jobs/notify.py` or equivalent), not silently swallowed either.
- If concurrency is added: a documented, configurable concurrency limit exists; it is not "run all sites at once with no ceiling."

---

## Summary table

| # | Fix | Type | Blocking? | Key file(s) confirmed broken |
|---|---|---|---|---|
| 1 | Cross-week duplicate candidates | Code (SQL) | Yes — will visibly recur once real data testing starts | `plan/01-04-generator-*.sql` |
| 2 | SERP/data freshness TTL | Code | Yes — before real DataForSEO billing starts | `src/connectors/sync.py`, `openseo_rest_adapter.py` |
| 3 | Cost/budget tracking + cap | Code + schema | Yes — before real billing starts | `openseo_rest_adapter.py`, `ollama_client.py`, new schema |
| 4 | Lost-result rollback | Process (SOP) + minor UI check | No — but should be scheduled once scale increases | N/A (no code owns this today) |
| 5 | Multi-brand job isolation/scheduling | Code | Fault-isolation part: yes, cheap and high-value now. Concurrency part: no, can wait for scale. | `orchestrator.py`, `weekly_candidates.py` |
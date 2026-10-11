# Framer Onboarding Runbook (plan/26)

Operator documentation for onboarding a **Framer** site (same pattern
applies to WordPress/custom headless with platform-specific adapters
later). Phases 0–4 of plan/26 are implemented and tested; live writes are
**fail-closed** until the Framer write API is verified (see §5).

Prerequisites: the pipeline is deployed with the plan/26 schema applied
(`plan/26-framer-onboarding.sql`, idempotent). Phases 0–4 shipped:
platform seam + multi-connector credentials, generic sitemap→pages
ingestion, generator platform routing, `framer.title`/`framer.description`
adapters, platform-aware API dispatch.

---

## 1. Site registration (`site_config`)

Every write-path decision dispatches on `site_config.platform`
(NOT NULL, DEFAULT `'shopify'`). Register the Framer site with:

```sql
INSERT INTO site_config
    (site_name, domain, gsc_property, catalogue_size_tier,
     platform, framer_site_ref)
VALUES
    ('<Brand>', 'example.com', 'sc-domain:example.com', 'small',
     'framer', '<framer-site-ref>');
```

| Column | Value | Notes |
|---|---|---|
| `domain` | bare registrable domain (`example.com`) | the audit/ingest match key; NO scheme |
| `gsc_property` | `sc-domain:example.com` (or URL prefix form) | **required (NOT NULL)** — GSC powers `search_performance`, generators 2–4, measurement, and the protect-winner guard. Framer credentials alone do NOT power measurement. |
| `catalogue_size_tier` | `small` for content sites | thresholds resolve from `threshold_tiers` |
| `platform` | `'framer'` | the one switch the write path dispatches on |
| `framer_site_ref` | your Framer workspace/site reference | required for `framer.*` credential resolution |
| `shopify_domain` | leave NULL | a framer site never resolves a Shopify credential |

**Do NOT** set `platform` on existing Shopify sites — the default
resolves them identically to before plan/26 (byte-identical legacy
behavior, covered by `run_phase0_credentials_tests.py`).

Applying plan/26 to an existing deployment (idempotent, re-runnable):

```bash
psql <connection> -f plan/26-framer-onboarding.sql
```

## 2. Ingestion (automatic)

Once registered, the **weekly crawl job** (`jobs/weekly_crawl.py`) picks
the site up for the generic path because `platform <> 'shopify'`:

1. `site_fetch.sync_pages_from_sitemap` — fetches robots.txt + sitemap.xml
   (explicit → robots-declared → `/sitemap.xml` convention), fetches each
   URL through the audit fetch stack (robots honored, 8s timeout, 3MB
   cap), and upserts `pages` (page_type from URL archetype:
   `/blog/` → blog, `/products/` → product, unknown → unknown).
   Bounded to **200 pages per run**; rerun-safe (`ON CONFLICT` upsert with
   COALESCE guards — a poorer fetch never overwrites a richer row).
2. `site_fetch.sync_sitemap_membership` — the existing Task 5 pass flags
   `pages.in_sitemap` / `robots_allowed` (incl. newly ingested rows).

No operator action required per run; the job's typed summary lands in the
weekly output under `sitemap_ingest` (and `skipped` + reason when the
sitemap is unreachable — never a crash). **Verify after first run:**

```sql
SELECT page_type, count(*) FROM pages
WHERE site_id = '<site_id>' GROUP BY page_type ORDER BY 2 DESC;
```

Candidate generation runs automatically for framer sites with the
content-site mix: Generators 2 (existing opportunities), 3 (technical),
4 (cannibalization). Generator 1 (missing commercial pages) is skipped by
design — it is catalogue-coverage gated (`skipped_platform` marker in the
run summary). `create_page`/`collection_create` and publish-state /
redirect fixes are Shopify-only surfaces: on framer sites they return
typed plan_only refusals, never mis-targeted rows.

## 3. Credentials provisioning

The resolver (`src/connectors/write_credentials.py`) requires, for
`platform='framer'`:

| Item | Where | Fail shape if missing |
|---|---|---|
| `FRAMER_API_KEY` | env (`.env` or service env; see `.env.example`) | `no_credentials`, detail names `FRAMER_API_KEY` |
| `site_config.framer_site_ref` | DB (set in §1) | `no_credentials`, detail names the column |
| `framer.api_base` | `system_config` (auto-provisioned by plan/26; override only if the base URL changes) | falls back to the built-in default |

Fail-closed everywhere: the executor, the `/fixes/{id}/execute` endpoint,
and the `/fixes/{id}/revert` endpoint refuse with 409 / typed
`no_credentials` naming the missing key (values are never logged).

## 4. GSC property linking

`site_config.gsc_property` is NOT NULL and the measurement pipeline
(baselines, `search_performance`, protect-winner, consolidate gates) is
GSC-driven and platform-agnostic:

1. Grant the service account (see `.env.example` GSC section) access to
   the property in Search Console (Domain property preferred:
   `sc-domain:example.com`).
2. Set `gsc_property` on the site row (done in the §1 INSERT).
3. The daily sync populates `search_performance`; measurement windows are
   resolved from `measurement_window_lookup` as for any site.

A framer site WITHOUT GSC still audits and drafts, but fixes cannot be
measured and `improve_page` candidates (GSC-evidence gated) will be thin.

## 5. Fix policy enablement (explicit, per operator)

Nothing executes until the operator enables it — plan/26 ships every
`framer.*` policy row **disabled** (`fix_policy.enabled = false`, seeded
idempotently for all sites by plan/26). After the first framer fixes are
generated and review-diffed in the UI:

```sql
-- Enable title fixes (weekly cap 10, risk 'low'):
UPDATE fix_policy SET enabled = true, weekly_cap = 10
WHERE site_id = '<site_id>' AND sub_type = 'framer.title';

-- Enable description fixes (weekly cap 5, risk 'medium'):
UPDATE fix_policy SET enabled = true, weekly_cap = 5
WHERE site_id = '<site_id>' AND sub_type = 'framer.description';
```

Enforcement summary (unchanged from plan/21): the executor re-checks the
policy row at pickup (missing/disabled/zero-cap = typed refusal), weekly
caps count applied rows over a 7-day window, `uq_fixes_active_per_target`
prevents two active fixes per (site, url, field), and a 3-strike
`access_denied` counter auto-disables a sub_type (kill-switch). To halt a
framer sub_type: `UPDATE fix_policy SET enabled = false ...`.

## 6. Taking Framer live writes off the fail-closed gate

Current state (by design): `FramerClient` refuses every live call with a
typed refusal — `INTEGRATION_MODE=live` alone is insufficient, and even
with `FRAMER_LIVE_WRITES=1` the live HTTP surface is an explicit
*unverified* refusal. **There is no code path that can write to a real
Framer site today.** Removing that gate is an operational decision gated
on verification, not a code change:

1. **Verify the write API surface** (plan/26 open question #1):
   - the endpoint + auth model that mutates a page's SEO title /
     description (key scope, headers),
   - **whole-object semantics**: whether the write replaces the entire
     meta object (drives the sibling-field snapshot discipline — the
     adapters already implement it),
   - read-back availability for verify (and eventual-consistency
     behavior, if any),
   - error taxonomy: what maps to `user_errors` vs `access_denied` vs
     throttled.
2. **Implement `FramerClient._live_call`** (src/connectors/framer.py):
   replace the typed-refusal stub with the verified HTTP calls, keeping
   the never-raise typed contract and the read-back verify shape.
3. **Provision credentials**: `FRAMER_API_KEY` live value + the site's
   `framer_site_ref`; `INTEGRATION_MODE=live` for the service process.
4. **Enable the flag** `FRAMER_LIVE_WRITES=1` (env) — the second gate.
   Roll out ONE site first; keep other framer sites in mock until
   verified.
5. **Enable policies per site** (§5) — the executor's scope-probe
   diagnostic reports the client's `probe_auth()` result at startup;
   verify it says `ok` before enabling.
6. **First-write rehearsal**: `python -m jobs.fix_executor --site_id
   <site_id> --dry-run` writes nothing anywhere; then queue ONE low-risk
   title fix, execute, read the `adapter_response` + `verification_status`
   in the UI, and use `/fixes/{id}/revert` once to prove the rollback
   path against the live surface.

If verification fails or the surface changes: leave the flag unset — the
typed refusals keep every gate honest (no code removal required).

## 7. What works today (test-proven), end to end

| Step | Framer behavior |
|---|---|
| Audit (on-demand URL) | brand-agnostic, unchanged (plan/23) |
| Sitemap → pages | automatic via weekly crawl (§2) |
| Candidate generation | Generators 2/3/4 (content mix) |
| Fix generation (title/meta) | mints `framer.title` / `framer.description`, target = canonical URL |
| Diff review / approve / queue | platform-agnostic (plan/21 two-gate) |
| Execution | full lifecycle (policy gate → fresh snapshot → whole-object write → read-back verify → change_log → measurement wiring) |
| Revert | snapshot restore of BOTH meta fields, audited revert row |
| Stale-diff guard | live `meta.title`/`meta.description` vs diff old_value → auto-expire |
| verify_failed | auto-revert from the fresh snapshot |

Test suites (all passing): `run_phase0_credentials_tests.py`,
`run_phase1_sitemap_ingest_tests.py`,
`run_phase2_platform_routing_tests.py`,
`run_phase3_framer_adapter_tests.py`,
`run_phase4_api_dispatch_tests.py` — plus the full pre-existing suite
(zero Shopify-path regressions; `run_scheduler_tests.py` carries 2
pre-existing weekday-silence failures unrelated to plan/26).
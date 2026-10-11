# Netlify / Static-Site Onboarding Runbook (plan/27)

Operator documentation for onboarding a **static-hosted** site (Netlify,
GitHub Pages, any git-backed host) whose writes are delivered as **Git
Pull Requests** — never live API overwrites, never pushes to the base
branch. The pattern extends the plan/26 platform seam (shopify / framer)
with a third write surface.

Prerequisites: the pipeline is deployed with the plan/27 schema applied
(`plan/27-netlify-github-pr.sql`, idempotent). Reading/auditing/recommending
needs nothing new (generic sitemap ingestion already covers non-Shopify
platforms, plan/26 Phase 1); the PR write path adds the adapter layer.

---

## 1. Site registration (`site_config`)

```sql
INSERT INTO site_config
    (site_name, domain, gsc_property, catalogue_size_tier,
     platform, github_repo, github_branch, github_root_dir)
VALUES
    ('<Brand>', 'example.com', 'sc-domain:example.com', 'small',
     'netlify', '<owner>/<repo>', 'main', '');
```

| Column | Value | Notes |
|---|---|---|
| `domain` | bare registrable domain (`example.com`) | the audit/ingest match key; NO scheme |
| `gsc_property` | `sc-domain:example.com` (or URL prefix form) | **required (NOT NULL)** — GSC powers `search_performance`, generators 2–4, measurement. GitHub credentials alone do NOT power measurement. |
| `catalogue_size_tier` | `small` for content sites | thresholds resolve from `threshold_tiers` |
| `platform` | `'netlify'` | the one switch the write path dispatches on |
| `github_repo` | `owner/name` of the site's content repo | required for the PR write path; leave NULL until the repo is bound (generation then refuses, typed) |
| `github_branch` | base branch PRs target (default `main`) | never written to directly |
| `github_root_dir` | repo dir mapping to the site origin | `''` = repo root; `'site'` = Jekyll/Hugo-style prefix |

Applying plan/27 to an existing deployment (idempotent, re-runnable):

```bash
psql <connection> -f plan/27-netlify-github-pr.sql
```

## 2. Ingestion (automatic, unchanged from plan/26)

`platform <> 'shopify'` sites take the generic path: the weekly crawl
fetches robots.txt + sitemap.xml → `pages` (page_type from the URL
archetype, bounded 200/run, rerun-safe upserts). Candidate generation
runs Generators 2/3/4 (content-site mix); Generator 1 (missing
commercial pages) skips by design.

## 3. Credentials provisioning

The resolver (`src/connectors/write_credentials.py`) requires, for
`platform='netlify'`:

| Item | Where | Fail shape if missing |
|---|---|---|
| `GITHUB_TOKEN` | env (`.env` or service env; see `.env.example`) | `no_credentials`, detail names `GITHUB_TOKEN` |
| `site_config.github_repo` | DB (set in §1) | `no_credentials`, detail names the column |
| `github.api_base` | `system_config` (auto-provisioned by plan/27) | falls back to `https://api.github.com` |

Token shape: a **fine-grained PAT** (or GitHub App user token) scoped to
the site's content repo with:
* **Contents: read/write** (file read for snapshots + file commit in PRs)
* **Pull requests: read/write** (open + close)
* **Metadata: read-only** (default)

The token is read but never logged; error details name the missing ENV
KEY, never a value. Fail-closed everywhere: the executor, the
`/fixes/{id}/execute` endpoint, and `/fixes/{id}/revert` refuse with 409
/ typed `no_credentials` naming the missing key.

## 4. What the write path does (plan/27 §3)

Every executed fix is **one PR per fix**:

1. **Snapshot** — the adapter reads the target file from the base branch
   (the stale-diff guard's live-value source AND the revert's old-state
   source).
2. **Transform** — the fix's action is applied to the HTML text
   deterministically (see §5). A transform that cannot find a safe
   insertion point refuses (`no_head_section`, `already_present`,
   `already_noindex`) — never a guessed edit.
3. **PR** — a branch `openseo/fix-<fix_id>` is created off the base
   branch, the file is committed, and the PR is opened, titled
   `fix(seo): [action-label] for [page-path]`. The PR body embeds
   `recommendation_id:` + `fix_id:` so the rollback links back to the
   recommendation in Supabase.
4. **Verify** — the PR's committed diff IS the verification surface
   (`verification_status='verified'` once the PR opens; a human merges).
5. **Rollback** — `/fixes/{id}/revert` (or verify_failed auto-revert)
   **closes the recorded PR** (never a force-push). A snapshot without
   the PR coordinates refuses (`no_pr_recorded`).

## 5. Actions → sub_types

| sub_type | Action | Risk/cap (policy seed) | What it writes |
|---|---|---|---|
| `netlify.title_meta_rewrite` | title_meta_rewrite | low / 10/wk | `<title>` + `<meta name="description">` replaced (or inserted when absent) in one PR |
| `netlify.structured_data` | structured_data_inject | medium / 5/wk | validated JSON-LD `<script type="application/ld+json">` block before `</head>`; identical `@type`+`name` block refuses |
| `netlify.noindex` | noindex_tag | high / 3/wk | `<meta name="robots" content="noindex, follow">` inserted/replaced; already-noindex refuses (no-op loop guard) |

All policy rows ship **disabled** (`fix_policy.enabled = false`). After
review-diffing the first generated fixes in the UI:

```sql
-- Enable title/meta PR fixes (weekly cap 10, risk 'low'):
UPDATE fix_policy SET enabled = true, weekly_cap = 10
WHERE site_id = '<site_id>' AND sub_type = 'netlify.title_meta_rewrite';

-- Enable structured-data PR fixes (cap 5, medium):
UPDATE fix_policy SET enabled = true, weekly_cap = 5
WHERE site_id = '<site_id>' AND sub_type = 'netlify.structured_data';

-- Enable noindex PR fixes (cap 3, high):
UPDATE fix_policy SET enabled = true, weekly_cap = 3
WHERE site_id = '<site_id>' AND sub_type = 'netlify.noindex';
```

Enforcement is identical to plan/21/26: policy re-checked at pickup,
weekly caps over a 7-day window, one active fix per (site, url, field),
3-strike `access_denied` auto-disable. To halt a netlify sub_type:
`UPDATE fix_policy SET enabled = false ...`.

## 6. Taking live writes off the flag gate

Current state (by design): live GitHub calls require BOTH
`INTEGRATION_MODE=live` AND `NETLIFY_LIVE_WRITES=1`. Without the flag,
every live call returns a typed `live_writes_disabled` refusal.

1. **Provision the token** (`GITHUB_TOKEN` env) with the §3 scopes.
2. **Enable the flag** `NETLIFY_LIVE_WRITES=1` — roll out ONE site first;
   keep other netlify sites in mock until verified.
3. **Probe check**: the executor's startup scope-probe reports the
   client's `probe_auth()` result; verify it says `ok` before enabling
   policies.
4. **First-write rehearsal**: `python -m jobs.fix_executor --site_id
   <site_id> --dry-run` writes nothing anywhere; then queue ONE low-risk
   title/meta fix, execute, read the `adapter_response` (PR number +
   URL) in the UI, and use `/fixes/{id}/revert` once to prove the
   rollback path (PR closed) against the live surface.

## 7. What works today (test-proven), end to end

| Step | netlify behavior |
|---|---|
| Audit (on-demand URL) | brand-agnostic, unchanged (plan/23) |
| Sitemap → pages | automatic via weekly crawl (plan/26 §2) |
| Candidate generation | Generators 2/3/4 (content mix) |
| Fix generation (title/meta) | mints `netlify.title_meta_rewrite`, target = repo file path |
| Fix generation (technical) | `structured_data_failure` → `netlify.structured_data`; explicit `noindex_intentional` → `netlify.noindex`; everything else plan_only |
| Diff review / approve / queue | platform-agnostic (plan/21 two-gate) |
| Execution | full lifecycle (policy gate → fresh snapshot → transform → branch+commit+PR → PR-diff verify → change_log → measurement wiring) |
| Revert | PR closed (snapshot carries pr.number), audited revert row |
| Stale-diff guard | live `meta.title`/`meta.description` vs diff old_value → auto-expire |

Test suite: `tests/run_phase5_netlify_pr_tests.py` (40 checks, zero
external network — fixture-backed mock repo). Pre-existing phase suites
(phase0/1/2/3/4 + api smoke) all pass unchanged.
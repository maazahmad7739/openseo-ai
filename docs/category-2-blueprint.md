# Category 2 Implementation Blueprint — Medium Difficulty / High ROI Infrastructure

Status tracker for the Category 2 infrastructure fixes (auth, DB health,
migrations, CI, UI resilience, batched-PR foundations). Execute phases in
order; each phase is independently deployable and revertible.

| Phase | Scope | Status |
|---|---|---|
| P1 | Migration runner + pr.url index + CI + suite runner | **done** (2026-09-28, verified locally) |
| P2 | Auth hardening (timing-safe, webhook HMAC, frontend 401) | **done** (2026-09-28, verified locally) |
| P3 | DB pooling + sslmode + connection lifecycle | **done** (2026-09-28, verified locally) |
| P4 | N+1 fixes (pipeline + fixes routes) | pending |
| P5 | UI error boundaries + optimistic rollback | pending |
| P6 | Shared sub_types contract (Python + TS) | pending |
| P7 | Batched PR execution layer | pending |

### P1 completion notes (2026-09-28)

All four P1 deliverables existed and were verified end-to-end on a fresh
local Postgres 16 (docker, port 5433 = `src/db.py` defaults):

- `scripts/migrate.py` — ledger applied 13 migrations (12→29, minus the
  NON_MIGRATIONS set), idempotent re-run ×2 prints "nothing to apply";
  `idx_fixes_pr_url` confirmed in `pg_indexes`.
- **Gap fixed in `ci.yml`**: CI's Postgres starts empty but `00-schema.sql`
  is excluded from the ledger (legacy, not idempotent DDL) — first
  `migrate.py` run on a fresh DB failed with 13 "relation does not exist"
  errors. Added an "Apply base schema" step (pgcrypto + `db.apply_schema()`)
  before `migrate.py`; also moved the service to port 5433 to match
  `src/db.py` defaults and the docs.
- `scripts/run_tests.py` — 38 suites discovered, sequential, 600s cap each.
  Two fixes:
  - runner crashed printing suite stderr containing U+FFFD on cp1252
    consoles → `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`.
  - `tests/run_agent_tests.py:19` — local `check()` took 2 args but :130
    passed a detail string (pre-existing bug; now 3-arg like every other
    suite).
- `tests/run_api_tests.py:231` — "consolidate row evidence intact"
  asserted `evidence_json` starts with `[` (array), but the plan/04
  generator writes consolidate evidence as an OBJECT
  (`jsonb_build_object`, same shape the consolidate-sharpen suite seeds).
  Predicate now accepts the real contract: array (legacy) OR object
  carrying `competing_urls`.
- Full suite result: **36 passed / 0 failed / 2 skipped** (documented
  skips: `run_live_agent.py` live LLM, `run_serp_grounding_tests.py` live
  DataForSEO — both pass standalone in their modes), repeated ×2.
- Data hygiene: removed the stale `FixSeq Test c703a85e` orphan site from
  the local fixture DB (leftovers of a 2026-09-24 crashed run; its
  consolidate row was the very row the broken array assertion tripped on).

---

### P2 completion notes (2026-09-28)

All four Workstream-1 items implemented + verified:

- **2.1 timing-safe auth** — `require_auth` now compares with
  `hmac.compare_digest` (never `!=` on secrets) and **returns** the 401
  `JSONResponse` instead of raising: an HTTPException raised inside
  `@app.middleware("http")` sits OUTSIDE ExceptionMiddleware, so it escaped
  to ServerErrorMiddleware and rendered as a bare **500** — meaning the
  pre-existing auth middleware never actually returned 401 through
  Starlette's middleware path. Middleware now returns the response
  (WWW-Authenticate: Bearer included).
- **2.1 webhook exemption** — `_WEBHOOK_EXEMPT_PATHS = {"/fixes/github-webhook"}`
  checked BEFORE the protected-prefix test (`"/fixes/github-webhook"
  .startswith("/fixes")` is True — the naive placement would exempt
  nothing). Verified: wrong-Bearer webhook still passes the middleware
  (its security is the HMAC, not the Bearer).
- **2.2 webhook HMAC-SHA256** — `verify_github_signature` in
  `github_sync.py`: fail-closed 401 on missing/invalid/tampered signature
  when `GITHUB_WEBHOOK_SECRET` is set; loud-warning bootstrap pass when
  unset. Body read via `await request.body()` → verify → `json.loads`
  (400 on parse failure).
- **2.3 rate limiter fixed** — `live_url_audit(body, request: Request, …)`
  (was `request=None`, which silently disabled the per-IP token bucket →
  unlimited paid SERP calls). Suite asserts 429 after the cap.
- **2.4 frontend 401 recovery** — `api()` prefers
  `localStorage["openseo_api_token"]` over build-time `VITE_API_TOKEN`;
  on 401 clears the key + dispatches `openseo:unauthorized`; root route
  (`__root.tsx`) mounts an `AuthGate` token form → `location.reload()`
  (no rebuild needed). New `ApiError` carries the status code.
- **Tests** — new `tests/run_p2_auth_hardening_tests.py` (20 checks,
  offline): timing-safe auth matrix, middleware exemption, HMAC
  fail-closed matrix (incl. e2e through the app), rate-limiter 429.
  Full suite: **39 passed / 0 failed / 2 skipped**, confirmed ×2.
  Frontend: `tsc --noEmit` + `oxlint` + `vite build` all clean.
- **Harness bugs fixed in passing** (pre-existing, unrelated to P2):
  `tests/run_schema_base_url_tests.py:365` — every test fn returns None,
  so `not all(results)` ALWAYS failed → exit 1 despite all checks PASSing
  (suite was never the gate it claimed to be); `main()` now relies on
  `FAILURES` alone.
  `run_fix_consolidate_sharpen_tests.py` flake investigated: the
  wrong-direction regression can hit the redirect-chain guard
  (`redirect_loop_source`) BEFORE the wrong-direction guard when a
  previous suite's applied redirect rule survives a crash-cleanup —
  passes consistently after a clean DB state (behavior correct either
  way: generation refused, just via the other guard).

---

### P3 completion notes (2026-09-28)

Pooling + connection lifecycle implemented per blueprint §2.2:

- **`src/db.py`** — `get_connection` refactored over a new `_db_params()`:
  env overrides + sslmode defaulting (`POSTGRES_SSLMODE` wins; unset =
  `require` for managed hosts, none for localhost via
  `sslmode_default_disabled`). New pool layer: process-wide
  `ThreadedConnectionPool` behind a `_pool_lock` (`_get_pool`),
  `get_pooled_connection()` (typed `DbError` on PoolError, never a raw
  traceback), `release_pooled_connection()` (rollback-first so the next
  borrower never inherits an open transaction; `close=True` for broken
  conns), `close_pool()` (idempotent). `get_connection()` itself is
  UNCHANGED for the 23+ job/script call sites. Bounds: `DB_POOL_MIN=0`
  (default), `DB_POOL_MAX=8` (default) — serverless-safe per-instance.
- **`src/api/common.py`** — `get_conn` is now a generator dependency:
  `yield conn` wrapped in `try/finally` → guaranteed
  `release_pooled_connection(conn)` (which rolls back) even on unhandled
  route exceptions. All 32 `Depends(get_conn)` call sites untouched.
- **`src/api/main.py`** — `@app.on_event("shutdown")` → `close_pool()`
  (warm instances no longer leak sockets on freeze).
- **Verified**: all three API suites pass
  (`run_api_tests`, `run_day_in_the_life`, `run_api_day_in_the_life`);
  full runner 43 suites — see final section for the standing result.
  Pool lifecycle smoke-tested: borrow → release → close all idempotent;
  sslmode matrix verified (localhost → none, managed → require, explicit
  env → wins).
- **Runner fix**: `run_day_in_the_life.py` now runs with `--skip-agent`
  in the automated runner (its documented deterministic mode). Its live
  path asserts "live LLM approves ≥1 of ~7 candidates" — non-deterministic
  (~4/5 clean runs fail on model behavior, not code), so the live-agent
  path is documented for manual/standalone verification in WALKTHROUGH.md
  §11 instead.
- **Pre-existing suite/migration debt fixed in passing** (surfaced by the
  P3 verification, unrelated to pooling):
  - `tests/run_day_in_the_life.py:61` + `tests/run_api_day_in_the_life.py:143`
    called `run_sync()` bare — plan/31 removed the default-site fallback
    (SiteRequiredError). Both now pass `site_id=...` explicitly.
  - `scripts/migrate.py` NON_MIGRATIONS was missing
    `05-generator-internal-links.sql` — it's a generator TEMPLATE (uses
    `:site_id` named binds; runtime-rewritten by runner.py), not DDL;
    migrate crashed with `syntax error at or near ":"`.
  - `plan/31-tenant-mode-and-provenance.sql` missed the
    `data_source` column on `page_business_performance` (sync.py stamps
    provenance there → UndefinedColumn on fresh DBs) and assumed
    `api_costs_archive` exists (it's legacy 19-cost-tracking DDL — FK
    guard now skips cleanly via `to_regclass` on fresh DBs).

---

## Original P1 section (kept for reference)

## Workstream 2 — DB health, pooling, migrations (2.2, 2.3, 2.4)

---

## Ground-truth corrections (code inspection, 2026-09-28)

1. **Auth is already middleware-wired** (`src/api/main.py:101-114`, protected
   prefixes :88-96) — but enabling `API_AUTH_TOKEN` **breaks the webhook**:
   `/fixes/github-webhook` sits inside the `/fixes` protected prefix and
   GitHub cannot send our Bearer header (`src/api/routes/github_sync.py:116`).
   Fix = exempt the webhook path in middleware + HMAC-verify it in-route.
2. **The frontend token is build-time only** (`VITE_API_TOKEN`,
   `apps/openseo-plus/src/features/operator/data/useOperatorData.ts:153`) —
   no runtime recovery path exists. The 401 fix must add one.
3. **Bonus finding:** `src/api/routes/audit.py:235` declares `request=None`
   as a default arg, so the per-IP rate limiter at :237-242 never fires.

---

## Workstream 1 — Auth & Security (2.1, 2.9)

### 1.1 Timing-safe auth + webhook exemption — `src/api/common.py`, `src/api/main.py`

`src/api/common.py:28-38` — replace `require_auth` body with
`hmac.compare_digest` on both sides (never `!=` on secrets).

`src/api/main.py:101-114` — exempt the webhook before the protected-prefix
check (note: `"/fixes/github-webhook".startswith("/fixes")` is True):

```python
_WEBHOOK_PATHS = {"/fixes/github-webhook"}
# inside auth_middleware, after the /api prefix strip:
if path.rstrip("/") in _WEBHOOK_PATHS:      # verified via HMAC in-route
    return await call_next(request)
```

Keep middleware-based auth (do NOT convert 32 routes to `Security()` deps —
churn with no gain; middleware already covers both bare and `/api` mounts).

### 1.2 Webhook HMAC-SHA256 — `src/api/routes/github_sync.py`

`import hashlib, hmac, os, logging` at top; verifier + handler change at
:116-126:

```python
GITHUB_WEBHOOK_SECRET_ENV = "GITHUB_WEBHOOK_SECRET"

def verify_github_signature(request: Request, body: bytes):
    """Fail-closed when GITHUB_WEBHOOK_SECRET is set; warns loudly when unset."""
    secret = os.environ.get(GITHUB_WEBHOOK_SECRET_ENV)
    if not secret:
        logging.getLogger("api.github_sync").warning(
            "webhook received with GITHUB_WEBHOOK_SECRET unset — signature NOT verified")
        return
    supplied = request.headers.get("X-Hub-Signature-256") or ""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, f"sha256={digest}"):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
```

Replace `payload = await request.json()` with
`body = await request.body()` → `verify_github_signature(request, body)` →
`json.loads(body.decode("utf-8"))` (400 on parse failure).

### 1.3 Expression index — `plan/29-webhook-hardening.sql` (built in P1)

```sql
CREATE INDEX IF NOT EXISTS idx_fixes_pr_url
    ON generated_fixes ((snapshot_json->>'pr.url'))
    WHERE snapshot_json ? 'pr.url';
```

### 1.4 Frontend 401 handling — `useOperatorData.ts`, `routes/__root.tsx`

- `api()` wrapper (:150-169): prefer `localStorage["openseo_api_token"]`
  over build-time `VITE_API_TOKEN`; on 401 clear the localStorage key,
  dispatch `window` event `openseo:unauthorized`, throw a typed `ApiError`
  carrying `status`.
- `routes/__root.tsx`: mount an inline auth gate listening for
  `openseo:unauthorized` → token form → `localStorage.setItem(...)` +
  `location.reload()` (no rebuild needed).

---

## Workstream 2 — DB health, pooling, migrations (2.2, 2.3, 2.4)

### 2.1 Migration ledger + runner — `scripts/migrate.py` (built in P1)
- `schema_migrations` table: `version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now()`.
- Ordered runner over `plan/*.sql` sorted by numeric prefix; records each
  applied file; re-runs skip applied files.
- Safe on a live DB that predates the ledger: every existing plan/*.sql is
  idempotent (`IF NOT EXISTS` / guarded DO blocks), so first run re-applies
  them as no-ops.
- Supersedes `tmp_migrate.py` (deleted in P1).

### 2.2 Pool + sslmode + request lifecycle — `src/db.py`, `src/api/common.py`, `src/api/main.py` (P3)

- `src/db.py`: refactor `get_connection` (:44-59) into `_db_params()` +
  keep `get_connection` for jobs/scripts (23 call sites); add
  `psycopg2.pool.ThreadedConnectionPool` used only by the API request path
  (`get_pooled_connection` / `release_pooled_connection` / `close_pool`).
- `POSTGRES_SSLMODE` env; default `sslmode=require` when host is not local.
- `src/api/common.py:20-21` — `get_conn` becomes a sync generator dependency
  (`yield` + rollback + return to pool). All 32 `Depends(get_conn)` sites
  unchanged. Requires FastAPI route handlers to remain sync (they are).
- `src/api/main.py` — `@app.on_event("shutdown")` → `database.close_pool()`.
- Serverless caution: pool is per warm instance; keep `DB_POOL_MAX` low
  (4-8). PgBouncer is the follow-up if instance count grows.

### 2.3 N+1 fixes (P4)

- `/pipeline` (`src/api/routes/queue.py:720-799`): per-row `_load_fix_rows`
  (:789) and `_site_platform` (:790) inside the loop → batch with
  `_load_fix_rows_batch(conn, rec_ids)` (`WHERE recommendation_id =
  ANY(%s::uuid[])`, grouped in Python) and `_site_platforms(conn, site_ids)`
  (single query, dict). Keep single-row helpers for `get_queue_detail` (:682).
- `GET /recommendations/{id}/fix` (`src/api/routes/fixes.py:665-676`):
  replace id-list + per-id `_fix_row` loop with one full-column query,
  build rows via `cur.description`.
- No response-shape changes; `run_api_tests.py` asserts shapes stay stable.

---

## Workstream 3 — CI (2.17) — built in P1

`.github/workflows/ci.yml`: Postgres 16 service container on 5433
(matches `src/db.py` defaults), mock-mode env, `pip install -r
requirements.txt`, `python scripts/migrate.py`,
`python scripts/run_tests.py`. Frontend job: pnpm install
(`--frozen-lockfile`), `types:check`, `lint`, `build`.

`scripts/run_tests.py`: sequential runner over the 38 bespoke suites in
`tests/` (they are standalone `check()`-harness scripts, NOT pytest).
Skip-list initially: `run_live_agent.py` (needs live LLM credentials).
Network-bound suites discovered in CI get added to the skip constant
rather than deleted coverage.

---

## Workstream 4 — UI resilience & contracts (2.6, 2.8, 2.15)

### 4.1 Error boundaries — no new dependency (P5)

Hand-rolled `ErrorBoundary` class (~30 lines,
`apps/openseo-plus/src/components/ErrorBoundary.tsx`):
`getDerivedStateFromError`, `componentDidCatch → console.error`,
`resetKeys`-based auto-reset. Wiring (three points):

- `main.tsx:32` — wrap `<RouterProvider />` (last resort).
- `routes/__root.tsx:4` — wrap `<Outlet />` + add `errorComponent`.
- `DetailDrawer.tsx:160` — wrap `<DrawerBody />` with
  `resetKeys={[recommendationId]}` — the exact crash recorded in the
  `WorkTaskList.tsx:319-322` comments.

### 4.2 Optimistic rollback (P5)

`useOperatorData.ts` — `snapshotCaches()` / `restoreCaches()` helpers over
`[queueKey, pipelineKey]`; each of the four mutations (:560, :576, :596,
:611) gains `onMutate` returning the snapshot and `onError` restoring it +
`toast.error` (sonner, already a dependency). Keep `onSettled: invalidate`
— refetch remains the reconciler after rollback.

### 4.3 Shared sub_types constants (P6)

Confirmed drift across six layers: adapters registry
(`src/fixes/adapters.py:767+`), classifier (`src/api/routes/queue.py:170-201`),
scope map (`src/connectors/shopify.py:700-721`), policy seeds
(`plan/22:99-112`, `plan/26:39-40`, `plan/27:70-72`, `plan/28:17`),
draft endpoint (`src/api/routes/fixes.py:254-272`), UI mirror
(`WorkTaskList.tsx:81-93`).

Casualties: `netlify.noindex` has adapter + policy but no draft/UI surface;
`page_create`/`product_create` have scope + policy but no adapter
(`no_adapter` fail-closed at execution); `content`, `product_publish`,
`article_body_link` are dead policy rows.

Plan:

1. New `src/fixes/sub_types.py` (zero imports): `ADAPTER_SUB_TYPES`
   (15 executable keys), `POLICY_ONLY_SUB_TYPES` (5 fail-closed rows),
   `NETLIFY_SUB_TYPES` derived, shared display labels.
2. `queue.py:144-146` + `fixes.py:254-272` import from it; wire
   `netlify.noindex` drafting with its crawl-verified guard
   (`src/fixes/generator.py:2499`).
3. New `apps/openseo-plus/src/features/operator/data/subTypes.ts`:
   `SUB_TYPES` const + `SubType` union; `WorkTaskList.tsx` imports it.
4. New `tests/run_sub_type_contract_tests.py` (no DB): registry ≡ constant,
   scope map ⊆ known set, draft lists ⊆ adapter set, and regex-parse of
   `subTypes.ts` asserting the TS string set matches Python exactly.

---

## Workstream 5 — Batched PR foundations (2.10) — outline, P7

Today each fix = own branch + PR (`GitHubPRClient._live_open_pr`,
`src/connectors/github_static.py:357-408`). Batch steps in order:

1. Generalize client: `open_pr(files, title, body, branch)` accepting a
   file list (per-file contents PUT loop at :391 already supports it).
2. New `src/fixes/batch.py` — `collect_batch(conn, fix_ids)`: same-site +
   same `github_repo` validation, in-memory transforms, cap ≤ 10, all
   `netlify.*`, all status `queued`.
3. Route `POST /fixes/batch-execute` — same advisory lock as `execute_fix`
   (:809), one PR for the batch, fan out `pr.*` snapshot keys into every
   fix + `Fix-Ids:` PR-body footer.
4. Route `POST /fixes/batch-approve` — gate-2 in bulk; reject the whole
   batch atomically if any single fix is blocked.
5. Merge sync is free — `github_sync.py:139-143` already matches fixes by
   `snapshot_json->>'pr.url'`; one webhook event syncs the whole batch.

Rollback = per-fix reverse-PR (outline only; defer).

---

## Execution sequence

| Phase | Verify before proceeding |
|---|---|
| P1 | ✅ `python scripts/migrate.py` idempotent re-run ×2 verified; full suite 36/0/2 ×2 (CI green pending first push — workflow ready) |
| P2 | ✅ Full suite + P2 suite green; deploy with `GITHUB_WEBHOOK_SECRET` unset → test webhook → set secret in GitHub + Vercel → 401 on bad signature, 200 on good (production drill pending) |
| P3 | ✅ API suites + full runner green locally; deploy with `POSTGRES_SSLMODE=require` (auto-defaults for managed hosts); watch `/health` + logs for `PoolError` |
| P4 | `run_api_tests.py`; spot-diff `/pipeline` JSON vs pre-change capture |
| P5 | `pnpm run types:check && pnpm run lint && pnpm run build`; manual drawer-crash drill |
| P6 | `python tests/run_sub_type_contract_tests.py`; CI green |
| P7 | Phase-5 test suite mirroring `run_phase5_netlify_pr_tests.py`, behind `NETLIFY_LIVE_WRITES=1` + `BATCH_FIXES=1` |

P1–P2 change no query semantics; P3–P4 change no response shapes; P5–P6
change no backend behavior.

`.env.example` additions in P2/P3: `API_AUTH_TOKEN=`, `GITHUB_WEBHOOK_SECRET=`,
`POSTGRES_SSLMODE=`, `DB_POOL_MIN=0`, `DB_POOL_MAX=8`.
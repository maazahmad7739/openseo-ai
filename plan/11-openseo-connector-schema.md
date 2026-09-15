# ============================================================
# 11 — OpenSEO Connector: Shared Single-Account Adapter Schema
# ============================================================
# Defines the OpenSEO connector layer used by every job, generator, and agent
# tool call that needs OpenSEO data. Key team correction: OpenSEO is ONE shared
# research tool (like Ahrefs/SEMrush) accessed via a single company-level
# account — NOT a per-brand integration. Only GSC stays per-site
# (site_config.gsc_property). Also: no external MCP access is available; the
# only supported mode is OpenSEO's documented REST API.
#
# The generic fetch()/supports() adapter pattern is kept (no capability-named
# functions) because a second access mode may become relevant later — but no
# second adapter is built until there is an actual second mode to support.
#
# Guiding rule: capability access must be ADDITIVE. A new OpenSEO capability
# (e.g. "content_gaps") requires a new capability string and a response-schema
# entry here — never a new function and never a change to calling code.

# ------------------------------------------------------------
# 1. SINGLE GENERIC INTERFACE
# ------------------------------------------------------------
#   fetch(capability: str, params: dict) -> response
#
# capability is a string identifier, NOT a function name. Current capabilities:
#   "keyword_volume"  — monthly search volume for a keyword / keyword set
#   "serp"            — live top-N results for a query
#   "competitors"     — domain overlap / competitor ranking research
#   "backlinks"       — backlink profile lookup (optional capability)
#   "rank_history"    — historical rank tracking (optional capability)
#   "crawl_audit"     — site crawl / site audit results (rendered vs raw HTML,
#                       indexability, status, crawl depth, …)
#
# params are capability-specific (see §4). Because OpenSEO is one shared
# account, the TARGET (which site's domain / which keyword set) is passed in
# params per call — it never selects an account/credential. Callers that work
# per site pass the site's domain from site_config.domain in params; nothing in
# the adapter is per-site. The response is a normalized, adapter-independent
# shape (see §4).

# ------------------------------------------------------------
# 2. ADAPTER CONTRACT
# ------------------------------------------------------------
# ONE adapter today (REST only — OpenSEO exposes no external MCP interface):
#   src/connectors/openseo_rest_adapter.py   (over OpenSEO's documented REST API)
#
# The contract below is written so a SECOND adapter (for any future access
# mode) must implement the same two methods with identical signatures and
# identical output shapes — but none exists until there is a real second mode.
#
#   supports(capability: str) -> bool
#       True when the shared OpenSEO connection can serve the capability
#       (seeded from the global 'openseo.capabilities' system_config row, not
#       assumed to include every capability for every purpose).
#
#   fetch(capability: str, params: dict) -> response
#       Performs the actual call and returns the normalized response (§4).
#       If supports(capability) would be false, fetch returns a clean, typed
#       "unsupported capability" result (see §2.1) — never a raw crash and
#       never an unhandled exception bubbling into a generator or job.
#
# 2.1 TYPED UNSUPPORTED RESULT
#       {"ok": false, "capability": "backlinks", "error": "unsupported_capability"}
#       Callers treat this as "skip this signal, log it, continue" — the run
#       must not fail.
#
# 2.2 NO CALLER-SIDE ADAPTER BRANCHING
#       Calling code never knows or checks which adapter/adapters exist, and
#       never branches on a provider's raw field names. If a branch like "if
#       the response came from provider field X vs field Y" appears in a
#       caller, the adapter's normalization is incomplete — fix it in the
#       adapter, not the caller.

# ------------------------------------------------------------
# 3. GLOBAL CONFIGURATION DRIVES THE SINGLE ADAPTER
# ------------------------------------------------------------
# OpenSEO config is SYSTEM-global, not per-site. site_config (00-schema.sql)
# carries NO OpenSEO columns — only GSC and domain/business fields. The global
# rows live in system_config (00-schema.sql):
#   'openseo.base_url'      → REST API base URL (non-sensitive)
#   'openseo.secret_ref'    → reference/ID of the ONE company credential in the
#                           secrets store (AWS Secrets Manager / Vault / equiv)
#                           — NEVER the credential value itself. If no secrets
#                           manager is provisioned yet, pgcrypto column-level
#                           encryption at rest on a dedicated column is the
#                           interim fallback — flagged as tech-debt (§7).
#   'openseo.capabilities'  → explicit capability list the shared account
#                           actually provides, e.g.
#                           '["keyword_volume","serp","competitors","crawl_audit"]'
#
# LOADER (resolves the single global credential once):
#   get_openseo_adapter() -> OpenseoRestAdapter
#     1. Read 'openseo.base_url', 'openseo.capabilities', and
#        'openseo.secret_ref' from system_config (NOT site_config).
#     2. Resolve the actual credential by looking up openseo.secret_ref in the
#        secrets store (this is the ONLY place allowed to talk to the secrets
#        store). Load once and reuse for every site's fetch() calls.
#     3. Pass the resolved credential into the adapter constructor.
#     4. NEVER log, cache in plaintext, or persist the resolved credential
#        anywhere beyond the adapter instance's in-memory lifetime for this
#        request/job run.
#     5. Seed supports() from 'openseo.capabilities'.
#     There is no openseo_access_mode to branch on — REST is the only mode.
#
# EVERY OPENSEO CALL PATTERN (enforced for all jobs/generators/agent tools):
#   adapter = get_openseo_adapter()
#   if adapter.supports("serp"):
#       params = {"query": keyword, "domain": site.domain}   # target in params,
#       rows   = adapter.fetch("serp", params)               # not in config
#   else:
#       log.skip(...)      # graceful: skip the signal, do not fail the run
#
# The per-site domain remains in site_config.domain; it is only carried INTO a
# fetch call as the query target. It never selects a different account.

# ------------------------------------------------------------
# 4. FIXED RESPONSE SCHEMAS (adapter-independent)
# ------------------------------------------------------------
# One normalized shape per capability. The REST adapter transforms its raw
# provider payload into this shared shape before returning. Any future adapter
# must return identical shapes.

# 4.1 keyword_volume
#   params: {"keywords": ["...", "..."], "date": "2026-09-01"}
#   response:
#     {"ok": true, "data": [
#         {"keyword": "wireless noise cancelling headphones", "search_volume": 14200, "date": "2026-09-01"}
#     ]}
#   → writes keyword_clusters.search_volume (aggregate across cluster keywords).
#
#   4.1.1 ADDITIVE FIELDS (as-built; superset of the §4.1 minimum)
#   The normalized row also carries "competition" (0-1 float, AdWords
#   competition index) and "cpc" — both returned by the live endpoint's items
#   and used by the catalogue-coverage job to flag commercial_intent
#   (competition >= 0.60). Consumers that only need §4.1's minimum three keys
#   are unaffected.

# 4.2 serp
#   params: {"query": "wireless noise cancelling headphones", "domain": "example.com",
#            "limit": 10, "geo": "us"}
#   response:
#     {"ok": true, "data": [
#         {"position": 1, "url": "https://competitor.com/...", "title": "...", "snippet": "..."}
#     ]}
#   4.2.1 ADDITIVE FIELD (as-built; superset of the §4.2 minimum): each
#   normalized row also carries "query" (the request's query). The connector's
#   sync_serp_snapshots() joins rows back to cluster_queries via it — callers
#   needing only the §4.2 minimum four keys are unaffected.
#   → persists rows into openseo_serp_snapshots (mark is_self=true when the
#     result_url is on the site's own domain). Generator 1's competitor
#     signals read this table.

# 4.3 competitors
#   params: {"domain": "competitor.com", "keyword": "wireless noise cancelling headphones"} | optional {}
#   response:
#     {"ok": true, "data": [
#         {"domain": "big-audio.com", "overlap_score": 0.83, "ranking_keywords_count": 412}
#     ]}
#   → may augment openseo_serp_snapshots or a competitors table; used by the
#     agent's on-site-vs-off-site constraint check.
#
#   4.3.1 IMPLEMENTATION DEVIATIONS (adapter as-built; fixture-driven)
#   The live DataForSEO competitors_domain response has NO per-domain overlap
#   score field. The API returns avg_position, sum_position, intersections and
#   full_domain_metrics.organic.count per item. Therefore:
#   - overlap_score is always null in normalized output (§4.3 consumers must
#     not rely on it; candidates for a derived metric later).
#   - ranking_keywords_count is populated from
#     full_domain_metrics.organic.count.
#   - The target domain itself appears as the LAST item in the live response;
#     the adapter skips it when it matches params["domain"].
#   Consumers relying on overlap_score are: none today (Generator SQL does not
#   read it). If a future consumer needs overlap, derive it from
#   intersections / ranking_keywords_count instead of expecting an API value.

# 4.4 backlinks
#   params: {"urls": ["https://example.com/..."], "limit": 100}
#   response:
#     {"ok": true, "data": [
#         {"target_url": "https://example.com/...", "source_url": "https://...", "anchor": "...", "first_seen": "2026-01-01"}
#     ]}
#   → feeds Skill 2's authority-gap step (documented secondary constraint).
#     Optional capability — skipped cleanly if 'openseo.capabilities' omits it.
#
#   4.4.1 IMPLEMENTATION DEVIATION (adapter as-built)
#   serp items include non-organic types (featured_snippet, paid, organic…).
#   §4.2 does not specify a type filter; as-built the adapter normalizes
#   organic items only and maps position = rank_absolute (absolute SERP
#   position), not rank_group. first_seen in backlinks is truncated to the
#   YYYY-MM-DD date part; field names map url_to→target_url, url_from→source_url.

# 4.5 rank_history
#   params: {"keywords": ["..."], "start": "2026-08-01", "end": "2026-09-01"}
#   response:
#     {"ok": true, "data": [
#         {"keyword": "...", "date": "2026-08-15", "position": 11.2, "url": "https://example.com/..."}
#     ]}
#   → validates position movement without re-fetching live SERP.

# 4.6 crawl_audit
#   params: {"site": "example.com"} | {"urls": ["..."]}
#   response:
#     {"ok": true, "data": [
#         {"url": "https://example.com/...", "status_code": 200, "indexable": true,
#          "canonical": "https://example.com/...", "page_type": "collection",
#          "template": "collection.liquid", "crawl_depth": 2,
#          "internal_links_in": 15, "internal_links_out": 24,
#          "raw_html_hash": "…", "rendered_html_hash": "…", "render_status": "js_rendered",
#          "structured_data": true}
#     ]}
#   → populates pages.* incl. render_status / raw vs rendered hashes used by
#     Generator 3's js_rendering_failure detection. weekly_crawl.py pulls THIS —
#     the platform never runs its own crawler.

# ------------------------------------------------------------
# 5. WHERE THIS PLUGS INTO THE EXISTING PLAN
# ------------------------------------------------------------
#   - Generator 1 competitor signals: openseo_serp_snapshots is populated by
#     the connector job via fetch("serp") / fetch("competitors") — the
#     generator SQL never talks to the OpenSEO REST API directly.
#   - keyword_clusters.search_volume job: fetch("keyword_volume").
#   - weekly_crawl.py OpenSEO audit pull: fetch("crawl_audit").
#   - Agent validation may reuse openseo_serp_snapshots or call
#     fetch("serp") through the adapter for live confirmation.
#   - All callers go through get_openseo_adapter() — the correction to one
#     global credential + one REST adapter is INVISIBLE to them (they never
#     touched site_config credentials or branched on access mode).
#   - Future capabilities (backlinks, rank_history, content_gaps, …) add a
#     capability string + a §4 schema entry and wire into existing tables —
#     no new function signatures, no changes to generator/job call sites.

# ------------------------------------------------------------
# 6. ACCEPTANCE CHECKLIST
# ------------------------------------------------------------
#   □ No function is named after a capability (no get_serp, get_backlinks…).
#   □ openseo_rest_adapter.py implements supports() + fetch() with the §4
#     output shapes. There is NO openseo_mcp_adapter.py anywhere in plan/.
#   □ get_openseo_adapter() (no site_id) resolves the ONE global credential
#     from system_config + the secrets store and reuses it for every fetch().
#   □ site_config contains NO OpenSEO credential, capability-list, base-url, or
#     access-mode columns — only GSC and domain/business fields remain; global
#     OpenSEO config lives in system_config.
#   □ get_openseo_adapter() is the ONLY code path that resolves a credential;
#     no other code reads openseo.secret_ref or talks to the secrets store.
#   □ A missing capability (not in 'openseo.capabilities') never crashes a job —
#     it is skipped and logged.
#   □ No log statement, error message, or stored JSON blob anywhere in the
#     connector layer contains the resolved credential value.
#   □ A migration exists (see §7.1 / 12-migration-openseo-secrets.sql) that
#     creates/collapses to a SINGLE global secret ref and drops any legacy
#     per-site openseo column.
#   □ Response schemas for every capability in current use are documented here.

# ------------------------------------------------------------
# 7. CREDENTIAL STORAGE (security fix)
# ------------------------------------------------------------
# Raw credentials must never live in the application database. The ONE global
# credential is referenced by system_config('openseo.secret_ref'); the value
# lives in the secrets store.
#
# STORAGE MODEL (unambiguous):
#   TARGET STATE — an external secrets manager (AWS Secrets Manager / HashiCorp
#   Vault / equivalent). openseo.secret_ref is that store's key/ARN/path.
#   get_openseo_adapter() resolves it at load time via the store's API.
#
#   INTERIM FALLBACK — if no secrets manager is provisioned, the platform
#   stores the single credential as a pgcrypto column-level-encrypted BYTEA
#   value (key held outside the DB via env var / KMS). This is explicitly
#   flagged as TECHNICAL DEBT: it does not meet the access-boundary or rotation
#   requirements of a real secrets store. Tech-debt remediation: provision a
#   secrets manager, move the value there, point openseo.secret_ref at it, and
#   remove pgcrypto as a dependency.
#
# See 00-schema.sql (system_config) and §7.1 for the migration.

# 7.1 CREDENTIAL MIGRATION (one-time)
# Background: earlier drafts stored a secret ref PER SITE. Real model is ONE
# shared OpenSEO account, so the migration collapses any legacy per-site refs
# into a single global row and drops the per-site column. The staging-completeness
# check confirms one GLOBAL credential exists — not one per configured site.
#
# STEP-BY-STEP:
#   STEP 1 — external (app code / runbook): write the ONE company OpenSEO
#     credential to the secrets store under a single global key, e.g.
#       secrets_store.put('openseo/company/credential', <api_key>)
#     If using the pgcrypto interim fallback (no secrets manager), encrypt the
#     single credential with a key outside the DB — flagged tech-debt (§7).
#
#   STEP 2 — SQL migration (12-migration-openseo-secrets.sql):
#     a. If a legacy site_config.openseo_secret_ref column exists: assert it
#        holds at most ONE distinct value across all sites (every site used
#        the shared account); backfill
#        system_config('openseo.secret_ref') from it; then DROP the column.
#     b. If no legacy value exists: INSERT system_config('openseo.secret_ref',
#        <key from Step 1>).
#     c. VERIFY (exactly-one check): count of system_config rows with
#        config_key='openseo.secret_ref' MUST equal 1 — RAISE otherwise.
#        Do NOT proceed to any dependent job with 0 or >1 global refs.
#     d. Provision 'openseo.base_url' and 'openseo.capabilities' rows the
#        same way (non-sensitive, plain values).
#
#   STEP 3 — rotate/verify: confirm no job log, error, or JSON blob echoes a
#     credential; then optionally rotate the API key so the migrated secret is
#     not also present in old backups.
# 35 - Off-page multi-platform expansion — architecture specs

**Status:** draft for review (migration: `plan/35-offpage-multi-platform-expansion.sql`).
**Builds on:** plan/33 (off-page pipeline), plan/34 (operator queue + publication), the verify sweep + scheduler minutes cadence.
**Decisions locked with the user:**
1. The applied `plan/34-offpage-operator-queue.sql` keeps its number; this expansion is **plan/35**.
2. Discovery adds a **rate-limited web-search seam** (for Shopify Community, and X where the DataForSEO SERP contract cannot resolve a post), in addition to the existing DataForSEO SERP reads and the `operator_import` escape hatch.

---

## 1. Goals

Deliver three off-page channels on the *same* pipeline, with no regression of the four safety invariants:

| Channel | What it is | What changes |
|---|---|---|
| **X (Twitter)** | Reply-to-post outreach for brands | New platform value; hard character ceilings; DataForSEO SERP discovery; aged-account/standing gates; never automated |
| **Medium long-form** | Authority articles (thought-leadership) with contextual backlinks | New content *kind* (`post_kind='article'`); long-form review surface; grounding identical to replies |
| **Shopify Community** | Merchant help Q&A where the client's Shopify app genuinely solves the problem | New platform + a new rate-limited web-search discovery seam; **fit gate** refuses promotional mentions without real app–merchant match |

## 2. Invariants preserved (unshaded)

- **Tenant isolation is structural.** Every new FK is composite `(site_id, <parent_pk>)` against the existing unique tenant indexes. `site_id` is `NOT NULL` on every new table. No NULL escape remains.
- **Human-in-the-loop.** Nothing in the system presses a platform button. Discovery + drafting are automated; publishing is a human pasting the copy and submitting the public permalink (existing gate-2 / permalink surface).
- **Strict grounding.** Both `reply` and `article` drafts draw facts only from the tenant's own corpus via `src/fixes/offpage_grounding.load_brand_corpus`. `grounding_corpus_hash` + `grounding_page_ids` freeze provenance at draft time. Long-form backlinks must resolve to members of `grounding_page_ids`.
- **Zero API-identity fingerprinting.** No platform write API is ever called; no authenticated surface is scraped. Discovery is a public search surface read under the tenant's own rate ceiling. `offpage_search_log` records calls, never credentials/cookies/session identity.

## 3. Schema delta (map to `plan/35...sql`)

| Change | Why |
|---|---|
| Widen `offpage_communities.platform` / `offpage_accounts.platform` CHECK to add `'x'`, `'shopify_community'` | Both must widen or `x` accounts could never be created |
| `offpage_threads.post_kind` (`'reply'` \| `'article'`, default `'reply'`) | One pipeline, two artifacts; Medium long-form reuses draft/queue/publication/verification |
| `offpage_threads.discovery_query TEXT` | Record which tenant keyword surfaced the thread |
| Widen `offpage_threads.context_source` to add `'web_search'` | Distinguish web-search seam context from SERP for the context-sufficiency gate + audit |
| `offpage_platform_settings` (PK `(site_id, platform)`) | Per-tenant, per-platform control + gate knobs; `enabled=false` default (fail-closed), the same posture as fix_policy |
| `offpage_search_log` | Discovery audit: query, seam, provider, count, latency |
| `offpage_search_quota` (PK `(site_id, platform, window_start)`) | Atomic rate-limit state for the web-search seam |

No changes to `offpage_drafts`, `offpage_publications`, `offpage_verifications`, or `offpage_events` — the 35 migration only *widens* what they may carry and adds the newest tables. `recommendations.generator` stays `'offpage_community'`; new channels are distinguished by `platform` + `post_kind` inside evidence_json.

## 4. Platform-specific gates

All gates are evaluated in the drafter (`src/fixes/offpage_drafter.py::draft_offpage_reply`) and recorded in `offpage_events` on pass *and* refuse (the plan/33 "a log that records only refusals is not an audit trail" rule). New gates below extend the existing HARD/SOFT split.

| Gate | Channel | Behaviour recorded | HARD/SOFT |
|---|---|---|---|
| `x_char_limit` | X | reply/post exceeds `reply_char_limit`/`post_char_limit` (280, conservative) → refuse. A reply over 280 cannot be pasted as-is | HARD |
| `x_account_standing` | X | account `account_created_on` < 90 days, or standing unassessed/`none` → refuse promotional; advice-only may continue if an active account exists | SOFT (promo) |
| `medium_longform_grounded` | Medium (article) | article outside the 650–1500 content-word window, any paragraph block below the 0.35 grounding floor, or a backlink outside `grounding_page_ids` → refuse. Grounding is measured at **paragraph-block level** (paragraph, or a 4-sentence group on a wall-of-text response) so transitional sentences do not false-positive while each thematic section stays corpus-anchored. ONE bounded corrective re-ask precedes the final gate decision: `_call_model` repeats with `revision_feedback` naming the classified failures (word-count window / insufficient corpus grounding / backlink requirement) and the offending block previews; the gates — not the re-ask — remain the authority and re-decide on the fixed answer. A single sample that passes needs no re-ask | HARD |
| `shopify_community_fit` | Shopify Community | client platform ≠ `site_config.platform='shopify'`, or `targeting.shopify` fit_keywords absent, or thread title/snippet does not map to the app's category → refuse promotional mention. Advice-only is allowed (helpful content is how standing is built) | SOFT (promo) |
| `platform_promo_cap` | all new | `offpage_platform_settings.promotional_posts_per_30d` reached → refuse; third brake alongside account cap and community cap re-checked at choose/permalink time | SOFT |

Existing gates (`ai_content_allowed`, `context_sufficient`, `grounding_corpus`, `community_allows_promotion`, `thread_allows_promotion`, `account_standing`, `promotional_cap`, `answer_useful`, `mention_grounded`, `mention_necessary`) apply unchanged. The **context-sufficiency gate** gets a new, honest behaviour for X/Shopify threads found by n-way search snippet: same as today, a thin snippet → drop, because the operator_import escape hatch stays the documented remedy.

X account facts (age, karma) stay **operator-asserted** unless `last_verified_at` is set — the plan/33 rule that a NULL means "never verified", not "verified". We never call the X API to check.

## 5. Discovery seam design

### Layering of brakes (all must trip independently)
1. `offpage_platform_settings.enabled` AND `discovery_seams` non-empty → else the leg **does not run** (fail-closed).
2. Web-search seam: `offpage_search_quota` atomic upsert caps calls per `(site_id, platform, window)` (hourly). DataForSEO legs continue through `connectors.costlog.check_budget` *before* the call (plan/23 #5).
3. Every search logs a row in `offpage_search_log` (seam, provider, count, http_status, latency).
4. `offpage_communities.enabled=false` kill-switch (verify 3-strike) also blocks discovery minting new rows — unchanged from plan/33.

### Per-channel discovery
- **X**: DataForSEO SERP for `site_config.target_keywords` + `site:x.com` (or `site:twitter.com`). Snippet is the post text (often thin → context gate does its job). No scraping of any timeline; no follower/API reads.
- **Medium**: DataForSEO SERP for topic + `site:medium.com`; the "thread" is the topic row with `post_kind='article'`, `community_id` = the intended tag/publication. `targeting.medium.publications` steers which communities the operator associates.
- **Shopify Community** (web-search seam): query shaped from an app-client's `targeting.shopify` (`app_name`, `app_category`, `fit_keywords`) + `community.shopify.com` site term, e.g. `community.shopify.com <app_category> <issue>`. Snapshot → `offpage_threads` with `context_source='web_search'`.

### New scheduler job
`offpage_discovery` (daily, after the site sync) iterates enabled `(site_id, platform)` legs, enforces §5 layering, upserts communities/threads (`ON CONFLICT (site_id, thread_url) DO NOTHING`, `data_source='live'`), and mirrors the existing per-tenant budget posture. Registration follows the fixed-daily pattern already used (`SCHEDULE` in `src/jobs/scheduler.py`), not the minutes cadence — discovery is cheap and bounded, verification is the frequency-sensitive path.

## 6. Workflow designs

### X — reply-to-post
```
DataForSEO SERP (site:x.com) → thread (post_kind='reply')
  → drafter (context gate, char cap, account standing, promo caps)
  → operator queue card (char counter, advice/mention split)
  → human copies & pastes → permalink → verify sweep (+15m/+2h/+24h/+72h)
```
Daily X ceiling per tenant from `discovery_calls_per_day`; per-account promotional ceiling from plan/33 caps; `x_char_limit` enforced at draft time so nothing paste-unfriendly is ever offered.

### Medium — long-form article
```
target_keywords + site:medium.com → topic thread (post_kind='article')
  → drafter: corpus-grounded long-form (advice = the article body;
    mention = the contextual backlink among grounding_page_ids)
  → operator queue card in EDITOR mode (render vs comment-style reply)
  → human edits/publishes in Medium → permalink → same verify sweep
```
`min_words` and the grounded-sentence tripwire make a thin, fact-free "article" unrepresentable. `promo_density`, `helpfulness_score`, `brand_mention_count`, `link_count` are already produced by the drafter and render on the queue card for both kinds.

### Shopify Community — merchant Q&A
```
web-search seam → thread (post_kind='reply', context_source='web_search')
  → drafter: fit gate (app category ↔ thread topic) BEFORE any mention
  → helpful advice always allowed → promo only on proven fit + all caps
  → operator pastes reply → permalink → verify sweep
```
High helpfulness is structural, not aspirational: the fit gate withholds promotion on weak matches, and the existing ceiling-only caps (never a ratio to game — plan/33 §"A floor is a target") apply across platforms through `offpage_platform_settings`.

## 7. Code changes (after plan approval — not in this migration)

- `src/api/routes/offpage.py::_PLATFORM_HOSTS`: add `'x' → ('x.com','twitter.com')`, `'shopify_community' → 'community.shopify.com'`; `_validate_permalink` cross-host rejection then covers the new platforms automatically.
- `src/jobs/offpage_verify.py`: host map for verification permalinks extended the same way (it must agree with the routes).
- `src/fixes/offpage_drafter.py`: new gates of §4 behind `post_kind`/`platform`; long-form Seq for `post_kind='article'`; char-limit truncation must never run before the char gate (HARD = refuse, not silently shorten).
- `src/jobs/scheduler.py`: register `offpage_discovery` daily.
- New `src/jobs/offpage_discovery.py` (+ seam dispatcher): enforces §5 layering, quota upsert, search-log write, staged upserts.
- `src/api/routes/offpage.py` + `src/api/models/schemas.py`: queue card carries `post_kind` (**mode comp** editor/library rendering), platform settings edit surface, discovery health (`v_offpage_search_spend`).
- Operator UI (`apps/openseo-plus/.../offpage/`): reply vs article render paths, char counters, fit-gate evidence display, per-platform settings panel.

## 8. Test plan additions

Extend the existing conventions instead of forking them (`tests/run_offpage_queue_tests.py`, `tests/run_scheduler_tests.py`):

- X: char-cap gate blocks >280; aged-account gate splits helpful/promotional; DataForSEO discovery leg honours `discovery_calls_per_day`; platform settings `enabled=false` blocks the leg.
- Medium: article drafts satisfy `min_words` + grounded-sentence tripwire; backlink ⊆ `grounding_page_ids`; mode comp renders article vs reply; a thin corpus still refuses via `grounding_corpus`.
- Shopify Community: fit gate withholds promotion on non-app clients and on topic→category mismatch; web-search seam rate limit (quota upsert) holds under concurrent legs; search log rows written per call.
- Cross-platform: permalink host validation rejects x.com permalinks on a medium card and vice versa; verify sweep host map agrees; new tables carry composite tenant FKs (a mixed-tenant insert is refused, not possible).
- Full suite stays green: `python scripts\run_tests.py` (currently 45 passed / 0 failed / 2 skipped — live LLM + DataForSEO).

## 9. Rollout order

1. Apply plan/35 migration (idempotent; no behaviour change until settings are enabled — the `enabled=false` default is the safety net).
2. Ship discovery seam + `offpage_discovery` job disabled-first.
3. Ship drafter gates + long-form path behind `post_kind`.
4. Extend API/verify host maps + queue UI modes.
5. Tests, full suite, then opt-in per-tenant via `offpage_platform_settings`.

## 10. Open questions (for review)

- X reply limit: **280** advised as the conservative ceiling; should the ceiling be per-account/tenant-overridable upward (paid-tier long posts) without reaching published limits?
- Medium `min_words` default **800**, and article backlink count cap (suggested **1** per article in v1) — confirm.
- Shopify Community fit evidence: keep it a `shopify_community_fit` gate over title/snippet + `targeting.shopify.fit_keywords` (current design), or add a per-thread operator-asserted `fit_asserted` flag for edge cases the keyword matcher cannot see?
- Should the web-search seam be a real provider integration now (DataForSEO web-search endpoint is the natural one and already budget-gated), or a pluggable stub that ships disabled until a tenant explicitly asks?
# Plan 25 — `collection_create` Auto-Fix Sub-Type (Missing Collection Pages)
# ============================================================
# Version: 0.1 (DRAFT for review) · Date: 2026-09-24 · Status: PLAN
#
# Closes the loop the drawer exposes today: a `missing_page` recommendation
# (/collections/snowboard-stomp-pad) shows the `⚡ Auto-fix` badge but can
# never produce a fix — every current generator requires a pages row +
# shopify_gid, and `collection_create` has no adapter. This plan makes
# missing-page tasks executable end-to-end: draft -> diff -> Approve &
# Queue -> Apply now -> collectionCreate + publishablePublish.
#
# Grounded against main (every file/claim verified in code):
#   * generator='missing_page', action_type='create_page', target_url IS
#     NULL, proposed_url='/collections/<slug>' (orchestrator.py:80-107,
#     plan/01:131-145)
#   * load_decision_inputs raises "target_url not in pages table" for
#     these rows today (generator.py:728-730) — the hard block
#   * queue.py classifies the task text automated (it contains "meta
#     description") but maps it to the WRONG sub_type (seo.description,
#     queue.py:74-79)
#   * `collection_create` is already policy-seeded (plan/22:107:
#     high/2/field-verify) and scope-mapped (shopify.py:708 ->
#     collectionCreate, write_products) — no seed/mapping changes needed
#
# Write target VERIFIED against Shopify Admin GraphQL 2026-01 docs
# (fetched 2026-09-24, shopify.dev CollectionInput + collectionCreate):
#   * CollectionInput carries: title (REQUIRED for create), handle,
#     descriptionHtml, seo (SEOInput!), products ([ID!]! — "Only valid
#     with collectionCreate"), ruleSet/sortOrder/templateSuffix
#   * collectionCreate(input: CollectionInput!) — scope write_products;
#     store must not be Starter/Retail
#   * DEPRECATION note on 2026-01 docs: the `input:` argument is
#     deprecated in favor of `collection:` + `sources`; `input:` remains
#     functional and is used in every official 2026-01 example. Decision:
#     pin `collection:` (the forward-compatible arg) — and RE-VERIFY
#     plan/24's collectionUpdate mutation string live at the same time
#     (its tests run against a fake client only).
#   * "The created collection is unpublished by default" (verbatim doc
#     note) -> publishablePublish afterwards (matches the verified
#     comment already in shopify.py:360-362)
#
# NON-NEGOTIABLES (inherited from plan/21):
#   1. Nothing publishes without the two approval gates (rec + diff).
#   2. Grounding: every fact in title/description names its data source.
#   3. Revert NEVER deletes the created entity — publishableUnpublish
#      only (the store's pre-fix state was "no public collection";
#      unpublish restores exactly that without destroying operator data).
# ============================================================

---

## 0. Summary

New auto-fix sub_type **`collection_create`** — the FIRST fix path that
runs with NO existing entity:

- WHO: approved `create_page` recommendations (generator `missing_page`)
  whose cluster has real catalogue depth.
- WHAT: one `collectionCreate` payload carrying `title`, `handle`,
  `descriptionHtml`, `seo {title, description}`, and `products` (member
  GIDs) in a SINGLE mutation, then `publishablePublish`.
- Reuses: the plan/24 validators verbatim (`sanitize_collection_html`,
  `validate_collection_draft`, `_grounding_corpus_text`,
  `draft_collection_description`), the executor state machine, policy
  kill-switch, and the drawer diff pipeline (no new UI components —
  only `FIELD_LABELS` + placeholder text additions).

sub_type: **`collection_create`** (string already used by the policy
seed and scope mapping — zero registry churn).

---

## 1. Grounding Sources & Generator Logic for Missing Pages

### 1.1 New branch in `load_decision_inputs` (generator.py)

Today every action_type path requires a `pages` row. Add a
`create_page` branch that:

1. Requires `rec["proposed_url"]` (raise `FixNotSupported` when absent —
   a create_page rec without a URL is a data anomaly, never guessed).
2. Sets `rec["target_url"] = rec["proposed_url"]` for the rest of the
   pipeline (conflict guard + diff keys stay uniform).
3. Reads NO pages row. Instead loads the grounding record:
   - `keyword_clusters.primary_keyword`, `.intent` (cluster_id is set
     on every missing_page rec — orchestrator.py:93).
   - `catalogue_coverage` row for `(site_id, cluster_id)`:
     `matching_product_ids`, `matching_product_count`,
     `in_stock_product_count`, `existing_collection_url`.
   - member product pages by GID (same join as plan/24 §1:
     `pages.shopify_gid = ANY('gid://shopify/Product/' || unnest(...))`)
     — titles + body sentences feed the drafter.
   - `site_config.domain` (handle/uniqueness checks).
4. Sets `rec["shopify_gid"] = None` and marks `rec["route"]="create"` —
   downstream code must treat a None GID as EXPECTED here (the GID is
   minted by Shopify at execute time and persisted via snapshot_patch).

### 1.2 Decision gates (deterministic, all must pass)

| # | Gate | Rule | Source |
|---|---|---|---|
| 1 | Depth floor | `matching_product_count >= 10` (plan/06:90 "10+ matching products"; plan/05:78 rejects thinner) | catalogue_coverage |
| 2 | Still missing | `pages` has NO row at `proposed_url` at generation AND at pickup — if one appeared, the rec is stale → `FixNotSupported("page already exists — improve_page path covers it")` | pages |
| 3 | No cannibalization | no indexable page already targeting the cluster's primary keyword (ILIKE on pages.title) | pages |
| 4 | Handle collision | slugified handle must not collide with any existing collection handle in `pages` (`/collections/<handle>` self-join) — collision → `FixNotSupported` (NEVER auto-variant; the operator renames or the rec regenerates) | pages |
| 5 | existing_collection_url | if `catalogue_coverage.existing_collection_url` is set, refuse (the cluster HAS a page; this fix is only for the truly-missing case) | catalogue_coverage |
| 6 | Shared suppressions | protect-winner N/A (no page) but consolidate-suppression check applies unchanged (an active consolidate rec on the cluster suppresses creation) | recommendations |

### 1.3 Grounding corpus

`_grounding_corpus_text(conn, rec)` works unchanged — it joins member
products via catalogue_coverage and the cluster keyword; the collection's
"own body text" leg is empty (no page yet), which the function already
tolerates. Facts available to the draft: cluster keyword + intent,
member-product titles and their own body sentences. NO competitor facts
(SERP grounds ordering only — plan/24 §1.1 invariant carried over).

### 1.4 Draft values (single-pass, all four surfaces)

1. **handle** — slug from `primary_keyword` (the EXACT rule plan/01:131
   used to mint `proposed_url`): `regexp_replace(lower(trim(kw)),
   '[^a-z0-9]+', '-', 'g')`. The proposed_url and the handle agree by
   construction.
2. **title** — `draft_title`-shaped but collection-framed: keyword +
   dominant competitor framing pattern, capped `TITLE_MAX_CHARS` (60),
   keyword in first half, all existing title validators
   (`validate_title_draft`) reused unchanged.
3. **descriptionHtml** — `draft_collection_description(rec,
   members=members)` + `validate_collection_draft` (150-word floor,
   overlap tripwire, sanitizer) — verbatim from plan/24 §3.2.
4. **seo.title / seo.description** — reuse `draft_title` /
   `draft_meta_description` + their validators (`validate_meta_draft`,
   `duplicate_meta_check`). Grounded on cluster keyword + member facts
   (the collection has no body of its own yet; the description draft IS
   the body).

`draft_meta_description` needs one collection-aware tweak: its
page-type tail already handles `page_type == 'collection'`
(generator.py:1276-1279); the create path passes
`page_type='collection'` explicitly.

### 1.4.1 Validators summary (all pre-INSERT, deterministic)

- title: `validate_title_draft` (length/keyword-first-half/denylist/brand).
- meta: `validate_meta_draft` + `duplicate_meta_check` (site-wide).
- description: `sanitize_collection_html` + `validate_collection_draft`.
- handle: `^[a-z0-9-]+$`, ≤ 255 chars, no leading/trailing dash.
- cross-field: keyword must appear in title AND meta AND stripped body.

---

## 2. Single-Pass `collectionCreate` Payload (generator.py)

```python
COLLECTION_CREATE_DOC_URL = (
    "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/collectionCreate"
)

def build_collection_create_payload(proposed_url, title, handle,
                                    description_html, seo_title,
                                    seo_description, product_gids):
    return {
        "mutation": "collectionCreate",
        "variables": {"collection": {
            "title": title,
            "handle": handle,
            "descriptionHtml": description_html,
            "seo": {"title": seo_title, "description": seo_description},
            "products": product_gids,          # [gid://shopify/Product/...]
        }},
        "scope_required": "write_products",
        "doc_url": COLLECTION_CREATE_DOC_URL,
    }
```

Notes (all verified on the 2026-01 CollectionInput page):
- `products` is legal ONLY on `collectionCreate` — perfect fit; attach
  the full member set (`matching_product_ids` → GIDs, cap 50 per call
  hygiene; >50 truncates with a grounding note, never silently).
- NO `ruleSet` — smart-collection rules are a separate risk surface
  (async job semantics); this fix creates a CUSTOM collection with an
  explicit product list. Operator can convert later by hand.
- `target_entity_ref` stays NULL at generation (no GID exists); the
  adapter records the created GID via `snapshot_patch`
  (fix_executor.py:200-217 — the exact pattern the redirect adapter
  uses for `redirect.created_id`).
- diff keys the conflict guard on `proposed_url` as `target_url` —
  `uq_fixes_active_per_target` semantics unchanged.
- `seo` here is the COLLECTION's own seo object (CollectionInput.seo);
  unlike ProductUpdateInput there is NO sibling-wipe hazard on create
  (nothing pre-exists to wipe).

---

## 3. Adapter Workflow — collectionCreate → publishablePublish

### 3.1 Mutations (adapters.py)

```graphql
mutation FixCollectionCreate($collection: CollectionInput!) {
  collectionCreate(collection: $collection) {
    collection { id title handle descriptionHtml seo { title description } }
    userErrors { field message }
  }
}
```

(Publish step reuses the existing `COLLECTION_PUBLISH_MUTATION` —
`publishablePublish(id, input:[{publicationId}])`, publication id from
the cached `_publication_id_from_config` path, identical to the
`collection_publish` adapter at adapters.py:958.)

### 3.2 Payload validator

Strict allowlist: exactly `{title, handle, descriptionHtml, seo,
products}`; `seo` limited to `{title, description}`; every product id
must match `gid://shopify/Product/<digits>`; anything else
(`ruleSet`, `templateSuffix`, `redirectNewHandle`, `image`,
`metafields`, `publications`) → `payload_invalid` BEFORE any network
call (plan/24 §2.2 discipline).

### 3.3 Execute sequence (two-phase, typed outcomes)

```
1. collectionCreate  -> userErrors? -> typed failure (nothing to revert)
2. snapshot_patch: collection.created_gid (executor persists it —
   the ONLY durable record of what this fix created)
3. publishablePublish(publicationId from cache) -> publish fails?
   -> outcome "create_publish_pending": collection EXISTS but is
   unpublished. NOT a failure to auto-undo: the entity is operator-
   visible in admin; the executor marks the fix failed with the created
   GID in the detail so a human decides (publish by hand or delete).
4. read-back verify: collectionByHandle(handle) -> title +
   publishedOnPublication == true -> verified
```

### 3.4 Snapshot semantics (executor deviation — the one special case)

`_execute_one` treats a failed fresh-read snapshot as `snapshot_failed`
(fix_executor.py:146-156). For `collection_create` the PRE-state is
"entity does not exist" — the snapshot adapter MUST return:

```python
{"ok": True, "snapshot": {"exists": False,
                          "handle": <handle>,
                          "read_at_api_version": ...}}
```

`collectionByHandle` returning null collection = VALID pre-state, not
an error. A collection FOUND at the handle pre-write = the stale-rec
case → snapshot `{"exists": True}` and the executor's stale-diff guard
fails the fix (`already_exists`) BEFORE the create fires. This is the
only sub_type where the stale guard runs in the POSITIVE direction.

### 3.5 Restore (revert) — strictly unpublish, never delete

```python
def collection_create_restore(conn, fix_row, config, client=None):
    # snapshot.created_gid required (refuse = no_snapshot, never guess)
    # 1. publishableUnpublish(created_gid, publicationId)
    # 2. read-back publishedOnPublication == False -> restored
    # NEVER collectionDelete — the operator may have already enriched
    # the collection (products, images, rules) between apply and revert;
    # deleting would destroy that work. Unpublish returns the storefront
    # to the exact pre-fix public state.
```

Executor pickup snapshot for the revert path: fresh
`publishedOnPublication` read (same shape as
`collection_publish_snapshot`, adapters.py:1020).

### 3.6 Registration

- `ADAPTERS["collection_create"] = {execute, restore, snapshot}` —
  new block appended after the plan/24 section.
- `required_scopes_for_sub_types`: entry EXISTS (`shopify.py:708`) —
  but it maps to `["collectionCreate"]` only; the publish step needs
  `write_publications` too. Update to the union:
  `"collection_create": ["collectionCreate", "publishablePublish"]`
  (the mapping supports unions — see `product_publish`, shopify.py:701).
- `MUTATION_REGISTRY`: both mutations already registered.
- `fix_policy` seed already exists (plan/22:107 — high/2/true/disabled).

---

## 4. Task Routing (queue.py)

The drawer's task-1 text ("Create a collection page at
/collections/snowboard-stomp-pad, add product listings, write
SEO-optimized title, meta description…") currently:

- classifies `automated` (contains "meta description") — correct by
  luck;
- maps to sub_type `seo.description` (queue.py:74-77) — WRONG, and it
  shadows the create-page semantics.

Fix in `enrich_work_tasks` (order matters — create-page check FIRST):

```python
def _sub_type_for_task(lowered):
    if ("create a collection" in lowered or "create page" in lowered
            or "missing page" in lowered
            or "/collections/" in lowered and "create" in lowered):
        return "collection_create"
    if "redirect" in lowered or "301" in lowered:
        return "redirect"
    if "meta description" in lowered or "description" in lowered:
        return "seo.description"
    return "seo.title"
```

Also extend `_AUTOMATED_TASK_PATTERNS` with `"create a collection"` and
`"create page"` (the current task text classifies automated only
because of the incidental "meta description" phrase — make it
intentional).

Cross-check the `FixNotSupported` UX: for `create_page` recs,
`draft-fixes` currently tries seo.title/seo.description/
collection_description — all fail (no pages row) and land in
`unsupported`. After this plan, the tuple list gains
`("collection_create", generate_collection_create_fix_for_recommendation,
{})` which SUCCEEDS, so the drawer shows the diff and the
`Approve & Queue` / `Reject` buttons appear; after approve, `Apply now`.
No drawer logic changes required for the buttons themselves.

---

## 5. Drawer Diff Preview — rendering the proposed collection

The diff is a MULTI-field array (the drawer already renders one card
per array entry — `diff_json.map(DiffRow)`, WorkTaskList.tsx:299-306):

```json
[
  {"field": "collection.title",       "old_value": null,
   "new_value": "Snowboard Stomp Pad"},
  {"field": "collection.handle",      "old_value": null,
   "new_value": "snowboard-stomp-pad"},
  {"field": "collection.seo_title",   "old_value": null,
   "new_value": "Snowboard Stomp Pad — protect deck & tail (≤60 chars)"},
  {"field": "collection.seo_description", "old_value": null,
   "new_value": "…70-155 char meta…"},
  {"field": "collection.description_html", "old_value": null,
   "new_value": "<p>150+ word grounded HTML body…</p>"},
  {"field": "collection.products",    "old_value": null,
   "new_value": "13 member products attached"}
]
```

UI additions (WorkTaskList.tsx, all small):

1. `FIELD_LABELS` gains:
   - `"collection.title": "Collection title"`
   - `"collection.handle": "URL handle"`
   - `"collection.seo_title": "SEO title"`
   - `"collection.seo_description": "SEO description"`
   (`collection.description_html` already exists from plan/24.)
2. Empty-old placeholder: the current line says "no previous
   description" (WorkTaskList.tsx:127) — generalize to
   `"nothing yet — this fix creates it"` for create-type fields
   (field-name-keyed label map so the description row keeps its
   specific wording).
3. Rendering rules per field:
   - `description_html`: existing stripped-text + 400-char expander
     (plan/24 §4.2) — untouched.
   - `title`/`seo_title`/`seo_description`: plain text rows (existing
     DiffRow behavior).
   - `handle`: plain text row, prefixed visually with `/collections/`.
   - `products`: rendered as a muted COUNT SUMMARY line
     ("13 member products will be attached") — the GID list itself is
     never dumped into the drawer.
4. The grounding block (`payload_json.grounding`) rides the same as
   plan/24: member count, draft sources, corpus size — the operator
   sees WHAT the copy is grounded in before approving.

After `Apply now`, the fix carries `collection.created_gid` in
snapshot_json; a follow-up daily sync ingests the new page into
`pages` (with `shopify_gid`), which is what the measurement pipeline
and the acceptance criteria ("returns HTTP 200, indexed within 7
days") observe. The executor's change_log row
(fix_executor.py:253-259) records before/after as usual.

---

## 6. Files Touched (implementation checklist)

| File | Change |
|---|---|
| `src/fixes/generator.py` | `load_decision_inputs` create_page branch (§1.1); depth/existence/handle gates (§1.2); `draft_collection_create` (assembles title/handle/seo/description via existing drafters); `build_collection_create_payload`; `generate_collection_create_fix_for_recommendation` (idempotent, conflict-guarded, INSERT sub_type='collection_create', target_url=proposed_url, risk 'high') |
| `src/fixes/adapters.py` | `COLLECTION_CREATE_MUTATION`; `_collection_create_payload_for`; `collection_create_execute` (create → snapshot_patch → publish → verify); `collection_create_snapshot` (exists-False semantics); `collection_create_restore` (unpublish-only); ADAPTERS entry |
| `src/connectors/shopify.py` | one mapping line: union scope for `collection_create` |
| `src/api/routes/fixes.py` | `draft_fixes` tuple gains collection_create; dedicated `POST /recommendations/{id}/collection-create-fix` route mirroring `/publish-fix` |
| `src/api/routes/queue.py` | `_sub_type_for_task` create-page branch FIRST + `_AUTOMATED_TASK_PATTERNS` additions |
| `apps/openseo-plus/.../WorkTaskList.tsx` | 4 `FIELD_LABELS` entries; generalized empty-old placeholder |
| `tests/run_fix_collection_create_tests.py` | mirrors the plan/24 suite: gates (depth/exists/handle), payload shape, create+publish via fake client, publish-failure typed outcome, unpublish-only restore, executor pickup, queue classification |

## 7. Explicit non-goals

- No smart-collection `ruleSet` writes (separate surface, async job).
- No image/templateSuffix on create (operator enriches by hand).
- No auto-delete on revert — unpublish only (§3.5).
- No auto-execute at ship: `fix_policy.enabled=false` for
  `collection_create` until the operator flips it (high risk, 2/week).
- No canonical/internal-link work — that stays task 2 (Manual) in the
  drawer; the acceptance criteria ride the recommendation, not the fix.
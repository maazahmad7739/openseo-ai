# Plan 24 — `collection_description` Auto-Fix Sub-Type
# ============================================================
# Version: 0.1 (DRAFT for review) · Date: 2026-09-24 · Status: PLAN
#
# Extends plan/21 §2.2 (improve_page content fixes) to collection pages:
# rewrite a thin/weak collection `descriptionHtml` with a grounded draft,
# through the SAME two-gate human pipeline as every other sub_type.
#
# Grounded against main: every file/claim below verified in code.
# Write target verified against Shopify Admin GraphQL 2026-01
# (plan/21 §2.6 + §4; MUTATION_REGISTRY already carries collectionUpdate).
#
# NON-NEGOTIABLES (inherited from plan/21):
#   1. Nothing publishes without the two approval gates (rec + diff).
#   2. Grounding rule: every fact in the draft names its data source.
#   3. Snapshot = fresh read at executor pickup; generation-time values
#      are never trusted for the write or the restore.
# ============================================================

---

## 0. Summary

New auto-fix sub_type `collection_description`:

- WHO: collection pages (`pages.page_type = 'collection'`) with a thin,
  keyword-empty, or duplicated collection description.
- WHAT: replace the collection's `descriptionHtml` (what the connector
  ingests as `pages.body_html` for collections — sync.py:295,
  shopify.py:221-224) with a grounded HTML description draft.
- HOW: `collectionUpdate(input: {id, descriptionHtml})` — scope
  `write_products` (VERIFIED plan/21 §2.6; registry row exists).
- Reuses: generator pipeline shape (§2.2 meta path), executor state
  machine, `fix_policy` kill-switch, drawer diff UI, revert path.
  NO state-machine or scheduler changes.

sub_type string: **`collection_description`** (distinct from the existing
`content` scope-mapping entry, which is a union covering product body
+ collection description; this is the collection-only, description-only
surface — mirroring how `product_publish_product` narrowed
`product_publish`).

---

## 1. Grounding Sources (data -> home)

Every input the generator reads, with its schema home. NO new ingestion
required — all columns exist (plan/22 §3 patch landed them).

| Input | Source column | Home |
|---|---|---|
| Cluster keyword + intent | `keyword_clusters.primary_keyword`, `.intent` | generator.py:737-748 (existing join) |
| Collection page identity | `pages.url`, `.title`, `.page_type='collection'` | pages (plan/00) |
| Current description HTML | `pages.body_html` (collection description ingested here) | sync.py:295; shopify.py:221-224 (`body_html` OR `description_html` OR `description`) |
| Current visible text | `_body_to_text(pages.body_html)` | generator.py:49 |
| Member products | `pages` rows `page_type='product'` joined via collection rules — pragmatically: `catalogue_coverage.matching_product_ids` / `matching_product_count` for the cluster (plan/00) | catalogue_coverage |
| Product facts for grounding | `pages.title` + `body_html` of member products (first N by GSC impressions) | pages |
| SERP framing (ordering only) | `load_competitor_context` (top non-self organic results) | generator.py:750-755 |
| Write target | `pages.shopify_gid` (minted at sync, sync.py:300) | plan/22 §3 |

### 1.1 Hard grounding invariants

1. **Facts are page-and-catalogue-owned**: the draft may state only facts
   present in (a) the collection's own current `body_html` text, (b) the
   cluster's `primary_keyword`/`intent`, (c) member product titles/types
   in `catalogue_coverage`. Never competitor claims (competitor data
   grounds ORDERING only — same rule as `draft_meta_description`,
   generator.py:1182-1192).
2. **HTML output, text-checked**: the draft is HTML (`descriptionHtml` is
   HTML on the Shopify side). The validator runs on the STRIPPED text
   (`_body_to_text` semantics); the payload carries the sanitized HTML.
3. **No fabricated specifics**: no prices, counts, shipping, warranty,
   certification claims unless the literal fact string exists in the
   grounding corpus (member-product titles + own body text).
4. `shopify_gid` required — refuse (typed `FixNotSupported`) when missing,
   same as the meta path (generator.py:1401-1405).

---

## 2. GraphQL Adapter — `collectionUpdate(descriptionHtml)`

### 2.1 Write target (VERIFIED)

- Mutation: `collectionUpdate(input: CollectionInput!)` — the description
  field is **`descriptionHtml`** on `CollectionInput` (plan/21:285, :352).
- Scope: `write_products` (MUTATION_REGISTRY, shopify.py:327-330; plan/21
  §4 table). Store must not be Starter/Retail (plan/21:349 note).
- Doc URL: `https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/collectionUpdate`

Mutation string (new, adapters.py):

```graphql
mutation FixCollectionDescriptionUpdate($collection: CollectionInput!) {
  collectionUpdate(collection: $collection) {
    collection { id descriptionHtml }
    job { id done }
    userErrors { field message }
  }
}
```

> Note (carry from plan/21:191 + :407): on CUSTOM collections
> `collectionUpdate` is synchronous; on SMART (rule-set) collections the
> mutation returns an async `job`. The adapter must handle BOTH response
> shapes and must treat `job{done=false}` as "write accepted, verification
> pending" — the read-back verify (below) may observe the OLD value for a
> short window. Policy: retry the read-back with a bounded backoff
> (3 attempts, ~2s apart, capped); if still stale, return
> `verification_status: 'verify_pending'` (NOT `verify_failed`) so the
> executor does not auto-revert a write that likely landed. Auto-revert
> fires only on a DEFINITIVE value mismatch after the backoff window
> (plan/21 §5.3 semantics unchanged).

### 2.2 Single-field discipline (mirror the seo.* adapters)

`collectionUpdate(input:)` accepts the whole CollectionInput; the adapter
payload validator MUST enforce that the fix writes ONLY
`{id, descriptionHtml}` — any `title`/`ruleSet`/`published` key in the
payload is a `payload_invalid` rejection (adapters.py `_meta_payload_for`
pattern, adapters.py:265-284). This prevents drift into rule-set edits
(async job surface, higher risk) through the description path.

### 2.3 Payload builder (generator.py)

```python
COLLECTION_UPDATE_DOC_URL = ".../2026-01/mutations/collectionUpdate"

def build_collection_description_payload(gid, new_description_html):
    return {
        "mutation": "collectionUpdate",
        "variables": {"collection": {"id": gid,
                                     "descriptionHtml": new_description_html}},
        "scope_required": "write_products",
        "doc_url": COLLECTION_UPDATE_DOC_URL,
    }
```

### 2.4 Read-back + snapshot

- Read query (new): `collection(id: $id) { id descriptionHtml }` —
  description-only read (the PUBLISHABLE_READ_QUERY at adapters.py:482
  does not carry descriptionHtml; a new small query avoids publicationId
  coupling entirely, sidestepping the NOT_FOUND-on-publicationless-stores
  trap documented at adapters.py:496-501).
- Snapshot fields at pickup: `descriptionHtml`, `title`, `read_at_api_version`.
  Restore = write the snapshot's `descriptionHtml` back via the same
  mutation (refuse with `no_snapshot` when absent — never guess).

### 2.5 Registration (all four seams)

1. `required_scopes_for_sub_types` mapping (shopify.py:697):
   `"collection_description": ["collectionUpdate"]` — derived scope
   `write_products`; the KeyError drift-guard applies automatically.
2. `ADAPTERS` registry (adapters.py:744):
   `"collection_description": {execute, restore, snapshot}`.
3. `fix_policy` seed (plan/22:96-109 VALUES list):
   `('collection_description', 'medium', 3, true)` — medium tier
   (customer-visible storefront copy, same tier as `content`/`seo.description`),
   cap 3/site/week, field-verify required. Ships `enabled=false`
   (kill-switch default, fail-closed).
4. Route (src/api/routes/fixes.py): extend `draft_fixes` (fixes.py:219)
   tuple list with the collection_description generator — lazy-drafts
   alongside seo.title/seo.description, per-field failure tolerated;
   plus a dedicated `POST /recommendations/{id}/collection-description-fix`
   route mirroring `/meta-fix` (fixes.py:185) if a direct entry point is
   wanted for the drawer.

---

## 3. Validators (deterministic, pre-row and pre-write)

### 3.1 Generation gate — WHEN a fix is warranted

`collection_description_quality_checks(current_body_html, primary_keyword)`
(mirrors `meta_quality_checks`, generator.py:1126):

| Check | Fails when |
|---|---|
| `is_missing` | current `body_html` empty/whitespace |
| `thin_content` | stripped text < **150 words** (user-specified floor; NOT the 70-char meta rule — this is body copy) |
| `has_primary_kw` fails | `primary_keyword.lower()` not in stripped text |
| `near_duplicate` | `body_text_hash` matches another collection's on-site (pages self-join, `body_text_hash` col exists, plan/22 §3) |

All-pass -> NO fix row (only-fix-what's-broken). At least one fail ->
draft.

### 3.2 Draft validator (deterministic, pre-INSERT)

Runs on the STRIPPED text of the draft HTML:

- **Length**: >= 150 words AND <= 1000 words (ceiling keeps the drawer
  diff readable; Shopify has no hard cap but bloat is a smell).
  - words = whitespace-split tokens on stripped text.
- **Token overlap / grounding checks**:
  - primary keyword must appear in the draft (reuse
    `validate_meta_draft` keyword rule, generator.py:1306).
  - `_token_overlap(draft_fact_tokens, grounding_corpus_tokens)` floor:
    every NOUN-ish content token in the draft's factual claims must
    appear in the grounding corpus (own body text + member-product
    titles + cluster keywords) — reuse the existing
    `TOKEN_OVERLAP_MAX = 0.5` machinery (generator.py:980) in the
    INVERSE direction as a hallucination tripwire: if the draft's
    content-token set overlaps the corpus < 0.35, reject as
    "unsupported facts" (corpus too thin to confirm the draft's
    vocabulary -> no fix, never guess).
  - denylist: `_meta_denylist` patterns + emoji + ALL-CAPS runs +
    spam punctuation apply to body copy too (generator.py:1165-1179);
    double-quote rule NOT applied (HTML body, not a snippet attribute).
- **HTML sanity**: draft must parse as simple block HTML
  (`<p>`, `<ul>`, `<li>`, `<strong>`, `<em>`, `<h2>`-`<h3>` allowed);
  strip `<script>`/`<style>`/event-handler attrs/inline styles
  (deterministic sanitizer; anything un-parseable -> reject).
- **Duplicate guard**: stripped draft must not equal any OTHER page's
  `body_text_hash` site-wide (collection bodies are ranking surfaces;
  near-dup = the exact `body_text_hash` semantic from plan/21 §2.2).

### 3.3 Policy limits (execution-time, unchanged machinery)

- `fix_policy` row per site: medium / cap 3 / requires_field_verify
  (seed above). `check_execution_allowed` (policy.py:52) gates queueing
  + execution; missing or disabled row = typed refusal (fail-closed).
- `check_conflict` (policy.py:75) guards one active
  `collection_description` fix per target URL.
- Weekly cap counts `status='applied'` rows (policy.py:38).
- Scope probe: `required_scopes_for_sub_types(['collection_description'])`
  → `write_products`; missing scope = `execution_blocked_scope` +
  3-strike auto-disable (fix_executor.py:466, unchanged).

---

## 4. Drawer Diff Rendering — old vs new

### 4.1 diff_json shape (unchanged contract)

The drawer reads `diff_json: [{field, old_value, new_value}]`
(WorkTaskList.tsx:73). For this sub_type:

```json
[{"field": "collection.description_html",
  "old_value": "<p>Current short or thin HTML…</p>",
  "new_value": "<p>New grounded HTML…</p>"}]
```

### 4.2 Rendering format (WorkTaskList.tsx `DiffRow`)

Because the values are HTML, render a **text preview, not raw HTML**
(never `dangerouslySetInnerHTML` on provider-adjacent copy):

- Field label: add `"collection.description_html": "Collection description"`
  to `FIELD_LABELS` (WorkTaskList.tsx:77-80).
- `old_value` / `new_value` render the HTML-STRIPPED text
  (mirror `_body_to_text`: tag-strip + whitespace-collapse) in the
  existing −/+ rows. Truncate each side at ~400 chars in the collapsed
  view with an inline "show full" expander — collection bodies are
  longer than metas and would blow out the drawer height.
- If either side contains block structure (p/ul), render paragraphs as
  separate −/+ lines (split on `</p>`/`<li>` boundaries client-side) so
  the operator reads it the way the storefront will.
- The missing-old placeholder line ("no previous description",
  WorkTaskList.tsx:127) already covers the `is_missing` case verbatim.

### 4.3 Supporting evidence in the drawer

Reuse the grounding block pattern from the meta fix (generator.py:1427-
1439): `payload_json.grounding` carries
`{member_products_sampled, draft_source, outline_missing}` so the
operator sees WHAT the draft is grounded in, not just the diff.

---

## 5. Files Touched (implementation checklist)

| File | Change |
|---|---|
| `src/fixes/generator.py` | `COLLECTION_DESC_*` constants; `collection_description_quality_checks`; `draft_collection_description`; `validate_collection_draft`; `build_collection_description_payload`; `generate_collection_description_fix_for_recommendation` (idempotent, conflict-guarded, same contract as meta path) |
| `src/fixes/adapters.py` | `COLLECTION_DESCRIPTION_READ_QUERY` + mutation; `_collection_description_payload_for`; `collection_description_execute/ snapshot/ restore`; ADAPTERS entry |
| `src/connectors/shopify.py` | one mapping line in `required_scopes_for_sub_types` |
| `plan/22-stage2-phase0-schema.sql` | one `fix_policy` seed row |
| `src/api/routes/fixes.py` | extend `draft_fixes` tuple; optional `/collection-description-fix` route |
| `apps/openseo-plus/.../WorkTaskList.tsx` | FIELD_LABELS entry; DiffRow text-strip + truncation/expander for HTML values |
| `tests/` | mirror the seo.description adapter/generator test seams (`client=` injection seam exists, adapters.py:26-32) |

## 6. Explicit non-goals

- No rule-set (`smart collection rules`) writes — separate surface,
  separate risk (async job + ordering semantics).
- No collection `title` writes through this adapter (single-field
  discipline, §2.2).
- No auto-execute at ship: `fix_policy.enabled=false` until the operator
  flips it (unchanged kill-switch semantics, WALKTHROUGH.md:253).
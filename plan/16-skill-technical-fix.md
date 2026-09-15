# Skill 4: Validate Technical Fix (Generator 3)
# ============================================================
# Used when: Agent evaluates a `technical_fix` candidate produced by
# 03-generator-technical-root-causes.sql. The candidate's `primary_keyword`
# field carries the issue_type (not_indexable, canonical_conflict,
# status_failure, broken_internal_link, orphan_page, deep_page,
# sitemap_index_mismatch, duplicate_faceted, structured_data_failure,
# js_rendering_failure).
# ============================================================

## How to Read the Candidate

Generator 3 groups crawl audit findings and pre-assigns impact/confidence.
The candidate payload contains exactly these signals — never assume data
beyond them:

```
AVAILABLE IN PAYLOAD:
  target_url, page_type, template, status_code, issue_type (in primary_keyword),
  organic_sessions_28d, gsc_clicks_28d,
  evidence:      {issue-specific fields — see per-issue sections below},
  work_required: [{owner, task, acceptance_criteria}] (generator default),
  impact/confidence/effort/owner (generator defaults)

NOT IN PAYLOAD (must be verified via connector/crawl/GSC before asserting):
  WHICH mechanism blocks indexing (noindex vs robots.txt vs canonical vs header),
  whether the defect still reproduces (crawl snapshots go stale),
  whether Google actually renders/indexes the page,
  sitemap membership, structured-data validity, live SERP state.
```

The generator's diagnosis strings are generic ("Investigate why page is not
indexable"). Your job is to replace "investigate" with a named mechanism.

---

## Step 0: Cross-Issue Dedupe (do this first)

The generator's de-dup clause only checks the recommendations table — it does
NOT see sibling rows emitted in the same batch. The same URL therefore arrives
multiple times with different issue_types. Known overlap pairs:

```
  status_failure  ⊃  broken_internal_link   (same WHERE: status>=400; broken adds links_in>2)
  not_indexable   ⊃  sitemap_index_mismatch (mismatch = indexable=false AND status=200 AND clicks>0)
  duplicate_faceted ∩ canonical_conflict    (faceted URL whose canonical points to the clean URL)
  not_indexable   ∩  canonical_conflict     (canonical points elsewhere → page flagged non-indexable)

RULE:
  IF multiple candidates share target_url:
    1. Keep ONE recommendation rooted in the issue that identifies the ROOT CAUSE
       (canonical_conflict explains not_indexable → root cause is the canonical).
    2. Reject the symptom-only duplicates: "Duplicate of candidate {id} — same URL;
       root cause diagnosed under {issue_type}."
    3. Exception: broken_internal_link on URL A and status_failure on URL B are
       separate targets — keep both.
```

---

## Step 1: Verify the Defect Is Real (per issue_type)

### Issue: not_indexable

```
EVIDENCE IN PAYLOAD: current_status, indexable, canonical, organic_sessions_28d, gsc_clicks_28d

THE GAP: `indexable = false` says THAT it's blocked, not HOW. Before approving,
identify the mechanism — the fix differs completely:

  1. canonical is non-self-referencing AND differs from page URL
     → root cause is likely the canonical, not a noindex. Diagnose as
       canonical_conflict; do not propose touching meta robots.

  2. status_code = 200, canonical self-referencing
     → blocking factor is meta robots, X-Robots-Tag, or robots.txt.
       Verify via fetch: check <meta name="robots"> / header / robots.txt
       and NAME it in the diagnosis.

  3. status_code >= 400 with indexable=false → this is a status_failure, not
     an indexability defect. Reject as duplicate of the status candidate.

FALSE POSITIVES (reject or downgrade):
  - Deliberately noindexed pages: paid-campaign landing pages, internal search
    results, tag archives auto-generated with noindex. IF the page_type is
    'landing' AND organic_sessions_28d = 0 AND gsc_clicks_28d = 0
      → likely intentional: REJECT with "Noindex appears intentional (landing
        page with zero organic visibility). Confirm with operator if unsure."
  - Homepage behind a geo/consent interstitial returning 200 but flagged
    non-indexable in the crawl snapshot only — re-crawl before asserting.

ACCEPT only if:
  - Page has organic visibility (gsc_clicks_28d > 0 or organic_sessions_28d > 0), AND
  - mechanism identified or verifiable in one fetch, AND
  - page_type is revenue-bearing (collection/product/landing/category/homepage).
```

### Issue: canonical_conflict

```
EVIDENCE IN PAYLOAD: page_url, canonical_url, status_code, organic_sessions_28d

INTENTIONAL PATTERNS THAT LOOK LIKE DEFECTS — check before approving:

  1. Faceted/parameterized URL canonicals to the clean URL
     IF page_url contains '?' AND canonical_url strips the parameters
       → WORKING AS DESIGNED (this IS the fix for duplicate_faceted).
         REJECT: "Canonical is intentionally consolidating faceted variant
         to {canonical_url} — correct behaviour, not a defect."

  2. Cross-domain canonical (syndicated/licensed content)
     → legitimate only with a documented reason. IF target_url's domain differs
       from site domain AND the content is syndicated → REJECT or require
       operator confirmation. The generator's own acceptance criterion allows
       "intentional cross-domain canonical with documented reason" — demand
       the documentation.

  3. Pagination canonicaling to page 1, or country/language variants
     canonicaling with hreflang present
     → intentional consolidation pattern. Downgrade to LOW impact; approve only
       if the target page holds meaningful impressions it is losing.

REAL DEFECTS (approve):
  - Canonical points to a URL that 404s/redirects (dead canonical) — verify
    the canonical target's status via fetch.
  - Canonical points to an unrelated page (different template/intent).
  - Canonical points to itself with wrong scheme/host (http vs https, www).
    NOTE: the generator compares canonical_url != url as STRINGS — trailing
    slashes, query strings, or scheme differences alone are weak evidence.
    Confirm the canonical target actually differs in content/behaviour.

IMPACT: proportional to organic_sessions_28d in evidence. A canonical conflict
on a page with 0 sessions/0 clicks is LOW regardless of generator default.
```

### Issue: status_failure

```
EVIDENCE IN PAYLOAD: status_code, gsc_clicks_28d, organic_sessions_28d

CLASSIFY THE STATUS:
  5xx → real defect (server instability). HIGH if the page carries traffic.
  404 on product/collection → check WHY:
      - Product discontinued AND delisted everywhere (nav, sitemap, no links)
        → intentional; REJECT unless gsc_clicks_28d shows Google still sending
          traffic (then fix = 301 to nearest equivalent, not "fix 404").
      - Product in catalogue (still purchasable) but page 404s → REAL defect,
        high priority: the page earns clicks but serves an error.
  403/401 on public commerce pages → real defect (bot management or auth
        wrongly gating Googlebot — verify user-agent treatment).

REJECT if:
  - status_code >= 400 AND gsc_clicks_28d = 0 AND organic_sessions_28d = 0
    AND internal link context unknown → LOW or REJECT: no visibility at risk.
  - The URL is a pagination/facet variant Google is supposed to forget.

ACCEPTANCE CRITERIA rewrite: generator default is "Page returns HTTP 200 and
is indexable" — acceptable for 4xx/5xx. For a 404 with residual traffic, the
correct work is a 301 map: name the destination URL.
```

### Issue: broken_internal_link

```
EVIDENCE IN PAYLOAD: status_code, internal_links_in

This is the link-graph view of status_failure (same broken URL, symptom = who
links to it). After dedupe (Step 0), approve only when BOTH hold:
  - internal_links_in > 2 (generator threshold — navigation-level breakage,
    not a single stale blog link), AND
  - the fix is stated as EITHER "repair target" OR "repoint the linking pages" —
    with the linking decision justified: repair if the URL should exist
    (collection with traffic history), repoint if the target is gone for good.

The generator sets impact='high' unconditionally. Override to match visibility:
  internal_links_in high BUT gsc_clicks_28d = 0 → MEDIUM (link hygiene, not
  traffic loss yet). Impact must be proportional to organic visibility —
  see Step 2.
```

### Issue: orphan_page / deep_page

```
EVIDENCE IN PAYLOAD: crawl_depth, internal_links_in, product_count

FALSE POSITIVES — the link graph is a crawl artifact, not ground truth:
  1. JS-rendered navigation: IF the site's nav is client-rendered and the crawl
     recorded render_status = 'render_failed' or rendered==raw on TEMPLATES,
     internal_links_in = 0 may mean the CRAWLER never saw the nav.
     Cross-check: does this URL share its template with pages that DO have
     internal_links_in > 0? IF yes → crawl artifact; REJECT (or downgrade to
     "re-crawl first").
  2. Intentionally deep paginated/archived pages with no search demand
     (gsc_clicks_28d = 0) → REJECT.
  3. New pages not yet linked (published < 14 days) → premature; REJECT and let
     the next crawl re-flag if still orphaned.

ACCEPT if: product_count > 0 or page_type in (collection, product) AND the page
has or should have demand. The work is a named linking plan (source pages +
anchors), not "add internal links" — see Skill 2 Step 6 for the link-audit
format. Generator acceptance criterion "reachable within 3 clicks" is
verifiable against the next crawl; keep it.
```

### Issue: sitemap_index_mismatch

```
EVIDENCE IN PAYLOAD: indexable, gsc_clicks_28d
NOTE: despite the name, the detected condition is "indexable=false AND
status=200 AND gsc_clicks_28d > 0" — Google is still clicking a page the site
declares non-indexable. That is the sharpest defect signal in Generator 3:
Google has NOT dropped the page, so the block is recent/reversible.

DEDUPE: this URL also appears under not_indexable (Step 0). Keep this
diagnosis when both exist — it proves ongoing traffic loss.

ACCEPT if: clicks > 0 in the last 28d window. THEN root-cause the block using
the not_indexable checklist (canonical first, then meta/header/robots).
Generator task text ("Add page to sitemap and resolve indexability issue") is
misleading — sitemap membership is a symptom, not the cause. Rewrite the task
to name the blocking mechanism.

IMPACT: high when gsc_clicks_28d > 100 (generator default) — confirmed clicks
being shed by an active block.
```

### Issue: duplicate_faceted

```
EVIDENCE IN PAYLOAD: page_url, status_code.  gsc_clicks_28d IS NULL — the
generator has NO traffic signal for this issue. The detection predicate is
only: url contains '?' AND status=200 AND indexable AND page_type=collection.

Weakest evidence class in Generator 3. The URL having parameters is NOT a
defect by itself. Before approving, establish harm via GSC:

  APPROVE (medium) if:
    - GSC shows the faceted URL earning impressions/clicks that the clean URL
      also earns (split signals on the same queries) — cite both rows, OR
    - site-wide crawl shows MANY parameterized collection URLs indexable
      (template-level defect → one fix, cite the count), OR
    - parameters are tracking/commerce junk (?gclid, ?utm_, ?session)
      → cheap noindex/canonical fix, impact LOW.

  REJECT if:
    - The parameter carries genuine search demand and is intentionally
      indexable (e.g. ?brand=nike ranks independently) — check the faceted
      URL's GSC queries; if its queries do NOT intersect the clean URL's,
      both may legitimately coexist.
    - The URL is already canonicalized to the clean version (then "only
      canonical version is indexable" is ALREADY true → candidate contradicts
      its own acceptance criterion).

Acceptance criterion rewrite: "Only canonical version is indexable" is only
verifiable once you NAME the canonical version. Do that in the work item.
```

### Issue: structured_data_failure

```
EVIDENCE IN PAYLOAD: has_structured_data, page_type, template.
gsc_clicks_28d IS NULL — no traffic signal from the generator.

This is an ENHANCEMENT, not a blocker: missing schema does not prevent
ranking; it forfeits rich-result eligibility. Keep that framing in the
diagnosis — never claim schema absence caused a traffic decline.

VERIFY before asserting:
  - has_structured_data=false may mean the crawl only looked for JSON-LD
    while the template emits microdata. Fetch the rendered HTML and check for
    ANY of JSON-LD / microdata / RDFa before claiming absence.
  - Check whether Google has ALREADY awarded rich results (GSC enhancement
    reports or live SERP) — if yes, the "defect" doesn't exist in effect.

IMPACT: product pages (generator: high) — justify high ONLY if the page has
impressions that could convert to richer presentation; else cap at medium.
Collections: medium per generator, but if the template-level fix (one include)
covers N pages, say so — template-wide fixes outrank single-page fixes.

Acceptance criterion "passes Rich Results Test" is verifiable — keep, and add
the schema types named for the page_type (Product+Offer for product,
CollectionPage+BreadcrumbList for collection).
```

### Issue: js_rendering_failure

```
EVIDENCE IN PAYLOAD: render_status, raw_html_hash_present,
rendered_html_hash_present, rendered_matches_raw, gsc_clicks_28d

THE TRICKIEST HEURISTIC IN GENERATOR 3. Two detection branches:

  Branch A: render_status = 'render_failed'
    → the crawler itself failed to render. This proves OUR CRAWLER choked,
      not that Google does. Before approving, check what Google sees:
      IF gsc_clicks_28d > 0 AND page is indexed → Google renders it fine;
      the defect is in the crawl setup, not the site.
      REJECT as site defect (note it as a crawl-infrastructure issue).

  Branch B: raw_html_hash == rendered_html_hash (rendered_matches_raw=true)
    → AMBIGUOUS BY CONSTRUCTION. For a server-rendered page, rendered==raw is
      the EXPECTED outcome: the crawler fetched identical static HTML twice.
      The heuristic only indicates a defect when the RAW HTML is a JS shell
      (body content absent from raw).
    REQUIRED check: fetch raw HTML and confirm body content is absent
    (empty <body>/app-shell markup). IF raw already contains the full body
      → rendered==raw is expected for a static page; REJECT.

  ACCEPT only when: raw HTML is confirmed content-empty shell AND (clicks are
  present or the page is commercially important) AND render_status in
  ('render_failed', missing/stale rendered capture).

  The generator's acceptance criterion ("rendered_html_hash differs from
  raw_html_hash with body content present") is well-formed — keep it.
```

---

## Step 2: Impact / Confidence / Effort Recalibration

The generator pre-assigns these; you may override WITH justification tied to
the evidence block's numbers (organic_sessions_28d / gsc_clicks_28d).

```
IMPACT:
  HIGH  → defect is actively suppressing visibility on a page with proven
          demand: gsc_clicks_28d > 50 OR organic_sessions_28d > 0 AND the
          defect blocks indexing/rendering entirely (not_indexable,
          sitemap_index_mismatch, js shell, 5xx on live page).
  MEDIUM → defect degrades rather than blocks (canonical conflict with traffic,
          duplicate_faceted with proven split, structured data, or any defect
          on a page with impressions but ~no clicks).
  LOW   → hygiene on pages with no current visibility (gsc_clicks_28d = 0
          AND organic_sessions_28d = 0), or defects whose acceptance criterion
          is already satisfied. Default to REJECT if there is no plausible
          visibility gain within the 21-day technical_fix window.

CONFIDENCE:
  HIGH  → defect directly observed in payload evidence (status_code,
          canonical, hashes) AND mechanism named.
  MEDIUM → defect observed but mechanism inferred (not_indexable without
          mechanism fetch).
  LOW   → evidence contradicts itself or relies on a possibly-stale crawl
          snapshot you could not re-verify.

EFFORT: hours unless the fix requires template/engineering changes across
multiple templates (then days) — orphan fixes needing a linking plan across
many source pages may also be days.

MEASUREMENT: technical_fix window = 21 days (from measurement_window_lookup);
metric = impressions (plan/16 sets technical_fix → 'impressions'). State the
metric in the recommendation.
```

---

## Step 3: Acceptance Criteria Quality Bar

The generator ships default criteria; some are good, some are vague. Rewrite
vague ones — an operator must be able to verify without asking questions.

```
VAGUE (rewrite)                        → VERIFIABLE (target)
"Investigate why page is not indexable"→ "Remove noindex meta from
                                          /collections/x; URL Inspection shows
                                          'URL is indexable'"
"Resolve canonical conflict"           → "canonical of /collections/x is
                                          self-referencing; canonical target
                                          /old-page returns 410"
"Add internal links or adjust
 site architecture"                    → "links added from {page1}, {page2}
                                          with anchors {a1}, {a2}; crawl_depth
                                          <= 3 in next crawl"
"Add appropriate structured data"      → "Product + Offer JSON-LD passes Rich
                                          Results Test with 0 errors on
                                          {url}"

RULES:
  - The criterion must name the CHECK and the EXPECTED OUTCOME, both observable
    post-fix (URL Inspection, crawl field, header, SERP feature).
  - If the criterion depends on a future crawl, say which field changes
    (indexable=true, crawl_depth<=3, render_status='js_rendered').
  - One criterion per mechanism — a fix with 3 defects needs 3 work items.
```

---

## Step 4: Final Validation Checklist

```
BEFORE ACCEPTING a technical_fix candidate:
  □ Dedupe applied — no sibling candidate for the same target_url is also approved
  □ Defect confirmed against payload evidence (not just the issue label)
  □ Intentional-pattern check passed for the issue type (canonical/faceted/noindex)
  □ Root-cause MECHANISM named in diagnosis (not "investigate")
  □ Impact tied to organic_sessions_28d / gsc_clicks_28d in the evidence
  □ Acceptance criteria name check + expected outcome
  □ Rejection_context consulted — if the same defect pattern was already
    rejected by the operator, do not re-propose without new evidence
IF any check fails → REJECT with a reason citing the failing evidence.
```

---

## Worked Examples

```
EXAMPLE 1 — ACCEPT (root-cause chain)
  Candidate: issue_type=not_indexable, /collections/running-shoes,
  status=200, gsc_clicks_28d=180, canonical=/collections/shoes-all
  → Root cause is the canonical, not robots. Approve ONE canonical_conflict
    recommendation: repoint canonical to self; acceptance = URL Inspection
    indexable + self-referencing canonical. Impact HIGH (180 clicks being
    absorbed by the wrong URL). Reject the duplicate not_indexable row.

EXAMPLE 2 — REJECT (intentional pattern)
  Candidate: issue_type=canonical_conflict,
  /collections/sneakers?color=black → canonical=/collections/sneakers
  → Faceted variant canonicaling to clean URL is the system working.
    REJECT: "Parameterized collection canonicals to clean URL — intentional
    consolidation; approving would reintroduce index bloat."

EXAMPLE 3 — DOWNGRADE (crawl artifact)
  Candidate: issue_type=orphan_page, /products/anc-headphone-x,
  internal_links_in=0, product_count=1, gsc_clicks_28d=0
  → Sibling product pages on the same template average 40 internal links;
    nav is JS-rendered and the crawl recorded render failures on that template.
    REJECT as crawl artifact: "internal_links_in=0 is not credible given
    template peers; re-crawl with rendering enabled before re-flagging."
```

---

## Output Reminder

For accepted candidates keep the standard recommendation shape
(action_type=technical_fix, target_url set, proposed_url empty, owner=
engineering). For rejected candidates, log the issue_type as the reason
prefix, e.g. "not_indexable — noindex intentional on zero-traffic landing
page", so rejection_context teaches future runs.
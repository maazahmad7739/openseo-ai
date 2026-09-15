# ============================================================
# Skill 5: Validate Consolidation (Cannibalization, Generator 4)
# ============================================================
# Used when: Agent evaluates a `cannibalization` candidate produced by
# 04-generator-cannibalization.sql. The candidate proposes consolidating a
# weaker page (proposed_url) into a stronger survivor (target_url) via a 301
# redirect, based on 28 days of GSC query×URL data.
# ============================================================

## How to Read the Candidate

Generator 4 detects: 2+ own-domain URLs earning impressions for queries in the
same keyword cluster, where neither URL dominates (top-2 impression ratio
< 3x), and similar page types. The payload contains exactly these signals:

```
AVAILABLE IN PAYLOAD:
  target_url         — survivor (higher impressions)
  proposed_url       — weaker page (redirect source)
  cluster_id, primary_keyword
  competing_urls, competing_page_types,
  cannibalization_pattern (same_type | mixed_types | multiple_types)
  combined_impressions, combined_clicks,
  page_metadata      — [{url, title, h1, page_type}] for each competing page
  evidence.impressions_distribution — {url: impressions} per URL
  evidence.weekly_positions        — [{url, pos_w1..w4}] weekly avg positions

NOT IN PAYLOAD (verify before asserting):
  - Whether the two pages' CONTENT actually overlaps (titles/H1s are proxies)
  - Whether redirects are already in place or pages have diverged since
  - The weaker page's TOTAL footprint: GSC rows for OTHER clusters it serves
  - Backlink/external equity of each page (not in payload)
  - Whether the cluster itself is worth fighting for (intent, volume)
```

The generator's SQL already filtered: impressions >= 100 per query×URL,
2+ distinct URLs per query, top-2 impression ratio < 3x, page-type count <= 2.
Your job is to confirm the CONFLICT is real and the consolidation preserves
the stronger page's equity — the SQL cannot check either.

---

## Step 1: Confirm Genuine Cannibalization (not benign multiplicity)

Multiple pages ranking for related queries is only a problem when they swap
positions on the SAME query with similar intent. The SQL enforces the
mechanics; you must rule out the benign explanations:

```
CHECK 1 — Distinct sub-intents (most common false positive):
  IF the two pages' titles/H1s (page_metadata) target recognizably different
  sub-intents of the cluster (e.g. "best noise cancelling headphones" listicle
  vs "buy noise cancelling headphones" collection):
    → This is an intentional content strategy, NOT cannibalization.
    → VERIFY with live SERP (OpenSEO fetch("serp")): IF Google shows different
      result types for the two queries → REJECT:
      "Pages serve distinct sub-intents; SERP confirms both can rank without
      trading places. Not cannibalization."

CHECK 2 — Position alternation is real, not noise:
  Read evidence.weekly_positions. Genuine cannibalization = the leader
  SWAPS across weeks (A leads w1, B leads w2, ...).
  IF both URLs hold stable, clearly separated positions (e.g. A always ~4,
  B always ~9, stddev low) → they may coexist (SERP showing both).
    → Downgrade or REJECT: "Positions stable and separated — no ranking
      volatility; consolidation would sacrifice the #2 listing (double
      listing disappears on consolidation)."

CHECK 3 — Click split vs impression split:
  IF one URL takes nearly ALL clicks despite balanced impressions
  (e.g. impressions 60/40 but clicks 95/5): Google has already decided.
    → The "conflict" is already resolved in practice; consolidation is
      optional tidy-up, not growth. Downgrade to LOW impact or reject when
      combined clicks are trivial (<10 over 28d).

CHECK 4 — Query-level confirmation:
  The candidate is per query within the cluster. IF only 1 of the cluster's
  many queries shows competition and it is a LOW-volume long-tail:
    → the aggregate harm is tiny; REJECT unless combined_impressions is
      material (>= ~500) or the query is commercially core.
```

---

## Step 2: Identify the Weaker Page — and What Consolidation Loses

The SQL picks `weaker_url` purely by fewer impressions for THIS cluster. That
is necessary but not sufficient. Verify the weaker page is genuinely the one
to retire:

```
BEFORE confirming proposed_url as the redirect source:
  1. Cluster footprint beyond this query:
     IF the weaker page is the PRIMARY earner for OTHER clusters (check its
     GSC rows / other candidates referencing the same URL):
       → Do NOT redirect it. The right fix may be DIFFERENTIATION (re-title,
         re-scope both pages) not consolidation.
       → REJECT with: "Weaker page for '{query}' is a primary earner for
         other clusters; redirect would trade one conflict for a traffic
         loss. Differentiate instead."

  2. Asset asymmetry (the equity check from agent_system):
     The consolidation must preserve the STRONGER page's equity, but also not
     destroy the weaker page's:
     - IF weaker page holds the cluster's only product coverage, buying
       guide, or in-stock products → consolidation must MERGE that content
       into the survivor first. Add a work item: "Migrate {content/products}
       from {proposed_url} to {target_url} before redirecting."
     - IF the weaker page has materially more orders/revenue history than the
       survivor → flag for operator judgment rather than auto-approving.

  3. Survivor sanity check:
     IF the "stronger" survivor has an indexability defect (non-200,
     non-indexable, canonical elsewhere — visible via crawl context if
     supplied): consolidating INTO it is nonsense.
       → REJECT the consolidate; route as technical_fix on the survivor.

RULE: redirect the weaker page only when it is weaker overall, not merely
weaker for this single query.
```

---

## Step 3: Pattern-Based Handling

```
cannibalization_pattern = same_type (both collections, or both products):
  Cleanest case. Consolidation (301 weaker → stronger) is standard.
  IF both are COLLECTIONS: confirm the survivor's product coverage can absorb
  the weaker page's products (cite product_count from page context when
  available; else add verification work item).
  IF both are PRODUCTS: prefer merging into the better-converting page; the
  redirect is correct only if the products are variants/interchangeable. Two
  DIFFERENT products ranking for one query usually need canonical/differentiation,
  not deletion — check titles/H1s for distinct products.

cannibalization_pattern = mixed_types (collection vs product, collection vs blog):
  The SQL only permits <= 2 distinct types. Mixed-type "cannibalization" is
  frequently SERP fluidity, not a site defect:
    → Verify live SERP: IF the query's intent supports BOTH types appearing
      (e.g. Google shows 1 collection + 9 articles) → the two pages aren't
      fighting for the same slot. REJECT or downgrade; prefer differentiation
      (improve_page) over consolidate.

multiple_types (>2 types present):
  Generator only emits this when array length <= 2, so this label is not
  expected; IF seen, treat as mixed_types with extra caution.
```

---

## Step 4: Impact / Confidence / Effort

```
IMPACT:
  HIGH   → combined_impressions >= 1000 AND weekly alternation is real
           AND the cluster is commercially core (cluster.commercial_value =
           'high'); the site is visibly trading positions instead of holding
           one strong rank.
  MEDIUM → conflict confirmed but smaller scale (combined_impressions
           100-1000, or clicks concentrated on one URL already).
  LOW    → combined_clicks < 10 over 28d; or conflict resolved in practice
           (CHECK 3). Prefer REJECT over LOW unless the fix is trivially safe.

  Justify impact from combined_impressions/combined_clicks in the evidence —
  these are the only traffic numbers the generator provides.

CONFIDENCE:
  HIGH   → alternation visible in weekly_positions AND SERP confirms single
           dominant result type AND weaker page has no other-cluster primacy.
  MEDIUM → conflict real but sub-intent boundary is blurry, or payload lacks
           the weaker page's full footprint.
  LOW    → single-query evidence only, small samples (impressions barely above
           the 100 floor), or pattern = mixed_types without SERP check.

EFFORT: days (redirect + internal-link updates + content migration + 28-day
consolidate window). Not hours — never accept the generator default of hours
for consolidation work; orchestrator sets effort='days' for this generator.

MEASUREMENT: consolidate window = 28 days (measurement_window_lookup);
metric = organic_sessions (plan/16 sets consolidate → 'organic_sessions').
Cite the window in the measurement plan.
```

---

## Step 5: Work Specification Requirements

The recommendation must go beyond the generator's default 301 task. A
consolidation that just deletes the weaker page destroys its equity.

```
REQUIRED WORK ITEMS (adapt orchestrator defaults, do not just copy):
  1. Content/product migration BEFORE the redirect:
     owner=SEO|content, task="Migrate {sections/products} from
     {proposed_url} into {target_url}", acceptance_criteria="Survivor covers
     {named content/products}; no orphaned product links"
  2. Redirect:
     owner=engineering, task="301 {proposed_url} → {target_url}; remove
     source from all sitemaps", acceptance_criteria="Source returns 301 to
     survivor; absent from sitemaps; survivor returns 200"
  3. Internal links + nav:
     owner=SEO, task="Repoint all internal links from {proposed_url} to
     {target_url}", acceptance_criteria="Zero internal links to source in
     next crawl"
  4. Optional canonical holding pattern:
     IF immediate redirect is risky (seasonal page, pending content merge):
     canonical from weaker → survivor as an interim step, redirect after
     migration. State the interim plan explicitly.

DO NOT propose consolidate when the correct fix is differentiation:
  IF Step 1/2 shows distinct sub-intents or the weaker page's other-cluster
  role matters → the recommendation becomes improve_page on ONE of the pages
  (re-scope it off this cluster's head query), NOT a redirect.
```

---

## Step 6: Final Validation Checklist

```
BEFORE ACCEPTING a cannibalization candidate:
  □ Weekly positions show genuine alternation (not stable separated ranks)
  □ Live SERP confirms one dominant result type for the primary query
  □ Weaker page verified weaker across clusters (not a single-query artifact)
  □ Weaker page's assets (products/content) have a migration destination
  □ Survivor is indexable/healthy — consolidating into a broken page rejected
  □ Pattern handled per Step 3 (same_type consolidated; mixed_types
    differentiated or rejected)
  □ Impact justified from combined_impressions/combined_clicks
  □ rejection_context consulted — operator already rejected consolidation for
    this cluster? Do not re-propose without new evidence.
IF any check fails → REJECT citing the specific evidence.
```

---

## Worked Examples

```
EXAMPLE 1 — ACCEPT (textbook alternation)
  /collections/anc-headphones vs /collections/noise-cancelling, both
  same_type=collection; weekly positions swap leadership across all 4 weeks;
  combined_impressions=2400, combined_clicks=140; both titles target the same
  commercial head term.
  → ACCEPT: redirect /collections/noise-cancelling → /collections/anc-headphones
    after migrating its 12 featured products. Impact HIGH (alternation across
    4 weeks, 140 clicks at stake), confidence HIGH (same_type + clear swaps).

EXAMPLE 2 — REJECT (benign multiplicity)
  /blog/best-wireless-headphones ranks #6, /collections/wireless-headphones
  ranks #7, both stable for 4 weeks; SERP shows mixed article/collection
  results; clicks 90/10 in favour of the blog post.
  → REJECT: "Stable separated positions with resolved click split — Google
    shows both; consolidation would remove a second listing and the blog's
    funnel entry. Differentiate the collection's title instead."

EXAMPLE 3 — REJECT (weaker page is primary elsewhere)
  Weaker URL /collections/wireless-earbuds loses THIS query 40/60, but its
  GSC footprint shows it is the top earner for 3 other clusters.
  → REJECT: "Page is primary earner for {other clusters}; redirecting trades
    one conflict for three traffic losses. Re-scope this page off the head
    term instead (improve_page)."
```

---

## Output Reminder

Accepted candidates keep the generator's field semantics: action_type =
consolidate, target_url = survivor, proposed_url = redirect source. In
rejection_log, prefix the reason with the pattern, e.g. "cannibalization —
stable separated positions, no alternation", so rejection_context teaches
future runs.
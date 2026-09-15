# ============================================================
# Skill 1: Validate and Prioritize Opportunity
# ============================================================
# Used when: Agent evaluates whether a candidate represents a
# genuine SEO opportunity worth pursuing.
# ============================================================

## Decision Tree

### 1. Search Intent Validation

```
FOR EACH candidate:
  1. Fetch live SERP for primary keyword (via OpenSEO connector — openseo_serp_snapshots or live fetch("serp"))
  2. Analyze top 10 results:
     - Page types present (collection, product, article, video, etc.)
     - Content format (listicle, comparison, product grid, guide)
     - Commercial signals (price displays, CTAs, product cards)
  3. Classify intent:
     - INFORMATIONAL: User wants to learn/compare
     - COMMERCIAL: User is researching to buy
     - TRANSACTIONAL: User is ready to buy
     - NAVIGATIONAL: User is looking for specific brand/site
  4. Compare to candidate's proposed page type:
     IF SERP shows 80%+ collection pages for commercial query
       AND candidate proposes a blog post → REJECT
       Reason: "Intent mismatch — SERP dominated by collection pages but candidate proposes informational content"
     IF SERP shows mixed types (40/60 split)
       → FLAG for agent judgment
       Note: "Mixed SERP landscape — agent should evaluate whether our domain authority supports competing with established page type"
```

### 2. Commercial Relevance Check

```
FOR EACH candidate:
  1. Check cluster keywords against brand terms:
     IF primary keyword contains brand name → SKIP (brand exclusion)
  
  2. Check product availability:
     IF in_stock_products < site.min_in_stock_products → REJECT
     Reason: "Insufficient catalogue depth — {n} in-stock products below threshold of {threshold}"
     NOTE: "In stock" follows site.in_stock_definition (any_variant | majority_variants).
           A multi-variant product counts only per that rule — do not guess.
  
  3. Check commercial value alignment:
     IF cluster.commercial_value = 'high' AND page_match_score.catalogue_match > site.catalogue_match_min + 0.10
       → HIGH PRIORITY
     IF cluster.commercial_value = 'medium' AND page_match_score.catalogue_match >= site.catalogue_match_min
       → MEDIUM PRIORITY
     IF cluster.commercial_value = 'low'
       → LOW PRIORITY or REJECT
       Reason: "Low commercial value — keyword has demand but limited revenue potential"
```

> All numeric thresholds above are resolved from `threshold_tiers` by
> `site.catalogue_size_tier` (or per-site overrides) — never hardcoded.

### 3. Catalogue Suitability

```
FOR EACH candidate:
  1. Check if proposed page type matches cluster.recommended_page_type:
     IF mismatch AND recommended_page_type is 'collection'
       AND candidate proposes 'product' → REJECT
       Reason: "Page type mismatch — cluster needs a collection/category page, not individual product"
  
  2. Check product diversity:
     IF matching_products span only 1 vendor/category
       → FLAG as thin potential
       Note: "Limited product diversity — consider whether a single-vendor collection provides enough value"
  
  3. Check price range:
     IF average_price is very high (>$500) AND cluster.search_volume is high
       → Consider landing page vs collection debate
       Note: "High-value products — evaluate whether comparison content would serve better than product grid"
```

### 4. Page-Type Selection Rules

```
RECOMMENDED PAGE TYPE SELECTION:

IF search_volume > 5000 AND intent = 'commercial'
  → collection page (primary choice)
  → OR comparison/buying guide if SERP shows informational dominance

IF search_volume < 500 AND intent = 'transactional'
  → product page optimization (not new page creation)
  → OR collection page if 10+ matching products

IF intent = 'informational'
  → blog/article (NOT collection)
  → Link to relevant collection from article

IF competitor pages are collection-type
  → match the format (don't create a blog post to compete with collections)
```

### 5. Thin Page Avoidance

```
REJECT if any of:
  1. Proposed page would have <800 words of unique content
  2. Proposed page would display fewer products than site.min_in_stock_products (tier-resolved)
  3. Proposed page has no unique value proposition vs existing pages
  4. Proposed page would duplicate >70% content of another page

REASON FORMAT: "Thin page risk — {specific reason}"
```

### 6. Impact/Confidence/Effort Evaluation

```
IMPACT ASSESSMENT:
  HIGH if:
    - search_volume >= 5000
    - commercial_value = 'high'
    - page_match_score.aggregate < site.page_match_threshold - 0.15 (big gap to close; threshold tier-resolved)
    - competitors ranking are weaker domain authority
  
  MEDIUM if:
    - search_volume between 1000-5000
    - OR commercial_value = 'medium' with decent catalogue match
  
  CONFIDENCE ASSESSMENT:
  HIGH if:
    - GSC data shows existing impressions (demand confirmed)
    - Page-match score is calculated from real data
    - SERP analysis confirms intent match
  
  MEDIUM if:
    - Limited GSC data but strong catalogue match
    - New cluster with no existing ranking page
  
  LOW if:
    - Estimated search volume (no GSC data)
    - Unclear SERP landscape
    - Seasonal keyword with uncertain demand
  
  EFFORT ASSESSMENT:
  HOURS if:
    - Technical fix (canonical, robots, schema)
    - Title/meta optimization
    - Internal linking changes
  
  DAYS if:
    - New collection page creation
    - Content writing (>1000 words)
    - Template modification
```

### 7. Final Validation Checklist

```
BEFORE ACCEPTING a candidate, verify:
  □ Intent is validated against live SERP (OpenSEO)
  □ Page type matches SERP landscape
  □ Catalogue has sufficient depth (in-stock products ≥ site.min_in_stock_products, per in_stock_definition)
  □ No brand query contamination
  □ Not duplicating existing recommendation
  □ Impact/confidence/effort assigned with justification
  □ Measurement metric defined (not vanity metric)
  □ Acceptance criteria are specific and verifiable

IF any check fails → REJECT with specific reason
```

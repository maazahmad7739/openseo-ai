# ============================================================
# Skill 2: Diagnose Existing Page
# ============================================================
# Used when: Agent analyzes why an existing page is underperforming
# and determines the root cause constraint.
# ============================================================

## Diagnostic Order (Strict Sequence)

The diagnostic must follow this order. Do NOT skip steps or jump to conclusions.
Each step either identifies the constraint or clears the page for the next check.

---

### Step 1: Indexability & Rendering

```
CHECK:
  1. Is the page indexable?
     - status_code = 200?
     - No noindex tag?
     - Not blocked by robots.txt?
     - Canonical is self-referencing (or correct cross-domain)?
     - Not blocked by X-Robots-Tag header?
  
  2. Can Googlebot render the page?
     - JavaScript-rendered content visible in rendered HTML?
     - Critical content not hidden behind client-side rendering?
     - CSS not hiding content?
     - Check pages.render_status from the OpenSEO crawl:
       render_failed → "Page loads content via JS but rendering failed/stale"
       (raw_html_hash present, rendered_html_hash == raw_html_hash → JS body never executed)
  
  3. Is the page in the sitemap?
     - Listed in sitemap.xml?
     - Sitemap is referenced in robots.txt?
     - Last modified date is recent?

IF ANY ISSUE FOUND:
  → CONSTRAINT: "Page is not fully indexable/renderable"
  → ACTION TYPE: technical_fix
  → EVIDENCE: Specific blocking factor with HTTP header / meta tag / robots.txt evidence
  → SKIP TO: Work specification (no need for further diagnosis)
```

---

### Step 2: Page Type & Intent Match

```
CHECK:
  1. What is the page's actual type?
     - collection, product, blog, landing, category, homepage?
  
  2. What is the search intent for the target cluster?
     - Informational, commercial, transactional?
  
  3. Does page type match intent?
     MATCH TABLE:
       Informational query → blog/article ✅
       Commercial query → collection ✅, product page ⚠️ (partial)
       Transactional query → product page ✅, collection ✅ (if filtered)
       Navigational query → homepage ✅
     
     MISMATCH EXAMPLES:
       Commercial query → blog post (❌ mismatch)
       Transactional query → category listing (❌ mismatch)
       Informational query → product page (❌ mismatch)

  4. Compare page title/H1 to query:
     - Does the primary keyword appear in title?
     - Does the primary keyword appear in H1?
     - Are they semantically related?

IF MISMATCH FOUND:
  → CONSTRAINT: "Page type does not match search intent"
  → DIAGNOSIS: "Query '{keyword}' has {intent} intent but page is {page_type}"
  → EVIDENCE: SERP analysis showing dominant page type + current page metadata
  → ACTION TYPE: improve_page (if page exists) or create_page (if wrong type entirely)
```

---

### Step 3: Catalogue & Product Coverage

```
CHECK:
  1. How many products does the page display?
     - product_count from crawl data
  
  2. How many products match the target cluster?
     - Compare page products to cluster.matching_product_ids
     - Calculate overlap percentage
  
  3. Are the matching products in stock?
     - in_stock_product_count vs matching_product_count
     - "In stock" per site.in_stock_definition (any_variant | majority_variants) —
       a product with multiple variants is counted only by that rule, never guessed
  
  4. Is the product range competitive?
     - Price range vs competitors
     - Brand diversity
     - Category breadth
  
  5. Are filters/facets available?
     - Can users narrow by price, brand, rating, etc.?
     - Are faceted URLs canonicalized correctly?

SCORING:
  catalogue_coverage = matching_products_on_page / total_matching_products_in_cluster
  
  IF coverage < 0.5:
    → CONSTRAINT: "Insufficient catalogue coverage — page shows {n} of {total} relevant products"
    → EVIDENCE: Product list comparison
    → ACTION: improve_page (add products, fix collection rules)
  
  IF coverage >= 0.5 but products are out of stock:
    → CONSTRAINT: "Catalogue coverage adequate but availability poor — {n} of {m} in stock"
    → ACTION: inventory/merchandising issue (may be outside SEO scope, flag for ops)
```

---

### Step 4: Title & SERP Proposition

```
CHECK:
  1. Current title tag:
     - Length (optimal: 50-60 chars)
     - Primary keyword presence (front-loaded?)
     - Value proposition (price, delivery, range, quality signals?)
     - Differentiation from competitors?
  
  2. Current meta description:
     - Length (optimal: 150-160 chars)
     - Includes target keyword?
     - Has compelling CTA?
     - Differentiates from SERP competitors?
  
  3. Compare to top 5 SERP competitors:
     - What value props do they use?
     - What are we missing?
     - Is our title indistinguishable from others?

IF WEAK TITLE/DESCRIPTION:
  → CONSTRAINT: "Title/description does not compete in SERP"
  → EVIDENCE: Side-by-side comparison with top 3 competitors
  → ACTION: improve_page (title/meta optimization)
  → WORK: Specific new title and description with A/B test plan
```

---

### Step 5: Content & Information Gaps

```
CHECK:
  1. Content depth:
     - Word count vs competitor average
     - Unique content percentage (vs template boilerplate)
     - Content freshness (last updated date)
  
  2. Content structure:
     - H2/H3 hierarchy covers key topics?
     - Internal navigation within page?
     - FAQ/schema for common questions?
  
  3. Information gaps:
     - What questions does the query ask that the page doesn't answer?
     - What comparison points are missing?
     - What trust signals are missing (reviews, guarantees, shipping info)?
  
  4. Visual content:
     - Product images adequate?
     - Video content present?
     - Comparison tables?

IF CONTENT GAP:
  → CONSTRAINT: "Content does not match query information needs"
  → DIAGNOSIS: Specific missing elements
  → ACTION: improve_page (content enhancement)
  → WORK: Detailed content brief with sections, word counts, required elements
```

---

### Step 6: Internal Linking

```
CHECK:
  1. internal_links_in:
     - How many pages link to this page?
     - Are the linking pages topically relevant?
     - Are the anchor texts descriptive?
  
  2. internal_links_out:
     - Does this page link to related pages?
     - Are there contextual links to parent/sibling collections?
     - Broken outbound links?
  
  3. Navigation placement:
     - Is the page in main navigation?
     - Is it in footer?
     - Is it linked from homepage?
     - Breadcrumb depth?
  
  4. Link equity flow:
     - Linking pages' own authority (organic sessions as proxy)
     - Is the page isolated from high-traffic pages?

IF LINK ISSUE:
  → CONSTRAINT: "Insufficient internal link equity — page receives {n} links from {m} pages"
  → EVIDENCE: Link audit with source pages and anchor texts
  → ACTION: improve_page (internal linking)
  → WORK: Specific linking plan with source pages and anchor texts
```

---

### Step 7: Authority Gap

```
CHECK (ONLY if all above steps are clear):
  1. Domain authority comparison:
     - Our DA vs competitors ranking for this cluster
     - Backlink profile comparison
  
  2. Page authority:
     - External links to this specific page
     - Internal link equity distribution
  
  3. Competitive context:
     - Is this a high-competition keyword?
     - What would it take to compete?
     - Is the authority gap bridgeable with on-site optimization?

IF AUTHORITY GAP:
  → CONSTRAINT: "Authority deficit — competitors have significantly stronger backlink profiles"
  → DIAGNOSIS: "Domain authority gap of {n} points; page receives {m} external links vs competitor average of {x}"
  → ACTION: improve_page (but note: this may require off-site work too)
  → WORK: On-site improvements that can compete despite authority gap
  → NOTE: "Backlink outreach is out of scope for v1 — document this as a secondary constraint"
```

---

## Output Format

```json
{
  "page_url": "https://example.com/collections/headphones",
  "cluster_id": "uuid",
  "diagnostic_result": {
    "primary_constraint": "catalogue_coverage",
    "constraint_severity": "high",
    "diagnosis": "Page shows only 6 of 24 relevant products in the cluster. Collection rules filter out products based on an outdated tag system.",
    "evidence": [
      { "source": "catalogue", "finding": "24 products match cluster, only 6 displayed" },
      { "source": "crawl", "finding": "Collection uses tag filter 'noise-cancelling' but 18 products use 'anc' tag instead" }
    ],
    "recommended_action": "improve_page",
    "secondary_constraints": [
      {
        "constraint": "title_optimization",
        "severity": "medium",
        "note": "Title is generic — could be more competitive"
      }
    ]
  },
  "work_required": [
    {
      "owner": "engineering",
      "task": "Update collection rules to include all relevant product tags",
      "acceptance_criteria": "All 24 matching products appear in collection"
    }
  ]
}
```

# ============================================================
# Skill 3: Write Implementation Specification
# ============================================================
# Used when: Agent translates a validated, diagnosed opportunity
# into an exact implementation brief that operators can execute.
# ============================================================

## Purpose

This skill produces an **implementation brief**, not generic SEO commentary.
Every output must be specific enough that a developer, content writer, or SEO
specialist can execute it without asking follow-up questions.

---

## Specification Structure

### 1. Proposed URL

```
RULES:
  - Use lowercase, hyphenated slugs
  - Include primary keyword naturally
  - Follow existing URL patterns on the site
  - No duplicate slugs (check existing pages)
  
FORMAT:
  /collections/{primary-keyword-slug}
  /pages/{topic-slug}
  /products/{product-slug}

EXAMPLE:
  Input: cluster.primary_keyword = "wireless noise cancelling headphones"
  Output: /collections/wireless-noise-cancelling-headphones
  
  CHECK FIRST:
    IF /collections/noise-cancelling-headphones exists → use different variant
    IF /collections/wireless-headphones exists → consider if this is duplicate intent
```

---

### 2. Title & H1 Direction

```
TITLE FORMULA:
  {Primary Keyword} - {Value Proposition} | {Brand}
  
  Value proposition options:
    - Price: "From $X" or "Best Prices"
    - Range: "{N}+ Options" or "Shop All"
    - Quality: "Premium" or "Top Rated"
    - Delivery: "Free Shipping" or "Same Day"
  
  LENGTH: 50-60 characters (include primary keyword in first 40)

H1 FORMULA:
  {Primary Keyword} - {Qualifier}
  
  LENGTH: 20-70 characters
  RULES:
    - Must include primary keyword
    - Must be different from title (don't duplicate)
    - Must be descriptive of page content

EXAMPLES:
  Title: "Wireless Noise Cancelling Headphones | Shop 50+ Models | BrandName"
  H1: "Wireless Noise Cancelling Headphones"

  Title: "Men's Running Shoes - Free Shipping Over $50 | BrandName"
  H1: "Men's Running Shoes"
```

---

### 3. Page Sections (Content Blueprint)

```
FOR COLLECTION PAGES:
  1. HERO SECTION
     - H1 (as defined above)
     - 1-2 sentence value proposition
     - Primary CTA (Shop Now / Browse Collection)
  
  2. INTRODUCTION (100-200 words)
     - What this collection covers
     - Who it's for
     - Key buying considerations
     - Include primary and secondary keywords naturally
  
  3. PRODUCT GRID
     - Default sort: Best Selling (not random)
     - Display: 24-36 products per page
     - Pagination or infinite scroll
     - Product cards: image, title, price, rating, quick-add
  
  4. FILTER SYSTEM
     - Price range slider
     - Brand multi-select
     - Rating filter
     - Availability (in-stock only default)
     - Feature filters (relevant to category)
     - CLEAR ALL button visible
  
  5. BUYING GUIDE SECTION (200-400 words)
     - "How to Choose {Category}"
     - Answer 3-5 common questions
     - Link to detailed blog posts if they exist
     - Internal links to related collections
  
  6. FAQ SECTION
     - 5-8 questions with schema markup
     - Questions sourced from:
       - "People Also Ask" for this keyword
       - Customer service common questions
       - Competitor FAQ sections
  
  7. RELATED COLLECTIONS
     - 3-4 related collection links
     - Contextual (not random)
     - Internal link equity distribution
  
  8. BREADCRUMB
     - Home > {Category} > {This Collection}
     - Schema markup (BreadcrumbList)

FOR PRODUCT PAGES (optimize existing):
  1. Title tag update
  2. Meta description update
  3. Schema markup (Product, Review, Offer)
  4. Image alt text optimization
  5. Internal links to related products/collections
```

---

### 4. Filter & Product Rules

```
COLLECTION PRODUCT SELECTION:
  Method: Shopify collection rules OR manual curation
  
  Rules (if automated):
    product_type CONTAINS "{category}"
    OR tags CONTAINS "{relevant-tag-1}"
    OR tags CONTAINS "{relevant-tag-2}"
    AND status = "active"
    AND inventory > 0
  
  VALIDATION:
    - Run query to verify product count matches expected
    - Check that top-selling products in cluster are included
    - Verify no irrelevant products are included
    - Confirm price range is competitive vs SERP competitors
  
  MANUAL OVERRIDE:
    - Pin top 3-4 products at top (best-sellers or highest margin)
    - Exclude any products that don't fit the commercial intent
    - Ensure product images are high-quality
```

---

### 5. Internal Linking Plan

```
FROM PAGES (link TO this new/existing page):
  1. Parent category page (if exists)
  2. Homepage (if high-priority collection)
  3. Related collection pages (3-5 contextual links)
  4. Blog posts mentioning this keyword (2-3 links)
  5. Product pages that belong to this collection (cross-links)

ANCHOR TEXT RULES:
  - Primary: exact match (1-2 links only)
  - Secondary: partial match or branded (5-7 links)
  - Contextual: natural language in sentences
  
  DON'T:
    - Over-optimize anchor text
    - Use "click here" or generic anchors
    - Force links where context doesn't fit

TO PAGES (this page links OUT):
  - Related collections (3-5)
  - Individual product pages (in product grid)
  - Blog content (in buying guide section)
  - FAQ answers linking to detailed guides
```

---

### 6. Technical Changes

```
REQUIRED CHANGES:
  1. URL & Canonical
     - Create page at proposed URL
     - Self-referencing canonical tag
     - Add to sitemap.xml
     - Set lastmod to current date
  
  2. Structured Data
     - CollectionPage schema
     - BreadcrumbList schema
     - FAQ schema (if FAQ section included)
     - Product schema (for products on page)
  
  3. Meta Robots
     - index, follow (no noindex)
     - No nofollow on internal links
  
  4. Page Speed
     - Lazy-load below-fold images
     - Optimize product images (WebP format)
     - Minimize render-blocking resources
     - Target: LCP < 2.5s, CLS < 0.1
  
  5. Mobile Optimization
     - Responsive product grid
     - Touch-friendly filters
     - Collapsible sections for content
     - Sticky CTA on mobile
  
  6. Analytics
     - Track: page_view, scroll, filter_use, product_click, add_to_cart
     - Custom dimension: collection_name
     - Goal: add_to_cart event from this page
```

---

### 7. Acceptance Criteria

```
EVERY recommendation MUST include verifiable acceptance criteria:

FOR NEW PAGES:
  □ Page returns HTTP 200
  □ Page is in sitemap.xml
  □ Self-referencing canonical tag present
  □ Title tag contains primary keyword (50-60 chars)
  □ H1 contains primary keyword (20-70 chars)
  □ Meta description contains keyword (150-160 chars)
  □ Structured data passes Rich Results Test
  □ Page loads in <3 seconds (LCP)
  □ Mobile responsive (passes mobile-friendly test)
  □ Internal links from ≥3 existing pages
  □ Breadcrumb displays correctly
  □ Product grid shows expected products
  □ Filters function correctly
  □ Analytics tracking fires correctly
  □ No duplicate content warnings

FOR PAGE IMPROVEMENTS:
  □ Before snapshot stored
  □ Changes implemented as specified
  □ After snapshot stored (after the action_type-specific measurement window)
  □ Measurement metric defined
  □ No unintended side effects on other pages
```

---

### 8. Measurement Plan

```
MEASUREMENT WINDOW (per action_type — resolved from measurement_window_lookup, NEVER 28 default):
   create_page  → 49 days   (discovery + crawl + ranking takes longer)
   improve_page → 28 days
   consolidate  → 28 days
   technical_fix→ 21 days

BASELINE (store at implementation):
  - Target page, previous window (measurement_window_lookup days) of GSC data:
    - impressions, clicks, CTR, avg_position
  - Target page, GA4 data for the same period:
    - organic_sessions, add_to_carts, orders, revenue
  - Comparable unaffected pages (control group) — same site, same period, same
    page type/template — stored in measurement_snapshots with
    comparison_type='control' (Priority 3.5). The control group's delta is
    computed alongside the target's delta to catch algorithm-update or
    seasonality confounds. This is a simple aggregate query, not causal inference.
  - Year-over-year (same period last year) is an OPTIONAL bonus signal
    (comparison_type='yoy', Priority 3.6). For new/low-traffic sites YoY is
    empty: fall back cleanly to the window-vs-window comparison with no error.
    YoY is never a required input.

MEASUREMENT (window days post-implementation):
  - Primary metric: organic sessions for target cluster
  - Secondary metrics: orders, revenue, conversion rate
  - Ranking metrics: impressions, position, CTR

CLASSIFICATION (Priority 3.3 — raw percentage movement is NOT enough):
  Before labelling Won/Lost, run a two-proportion/binomial significance test on
  the click/impression deltas, scaled by sample size (statistical noise filter).
  Low-traffic pages (below min_sample_for_significance, e.g. 50 impressions)
  cannot produce noise-free signals → classify as INCONCLUSIVE, not Won/Lost.

  WON:
    - Target search metric improved by >15%
    - OR target commercial metric improved by >10%
    - AND the improvement clears the significance test
    - AND not contradicted by the control group's trend

  NEUTRAL:
    - No meaningful movement (<5% change)
    - OR improvement is within normal variance
    - OR movement fails the significance test

  LOST:
    - Target metric declined by >15%
    - AND decline is beyond seasonal pattern (control group / YoY if available)
    - AND decline clears the significance test

  INCONCLUSIVE:
    - Insufficient data (low traffic page — below the minimum sample bar)
    - OR seasonal demand shift masks result
    - OR external factor impacted measurement
    - OR YoY was unavailable AND the control group was also unavailable

LEARNINGS CAPTURE:
  - What worked and why
  - What didn't work and why
  - What to try next time
  - Patterns across similar recommendations
```

---

## Output Format

```json
{
  "action_type": "create_page",
  "target_url": "",
  "proposed_url": "/collections/wireless-noise-cancelling-headphones",

  "specification": {
    "url": "/collections/wireless-noise-cancelling-headphones",
    "title": "Wireless Noise Cancelling Headphones | Shop 50+ Models | BrandName",
    "h1": "Wireless Noise Cancelling Headphones",
    "meta_description": "Shop our range of wireless noise cancelling headphones. Free shipping on orders over $50. 30-day returns. Top brands at competitive prices.",
    
    "page_sections": [
      {
        "section": "hero",
        "content": "H1 + value proposition + primary CTA"
      },
      {
        "section": "introduction",
        "word_count": "150-200",
        "keywords_to_include": ["wireless noise cancelling headphones", "ANC headphones", "best noise cancelling"],
        "internal_links": 2
      },
      {
        "section": "product_grid",
        "products_per_page": 24,
        "default_sort": "best_selling",
        "product_card_elements": ["image", "title", "price", "rating", "quick_add"]
      },
      {
        "section": "filters",
        "required_filters": ["price_range", "brand", "rating", "availability", "feature"]
      },
      {
        "section": "buying_guide",
        "word_count": "200-400",
        "topics": ["How to choose", "Key features to compare", "Active noise cancelling vs passive"],
        "internal_links": 3
      },
      {
        "section": "faq",
        "question_count": 6,
        "schema_type": "FAQPage",
        "source": "People Also Ask + customer service data"
      },
      {
        "section": "related_collections",
        "count": 3,
        "selection": "contextually related"
      }
    ],
    
    "product_rules": {
      "method": "automated",
      "rules": {
        "product_type": "Headphones",
        "tags_include": ["wireless", "noise-cancelling"],
        "status": "active",
        "inventory": "> 0"
      },
      "manual_override": {
        "pin_top": ["product-handle-1", "product-handle-2"],
        "exclude": []
      },
      "expected_product_count": 24
    },
    
    "internal_links": {
      "inbound": [
        { "source": "/collections/headphones", "anchor": "wireless noise cancelling headphones", "type": "exact_match" },
        { "source": "/blog/best-headphones-2026", "anchor": "our noise cancelling collection", "type": "contextual" },
        { "source": "/collections/wireless-audio", "anchor": "noise cancelling headphones", "type": "partial_match" }
      ],
      "outbound": [
        { "target": "/collections/bluetooth-headphones", "anchor": "related" },
        { "target": "/blog/noise-cancelling-guide", "anchor": "learn more" }
      ]
    },
    
    "technical_requirements": [
      {
        "task": "Create Shopify collection with automated rules",
        "owner": "engineering",
        "effort": "2 hours"
      },
      {
        "task": "Add FAQ schema markup",
        "owner": "engineering",
        "effort": "1 hour"
      },
      {
        "task": "Write introduction and buying guide content",
        "owner": "content",
        "effort": "3 hours"
      },
      {
        "task": "Set up analytics tracking",
        "owner": "engineering",
        "effort": "1 hour"
      },
      {
        "task": "Add internal links from 3 existing pages",
        "owner": "SEO",
        "effort": "1 hour"
      }
    ],
    
    "acceptance_criteria": [
      "Page returns HTTP 200",
      "Page in sitemap.xml",
      "Self-referencing canonical present",
      "Title: 50-60 chars with primary keyword",
      "H1: 20-70 chars with primary keyword",
      "Meta description: 150-160 chars",
      "Structured data passes Rich Results Test",
      "LCP < 2.5s",
      "Mobile responsive",
      "≥3 inbound internal links",
      "Product grid shows expected products",
      "Filters function correctly",
      "Analytics tracking fires on add_to_cart"
    ],
    
    "measurement": {
      "window_days": 49,
      "window_source": "measurement_window_lookup[action_type=create_page]",
      "baseline_period": "2026-08-09 to 2026-09-27",
      "measurement_period": "2026-10-12 to 2026-11-30",
      "primary_metric": "organic_sessions for cluster",
      "secondary_metrics": ["orders", "revenue", "conversion_rate"],
      "control_pages": [
        "/collections/bluetooth-headphones",
        "/collections/over-ear-headphones"
      ],
      "control_group_key": "collection:collection.liquid",
      "yoy_comparison": "optional — fallback to window-vs-window if data unavailable",
      "classification": "won/lost require two-proportion significance test; low-sample → inconclusive"
    }
  }
}
```

---

## Quality Checks

Before finalizing any implementation brief:

1. **Specificity**: Can a developer execute this without asking questions?
2. **Completeness**: All sections (URL, title, content, links, technical, measurement) filled?
3. **Accuracy**: Do product counts, keyword usage, and link targets match real data?
4. **Feasibility**: Is the effort estimate realistic for the required work?
5. **Measurability**: Is the baseline defined and the measurement plan specific?

# ============================================================
# OpenSEO++ MVP — Agent Execution Structure
# ============================================================

## Overview

A single principal SEO agent receives pre-filtered candidates from the four
deterministic generators and produces ≤5 production-ready recommendations.

The agent is invoked once per weekly cycle after candidate generation completes.

---

## Agent Input Format

```json
{
  "run_id": "uuid",
  "site": {
    "domain": "example.com",
    "site_name": "Example Store",
    "crawl_date": "2026-09-01",
    "total_pages": 2340,
    "total_collections": 85,
    "shopify_theme": "dawn"
  },
  "candidates": [
    {
      "candidate_id": "uuid",
      "generator": "missing_page | existing_opportunity | technical_fix | cannibalization",
      "action_type": "create_page | improve_page | consolidate | technical_fix",
      "target_url": "",
      "proposed_url": "",
      "cluster": {
        "cluster_id": "uuid",
        "primary_keyword": "wireless noise cancelling headphones",
        "keywords": ["best noise cancelling headphones", "wireless ANC headphones", ...],
        "intent": "commercial",
        "search_volume": 14200,
        "commercial_value": "high"
      },
      "evidence_summary": {
        "gsc": { "impressions_28d": 8400, "avg_position": 11.2, "clicks_28d": 320 },
        "catalogue": { "matching_products": 12, "in_stock": 10, "avg_price": 189.99 },
        "competitors": [
          { "url": "https://competitor.com/collections/noise-cancelling", "position": 3 }
        ],
        "technical": null
      },
      "page_match_score": {
        "aggregate": 0.42,
        "intent_page_type": 0.60,
        "catalogue_match": 0.75,
        "gsc_evidence": 0.30,
        "semantic_content": 0.20
      }
    }
  ],
  "page_context": {
    "candidate_1_target_url": {
      "title": "Headphones - Our Store",
      "h1": "All Headphones",
      "page_type": "collection",
      "template": "collection.liquid",
      "product_count": 24,
      "indexable": true,
      "status_code": 200,
      "internal_links_in": 15,
      "organic_sessions_30d": 450,
      "revenue_30d": 2340.00
    }
  },
  "rejection_context": {
    "site_id": "…",
    "last_rejections": [
      {
        "generator": "missing_page",
        "reason": "Insufficient catalogue depth for proposed collection — only 3 products in stock",
        "primary_keyword": "…"
      },
      {
        "generator": "existing_opportunity",
        "reason": "Low commercial value — keyword has demand but limited revenue potential",
        "primary_keyword": "…"
      }
    ],
    "bulk": "Avoid re-proposing these patterns this week."
  },
  "config": {
    "max_recommendations": 5,
    "excluded_patterns": ["brand"],
    "measurement": {
      "window": "resolved per action_type from measurement_window_lookup",
      "yoy_required": false,
      "control_group_required": true,
      "min_sample_for_significance": 50
    },
    "site": {
      "catalogue_size_tier": "medium",
      "page_match_threshold": 0.65,
      "in_stock_definition": "any_variant",
      "agent_prefetch_limit": 25
    }
  }
}
```
> NOTE (Priority 3.2): `title_h1` is no longer a separate top-level component — it
> is folded into `semantic_content` as a sub-signal (title/H1 text is weighted
> higher inside the embedding/similarity computation).
> NOTE (Priority 2.1): competitor/SERP evidence is sourced from OpenSEO
> (`openseo_serp_snapshots` via the connector's fetch("serp")), never from GSC-derived tables.
> NOTE (Priority 4.1): only the pre-ranked top slice (~25) reaches this stage; the
> rest stay in a backup pool.

---

## Agent Execution Prompt

```
You are the OpenSEO Principal Agent. Your job is to evaluate SEO action candidates
and produce a maximum of 5 production-ready recommendations.

PROCESS:
1. For each candidate, load the relevant skill file based on candidate type.
2. Validate the candidate using live SERP data (OpenSEO connector) and page context.
3. Reject weak candidates with a documented reason.
4. For accepted candidates, diagnose the constraint and specify exact work.

CONTEXT:
- A rejection_context block carries the last N operator rejections for this site.
  Do not re-propose candidates that follow the same rejected patterns.
- Measurement windows are resolved per action_type (e.g. create_page ≈ 49 days,
  improve_page ≈ 28 days) — never assume a fixed 28-day window.

RULES (in order of priority):
- Never invent traffic or revenue forecasts.
- Never recommend content solely because a keyword has volume.
- Never treat audit hygiene (alt text, meta descriptions) as growth actions.
- Never recommend backlinks before evaluating on-site constraints.
- Never create multiple pages for indistinguishable intent.
- Never claim causality from before/after movements.
- Every recommendation must include: action_type, target, evidence, diagnosis,
  work_required with acceptance criteria, impact, confidence, effort, owner,
  and measurement plan.

OUTPUT: Return a JSON array of 0-5 recommendations following the schema below.
```

---

## Agent Output Schema

```json
{
  "run_id": "uuid",
  "agent_version": "1.0.0",
  "evaluated_at": "2026-09-06T10:00:00Z",
  "total_evaluated": 20,
  "total_rejected": 15,
  "rejection_log": [
    {
      "candidate_id": "uuid",
      "reason": "Keyword volume (320/mo) below commercial threshold; page match score 0.41 indicates weak catalogue alignment",
      "skill_applied": "validate_opportunity"
    }
  ],
  "recommendations": [
    {
      "action_type": "create_page",
      "target_url": "",
      "proposed_url": "/collections/wireless-noise-cancelling-headphones",
      "query_cluster": ["wireless noise cancelling headphones", "best ANC headphones"],
      "diagnosis": "High-intent commercial cluster with 14,200 monthly searches has no dedicated collection page. Existing /headphones page ranks position 11 but targets generic intent. Competitors with dedicated collection pages rank positions 2-4.",

      "evidence": [
        {
          "source": "GSC",
          "finding": "Generic /headphones page receives 8,400 impressions for this cluster at avg position 11.2 but CTR is only 3.8% — intent mismatch between generic page and commercial query."
        },
        {
          "source": "catalogue",
          "finding": "12 matching products in stock, average price $189.99, 3 products have 4+ reviews. Sufficient catalogue depth for a dedicated collection."
        },
        {
          "source": "SERP",
          "finding": "Top 3 results are all dedicated collection pages with structured data, price filters, and product grids. Our generic page lacks these commercial signals."
        },
        {
          "source": "crawl",
          "finding": "No existing page targets this cluster specifically. Page-match score 0.42 (below 0.65 threshold) confirms gap."
        }
      ],

      "work_required": [
        {
          "owner": "SEO",
          "task": "Define collection page requirements: title, H1, meta description, URL structure, initial product selection, internal linking targets",
          "acceptance_criteria": "Page brief approved with target keyword map, 500+ word unique content outline, filter set defined"
        },
        {
          "owner": "content",
          "task": "Write collection page content: introduction, buying guide sections, FAQ schema, product category descriptions",
          "acceptance_criteria": "Content passes uniqueness check, includes target keywords naturally, minimum 800 words, includes internal links to related collections"
        },
        {
          "owner": "engineering",
          "task": "Implement collection page template with Product structured data, breadcrumbs, filter system, and canonical tag",
          "acceptance_criteria": "Page passes Rich Results Test, loads in <3s, is included in sitemap, returns HTTP 200 with self-referencing canonical"
        },
        {
          "owner": "SEO",
          "task": "Set up internal linking from /headphones parent page and related collection pages to new collection",
          "acceptance_criteria": "At least 3 contextual internal links point to new collection within 48 hours of launch"
        }
      ],

      "impact": "high",
      "confidence": "high",
      "effort": "days",

      "measurement_metric": "organic sessions and orders for query cluster",
      "measurement_window_days": 49,
      "measurement_baseline": {
        "impressions_28d": 8400,
        "avg_position": 11.2,
        "clicks_28d": 320,
        "organic_sessions": 450,
        "orders": 12,
        "revenue": 2280.00
      }
    }
  ]
}
```
> NOTE: `measurement_window_days` is resolved from `measurement_window_lookup` by
> action_type — shown here as 49 because this is a `create_page` recommendation.

---

## Validation Rules (Applied Before Agent)

The following checks run automatically on each candidate before agent evaluation.
All thresholds are resolved per site from `threshold_tiers` by
`catalogue_size_tier` (or a per-site override in `site_config`) — never hardcoded
in the generators:

1. **Deduplication**: If a candidate cluster already has an active recommendation, skip it.
2. **Brand exclusion**: If primary keyword matches any brand query pattern, skip it.
3. **Minimum signal**: Candidate must have ≥100 impressions or ≥5 organic sessions.
4. **Threshold check**: Generator-specific thresholds (search_volume, in_stock_products, page_match_threshold, etc.) must be met.
5. **Recent exclusion**: If the same page had a recommendation in the last 90 days, flag for manual review.
6. **Pre-ranking (Priority 4.1)**: Rank valid candidates by priority_score/search_volume, keep the top ~25 (`site_config.agent_prefetch_limit`) for the fetch stage, and hold the rest as a backup pool — pulled in only if the top slice yields fewer than 5 (`site_config.agent_min_valid`) valid recommendations.

---

## Agent Invocation Flow

```
Weekly cron (Sunday 02:00 UTC) — per site with :site_id bound:
  1. Run data sync job (GSC, Shopify, GA4, OpenSEO)
  2. Run crawl/audit pull job (results pulled FROM OpenSEO, not our own crawler)
  3. Execute Generator 1 SQL → candidates_missing_pages (site-scoped)
  4. Execute Generator 2 SQL → candidates_existing_opportunities (site-scoped)
  5. Execute Generator 3 SQL → candidates_technical_fixes (site-scoped)
  6. Execute Generator 4 SQL → candidates_cannibalization (site-scoped)
  7. Combine candidates per site (each generator keeps its own LIMIT quota)
  8. Apply validation rules (dedup, brand, tier-resolved thresholds)
  9. Pre-rank candidates and select top ~25 (backup pool for the rest)
 10. Load last N rejection reasons from rejection_log → rejection_context block
 11. For each candidate in the top slice:
      a. Fetch live SERP data for primary keyword (OpenSEO connector)
      b. Fetch target page content and metadata
      c. Fetch top 3 competitor pages
      d. Prepare candidate context JSON
 12. If fewer than 5 candidates pass validation, draw from the backup pool
 13. Send batch of prepared candidates to agent
 14. Agent evaluates, diagnoses, and specifies work
 15. Store recommendations in recommendations table; log rejections to rejection_log
 16. Send notification (email/Slack) with summary
```

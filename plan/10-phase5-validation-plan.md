# ============================================================
# OpenSEO++ MVP — Phase 5 Validation Plan
# ============================================================
# Tests the whole system before rollout: 3 site-size tiers, SEO-expert grading,
# and concrete definitions for the tracking metrics.
#
# Gates: Phase 5 starts only after Priorities 1-3 are verified against a seeded
# test database for a real site (generators run clean per site, OpenSEO +
# semantic scoring populate Generator 1's inputs, design corrections are in place).
# ============================================================

# ------------------------------------------------------------
# 1. TEST SITES AND SELECTION CRITERIA
# ------------------------------------------------------------
# One site per catalogue-size tier, chosen to exercise different failure modes.
#
# 1a. TiER 1 — Small (< 500 indexed URLs)
#     Selection criteria:
#       - Independent retailer or low-catalogue DTC store
#       - Thin catalogue coverage (most clusters have few matching products)
#       - Historically low organic traffic (tests YoY fallback + significance bar)
#     Expected pressure: generator thresholds too strict/loose, low sample noise,
#     page-match scores based on thin catalogue.
#
# 1b. TIER 2 — Medium (500-5,000 URLs)
#     Selection criteria:
#       - Established Shopify store with full collections + product pages
#       - Real but imperfect technical health (some noindex, some canonicals, some orphans)
#       - Enough GSC history for 28-day comparisons and a usable control group
#     Expected pressure: cannibalization (faceted URLs), existing-page opportunities,
#     technical root causes, and appred_app in stock/out-of-stock dynamics.
#
# 1c. TIER 3 — Large faceted catalogue (> 5,000 URLs)
#     Selection criteria:
#       - Catalogue with material faceting / filter parameterization
#       - Multiple templates and duplicate-faceted URL risk
#       - High crawl volume; large site → tier defaults must raise the match bar
#     Expected pressure: duplicate_faceted, sitemap_index_mismatch, JS rendering,
#     scale/perf of the generators, and the agent pre-ranking cost bound.
#
# Each site must have: site_config row (with catalogue_size_tier), OpenSEO
# connector access, GSC + GA4 + Shopify connectors configured, and ≥ 6 weeks of
# historical data before recommendations are measured.

# ------------------------------------------------------------
# 2. SEO-EXPERT GRADING RUBRIC
# ------------------------------------------------------------
# For every recommendation produced (accepted or not), an SEO expert scores the
# candidate on a 5-point scale (or 0-4 per dimension, averaged):
#
#   a) Accuracy of diagnosis (does the stated constraint match the page/SERP?)
#      - 1 = diagnosis contradicts evidence
#      - 3 = plausible, partially supported
#      - 5 = diagnosis fully supported by evidence and verified against SERP
#
#   b) Action type fit (create/improve/consolidate/technical vs. what the data shows)
#      - 1 = wrong action type
#      - 5 = clearly the right action
#
#   c) Work plan completeness (are acceptance criteria specific and testable?)
#      - 1 = vague/vapor
#      - 5 = executable without follow-up questions
#
#   d) Impact/priority correctness (would this be near the top of a human SEO backlog?)
#      - 1 = not on a sane backlog
#      - 5 = top-of-list
#
#   e) Measurement plan quality (window, metric, control group, YoY fallback)
#      - 1 = not measurable as written
#      - 5 = measurement plan fully specified and feasible
#
# RULE: the expert grades BLIND to generator identity and to the agent's own
# confidence/impact labels, to avoid label-anchoring.

# ------------------------------------------------------------
# 3. NEGATIVE-VALIDATION (quiet rejection audit)
# ------------------------------------------------------------
# Every week the system rejects many candidates. The expert spot-audits a sample
# (≥ 10/week/site) of REJECTED candidates with the same rubric. A candidate that
# the expert would have accepted, but the system rejected, is a FALSE NEGATIVE.
# Track the false-negative rate alongside the false-positive rate below.

# ------------------------------------------------------------
# 4. METRIC DEFINITIONS AND QUERIES
# ------------------------------------------------------------

# 4a. FALSE-POSITIVE RATE
#       false_positive = recommendation that an SEO expert grades as not
#                        actionable/correct (expert average <= 2 across a-c on
#                        the rubric) OR that fails its own acceptance criteria.
#       Definition:
#         false_positive_rate(week) =
#             expert-flagged_as_bad(recommendations in week)
#             / total recommendations graded in that week
#       Query shape:
#         SELECT week, count(*) FILTER (WHERE expert_grade <= 2)::float
#                / count(*) AS fp_rate
#         FROM recommendation_reviews       -- phase-5 review table
#         GROUP BY week;

# 4b. ACCEPTANCE RATE
#       acceptance_rate(week) = approved / (approved + rejected_by_operator)
#       NOTE: operator "reject" differs from expert "false positive" — an
#       operator may reject a correct recommendation for business reasons.
#         SELECT week,
#                count(*) FILTER (WHERE status='approved')::float /
#                count(*) FILTER (WHERE status IN ('approved','rejected')) AS acceptance_rate
#         FROM recommendations
#         WHERE status != 'proposed'  -- decided this week
#         GROUP BY week;

# 4c. IMPLEMENTATION RATE
#       implementation_rate(week) = implemented / approved
#       (an approved recommendation may stall; the rate tracks operational follow-through)
#         SELECT week,
#                count(*) FILTER (WHERE status='live')::float /
#                count(*) FILTER (WHERE status IN ('approved','live')) AS implementation_rate
#         FROM recommendations
#         GROUP BY week;

# 4d. RESULT TRACKING (action-type-adjusted windows)
#       Each recommendation is measured at measurement_window_lookup[action_type].
#       Classification requires the significance test (Skill 3 §8) and a control
#       group baseline; YoY is optional. Queries:
#         SELECT generator, action_type,
#                count(*) FILTER (WHERE result='won')::float /
#                count(*) FILTER (WHERE result IN ('won','lost','neutral')) AS improvement_rate
#         FROM recommendations
#         WHERE result != 'pending' AND status = 'measured'
#         GROUP BY generator, action_type;
#
#       Movement vs control:
#         SELECT r.recommendation_id,
#                ms.impressions - msb.impressions AS target_delta,
#                (SELECT max(control_impressions_after) - max(control_impressions_before)
#                 ...) AS control_delta
#         FROM recommendation r
#         JOIN measurement_snapshots ms (target after)
#         JOIN measurement_snapshots msb (target before)
#         WHERE ms.comparison_type='target' AND msb.snapshot_type='baseline';

# ------------------------------------------------------------
# 5. GATES / DURATION
# ------------------------------------------------------------
# Duration: 6 consecutive weekly cycles minimum per site (at least one full
# create_page measurement window). Data collected:
#   - ≥ 6 weeks of per-generator candidate counts + expert grades
#   - 90-day cooldown respected so a page isn't re-recommended during the test
#
# PASS CRITERIA (all must hold to proceed past Phase 5):
#   - All four generators run clean per site on real data for 6 weeks
#   - Generator 1's inputs are populated end-to-end (OpenSEO volumes + semantic scores)
#   - false_positive_rate <= 20% over the graded sample
#   - SEO-expert mean grade (dimensions a-e) >= 3.5
#   - acceptance_rate >= 50%, implementation_rate >= 50%
#   - improvement_rate (won / measured) >= MVP target share used in the MVP Score
#
# FAILURE RESPONSE: any failing gate is re-opened as a corrective work item
# before the system is exposed to additional sites or the UI workflow.
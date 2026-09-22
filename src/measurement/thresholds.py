"""Single source of truth for measurement thresholds (fix 2.4).

Every numeric classification/threshold constant is defined exactly once here.
plan/08 §8 documents the bands; plan/05 documents min_sample_for_significance
as site-resolvable config. No other module may restate these values.
"""

# Significance level for the two-proportion z-test (plan/08 §8).
ALPHA = 0.05

# WON band: target search metric improved by more than this fraction.
IMPROVE_THRESHOLD = 0.15

# WON band (commercial): revenue improved by more than this fraction.
COMMERCIAL_IMPROVE = 0.10

# Neutral band: movement within ±this fraction is "no meaningful movement".
NEUTRAL_BAND = 0.05

# LOST band: target metric declined by more than this fraction.
DECLINE_THRESHOLD = 0.15

# Difference-in-differences (Priority 3.5): when the control group itself
# moved over the window, the target's lift must beat the control lift by
# more than this fraction to count as on-page causality rather than a
# sitewide swing (seasonality/algorithm update).
DID_IMPROVE_THRESHOLD = 0.10

# plan/05 config.measurement.min_sample_for_significance default. Per-site
# overrides come from site_config.min_sample_for_significance (see
# classify.resolve_min_sample). NOTE: this column must exist in site_config;
# it is added by plan migration (see 14-min-measure-sample.sql) — when absent,
# resolve_min_sample falls back cleanly to this default.
MIN_SAMPLE_FOR_SIGNIFICANCE = 50

# GSC reporting lag: Google Search Console performance data is typically
# 3-4 days behind the present. A recommendation whose observation window ends
# "today" would be evaluated against incomplete final days and could be
# misclassified. The measurement clock therefore requires the window to have
# been closed for this many additional days before it fires. Fixed constant
# for now; expose in site_config only if real-world latency patterns vary
# across stores (post-cutover evidence needed before adding that knob).
GSC_SETTLE_DAYS = 4
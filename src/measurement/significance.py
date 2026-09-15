"""Statistical significance testing for measurement classification (plan/08 §8).

Two-proportion z-test on click/impression deltas — a noise filter, not causal
inference. Pure Python (math only), no external stats dependency.
"""

import math

from measurement.thresholds import ALPHA  # single source of truth (fix 2.4)


def two_proportion_z_test(clicks_a, impressions_a, clicks_b, impressions_b):
    """Two-proportion z-test, two-sided.

    Returns (z_score, p_value). H0: p_a == p_b. Pooled variance under H0.
    Inputs are (successes, trials) pairs: e.g. (clicks, impressions) before
    vs after, or target vs control.
    """
    if impressions_a <= 0 or impressions_b <= 0:
        return None, None
    p1 = clicks_a / impressions_a
    p2 = clicks_b / impressions_b
    pooled = (clicks_a + clicks_b) / (impressions_a + impressions_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / impressions_a + 1 / impressions_b))
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    p_value = 2 * (1 - _normal_cdf(abs(z)))
    return z, p_value


def is_significant(p_value, alpha=None):
    if p_value is None:
        return False
    return p_value < (ALPHA if alpha is None else alpha)


def _normal_cdf(x):
    # Abramowitz-Stegun 7.1.26 (|err| < 1.5e-7), sufficient for 0.05-level tests.
    t = 1.0 / (1.0 + 0.2316419 * x)
    poly = (t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 +
            t * (-1.821255978 + t * 1.330274429)))))
    cdf = 1.0 - _normal_pdf(x) * poly
    return cdf


def _normal_pdf(x):
    return math.exp(-x * x / 2.0) / math.sqrt(2 * math.pi)


def sample_sufficient(impressions, min_sample=None):
    """Low-traffic gate: below min_sample, classification must be inconclusive.

    min_sample defaults to the single-source thresholds module (plan/05
    documented default); no local magic number.
    """
    from measurement.thresholds import MIN_SAMPLE_FOR_SIGNIFICANCE
    return impressions >= (MIN_SAMPLE_FOR_SIGNIFICANCE if min_sample is None else min_sample)
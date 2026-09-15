"""Classification (plan/08 §8 CLASSIFICATION, Priority 3.3).

Raw percentage movement is NOT enough: before labelling Won/Lost, the
click/impression deltas must clear a two-proportion significance test, the
sample must clear the minimum bar, and the control group's trend must not
contradict the target's movement.

Difference-in-differences (Priority 3.5, Comparable Unaffected Pages): the
target's lift is normalized against the paired control group's lift over the
same window. A target improvement that is matched by an identical sitewide
surge on the controls is seasonality/algorithm noise — neutral, not won.
"""

import db as database  # noqa: E402
from measurement.significance import two_proportion_z_test, is_significant, sample_sufficient
from measurement.thresholds import (
    ALPHA,
    IMPROVE_THRESHOLD,
    COMMERCIAL_IMPROVE,
    NEUTRAL_BAND,
    DECLINE_THRESHOLD,
    DID_IMPROVE_THRESHOLD,
)

MIN_SAMPLE_DEFAULT = 50  # plan/05 config.measurement.min_sample_for_significance


def _pct_change(before, after):
    """Percentage change with explicit zero-baseline semantics.

    before is None (no data)      -> None (no movement computable)
    before == 0 and after > 0     -> None here, handled as growth-from-zero by
                                     the caller (2.1 fix: never silently no-op)
    otherwise                     -> (after - before) / before
    """
    if before is None:
        return None
    if before == 0:
        return None
    return (after - before) / before


def _commercial_improved(b_orders, p_orders, b_rev, p_rev):
    orders_up = (p_orders is not None and b_orders is not None
                 and p_orders > b_orders)
    rev_up = (b_rev is not None and p_rev is not None
              and float(p_rev) > float(b_rev) * (1 + COMMERCIAL_IMPROVE))
    return bool(orders_up or rev_up)


def _control_trend(conn, recommendation_id):
    """Control-group CTR delta, baseline → post (confound catcher, Priority 3.5).

    Returns None when the trend is unavailable: missing snapshots, zero
    impressions, or a zero baseline CTR (would divide by zero — fix 1.2).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT snapshot_type, clicks, impressions
            FROM measurement_snapshots
            WHERE recommendation_id = %s AND comparison_type = 'control'
              AND snapshot_type IN ('baseline', 'post_implementation')
            """,
            (recommendation_id,),
        )
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    base = rows.get("baseline")
    post = rows.get("post_implementation")
    if not base or not post or not base[1] or not post[1]:
        return None
    base_ctr = base[0] / base[1]
    if base_ctr == 0:
        return None
    post_ctr = post[0] / post[1]
    return (post_ctr - base_ctr) / base_ctr


def _control_lifts(conn, recommendation_id):
    """Difference-in-differences inputs (Priority 3.5): control click-lift vs target lift.

    Returns None when either control snapshot is missing, has zero baseline
    impressions (relative lift undefined), or the baseline click set is empty
    in a way that makes a % comparison degenerate. Otherwise returns the
    control group's relative click lift: (post - base) / base, computed on the
    SAME paired URLs as the target's before/after (stored control_group_json).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT snapshot_type, clicks, impressions, control_group_json
            FROM measurement_snapshots
            WHERE recommendation_id = %s AND comparison_type = 'control'
              AND snapshot_type IN ('baseline', 'post_implementation')
            ORDER BY snapshot_type
            """,
            (recommendation_id,),
        )
        rows = {r[0]: r for r in cur.fetchall()}
    base = rows.get("baseline")
    post = rows.get("post_implementation")
    if not base or not post:
        return None
    if not base[1]:  # zero baseline clicks — relative control lift undefined
        return None
    if not base[2]:
        return None
    return (post[1] - base[1]) / base[1]


def _load_target_snapshots(conn, recommendation_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT snapshot_type, impressions, clicks, ctr, avg_position,
                   organic_sessions, orders, revenue
            FROM measurement_snapshots
            WHERE recommendation_id = %s AND comparison_type = 'target'
              AND snapshot_type IN ('baseline', 'post_implementation')
            ORDER BY snapshot_type
            """,
            (recommendation_id,),
        )
        return {r[0]: r[1:] for r in cur.fetchall()}


def _confound_protection(conn, recommendation_id):
    """Availability of the two confound signals (plan/08 §8 INCONCLUSIVE rule).

    Returns (control_available, yoy_available).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT comparison_type, count(*) FROM measurement_snapshots
            WHERE recommendation_id = %s
              AND comparison_type IN ('control', 'yoy')
            GROUP BY comparison_type
            """,
            (recommendation_id,),
        )
        counts = {r[0]: r[1] for r in cur.fetchall()}
    return counts.get("control", 0) > 0, counts.get("yoy", 0) > 0


def resolve_min_sample(conn, site_id):
    """min_sample_for_significance resolved from site config (plan/05).

    Single source: the documented default lives in thresholds.py; per-site
    overrides come from site_config.min_sample_for_significance when present.
    """
    from measurement.thresholds import MIN_SAMPLE_FOR_SIGNIFICANCE
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min_sample_for_significance FROM site_config WHERE site_id = %s",
            (site_id,),
        )
        row = cur.fetchone()
    if row and row[0] is not None:
        return int(row[0])
    return MIN_SAMPLE_FOR_SIGNIFICANCE


def classify_recommendation(conn, recommendation_id, min_sample=None):
    """Apply plan/08 classification rules to stored snapshots.

    min_sample: explicit override; when omitted it is resolved from site
    config (plan/05 config.measurement.min_sample_for_significance), falling
    back to the documented default. No silent function-default magic number.

    Returns dict: {result, reasons[], metrics{...}}.
    result ∈ won | neutral | lost | inconclusive.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT site_id, measurement_window_days FROM recommendations "
            "WHERE recommendation_id = %s",
            (recommendation_id,),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError(f"unknown recommendation {recommendation_id}")
    site_id, rec_window_days = row
    if min_sample is None:
        min_sample = resolve_min_sample(conn, site_id)

    rows = _load_target_snapshots(conn, recommendation_id)
    base = rows.get("baseline")
    post = rows.get("post_implementation")
    if not base or not post:
        return {"result": "inconclusive",
                "reasons": ["missing baseline or post_implementation target snapshot"],
                "metrics": None}

    (b_imp, b_clicks, b_ctr, b_pos, b_sessions, b_orders, b_rev) = base
    (p_imp, p_clicks, p_ctr, p_pos, p_sessions, p_orders, p_rev) = post
    metrics = {
        "impressions": {"before": b_imp, "after": p_imp, "pct_change": _pct_change(b_imp, p_imp)},
        "clicks": {"before": b_clicks, "after": p_clicks, "pct_change": _pct_change(b_clicks, p_clicks)},
        "organic_sessions": {"before": b_sessions, "after": p_sessions,
                             "pct_change": _pct_change(b_sessions, p_sessions)},
        "orders": {"before": b_orders, "after": p_orders, "pct_change": _pct_change(b_orders, p_orders)},
        "revenue": {"before": float(b_rev or 0), "after": float(p_rev or 0),
                    "pct_change": _pct_change(float(b_rev or 0), float(p_rev or 0))},
    }
    reasons = []
    control_delta = _control_trend(conn, recommendation_id)
    control_available, yoy_available = _confound_protection(conn, recommendation_id)
    control_lift = _control_lifts(conn, recommendation_id)

    # Gate 1 — minimum sample: low-traffic pages cannot produce noise-free
    # signals → INCONCLUSIVE, never Won/Lost (plan/08 §8).
    if not sample_sufficient(p_imp or 0, min_sample) and not sample_sufficient(b_imp or 0, min_sample):
        reasons.append(
            f"insufficient sample: impressions before={b_imp} after={p_imp} "
            f"below min_sample_for_significance={min_sample}")
        return {"result": "inconclusive", "reasons": reasons, "metrics": metrics}

    # Gate 2 — confound protection (plan/08 §8 INCONCLUSIVE rule, fix 1.3):
    # with neither a control group nor YoY data, a Won/Lost verdict would be
    # undefendable against seasonal or site-wide movement.
    if not control_available and not yoy_available:
        reasons.append(
            "no confound protection: control group unavailable AND YoY data "
            "unavailable (plan/08 mandates inconclusive)")
        return {"result": "inconclusive", "reasons": reasons, "metrics": metrics}

    # Gate 3 — two-proportion significance test on the click deltas.
    z, p_value = two_proportion_z_test(b_clicks, b_imp, p_clicks, p_imp)
    sig = is_significant(p_value, ALPHA)
    metrics["significance"] = {"z": z, "p_value": p_value, "significant": sig, "alpha": ALPHA}

    click_change = _pct_change(b_clicks, p_clicks)
    session_change = _pct_change(b_sessions, p_sessions)
    grew_from_zero_clicks = (b_clicks or 0) == 0 and (p_clicks or 0) > 0
    grew_from_zero_sessions = (b_sessions or 0) == 0 and (p_sessions or 0) > 0
    zero_baseline_growth = grew_from_zero_clicks or grew_from_zero_sessions
    if control_lift is not None:
        metrics["difference_in_differences"] = {
            "control_click_lift": round(control_lift, 4),
            "target_click_lift": round(click_change, 4) if click_change is not None else None,
        }
    improved = bool(
        (click_change is not None and click_change > IMPROVE_THRESHOLD)
        or (session_change is not None and session_change > IMPROVE_THRESHOLD)
        or _commercial_improved(b_orders, p_orders, b_rev, p_rev)
        or zero_baseline_growth
    )
    declined = click_change is not None and click_change < -DECLINE_THRESHOLD

    # DID (Priority 3.5): target improvement mirrored by an identical sitewide
    # surge on the paired controls is seasonality/algorithm noise, not
    # on-page causality → NEUTRAL, never Won. Controls only get a vote when
    # they actually moved (|control lift| beyond the neutral band); a flat
    # control group is the clean read and imposes no penalty.
    did_blocked = bool(
        control_lift is not None
        and click_change is not None
        and abs(control_lift) > NEUTRAL_BAND
        and (click_change - control_lift) < DID_IMPROVE_THRESHOLD
    )

    # WON: improved AND clears significance AND not contradicted by control.
    if improved:
        if not sig and not zero_baseline_growth:
            reasons.append(
                f"improvement (clicks {click_change:+.1%}) does not clear the "
                f"significance test (p={p_value:.4f})" if p_value is not None else
                "improvement does not clear the significance test")
        if control_delta is not None and control_delta < -NEUTRAL_BAND:
            reasons.append(f"contradicted by control group trend ({control_delta:+.1%})")
        if did_blocked:
            reasons.append(
                f"target lift ({click_change:+.1%}) not ahead of the control group's "
                f"own lift ({control_lift:+.1%}) by the required margin "
                f"(difference-in-differences < {DID_IMPROVE_THRESHOLD:+.0%}) — "
                f"sitewide swing, not on-page causality")
        if not reasons:
            detail = ""
            if zero_baseline_growth:
                detail += "grew from zero baseline"
                if click_change is None and p_clicks:
                    detail += f" (0 to {p_clicks} clicks)"
                if session_change is None and p_sessions:
                    detail += f" (0 to {p_sessions} sessions)"
            else:
                detail = f"clicks improved {click_change:+.1%}"
                if session_change is not None:
                    detail += f", sessions {session_change:+.1%}"
                if control_lift is not None:
                    detail += f" vs control lift {control_lift:+.1%}"
            if p_value is not None and not zero_baseline_growth:
                detail += f", significant (p={p_value:.4f})"
            reasons.append(detail)
            return {"result": "won", "reasons": reasons, "metrics": metrics}
        return {"result": "neutral", "reasons": reasons, "metrics": metrics}

    # LOST: decline >15% AND beyond seasonal pattern (control group / YoY)
    # AND clears the significance test.
    if declined:
        if control_delta is not None and control_delta < 0:
            reasons.append(
                f"decline (clicks {click_change:+.1%}) mirrors the control group's "
                f"trend ({control_delta:+.1%}) — seasonal/site-wide pattern")
            return {"result": "neutral", "reasons": reasons, "metrics": metrics}
        # DID on the decline side: when the paired controls rose while the
        # target fell, the target underperformed its peers — still a real
        # loss (relative underperformance), keep the lost path.
        if not sig:
            reasons.append(
                f"decline (clicks {click_change:+.1%}) does not clear the "
                f"significance test (p={p_value:.4f})" if p_value is not None else
                "decline does not clear the significance test")
            return {"result": "neutral", "reasons": reasons, "metrics": metrics}
        reasons.append(
            f"clicks declined {click_change:+.1%} beyond the control group's trend "
            f"(control {control_delta if control_delta is not None else 'n/a'}), "
            f"significant (p={p_value:.4f})" if p_value is not None else
            f"clicks declined {click_change:+.1%} beyond control, significant")
        return {"result": "lost", "reasons": reasons, "metrics": metrics}

    # NEUTRAL: <5% movement, within normal variance, or failed significance.
    if click_change is not None and abs(click_change) <= NEUTRAL_BAND:
        reasons.append(f"clicks moved only {click_change:+.1%} (within neutral band)")
    elif zero_baseline_growth:
        reasons.append("zero-baseline growth present but blocked by earlier gates")
    else:
        reasons.append("no classification rule matched with the available evidence")
    return {"result": "neutral", "reasons": reasons, "metrics": metrics}


def persist_classification(conn, recommendation_id, classification, measured_at=None):
    """Write result into the recommendations row (status → measured)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE recommendations SET
                result = %s,
                status = 'measured',
                measured_at = COALESCE(%s, now())
            WHERE recommendation_id = %s
            """,
            (classification["result"], measured_at, recommendation_id),
        )
    return classification["result"]
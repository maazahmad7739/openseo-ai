"""Post-implementation measurement (plan/08 §8 MEASUREMENT).

After the action_type-specific measurement window elapses, captures the
post_implementation snapshots (target + control) over the window period.
Primary metric: organic sessions; secondary: orders/revenue/conversion;
ranking: impressions/position/CTR.

For create_page recommendations, the target is the cluster-level GSC
footprint (same query set as baseline — fair before/after comparison
across whatever pages earned those queries).  GA4 commercial metrics are
unavailable at query level and left at 0; classification uses GSC clicks
as the primary metric for create_page.
"""

from datetime import timedelta

import db as database  # noqa: E402
from measurement.baseline import (
    resolve_window_days,
    _aggregate_metrics,
    _store_snapshot,
    _control_candidates,
    _control_url_metrics,
    control_group_key,
    CONTROL_GROUP_LIMIT,
    store_baseline,
)
from measurement.thresholds import GSC_SETTLE_DAYS


def _load_paired_control_urls(conn, recommendation_id):
    """URLs paired at baseline (control_group_json on the baseline control snapshot).

    Returns [(url, url_hash), ...] or [] when no pairing was stored —
    the post capture measures exactly this set, never a re-paired one.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT control_group_json FROM measurement_snapshots
            WHERE recommendation_id = %s AND snapshot_type = 'baseline'
              AND comparison_type = 'control' AND control_group_json IS NOT NULL
            ORDER BY created_at DESC LIMIT 1
            """,
            (recommendation_id,),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return []
    cg = row[0]
    if isinstance(cg, str):
        import json
        try:
            cg = json.loads(cg)
        except ValueError:
            return []
    urls = []
    for u in (cg.get("urls") or []):
        url, url_hash = u.get("url"), u.get("url_hash")
        if url and url_hash:
            urls.append((url, url_hash))
    return urls


def store_post_snapshots(conn, recommendation_id, measured_at):
    """Capture post_implementation snapshots over the measurement window.

    The window is the action_type-resolved number of days ENDING at
    measured_at (implementation date + window from measurement_window_lookup
    recorded in the recommendation; the snapshot period uses measured_at as
    anchor so the window length is exactly the resolved window).
    Returns the stored period.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT site_id, action_type, target_url FROM recommendations "
            "WHERE recommendation_id = %s",
            (recommendation_id,),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError(f"unknown recommendation {recommendation_id}")
    site_id, action_type, target_url = row
    if not target_url:
        raise ValueError(f"recommendation {recommendation_id} has no target_url")
    window_days = resolve_window_days(conn, action_type)
    with conn.cursor() as cur:
        cur.execute("SELECT url_hash FROM pages WHERE site_id = %s AND url = %s",
                    (site_id, target_url))
        page_row = cur.fetchone()
    if not page_row:
        raise ValueError(f"target page not in pages table: {target_url}")
    url_hash = page_row[0]

    period_end = measured_at
    period_start = period_end - timedelta(days=window_days)

    metrics = _aggregate_metrics(conn, site_id, url_hash, period_start, period_end)
    _store_snapshot(conn, recommendation_id, "post_implementation", "target", None,
                    period_start, period_end, metrics)

    # Paired control group: reuse the IDENTICAL set of URLs captured at
    # baseline (control_group_json on the baseline control snapshot) —
    # re-pairing at post time would let the control drift and break the
    # difference-in-differences comparison. Falls back to fresh pairing
    # only for legacy recommendations without a stored pairing.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT page_type, template FROM pages WHERE site_id = %s AND url_hash = %s",
            (site_id, url_hash),
        )
        ptype, template = cur.fetchone()
    control_key = control_group_key(ptype, template)
    paired = _load_paired_control_urls(conn, recommendation_id)
    if paired:
        control_urls = paired
    else:
        control_urls = _control_candidates(conn, site_id, ptype, template, url_hash,
                                           period_start, period_end)
    control_size = 0
    if control_urls:
        agg, per_url = _control_url_metrics(conn, site_id, control_urls,
                                            period_start, period_end)
        if per_url:
            _store_snapshot(conn, recommendation_id, "post_implementation", "control",
                            control_key, period_start, period_end, agg,
                            control_group_json={
                                "urls": [{"url": u["url"], "url_hash": u["url_hash"],
                                          "metrics": u["metrics"]} for u in per_url],
                                "period": [str(period_start), str(period_end)],
                                "paired_from_baseline": bool(paired),
                            })
            control_size = len(per_url)

    return {
        "window_days": window_days,
        "measurement_period": [str(period_start), str(period_end)],
        "control_group_size": control_size,
    }


def load_snapshots(conn, recommendation_id):
    """All snapshot rows for one recommendation as dicts."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot_type, comparison_type, control_group_key, "
            "control_group_json, period_start, period_end, impressions, avg_position, "
            "ctr, clicks, organic_sessions, orders, revenue "
            "FROM measurement_snapshots "
            "WHERE recommendation_id = %s ORDER BY snapshot_type, comparison_type",
            (recommendation_id,),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


def store_cluster_post_snapshots(conn, recommendation_id, measured_at):
    """Capture post_implementation cluster-level GSC snapshots.

    The query set (cluster_queries for the cluster) is the same as the
    baseline — fair before/after comparison.  New page impressions for
    those queries appear in the post window automatically.
    """
    from measurement.baseline import (
        NIL_CLUSTER_ID,
        _aggregate_cluster_metrics,
        _cluster_control_metrics,
        control_group_key,
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT site_id, action_type, cluster_id FROM recommendations "
            "WHERE recommendation_id = %s",
            (recommendation_id,),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError(f"unknown recommendation {recommendation_id}")
    site_id, action_type, cluster_id = row
    if not cluster_id or str(cluster_id) == NIL_CLUSTER_ID:
        raise ValueError(
            f"recommendation {recommendation_id} has no cluster_id"
        )

    window_days = resolve_window_days(conn, action_type)
    period_end = measured_at
    period_start = period_end - timedelta(days=window_days)

    cluster_key = control_group_key("cluster", str(cluster_id))

    # Target: cluster-level GSC footprint (same query set as baseline).
    metrics = _aggregate_cluster_metrics(conn, site_id, cluster_id,
                                         period_start, period_end)
    _store_snapshot(conn, recommendation_id, "post_implementation", "target",
                    cluster_key, period_start, period_end, metrics)

    # Control: same page_type pages, excluding cluster queries.
    control_metrics = _cluster_control_metrics(conn, site_id, cluster_id,
                                               period_start, period_end)
    control_size = 0
    if control_metrics and control_metrics["impressions"] > 0:
        rec_page_type = None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT recommended_page_type FROM keyword_clusters "
                "WHERE cluster_id = %s",
                (cluster_id,),
            )
            r = cur.fetchone()
            if r:
                rec_page_type = r[0]
        control_key = control_group_key(rec_page_type or "unknown", "cluster")
        _store_snapshot(conn, recommendation_id, "post_implementation",
                        "control", control_key, period_start, period_end,
                        control_metrics)
        control_size = 1

    return {
        "window_days": window_days,
        "measurement_period": [str(period_start), str(period_end)],
        "control_group_size": control_size,
    }


def run_measurement_batch(conn, recommendation_ids=None, reference_date=None):
    """Batch measurement over approved recommendations (never-crash policy).

    create_page rows are measured on their cluster-level GSC footprint
    (store_cluster_baseline + store_cluster_post_snapshots); create_page
    rows without a cluster_id are skipped with a logged reason.  Rows
    whose target page is missing from pages are skipped too.  Direct
    calls to store_baseline/store_post_snapshots keep their loud
    ValueError guards.

    reference_date defaults to today (UTC); tests may pass any date.
    Returns {measured: [...], skipped: [{recommendation_id, reason}]}.
    """
    from datetime import date as _date, datetime, timezone

    if reference_date is None:
        reference_date = datetime.now(timezone.utc).date()
    with conn.cursor() as cur:
        if recommendation_ids:
            cur.execute(
                "SELECT recommendation_id, implemented_at FROM recommendations "
                "WHERE status = 'approved' AND recommendation_id = ANY(%s::uuid[])",
                ([str(r) for r in recommendation_ids],),
            )
        else:
            cur.execute(
                "SELECT recommendation_id, implemented_at FROM recommendations "
                "WHERE status = 'approved'"
            )
        rows = cur.fetchall()

    measured = []
    skipped = []
    for rec_id, implemented_at in rows:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action_type, target_url, cluster_id FROM recommendations "
                    "WHERE recommendation_id = %s",
                    (rec_id,),
                )
                action_type, target_url, cluster_id = cur.fetchone()
            anchor = implemented_at.date() if implemented_at else reference_date
            window_days = resolve_window_days(conn, action_type)
            # GSC settle gate (thresholds.py single source): a window that
            # closes inside the settle buffer is not yet measurable — GSC's
            # final days would be incomplete. Shift the evaluation anchor
            # past the buffer; the window length itself is unchanged.
            if anchor + timedelta(days=window_days) > reference_date - timedelta(days=GSC_SETTLE_DAYS):
                skipped.append({
                    "recommendation_id": str(rec_id),
                    "reason": f"window closes within the {GSC_SETTLE_DAYS}-day "
                              "GSC settle buffer — not yet measurable",
                })
                continue
            if action_type == "create_page":
                from measurement.baseline import NIL_CLUSTER_ID, store_cluster_baseline
                if not cluster_id or str(cluster_id) == NIL_CLUSTER_ID:
                    skipped.append({
                        "recommendation_id": str(rec_id),
                        "reason": "create_page has no cluster_id (required for "
                                  "cluster-level measurement)",
                    })
                    continue
                store_cluster_baseline(conn, rec_id, anchor)
                post_info = store_cluster_post_snapshots(
                    conn, rec_id, anchor + timedelta(days=window_days))
            elif not target_url:
                skipped.append({
                    "recommendation_id": str(rec_id),
                    "reason": f"action_type '{action_type}' has no target_url "
                              "and is not a cluster-measurable action type",
                })
                continue
            else:
                store_baseline(conn, rec_id, anchor)
                post_info = store_post_snapshots(
                    conn, rec_id, anchor + timedelta(days=window_days))
            measured.append({
                "recommendation_id": str(rec_id),
                "action_type": action_type,
                "measurement_period": post_info["measurement_period"],
            })
        except ValueError as exc:
            skipped.append({
                "recommendation_id": str(rec_id),
                "reason": f"unmeasurable: {exc}",
            })
    conn.commit()
    return {"measured": measured, "skipped": skipped}
"""Measurement endpoints (brief §1.4): implement / measurement read / results / batch.

Calls existing pipeline functions — never re-implements their logic.
create_page implement stores its cluster-level baseline (measurement lives
on the keyword cluster, not a URL — plan/08 §8).
"""

import json
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from api.common import get_conn, get_recommendation
from api.models.schemas import (
    ImplementRequest, ImplementOut, MeasurementOut, MeasurementRunRequest,
    MeasurementRunOut, ResultsOut, SnapshotOut, ClassificationOut,
)

logger = logging.getLogger("api.measurements")

router = APIRouter(tags=["measurements"])


def _load_json(value):
    """control_group_json arrives as a dict (psycopg2 jsonb) or a JSON string.

    Also tolerates empty/[]-style JSONB states without raising: a malformed
    or non-object payload degrades to None (UI renders an empty panel) —
    never a serialization exception.
    """
    if value is None or isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


@router.post("/recommendations/{recommendation_id}/implement", response_model=ImplementOut)
def implement(recommendation_id, body: ImplementRequest, conn=Depends(get_conn)):
    rec = get_recommendation(conn, recommendation_id)
    if rec["status"] != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"implement requires status 'approved' (current: '{rec['status']}')",
        )
    implemented_at = body.implemented_at
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE recommendations SET status = 'in_progress', "
            "implemented_at = COALESCE(%s::date, now()::date), "
            "assigned_to = COALESCE(%s, assigned_to) WHERE recommendation_id = %s",
            (implemented_at, body.assigned_to, recommendation_id),
        )
    if rec["action_type"] == "create_page":
        from measurement.baseline import store_cluster_baseline
        baseline_info = store_cluster_baseline(conn, recommendation_id, implemented_at)
    else:
        from measurement.baseline import store_baseline
        baseline_info = store_baseline(conn, recommendation_id, implemented_at)
    conn.commit()
    return ImplementOut(
        recommendation_id=recommendation_id,
        status="in_progress",
        implemented_at=implemented_at,
        baseline=baseline_info,
    )


@router.post("/recommendations/{recommendation_id}/implement-safe",
             response_model=ImplementOut, include_in_schema=False)
def implement_safe(recommendation_id, body: ImplementRequest, conn=Depends(get_conn)):
    """Implementation that never 500s on degraded measurement data.

    The status transition always commits first (the operator action must
    succeed). Baseline computation is then best-effort: degraded inputs
    (target URL absent from pages/search_performance, unparseable JSONB)
    store zeroed defaults instead of raising. The baseline result reports
    what was degraded so the operator/UI can see it honestly.
    """
    rec = get_recommendation(conn, recommendation_id)
    if rec["status"] != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"implement requires status 'approved' (current: '{rec['status']}')",
        )
    implemented_at = body.implemented_at
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE recommendations SET status = 'in_progress', "
            "implemented_at = COALESCE(%s::date, now()::date), "
            "assigned_to = COALESCE(%s, assigned_to) WHERE recommendation_id = %s",
            (implemented_at, body.assigned_to, recommendation_id),
        )
    if rec["action_type"] == "create_page":
        from measurement.baseline import store_cluster_baseline
        baseline_fn = store_cluster_baseline
    else:
        from measurement.baseline import store_baseline
        baseline_fn = store_baseline
    baseline_info = None
    try:
        baseline_info = baseline_fn(conn, recommendation_id, implemented_at)
    except Exception as exc:  # never-crash policy: transition wins
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE recommendations SET status = 'in_progress', "
                "implemented_at = COALESCE(%s::date, now()::date), "
                "assigned_to = COALESCE(%s, assigned_to) WHERE recommendation_id = %s",
                (implemented_at, body.assigned_to, recommendation_id),
            )
        baseline_info = {
            "degraded": f"baseline skipped: {type(exc).__name__}",
            "window_days": None,
            "baseline_period": None,
            "control_group_size": 0,
            "control_urls": [],
            "control_group_key": None,
            "yoy_available": False,
        }
        logger.warning(
            "implement %s: baseline degraded (%s: %s) — stored zeroed fallback",
            recommendation_id, type(exc).__name__, exc,
        )
    # Audit trail (spec §3): the implement event lands in change_log with the
    # baseline metrics frozen as before_snapshot. Best-effort — an audit miss
    # never blocks the operator's status transition.
    change_log_url = rec.get("target_url") or rec.get("proposed_url") or ""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO change_log (recommendation_id, url, before_snapshot, "
                "implementation_date, implemented_by) VALUES (%s, %s, %s::jsonb, "
                "COALESCE(%s::date, now()::date), %s)",
                (recommendation_id, change_log_url,
                 json.dumps({"baseline": baseline_info,
                             "status_transition": f"{rec['status']} -> in_progress"}),
                 implemented_at, body.assigned_to or "operator"),
            )
    except Exception as exc:
        logger.warning("implement %s: change_log write skipped (%s)",
                       recommendation_id, exc)
    conn.commit()
    return ImplementOut(
        recommendation_id=recommendation_id,
        status="in_progress",
        implemented_at=implemented_at,
        baseline=baseline_info,
    )


@router.get("/recommendations/{recommendation_id}/measurement", response_model=MeasurementOut)
def get_measurement(recommendation_id, conn=Depends(get_conn)):
    rec = get_recommendation(conn, recommendation_id)
    from measurement.measure import load_snapshots
    from measurement.classify import _confound_protection
    snapshots = load_snapshots(conn, recommendation_id)
    snap_models = [
        SnapshotOut(
            snapshot_type=s["snapshot_type"],
            comparison_type=s["comparison_type"],
            control_group_key=s["control_group_key"],
            control_group_json=_load_json(s.get("control_group_json")),
            period_start=s["period_start"],
            period_end=s["period_end"],
            impressions=s["impressions"],
            avg_position=float(s["avg_position"]) if s["avg_position"] is not None else None,
            ctr=float(s["ctr"]) if s["ctr"] is not None else None,
            clicks=s["clicks"],
            organic_sessions=s["organic_sessions"],
            orders=s["orders"],
            revenue=float(s["revenue"]) if s["revenue"] is not None else None,
        )
        for s in snapshots
    ]
    control_available, yoy_available = _confound_protection(conn, recommendation_id)
    classification = ClassificationOut(
        verdict=rec.get("result") if rec["result"] != "pending" else None,
        result=rec.get("result"),
        reasons=[rec.get("diagnosis") or ""] if rec["status"] == "measured" else [],
        control_available=control_available,
        yoy_available=yoy_available,
    )
    conn.rollback()
    return MeasurementOut(
        recommendation_id=recommendation_id,
        snapshots=snap_models,
        classification=classification,
    )


@router.get("/results", response_model=ResultsOut)
def get_results(site_id, conn=Depends(get_conn)):
    """Results dashboard: verdicts, win rate, plan/10 §4d rollups."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT result, count(*) FROM recommendations "
            "WHERE site_id = %s AND status = 'measured' GROUP BY result",
            (site_id,),
        )
        verdicts = {str(r[0]): r[1] for r in cur.fetchall()}
        total = sum(verdicts.values())
        wins = verdicts.get("won", 0)
        decided = sum(v for k, v in verdicts.items() if k in ("won", "lost", "neutral"))
        win_rate = round(wins / decided, 4) if decided else None
        cur.execute(
            """
            SELECT generator, action_type,
                   count(*) FILTER (WHERE result = 'won')::float /
                   NULLIF(count(*) FILTER (WHERE result IN ('won','lost','neutral')), 0) AS improvement_rate,
                   count(*) AS measured
            FROM recommendations
            WHERE site_id = %s AND result != 'pending' AND status = 'measured'
            GROUP BY generator, action_type
            ORDER BY generator, action_type
            """,
            (site_id,),
        )
        columns = [d[0] for d in cur.description]
        rollup = [
            dict(zip(columns, r), improvement_rate=(
                round(float(r[2]), 4) if r[2] is not None else None))
            for r in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT recommendation_id, generator, action_type, target_url,
                   proposed_url, result, measured_at, diagnosis
            FROM recommendations
            WHERE site_id = %s AND status = 'measured' AND result != 'pending'
            ORDER BY measured_at DESC NULLS LAST
            LIMIT 10
            """,
            (site_id,),
        )
        columns = [d[0] for d in cur.description]
        recent = [dict(zip(columns, r)) for r in cur.fetchall()]
        # KPI impact rollup: clicks/revenue delta counted for WON
        # recommendations only (target baseline vs post window).
        cur.execute(
            """
            SELECT COALESCE(SUM(post.clicks - base.clicks), 0),
                   COALESCE(SUM(post.revenue - base.revenue), 0)
            FROM recommendations r
            JOIN measurement_snapshots base
              ON base.recommendation_id = r.recommendation_id
             AND base.snapshot_type = 'baseline'
             AND base.comparison_type = 'target'
            JOIN measurement_snapshots post
              ON post.recommendation_id = r.recommendation_id
             AND post.snapshot_type = 'post_implementation'
             AND post.comparison_type = 'target'
            WHERE r.site_id = %s AND r.status = 'measured' AND r.result = 'won'
            """,
            (site_id,),
        )
        incremental_clicks, incremental_revenue = cur.fetchone()
        # Latest measured recommendation with its before/after target
        # snapshots — feeds the "currently measured" fallback table when no
        # multi-action rollup exists yet.
        cur.execute(
            """
            WITH latest AS (
                SELECT recommendation_id FROM recommendations
                WHERE site_id = %s AND status = 'measured'
                ORDER BY measured_at DESC NULLS LAST
                LIMIT 1
            )
            SELECT r.recommendation_id, r.generator, r.action_type,
                   COALESCE(r.proposed_url, r.target_url) AS url,
                   kc.primary_keyword AS cluster,
                   r.result AS verdict,
                   ms.snapshot_type, ms.clicks, ms.avg_position,
                   ms.orders, ms.revenue, ms.impressions
            FROM latest l
            JOIN recommendations r ON r.recommendation_id = l.recommendation_id
            LEFT JOIN keyword_clusters kc ON kc.cluster_id = r.cluster_id
            LEFT JOIN LATERAL (
                SELECT DISTINCT ON (ms2.snapshot_type)
                       ms2.snapshot_type, ms2.clicks, ms2.avg_position,
                       ms2.orders, ms2.revenue, ms2.impressions
                FROM measurement_snapshots ms2
                WHERE ms2.recommendation_id = r.recommendation_id
                  AND ms2.comparison_type = 'target'
                  AND ms2.snapshot_type IN ('baseline', 'post_implementation')
                ORDER BY ms2.snapshot_type, ms2.created_at DESC
            ) ms ON true
            """,
            (site_id,),
        )
        columns = [d[0] for d in cur.description]
        detail_rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    measured_details = []
    if detail_rows:
        first = detail_rows[0]
        snaps = {r["snapshot_type"]: r for r in detail_rows if r["snapshot_type"]}
        # Pending rows surface as an honest "Observing (Day N/28)" badge:
        # day elapsed since the baseline capture, against the resolved
        # measurement window (never a hardcoded 28).
        baseline_info = snaps.get("baseline")
        window_days = None
        day_elapsed = None
        baseline_date = None
        if first.get("action_type"):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT measurement_window_days FROM measurement_window_lookup "
                    "WHERE action_type = %s",
                    (first["action_type"],),
                )
                wrow = cur.fetchone()
                if wrow:
                    window_days = wrow[0]
        if baseline_info is not None:
            baseline_date = baseline_info.get("period_end")
            if baseline_date is not None:
                baseline_date = str(baseline_date)
                day_elapsed = (date.today() - date.fromisoformat(str(baseline_date))).days
        if window_days is None and day_elapsed is not None:
            window_days = max(day_elapsed, 28)
        measured_details = [{
            "recommendation_id": str(first["recommendation_id"]),
            "generator": first["generator"],
            "action_type": first["action_type"],
            "url": first["url"],
            "cluster": first["cluster"],
            "verdict": first["verdict"],
            "before": {
                "position": float(snaps["baseline"]["avg_position"]) if snaps.get("baseline") and snaps["baseline"]["avg_position"] is not None else None,
                "clicks": int(snaps["baseline"]["clicks"] or 0) if snaps.get("baseline") else None,
                "impressions": int(snaps["baseline"]["impressions"] or 0) if snaps.get("baseline") else None,
                "orders": int(snaps["baseline"]["orders"] or 0) if snaps.get("baseline") else None,
                "revenue": float(snaps["baseline"]["revenue"] or 0) if snaps.get("baseline") and snaps["baseline"]["revenue"] is not None else None,
            } if snaps.get("baseline") else None,
            "after": {
                "position": float(snaps["post_implementation"]["avg_position"]) if snaps.get("post_implementation") and snaps["post_implementation"]["avg_position"] is not None else None,
                "clicks": int(snaps["post_implementation"]["clicks"] or 0) if snaps.get("post_implementation") else None,
                "impressions": int(snaps["post_implementation"]["impressions"] or 0) if snaps.get("post_implementation") else None,
                "orders": int(snaps["post_implementation"]["orders"] or 0) if snaps.get("post_implementation") else None,
                "revenue": float(snaps["post_implementation"]["revenue"] or 0) if snaps.get("post_implementation") and snaps["post_implementation"]["revenue"] is not None else None,
            } if snaps.get("post_implementation") else None,
            "observation_window_days": window_days,
            "observation_day": min(day_elapsed, window_days) if (day_elapsed is not None and window_days) else None,
            "baseline_date": baseline_date,
        }]
    by_action = {}
    for row in rollup:
        key = row["action_type"]
        agg = by_action.setdefault(key, {"improvement_rate_sum": 0.0, "weight": 0})
        if row["improvement_rate"] is not None:
            agg["improvement_rate_sum"] += row["improvement_rate"] * row["measured"]
            agg["weight"] += row["measured"]
    by_action_type = [
        {"action_type": k,
         "improvement_rate": round(v["improvement_rate_sum"] / v["weight"], 4) if v["weight"] else None,
         "measured": v["weight"]}
        for k, v in by_action.items()
    ]
    conn.rollback()
    return ResultsOut(
        site_id=site_id,
        total_measured=total,
        win_rate=win_rate,
        verdicts=verdicts,
        by_generator=rollup,
        by_action_type=by_action_type,
        recent=recent,
        incremental_clicks=int(incremental_clicks or 0),
        incremental_revenue=float(incremental_revenue or 0),
        measured_details=measured_details,
    )


@router.post("/measurements/run", response_model=MeasurementRunOut)
def run_measurements(body: MeasurementRunRequest, conn=Depends(get_conn)):
    """Batch measurement wrap of run_measurement_batch (never-crash skips)."""
    from measurement.measure import run_measurement_batch
    result = run_measurement_batch(conn, body.recommendation_ids,
                                   reference_date=body.reference_date)
    return MeasurementRunOut(measured=result["measured"], skipped=result["skipped"])
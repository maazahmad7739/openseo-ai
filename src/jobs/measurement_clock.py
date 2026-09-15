"""Measurement clock (brief §2.1): measure + classify for `live` rows past window.

Scheduler-agnostic; cron or manual. All dates injectable — no now() hidden
inputs. Idempotent: a job re-run produces no duplicate snapshots (delete-before
rewrite for the same rec) and no re-classification drift (persist is a pure
update of the same fields).  Only post_implementation snapshots are replaced —
the baseline stored at implementation time is preserved so classification
can compare before/after.
"""

import os
import sys
import json
from datetime import date, datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from measurement.baseline import resolve_window_days, store_baseline  # noqa: E402
from measurement.measure import store_post_snapshots  # noqa: E402
from measurement.classify import classify_recommendation, persist_classification  # noqa: E402


def run_measurement_sweep(conn, reference_date=None, site_id=None):
    """For every recommendation in 'live' past its window: measure + classify.

    Window check: implemented_at + measurement_window_lookup[action_type] days
    <= reference_date (default: today UTC — visible parameter at the caller).
    Idempotent: existing snapshots for the rec are replaced, classification is
    recomputed from the snapshots (same inputs → same verdict).

    Returns {measured: [...], pending: [...], skipped: [...]}.
    """
    if reference_date is None:
        reference_date = date.today()
    with conn.cursor() as cur:
        sql = (
            "SELECT r.recommendation_id, r.action_type, r.implemented_at "
            "FROM recommendations r "
            "JOIN measurement_window_lookup mwl ON mwl.action_type = r.action_type "
            "WHERE r.status = 'live' AND r.implemented_at IS NOT NULL "
            "AND r.implemented_at::date + mwl.measurement_window_days <= %s"
        )
        params = [reference_date]
        if site_id:
            sql += " AND r.site_id = %s"
            params.append(site_id)
        cur.execute(sql, params)
        due = cur.fetchall()

    measured, pending, skipped = [], [], []
    for rec_id, action_type, implemented_at in due:
        window = resolve_window_days(conn, action_type)
        measured_at = implemented_at.date() if hasattr(implemented_at, "date") else implemented_at
        measured_at = measured_at + timedelta(days=window)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM measurement_snapshots WHERE recommendation_id = %s "
                    "AND snapshot_type = 'post_implementation'",
                    (rec_id,),
                )
            if action_type == "create_page":
                from measurement.measure import store_cluster_post_snapshots
                store_cluster_post_snapshots(conn, rec_id, measured_at)
            else:
                store_post_snapshots(conn, rec_id, measured_at)
            result = classify_recommendation(conn, rec_id)
            persist_classification(conn, rec_id, result, measured_at=measured_at)
            measured.append({
                "recommendation_id": str(rec_id),
                "action_type": action_type,
                "result": result["result"],
                "reason": result["reasons"][0] if result["reasons"] else None,
            })
        except ValueError as exc:
            skipped.append({
                "recommendation_id": str(rec_id),
                "action_type": action_type,
                "reason": str(exc),
            })
    conn.commit()
    return {"measured": measured, "pending": pending, "skipped": skipped}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site_id", default=None)
    parser.add_argument("--reference_date", default=None,
                        help="injectable clock (YYYY-MM-DD); default today")
    args = parser.parse_args()
    import db as database
    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else date.today()
    conn = database.get_connection()
    try:
        summary = run_measurement_sweep(conn, reference_date=reference_date,
                                        site_id=args.site_id)
        print(json.dumps(summary, indent=1))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
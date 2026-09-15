"""Stale-approval sweep: surface approved recommendations nobody implemented.

Closes visibility gap #2: an 'approved' row can sit indefinitely with no
one noticing. This job (scheduler-agnostic, cron or manual) finds approved
rows older than the threshold and reports them — via the standard notify
hook when a webhook is configured, and always as a stdout summary. It never
crashes on notification failure and never mutates state: detection is
read-only; the human (operator/dev) decides what to do with the row.

Threshold is a single knob shared with the UI endpoint:
api.routes.queue.STALE_APPROVAL_DAYS.
"""

import os
import sys
import json
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as database
import env as env_loader


def run(site_id=None, reference_date=None):
    env_loader.load_env_file(quiet=True)
    from api.routes.queue import STALE_APPROVAL_DAYS
    from jobs.notify import send_summary
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.recommendation_id, r.site_id, r.generator, r.action_type,
                       r.diagnosis, r.impact, r.assigned_to,
                       (%s::date - r.approved_at::date) AS stale_days
                FROM recommendations r
                WHERE r.status = 'approved'
                  AND r.approved_at IS NOT NULL
                  AND (%s::date - r.approved_at::date) >= %s
                ORDER BY stale_days DESC
                """,
                (reference_date or date.today(),
                 reference_date or date.today(),
                 STALE_APPROVAL_DAYS),
            )
            columns = [d[0] for d in cur.description]
            items = [dict(zip(columns, r)) for r in cur.fetchall()]
        conn.rollback()
        summary = {
            "stale_threshold_days": STALE_APPROVAL_DAYS,
            "count": len(items),
            "items": items,
        }
        send_summary("stale_approvals", summary)
        return summary
    finally:
        conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site_id", default=None)
    parser.add_argument("--reference_date", default=None,
                        help="injectable clock (YYYY-MM-DD); default today")
    args = parser.parse_args()
    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else None
    summary = run(args.site_id, reference_date)
    print("stale_approvals:", json.dumps(summary, default=str))


if __name__ == "__main__":
    main()
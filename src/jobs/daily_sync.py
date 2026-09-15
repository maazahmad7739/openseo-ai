"""Daily sync job (brief §3): per-site connector sync. Thin wrapper.

Runnable standalone: python -m jobs.daily_sync --site_id ... --reference_date ... --force-refresh
"""

import os
import sys
import json
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as database
import env as env_loader


def run(site_id=None, reference_date=None, force_refresh=False):
    env_loader.load_env_file(quiet=True)
    from connectors.costlog import check_budget
    from connectors.sync import run_sync
    from jobs.notify import send_summary, send_budget_alert

    conn = database.get_connection()
    try:
        budget = check_budget(conn, "openseo_serp")
        if budget is None:
            budget = check_budget(conn)
        if budget:
            spend, cap, is_over, is_warn, warn_threshold = budget
            if is_over:
                send_budget_alert("openseo", spend, cap)
                return {"status": "budget_cap_reached", "spend": spend, "cap": cap}
            if is_warn:
                send_budget_alert("openseo", spend, cap,
                                  message=f"OpenSEO spend ({spend:.2f}) over "
                                          f"{warn_threshold:.0%} of weekly cap")
        summary = run_sync(force_refresh=force_refresh)
    finally:
        conn.close()
    send_summary("daily_sync", summary)
    return summary


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site_id", default=None)
    parser.add_argument("--reference_date", default=None)
    parser.add_argument("--force-refresh", action="store_true",
                        help="Bypass the SERP/keyword-volume TTL and re-fetch paid DataForSEO data")
    args = parser.parse_args()
    summary = run(args.site_id, args.reference_date, args.force_refresh)
    print("daily_sync:", summary)


if __name__ == "__main__":
    main()
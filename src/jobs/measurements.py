"""Measurements job (brief §3): invokes the measurement clock sweep."""

import os
import sys
import json
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as database
import env as env_loader


def run(site_id=None, reference_date=None):
    env_loader.load_env_file(quiet=True)
    from jobs.measurement_clock import run_measurement_sweep
    conn = database.get_connection()
    try:
        summary = run_measurement_sweep(conn, reference_date=reference_date, site_id=site_id)
        from jobs.notify import send_summary
        send_summary("measurements", summary)
        return summary
    finally:
        conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site_id", default=None)
    parser.add_argument("--reference_date", default=None, help="injectable clock (YYYY-MM-DD)")
    args = parser.parse_args()
    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else None
    summary = run(args.site_id, reference_date)
    print("measurements:", json.dumps(summary, default=str))


if __name__ == "__main__":
    main()
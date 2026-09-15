"""Weekly candidates job (brief §3): per-site generator run, injectable date."""

import os
import sys
import json
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as database
import env as env_loader


def run(site_id=None, reference_date=None):
    env_loader.load_env_file(quiet=True)
    from generators.orchestrator import run_candidate_generation
    from jobs.notify import send_summary
    from jobs.run_multi import run_across_sites
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            if site_id:
                cur.execute("SELECT site_id FROM site_config WHERE site_id = %s", (site_id,))
            else:
                cur.execute("SELECT site_id FROM site_config")
            sites = [r[0] for r in cur.fetchall()]
        out = run_across_sites(
            sites,
            lambda sid: run_candidate_generation(sid, reference_date=reference_date),
        )
        send_summary("weekly_candidates", out)
        return out
    finally:
        conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site_id", default=None)
    parser.add_argument("--reference_date", default=None, help="pinned clock (YYYY-MM-DD)")
    args = parser.parse_args()
    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else None
    summary = run(args.site_id, reference_date)
    print("weekly_candidates:", json.dumps(summary, default=str))


if __name__ == "__main__":
    main()
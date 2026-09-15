"""Weekly agent job (brief §3): live agent pass per site (pre-ranked slice is
handled inside run_agent via site_config.agent_prefetch_limit)."""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as database
import env as env_loader


def run(site_id=None, reference_date=None):
    env_loader.load_env_file(quiet=True)
    from agents.seo_agent import run_agent
    from connectors.costlog import check_budget
    from jobs.notify import send_summary, send_budget_alert, send_failure
    from jobs.run_multi import run_across_sites

    conn = database.get_connection()
    try:
        budget = check_budget(conn, "ollama_chat")
        if budget is None:
            budget = check_budget(conn)
        budget_state = None
        if budget:
            spend, cap, is_over, is_warn, warn_threshold = budget
            budget_state = {
                "spend": spend, "cap": cap, "is_over": is_over,
                "is_warn": is_warn, "warn_threshold": warn_threshold,
            }
            if is_over:
                send_budget_alert("ollama", spend, cap)
                return {"status": "budget_cap_reached", "budget": budget_state}
            if is_warn:
                send_budget_alert("ollama", spend, cap,
                                  message=f"Ollama spend ({spend:.2f}) over "
                                          f"{warn_threshold:.0%} of weekly cap")

        with conn.cursor() as cur:
            if site_id:
                cur.execute("SELECT site_id FROM site_config WHERE site_id = %s", (site_id,))
            else:
                cur.execute("SELECT site_id FROM site_config")
            sites = [str(r[0]) for r in cur.fetchall()]

        def notify_failure(sid, result):
            send_failure("weekly_agent", sid, result.get("error"))

        out = run_across_sites(sites, run_agent, on_failure=notify_failure)
        send_summary("weekly_agent", out)
        return out
    finally:
        conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site_id", default=None)
    parser.add_argument("--reference_date", default=None)
    args = parser.parse_args()
    summary = run(args.site_id, args.reference_date)
    print("weekly_agent:", json.dumps(summary, default=str))


if __name__ == "__main__":
    main()
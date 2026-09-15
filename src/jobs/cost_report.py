"""Weekly cost report — show spend so far this week, by service.

Runnable standalone: python -m jobs.cost_report [--service openseo_serp]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as database
import env as env_loader


def run(service=None):
    env_loader.load_env_file(quiet=True)
    from connectors.costlog import weekly_spend, check_budget

    conn = database.get_connection()
    try:
        print("=" * 60)
        print("  WEEKLY COST REPORT")
        print("=" * 60)

        rows = weekly_spend(conn, service=service)
        if not rows:
            print("  No costs logged this week.")
        else:
            total_cost = 0.0
            total_calls = 0
            print(f"\n  {'Service':<28} {'Cost ($)':>10} {'Calls':>8}")
            print(f"  {'-'*28} {'-'*10} {'-'*8}")
            for svc, cost, calls in rows:
                cost = float(cost or 0)
                calls = int(calls or 0)
                total_cost += cost
                total_calls += calls
                print(f"  {svc:<28} {cost:>10.2f} {calls:>8}")
            print(f"  {'-'*28} {'-'*10} {'-'*8}")
            print(f"  {'TOTAL':<28} {total_cost:>10.2f} {total_calls:>8}")

        print()
        print("  Budget status:")
        for svc_name in ["openseo_serp", "ollama_chat", "*"]:
            budget = check_budget(conn, svc_name)
            if budget:
                spend, cap, is_over, is_warn, warn_threshold = budget
                status = "OVER CAP" if is_over else ("WARNING" if is_warn else "OK")
                print(f"  [{svc_name:>14}] ${spend:>7.2f} / ${cap:>7.2f}  ({status})")
        print()
        print("=" * 60)
    finally:
        conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Weekly cost report")
    parser.add_argument("--service", default=None,
                        help="Filter to a single service (e.g. openseo_serp)")
    args = parser.parse_args()
    run(args.service)


if __name__ == "__main__":
    main()
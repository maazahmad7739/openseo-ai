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

# Columns mirrored from site_config; run_sync(site=...) accepts this shape.
_SITE_COLS = (
    "site_name", "domain", "gsc_property", "shopify_domain",
    "ga4_property_id", "catalogue_size_tier", "min_in_stock_products",
)


def _load_sites(conn, site_id=None):
    """Iterate configured sites, or a single site_id; empty site_config → []."""
    query = (
        f"SELECT {', '.join(_SITE_COLS)} FROM site_config"
        + (" WHERE site_id = %s" if site_id else "")
        + " ORDER BY created_at"
    )
    params = (site_id,) if site_id else ()
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    sites = []
    for row in rows:
        site = dict(zip(_SITE_COLS, row))
        if site.get("min_in_stock_products") is None:
            site["min_in_stock_products"] = 1
        sites.append(site)
    return sites


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

        sites = _load_sites(conn, site_id)
        if not sites:
            # No configured sites: run_sync falls back to the sandbox mock site
            # so the empty-database demo keeps working.
            summary = run_sync(site=None, reference_date=reference_date,
                               force_refresh=force_refresh)
        else:
            summary = {}
            for site in sites:
                try:
                    per_site = run_sync(site=site, reference_date=reference_date,
                                        force_refresh=force_refresh)
                except Exception as exc:
                    print(f"[daily_sync] site {site.get('domain')}: sync failed ({exc})", flush=True)
                    per_site = {"error": str(exc)}
                summary[site.get("domain")] = per_site
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
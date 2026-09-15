"""Weekly crawl job (brief §3): crawl/audit FROM OpenSEO (plan/11 contract).

If the OpenSEO connector is not live (mock or missing capability), logs the
skip reason and exits clean — never crashes.
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as database
import env as env_loader


def run(site_id=None, reference_date=None):
    env_loader.load_env_file(quiet=True)
    from connectors.openseo import get_openseo_adapter
    adapter = get_openseo_adapter(config={"openseo.mode": os.environ.get("INTEGRATION_MODE", "mock")})
    if not adapter.supports("crawl_audit"):
        print("[weekly_crawl] OpenSEO not configured for crawl_audit — skipped", flush=True)
        return {"skipped": "openseo_not_configured"}
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            if site_id:
                cur.execute("SELECT site_id, domain FROM site_config WHERE site_id = %s", (site_id,))
            else:
                cur.execute("SELECT site_id, domain FROM site_config")
            sites = cur.fetchall()
        from connectors.sync import sync_crawl_audit
        out = {}
        for sid, domain in sites:
            out[str(sid)] = sync_crawl_audit(conn, adapter, sid, domain)
        conn.commit()
        return {"crawled": out}
    finally:
        conn.close()


def sync_crawl_audit(conn, adapter, site_id, domain):
    from connectors.sync import sync_crawl_audit as _impl
    return _impl(conn, adapter, site_id, domain)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site_id", default=None)
    parser.add_argument("--reference_date", default=None)
    args = parser.parse_args()
    summary = run(args.site_id, args.reference_date)
    print("weekly_crawl:", summary)


if __name__ == "__main__":
    main()
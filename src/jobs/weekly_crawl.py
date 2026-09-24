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
    from jobs.locks import job_lock, LOCK_KEYS, already_running
    adapter = get_openseo_adapter(config={"openseo.mode": os.environ.get("INTEGRATION_MODE", "mock")})
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
            with job_lock(conn, LOCK_KEYS["weekly_crawl"], site_id=str(sid)) as got:
                if not got:
                    out[str(sid)] = already_running("weekly_crawl", sid)
                    continue
                out[str(sid)] = sync_crawl_audit(conn, adapter, sid, domain)
        conn.commit()

        # Task 5: verified robots.txt + sitemap.xml membership pass —
        # typed-skip semantics (an unfetchable sitemap never crashes the
        # job; pages.in_sitemap is set to NULL/unknown instead).
        try:
            from site_fetch import sync_sitemap_membership
            sitemap_summary = {}
            for sid, domain in sites:
                sitemap_summary[str(sid)] = sync_sitemap_membership(conn, sid, domain)
            conn.commit()
            out["sitemap_verification"] = sitemap_summary
        except Exception as exc:
            conn.rollback()
            out["sitemap_summary"] = {"skipped": f"{type(exc).__name__}: {exc}"}
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
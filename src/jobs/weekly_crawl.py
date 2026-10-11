"""Weekly crawl job (brief §3): crawl/audit FROM OpenSEO (plan/11 contract).

Per-site mode (plan/31): the OpenSEO adapter is built with each site's
site_config.integration_mode ('import' sites are skipped entirely — zero
connector traffic). If the connector is not live (mock or missing
capability), logs the skip reason and exits clean — never crashes.
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
    from connectors.mode import resolve_site_mode, adapter_mode
    from jobs.locks import job_lock, LOCK_KEYS, already_running
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
                # plan/31: per-site mode (site_config.integration_mode), env
                # as fallback. 'import' sites get zero connector traffic.
                site_mode = resolve_site_mode(conn, sid)
                if site_mode == "import":
                    out[str(sid)] = "skipped: integration_mode=import"
                    continue
                amode = adapter_mode(site_mode)
                adapter = get_openseo_adapter(config={"openseo.mode": amode})
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

        # plan/26 Phase 1: generic sitemap → pages ingestion for
        # NON-Shopify platforms (Framer/WordPress/headless). Shopify
        # sites keep catalogue ingestion as their single pages source
        # (their URL shapes are handle-based, not sitemap-authoritative).
        try:
            from site_fetch import sync_pages_from_sitemap
            ingest_summary = {}
            for sid, domain in _non_shopify_sites(conn):
                ingest_summary[str(sid)] = sync_pages_from_sitemap(
                    conn, sid, domain)
            conn.commit()
            out["sitemap_ingest"] = ingest_summary
        except Exception as exc:
            conn.rollback()
            out["sitemap_ingest"] = {"skipped": f"{type(exc).__name__}: {exc}"}
        return {"crawled": out}
    finally:
        conn.close()


def sync_crawl_audit(conn, adapter, site_id, domain):
    from connectors.sync import sync_crawl_audit as _impl
    return _impl(conn, adapter, site_id, domain)


def _non_shopify_sites(conn):
    """(site_id, domain) rows whose platform is NOT shopify (plan/26).

    Legacy deployments whose site_config predates plan/26 (no platform
    column) resolve as shopify — the pre-Framer default — so they are
    excluded here exactly as an explicit 'shopify' row would be."""
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT site_id, domain FROM site_config "
                "WHERE COALESCE(platform, 'shopify') <> 'shopify'"
            )
        except Exception:
            conn.rollback()
            return []
        return cur.fetchall()


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
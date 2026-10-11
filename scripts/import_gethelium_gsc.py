# -*- coding: utf-8 -*-
"""Import the gethelium.co GSC export (zip) into the Supabase DB.

New site: 'Helium AI' (Framer, platform='framer') — separate from
Aurora Audio (example.com) and ActionSEO Dev (action-seo-test).

Ingests (plan/26 Phase 1 semantics, batch equivalent):
  Pages.csv     -> pages rows (page_type from URL archetype, indexable
                   heuristic: has clicks/impressions on a 200 page)
  Queries.csv   -> keyword_clusters (query->page grouping by landing page
                   URL path archetype) + cluster_queries
  Chart.csv     -> search_performance daily rows (aggregated query='(all)')
  Queries.csv   -> search_performance rows per (query, page) — the GSC
                   export gives query totals, mapped to the query's top
                   landing page
  Countries/Devices/Filters -> logged to the run summary (Devices
                   distribution informs the summary report)

DataForSEO (live) afterwards:
  serp per qualified cluster keyword -> openseo_serp_snapshots
  crawl_audit for www.gethelium.co   -> pages crawl columns (sync_crawl_audit)

Usage:
  python scripts/import_gethelium_gsc.py --zip <path-to-zip> \
      [--reference-date 2026-09-25] [--skip-agent] [--skip-serp]
"""
import argparse
import csv
import io
import json
import os
import sys
import zipfile
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import env as env_loader  # noqa: E402

env_loader.load_env_file(quiet=True)

import db as database  # noqa: E402

# Hard-bind the remote Supabase DB for this run (never local defaults).
database.DB_PARAMS_DEFAULT.update({
    "host": "aws-0-ap-northeast-1.pooler.supabase.com",
    "port": "6543",
    "dbname": "postgres",
    "user": "postgres.fzuckbubqrzcjukgwepr",
    "password": "mPizmxf6T8GfuDG8",
    "sslmode": "require",
})

from connectors.url_normalize import canonicalize_url  # noqa: E402
from audit.url_pattern import page_type_from_url  # noqa: E402

SITE_NAME = "Helium"
DOMAIN = "gethelium.co"
PRIMARY_HOST = f"www.{DOMAIN}"
GSC_PROPERTY = f"sc-domain:{DOMAIN}"


def _pct(value):
    """GSC CSV CTR percent ('4.94%') -> decimal (0.0494)."""
    raw = (value or "").strip().rstrip("%")
    try:
        return round(float(raw) / 100.0, 4)
    except ValueError:
        return 0.0


def _int(value):
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float((value or "0").replace(",", "")))
    except (ValueError, TypeError):
        return 0


def _float(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _read_csv(csv_bytes):
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def normalize_www(url):
    """Rehome every URL onto the canonical www host (the property's main
    host); subdomain hosts (app./leads./blog.) stay distinct pages."""
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("http"):
        return canonicalize_url(url, PRIMARY_HOST)
    return canonicalize_url(f"https://{url}", PRIMARY_HOST)


PRIMARY_HOST = f"www.{DOMAIN}"


def ensure_site(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT site_id FROM site_config WHERE domain = %s",
                    (DOMAIN,))
        row = cur.fetchone()
        if row:
            site_id = row[0]
            print(f"[site] existing site_id: {site_id}")
        else:
            cur.execute(
                """
                INSERT INTO site_config (site_name, domain, gsc_property,
                                         catalogue_size_tier, platform,
                                         framer_site_ref)
                VALUES (%s, %s, %s, 'small', 'framer', %s)
                RETURNING site_id
                """,
                (SITE_NAME, DOMAIN, GSC_PROPERTY, f"framer-{DOMAIN}"),
            )
            site_id = cur.fetchone()[0]
            print(f"[site] created site_id: {site_id} (platform=framer)")
    conn.commit()
    return site_id


def ingest_pages(conn, site_id, pages_csv):
    """Pages.csv -> pages rows (page_type from URL archetype)."""
    inserted = 0
    with conn.cursor() as cur:
        for row in pages_csv:
            url = normalize_www(row.get("Top pages"))
            if not url:
                continue
            clicks = _int(row.get("Clicks"))
            impressions = _int(row.get("Impressions"))
            # A URL earning clicks is live and indexable; the crawl audit
            # refines status_code later.
            indexable = clicks > 0 or impressions > 0
            cur.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title,
                                   indexable, status_code)
                VALUES (%s, %s, %s, %s, %s, 200)
                ON CONFLICT (site_id, url_hash) DO UPDATE SET
                    indexable = EXCLUDED.indexable,
                    last_crawled_at = now()
                """,
                (str(site_id), url, page_type_from_url(url),
                 None, indexable),
            )
            inserted += 1
    conn.commit()
    return inserted


def ingest_search_performance(conn, site_id, chart_csv, queries_csv,
                              pages_csv, reference_date):
    """Chart.csv -> daily totals; Queries.csv x top landing page -> rows.

    The GSC export aggregates per query WITHOUT its landing page; the
    query row is stored against the page that most plausibly owns it:
    the page whose path tokens overlap the query tokens, else the top
    landing page. The `search_performance` UNIQUE key includes
    page_url_hash, so a query landing on multiple pages keeps its best
    evidence row (same semantics the daily GSC sync produces).
    """
    rows = []
    # daily aggregate rows (query = the site's brand anchor)
    for row in chart_csv:
        d = (row.get("Date") or "").strip()
        if not d:
            continue
        clicks = _int(row.get("Clicks"))
        impressions = _int(row.get("Impressions"))
        ctr = _pct(row.get("CTR"))
        pos = _float(row.get("Position")) or 0
        rows.append({
            "site_id": str(site_id), "date": d,
            "query": "gethelium", "page_url": canonicalize_url(
                f"https://{PRIMARY_HOST}/", PRIMARY_HOST),
            "country": "all", "device": "all",
            "clicks": clicks, "impressions": impressions,
            "ctr": ctr, "position": pos,
        })
    # per-query rows on their best-matching landing page
    pages = []
    for row in pages_csv:
        url = normalize_www(row.get("Top pages"))
        if url:
            pages.append({"url": url, "path": url.split("://", 1)[-1],
                          "clicks": _int(row.get("Clicks"))})
    for row in queries_csv:
        query = (row.get("Top queries") or "").strip()
        if not query:
            continue
        clicks = _int(row.get("Clicks"))
        impressions = _int(row.get("Impressions"))
        ctr = _pct(row.get("CTR"))
        pos = _float(row.get("Position")) or 0
        tokens = set(query.lower().split())
        best = None
        best_score = -1
        for page in pages:
            path_tokens = set(
                t for t in page["path"].lower().replace("-", " ").replace(
                    "/", " ").split() if len(t) > 2)
            overlap = len(tokens & path_tokens)
            score = overlap * 100 + page["clicks"]
            if score > best_score:
                best_score = score
                best = page
        page_url = (best or {}).get("url") or canonicalize_url(
            f"https://{PRIMARY_HOST}/", PRIMARY_HOST)
        rows.append({
            "site_id": str(site_id),
            "date": str(reference_date),
            "query": query, "page_url": page_url,
            "country": "all", "device": "all",
            "clicks": clicks, "impressions": impressions,
            "ctr": ctr, "position": pos,
        })
    inserted = database.insert_rows(
        conn, "search_performance", rows,
        conflict_target="site_id, date, query, page_url_hash, country, device",
        update_keys=["clicks", "impressions", "ctr", "position"],
    )
    conn.commit()
    return len(rows), inserted


def ingest_clusters(conn, site_id, queries_csv):
    """Non-brand queries with real impressions -> keyword_clusters
    (informational/commercial by cue heuristic) + cluster_queries."""
    commercial_cues = ("buy", "pricing", "price", "cost", "demo", "book",
                       "schedule", "trial", "shop", "order")
    created = 0
    cluster_ids = {}
    with conn.cursor() as cur:
        for row in queries_csv:
            query = (row.get("Top queries") or "").strip()
            if not query:
                continue
            impressions = _int(row.get("Impressions"))
            if impressions < 40:
                continue  # below the small-tier evidence floor
            brandy = "gethelium" in query.replace(" ", "") or query in (
                "get helium", "helium co", "helium.co", "gethelium.co",
                "he2 ai")
            if brandy:
                continue  # brand queries are excluded from opportunity clusters
            lowered = query.lower()
            intent = ("commercial" if any(c in lowered for c in commercial_cues)
                      else "informational")
            cur.execute(
                "SELECT cluster_id FROM keyword_clusters "
                "WHERE site_id = %s AND primary_keyword = %s",
                (str(site_id), query),
            )
            existing = cur.fetchone()
            if existing:
                cluster_ids[query] = str(existing[0])
                continue
            cur.execute(
                """
                INSERT INTO keyword_clusters (site_id, primary_keyword,
                    keywords, intent, commercial_intent,
                    recommended_page_type, search_volume, commercial_value)
                VALUES (%s, %s, %s, %s, %s, 'landing', %s, %s)
                RETURNING cluster_id
                """,
                (str(site_id), query, [query], intent, intent == "commercial",
                 max(40, impressions),
                 "high" if intent == "commercial" else "medium"),
            )
            cluster_ids[query] = str(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO cluster_queries (cluster_id, query) "
                "VALUES (%s, %s) ON CONFLICT (cluster_id, query) DO NOTHING",
                (cluster_ids[query], query),
            )
            created += 1
    conn.commit()
    return created, cluster_ids


def seed_serp_snapshots_live(conn, site_id, cluster_ids, reference_date,
                             adapter):
    """DataForSEO SERP per qualified cluster keyword (live)."""
    inserted = 0
    with conn.cursor() as cur:
        for query, cluster_id in cluster_ids.items():
            result = adapter.fetch("serp", {"query": query, "limit": 10})
            if not result.get("ok"):
                print(f"[serp] {query}: skipped ({result.get('error')})")
                continue
            from fixes.generator import load_competitor_context  # noqa: F401
            for item in (result.get("data") or [])[:10]:
                url = item.get("url")
                if not url:
                    continue
                domain = url.split("/")[2] if "://" in url else None
                cur.execute(
                    """
                    INSERT INTO openseo_serp_snapshots (site_id, cluster_id,
                        query, result_url, result_domain, position, is_self,
                        result_title, result_snippet, url_pattern, snapshot_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (site_id, query, result_url, snapshot_date)
                    DO NOTHING
                    """,
                    (str(site_id), cluster_id, query, url, domain,
                     _int(item.get("position")),
                     (domain or "").endswith(DOMAIN),
                     item.get("title"), item.get("snippet"),
                     f"/{(url.split('://', 1)[-1].split('/', 1)[-1].split('/')[0:1] or [''])[0]}/"
                     if "/" in url.split("://", 1)[-1] else "/",
                     str(reference_date)),
                )
                inserted += cur.rowcount
    conn.commit()
    return inserted


def ingest_serp_rows_into_search(conn, rows_site_id, query, domain,
                                 serp_rows, reference_date):
    """Mirror of audit.engine._ingest_serp_rows for cluster grounding."""
    seen = set()
    with conn.cursor() as cur:
        for row in serp_rows:
            url = row.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
    return len(seen)


def verify(conn, site_id):
    out = {}
    with conn.cursor() as cur:
        for table in ("pages", "keyword_clusters", "search_performance",
                      "openseo_serp_snapshots", "recommendations",
                      "candidate_runs"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE site_id = %s",
                        (str(site_id),))
            out[f"count_{table}"] = cur.fetchone()[0]
        cur.execute(
            "SELECT page_type, count(*) FROM pages WHERE site_id = %s "
            "GROUP BY page_type ORDER BY 2 DESC", (str(site_id),))
        out["pages_by_type"] = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute(
            "SELECT min(date), max(date) FROM search_performance "
            "WHERE site_id = %s", (str(site_id),))
        out["sp_date_range"] = [str(v) for v in cur.fetchone()]
        cur.execute(
            "SELECT generator, action_type, COALESCE(target_url, proposed_url), "
            "COALESCE(kc.primary_keyword, ''), impact, diagnosis "
            "FROM recommendations r LEFT JOIN keyword_clusters kc "
            "ON kc.cluster_id = r.cluster_id "
            "WHERE r.site_id = %s AND r.status IN ('raw','proposed') "
            "ORDER BY generator, impact DESC", (str(site_id),))
        out["recommendations"] = [
            {"generator": r[0], "action_type": r[1], "url": r[2],
             "cluster": r[3], "impact": r[4], "diagnosis": r[5][:140]}
            for r in cur.fetchall()]
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", required=True,
                        help="path to the GSC Performance export zip")
    parser.add_argument("--reference-date", default="2026-09-25")
    parser.add_argument("--skip-serp", action="store_true",
                        help="skip live DataForSEO SERP pulls")
    parser.add_argument("--skip-agent", action="store_true")
    parser.add_argument("--skip-crawl", action="store_true",
                        help="skip live DataForSEO crawl_audit")
    args = parser.parse_args()
    ref_date = date.fromisoformat(args.reference_date)

    # unzip in-memory
    with zipfile.ZipFile(args.zip) as zf:
        names = zf.namelist()
        def read(name):
            for n in names:
                if n.endswith(name):
                    return zf.read(n)
        pages_csv = read("Pages.csv")
        queries_csv = read("Queries.csv")
        chart_csv = read("Chart.csv")
        countries_csv = read("Countries.csv")
        devices_csv = read("Devices.csv")
    pages_rows = _read_csv(pages_csv)
    queries_rows = _read_csv(queries_csv)
    chart_rows = _read_csv(chart_csv)
    print(f"[zip] pages={len(pages_rows)} queries={len(queries_rows)} "
          f"chart_days={len(chart_rows)}")

    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
        site_id = ensure_site(conn)
        summary = {"site_id": str(site_id)}

        # pages from the GSC export
        summary["pages_upserted"] = ingest_pages(conn, site_id, pages_rows)
        print(f"[pages] upserted: {summary['pages_upserted']}")

        # daily search_performance (Chart.csv) + per-query rows (Queries.csv)
        sp_total, sp_inserted = ingest_search_performance(
            conn, site_id, chart_rows, queries_rows, pages_rows, ref_date)
        summary["search_performance_rows"] = sp_total
        print(f"[search_performance] built {sp_total} rows "
              f"(upserted {sp_inserted})")

        # clusters from non-brand queries
        clusters_created, cluster_ids = ingest_clusters(
            conn, site_id, queries_rows)
        summary["clusters_created"] = clusters_created
        print(f"[clusters] created: {clusters_created}")

        # live DataForSEO: SERP per cluster keyword
        if not args.skip_serp:
            from connectors.openseo import get_openseo_adapter
            adapter = get_openseo_adapter(config={"openseo.mode": "live"})
            summary["serp_snapshots"] = seed_serp_snapshots_live(
                conn, site_id, cluster_ids, ref_date, adapter)
            print(f"[serp] live snapshots: {summary['serp_snapshots']}")

        # live DataForSEO: crawl audit -> pages crawl columns
        if not args.skip_crawl:
            from connectors.sync import sync_crawl_audit
            crawled = sync_crawl_audit(conn, adapter, site_id, PRIMARY_HOST)
            summary["crawl_audit_pages"] = crawled
            print(f"[crawl_audit] pages updated: {crawled}")

        # candidate generation (content-site mix for platform=framer)
        from generators.orchestrator import run_candidate_generation
        gen_counts = run_candidate_generation(
            site_id, conn=conn, reference_date=args.reference_date)
        summary["generators"] = gen_counts
        print(f"[generators] {json.dumps(gen_counts, default=str)}")

        # agent promotion (raw -> proposed)
        if not args.skip_agent:
            try:
                from agents.seo_agent import run_agent
                agent_summary = run_agent(str(site_id), conn=conn)
                summary["agent"] = agent_summary
                print(f"[agent] {json.dumps(agent_summary, default=str)[:600]}")
            except Exception as exc:
                conn.rollback()
                print(f"[agent] skipped after error: "
                      f"{type(exc).__name__}: {exc}")

        report = verify(conn, site_id)
        report.update(summary)
        print("\n=== GETHELIUM IMPORT REPORT ===")
        print(json.dumps(report, indent=2, default=str))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
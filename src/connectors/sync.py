"""Daily sync orchestrator (plan/09 Data Flow 1) — fully offline in mock mode.

Per-site sequence:
  1. GSC search_analytics → search_performance (upsert on UNIQUE constraint)
  2. Shopify products + collections → pages (catalogue crawl stand-in)
  3. GA4 page_performance → page_business_performance (upsert)
  4. OpenSEO keyword_volume → keyword_clusters.search_volume (existing clusters)

Unsupported capabilities are skipped and logged, never fatal. Every fetch is
idempotent via ON CONFLICT upserts, so re-running the sync is safe.
"""

import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from connectors.openseo import get_openseo_adapter  # noqa: E402
from connectors.gsc import get_gsc_adapter  # noqa: E402
from connectors.shopify import get_shopify_adapter  # noqa: E402
from connectors.ga4 import get_ga4_adapter  # noqa: E402

import db as database  # noqa: E402

FIXTURES_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "tests", "fixtures"
))

# Single anchor for the mock universe (fix 3.6): every fixture date in this
# file is derived relative to FIXTURE_ANCHOR_DATE so the whole dataset moves
# as one block and never drifts apart. The GSC fixture itself spans the 5
# weeks ending on the anchor; GA4 covers the anchor's last two days; SERP is
# snapshotted on the anchor.
FIXTURE_ANCHOR_DATE = "2026-09-10"


def _days_before(days):
    from datetime import date, timedelta
    anchor = date.fromisoformat(FIXTURE_ANCHOR_DATE)
    return str(anchor - timedelta(days=days))


def _is_fresh(conn, site_id, capability, ttl_days):
    """Return True if a recent snapshot exists within the TTL window.

    For 'serp': checks max(snapshot_date) in openseo_serp_snapshots.
    For 'keyword_volume': checks max(updated_at) in keyword_clusters.
    """
    from datetime import date, timedelta
    if ttl_days is None or ttl_days <= 0:
        return False
    with conn.cursor() as cur:
        if capability == "serp":
            cur.execute(
                "SELECT max(snapshot_date) FROM openseo_serp_snapshots "
                "WHERE site_id = %s",
                (site_id,),
            )
        elif capability == "keyword_volume":
            cur.execute(
                "SELECT max(updated_at) FROM keyword_clusters "
                "WHERE site_id = %s",
                (site_id,),
            )
        else:
            return False
        row = cur.fetchone()
    if not row or row[0] is None:
        return False
    latest = row[0]
    if isinstance(latest, str):
        from datetime import date as _date
        latest = _date.fromisoformat(latest)
    elif hasattr(latest, "date") and hasattr(latest, "timetz"):
        latest = latest.date()  # TIMESTAMPTZ -> date for the <date> comparison
    cutoff = date.today() - timedelta(days=ttl_days)
    return latest >= cutoff

# Overlap pre-gate for the match-score stand-in (fix 3.7: named constant).
MIN_TERM_OVERLAP = 0.5

MOCK_SITE = {
    "site_name": "Aurora Audio (mock)",
    "domain": "example.com",
    "gsc_property": "sc-domain:example.com",
    "shopify_domain": "example.com",
    "ga4_property_id": "mock-property-1",
    "catalogue_size_tier": "small",
    "min_in_stock_products": 1,
}


def ensure_site(conn, site=MOCK_SITE):
    """Insert the mock site once; return site_id (idempotent by domain)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT site_id FROM site_config WHERE domain = %s",
            (site["domain"],),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            "INSERT INTO site_config (site_name, domain, gsc_property, shopify_domain, "
            "ga4_property_id, catalogue_size_tier, min_in_stock_products) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING site_id",
            (site["site_name"], site["domain"], site["gsc_property"],
             site["shopify_domain"], site["ga4_property_id"], site["catalogue_size_tier"],
             site.get("min_in_stock_products")),
        )
        conn.commit()
        return cur.fetchone()[0]


def sync_search_performance(conn, gsc, site_id, site):
    result = gsc.fetch("search_analytics", {
        "site_url": site["gsc_property"],
        "start_date": _days_before(7),
        "end_date": FIXTURE_ANCHOR_DATE,
    })
    if not result.get("ok"):
        print(f"[sync] gsc.search_analytics skipped: {result.get('error')}")
        return 0
    rows = [dict(r, site_id=str(site_id)) for r in result["data"]]
    return database.insert_rows(
        conn, "search_performance", rows,
        conflict_target="site_id, date, query, page_url_hash, country, device",
        update_keys=["clicks", "impressions", "ctr", "position"],
    )


def sync_pages_from_shopify(conn, shopify, site_id, domain="example.com"):
    products = shopify.fetch("products", {})
    if not products.get("ok"):
        print(f"[sync] shopify.products skipped: {products.get('error')}")
        return 0
    collections = shopify.fetch("collections", {})
    if not collections.get("ok"):
        print(f"[sync] shopify.collections skipped: {collections.get('error')}")
        collections = {"data": []}

    rows = []
    for c in collections["data"]:
        if not c.get("url"):
            continue
        rows.append({
            "url": c["url"],
            "page_type": "collection",
            "title": c["title"],
            "indexable": bool(c["published"]),
        })
    product_urls = []
    for p in products["data"]:
        url = f"https://{domain}/products/{p['handle']}"
        product_urls.append(url)
        rows.append({
            "url": url,
            "page_type": "product",
            "title": p["title"],
            "indexable": p["status"] == "active",
            "product_count": 1,
        })

    inserted = 0
    for row in rows:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pages (site_id, url, page_type, title, indexable) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (site_id, url_hash) DO UPDATE SET "
                "title = EXCLUDED.title, indexable = EXCLUDED.indexable",
                (site_id, row["url"], row["page_type"], row["title"], row["indexable"]),
            )
            inserted += 1
    return inserted


def sync_page_business_performance(conn, ga4, site_id):
    result = ga4.fetch("page_performance", {
        "start_date": _days_before(1),
        "end_date": FIXTURE_ANCHOR_DATE,
    })
    if not result.get("ok"):
        print(f"[sync] ga4.page_performance skipped: {result.get('error')}")
        return 0
    rows = []
    for r in result["data"]:
        url = f"https://{MOCK_SITE['domain']}{r['page_path']}"
        rows.append({
            "site_id": str(site_id),
            "date": r["date"],
            "page_url": url,
            "organic_sessions": r["organic_sessions"],
            "engaged_sessions": r["engaged_sessions"],
            "add_to_carts": r["add_to_carts"],
            "checkouts": r["checkouts"],
            "orders": r["orders"],
            "revenue": r["revenue"],
            "conversion_rate": r["conversion_rate"],
        })
    return database.insert_rows(
        conn, "page_business_performance", rows,
        conflict_target="site_id, date, page_url_hash",
        update_keys=["organic_sessions", "engaged_sessions", "add_to_carts",
                     "checkouts", "orders", "revenue", "conversion_rate"],
    )


def sync_keyword_volumes(conn, openseo, site_id, queries):
    """OpenSEO search_volume for existing keyword_clusters (skips cleanly when none)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cluster_id, primary_keyword FROM keyword_clusters WHERE site_id = %s",
            (site_id,),
        )
        clusters = cur.fetchall()
    if not queries or not queries[0].get("ok"):
        return 0
    volume_rows = {r["keyword"]: r.get("search_volume") for r in queries[0].get("data", [])}
    updated = 0
    for cluster_id, keyword in clusters:
        volume = volume_rows.get(keyword)
        if volume is None:
            continue
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE keyword_clusters SET search_volume = %s, updated_at = now() "
                "WHERE cluster_id = %s",
                (volume, cluster_id),
            )
            updated += 1
    return updated


def tier_threshold(conn, site_id, column):
    """Resolve a threshold from threshold_tiers via the site's tier (fix 3.7).

    The mock seed logic must not drift from the generators that consume the
    tier-resolved values, so gates like the match threshold are read from
    the same source. Falls back to the page_match_threshold name documented
    for competition gating when the column is unknown.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT tt.{column} FROM site_config sc "
            "JOIN threshold_tiers tt ON tt.tier = sc.catalogue_size_tier "
            "WHERE sc.site_id = %s",
            (site_id,),
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        raise ValueError(f"no tier threshold resolved for site {site_id}, column '{column}'")
    return float(row[0])


def sync_keyword_clusters(conn, openseo, site_id, queries):
    """Upsert keyword_clusters + cluster_queries from OpenSEO keyword volumes.

    commercial_intent is flagged from fixture competition metadata against the
    tier-resolved page_match_threshold (fix 3.7: no local 0.60 copy);
    recommended_page_type defaults to 'collection' for commercial clusters.
    """
    if not queries or not queries[0].get("ok"):
        return 0
    commercial_gate = tier_threshold(conn, site_id, "page_match_threshold")
    created = 0
    with conn.cursor() as cur:
        for r in queries[0].get("data", []):
            keyword = r.get("keyword")
            volume = r.get("search_volume") or 0
            competition = r.get("competition") or 0
            if not keyword:
                continue
            cur.execute(
                "SELECT cluster_id FROM keyword_clusters "
                "WHERE site_id = %s AND primary_keyword = %s",
                (site_id, keyword),
            )
            row = cur.fetchone()
            if row:
                cluster_id = row[0]
                cur.execute(
                    "UPDATE keyword_clusters SET search_volume = %s, updated_at = now() "
                    "WHERE cluster_id = %s",
                    (volume, cluster_id),
                )
            else:
                cur.execute(
                    "INSERT INTO keyword_clusters "
                    "(site_id, primary_keyword, keywords, intent, commercial_intent, "
                    " recommended_page_type, search_volume, commercial_value) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING cluster_id",
                    (site_id, keyword, [keyword],
                     "commercial" if competition >= commercial_gate else "informational",
                     competition >= commercial_gate,
                     "collection",
                     volume,
                     "high" if competition >= 0.80 else "medium" if competition >= commercial_gate else "low"),
                )
                cluster_id = cur.fetchone()[0]
                created += 1
            cur.execute(
                "INSERT INTO cluster_queries (cluster_id, query) VALUES (%s, %s) "
                "ON CONFLICT (cluster_id, query) DO NOTHING",
                (cluster_id, keyword),
            )
    return created


def sync_serp_snapshots(conn, openseo, site_id, queries, skip=False):
    """Map SERP snapshot rows into openseo_serp_snapshots (is_self flag from domain)."""
    if skip:
        return 0
    if not queries or not queries[0].get("ok"):
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT domain FROM site_config WHERE site_id = %s", (site_id,))
        domain_row = cur.fetchone()
        domain = domain_row[0] if domain_row else "example.com"
        cur.execute(
            "SELECT cq.cluster_id, cq.query FROM cluster_queries cq "
            "JOIN keyword_clusters kc ON kc.cluster_id = cq.cluster_id "
            "WHERE kc.site_id = %s",
            (site_id,),
        )
        cluster_by_query = {q: cid for cid, q in cur.fetchall()}
    inserted = 0
    snapshot_date = FIXTURE_ANCHOR_DATE
    with conn.cursor() as cur:
        for r in queries[0].get("data", []):
            query = cluster_by_query_lookup(cluster_by_query, r.get("query"))
            cluster_id = cluster_by_query.get(r.get("query"))
            if cluster_id is None:
                continue
            url = r.get("url")
            if not url:
                continue
            is_self = domain in url
            cur.execute(
                "INSERT INTO openseo_serp_snapshots "
                "(site_id, cluster_id, query, result_url, result_domain, position, is_self, snapshot_date) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (site_id, query, result_url, snapshot_date) DO NOTHING",
                (site_id, cluster_id, r.get("query"), url,
                 url.split("/")[2] if "://" in url else None,
                 r.get("position"), is_self, snapshot_date),
            )
            inserted += 1
    return inserted


def cluster_by_query_lookup(cluster_by_query, query):
    return cluster_by_query.get(query)


def sync_crawl_audit(conn, openseo, site_id, domain):
    """OpenSEO weekly crawl pull → pages.* crawl/audit columns (plan/09 Data Flow 1 step 5)."""
    result = openseo.fetch("crawl_audit", {"site": domain})
    if not result.get("ok"):
        print(f"[sync] openseo.crawl_audit skipped: {result.get('error')}")
        return 0
    updated = 0
    with conn.cursor() as cur:
        for page in result["data"]:
            cur.execute(
                "UPDATE pages SET "
                "status_code = %s, indexable = %s, canonical_url = %s, template = %s, "
                "crawl_depth = %s, internal_links_in = %s, internal_links_out = %s, "
                "raw_html_hash = %s, rendered_html_hash = %s, render_status = %s, "
                "has_structured_data = %s, is_orphan = %s, last_crawled_at = now() "
                "WHERE site_id = %s AND url = %s",
                (page.get("status_code"), page.get("indexable"), page.get("canonical"),
                 page.get("template"), page.get("crawl_depth"), page.get("internal_links_in"),
                 page.get("internal_links_out"), page.get("raw_html_hash"),
                 page.get("rendered_html_hash"), page.get("render_status"),
                 page.get("structured_data"), page.get("is_orphan"), site_id, page.get("url")),
            )
            if cur.rowcount == 0 and page.get("url"):
                cur.execute(
                    "INSERT INTO pages (site_id, url, page_type, title, indexable, "
                    "status_code, canonical_url, template, crawl_depth, internal_links_in, "
                    "internal_links_out, raw_html_hash, rendered_html_hash, render_status, "
                    "has_structured_data, is_orphan, last_crawled_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
                    "ON CONFLICT (site_id, url_hash) DO NOTHING",
                    (site_id, page.get("url"), page.get("page_type") or "unknown",
                     page.get("url"), page.get("indexable"), page.get("status_code"),
                     page.get("canonical"), page.get("template"), page.get("crawl_depth"),
                     page.get("internal_links_in"), page.get("internal_links_out"),
                     page.get("raw_html_hash"), page.get("rendered_html_hash"),
                     page.get("render_status"), page.get("structured_data"), page.get("is_orphan")),
                )
            updated += 1
    return updated


def sync_catalogue_coverage(conn, site_id, domain):
    """Priority 3.7 job: compute catalogue_coverage per cluster (any_variant rule)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cluster_id, primary_keyword FROM keyword_clusters WHERE site_id = %s",
            (site_id,),
        )
        clusters = cur.fetchall()
    shopify = get_shopify_adapter(config={
        "shopify.mode": "mock", "shopify.mock_fixtures_dir": FIXTURES_DIR,
    })
    products_result = shopify.fetch("products", {})
    if not products_result.get("ok"):
        print(f"[sync] shopify.products (coverage) skipped: {products_result.get('error')}")
        return 0
    products = products_result["data"]

    updated = 0
    for cluster_id, primary_keyword in clusters:
        terms = [w for w in primary_keyword.lower().split() if len(w) > 3]
        matching = []
        for p in products:
            haystack = f"{p.get('title', '')} {p.get('product_type', '')} {p.get('tags', '')}".lower()
            if terms and all(t in haystack for t in terms):
                matching.append(p)
        if not matching:
            # relax: any term overlap
            matching = [
                p for p in products
                if any(t in f"{p.get('title', '')} {p.get('tags', '')}".lower() for t in terms)
            ]
        in_stock = [p for p in matching if p.get("in_stock")]
        prices = []
        for p in matching:
            for v in p.get("variants", []):
                try:
                    prices.append(float(v.get("price") or 0))
                except (TypeError, ValueError):
                    pass
        avg_price = round(sum(prices) / len(prices), 2) if prices else None
        existing_url = None
        for c in collections_cache(conn, site_id):
            handle_words = [w for w in (c.get("handle") or "").replace("-", " ").split() if len(w) > 3]
            if len(handle_words) >= 2 and all(t in primary_keyword.lower() for t in handle_words):
                existing_url = c.get("url")
                break
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO catalogue_coverage "
                "(cluster_id, site_id, matching_product_ids, matching_product_count, "
                " in_stock_product_count, average_price, existing_collection_url) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (site_id, cluster_id) DO UPDATE SET "
                "matching_product_ids = EXCLUDED.matching_product_ids, "
                "matching_product_count = EXCLUDED.matching_product_count, "
                "in_stock_product_count = EXCLUDED.in_stock_product_count, "
                "average_price = EXCLUDED.average_price, "
                "existing_collection_url = EXCLUDED.existing_collection_url",
                (cluster_id, site_id,
                 [str(p["product_id"]) for p in matching],
                 len(matching), len(in_stock), avg_price, existing_url),
            )
        updated += 1
    return updated


def collections_cache(conn, site_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT url, title FROM pages WHERE site_id = %s AND page_type = 'collection'",
            (site_id,),
        )
        rows = cur.fetchall()
    out = []
    for url, title in rows:
        handle = url.rstrip("/").rsplit("/", 1)[-1]
        out.append({"url": url, "handle": handle, "title": title})
    return out


def sync_page_query_match_scores(conn, site_id):
    """Priority 3.2 job: page×cluster match scores (semantic scoring stand-in).

    catalogue/semantic components use keyword overlap between the cluster's
    primary keyword and page title+url; gsc_evidence_score is 1.0 only when
    the page actually ranks for the query in search_performance (real GSC
    evidence). Scores below the tier-resolved page_match_threshold are NOT
    recorded (fix 3.7: no local 0.60 copy) — a weak/unknown match means
    Generator 1 may legitimately propose a missing page for the cluster.
    The overlap pre-gate (0.5) is a named constant: MIN_TERM_OVERLAP.
    """
    match_threshold = tier_threshold(conn, site_id, "page_match_threshold")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cluster_id, primary_keyword, intent FROM keyword_clusters WHERE site_id = %s",
            (site_id,),
        )
        clusters = cur.fetchall()
        cur.execute(
            "SELECT page_id, url, page_type, title FROM pages WHERE site_id = %s",
            (site_id,),
        )
        pages = cur.fetchall()
        cur.execute(
            "SELECT DISTINCT page_url FROM search_performance WHERE site_id = %s",
            (site_id,),
        )
        ranking_urls = {row[0] for row in cur.fetchall()}
    inserted = 0
    for cluster_id, primary_keyword, intent in clusters:
        terms = [w for w in primary_keyword.lower().split() if len(w) > 3]
        if not terms:
            continue
        best_page = None
        best_score = 0.0
        for page_id, url, page_type, title in pages:
            hay = f"{title or ''} {url}".lower()
            overlap = sum(1 for t in terms if t in hay) / len(terms)
            if overlap < MIN_TERM_OVERLAP:
                continue
            type_fit = 1.0 if (intent == "commercial" and page_type == "collection") else 0.4
            gsc_evidence = 1.0 if url in ranking_urls else 0.0
            score = round(0.30 * type_fit + 0.30 * overlap + 0.25 * gsc_evidence + 0.15 * overlap, 2)
            if score > best_score:
                best_score = score
                best_page = (page_id, overlap, type_fit, gsc_evidence)
        if best_page and best_score >= match_threshold:
            page_id, overlap, type_fit, gsc_evidence = best_page
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO page_query_match_scores "
                    "(site_id, page_id, cluster_id, intent_page_type_score, "
                    " catalogue_match_score, gsc_evidence_score, semantic_content_score) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (page_id, cluster_id) DO UPDATE SET "
                    "intent_page_type_score = EXCLUDED.intent_page_type_score, "
                    "catalogue_match_score = EXCLUDED.catalogue_match_score, "
                    "gsc_evidence_score = EXCLUDED.gsc_evidence_score, "
                    "semantic_content_score = EXCLUDED.semantic_content_score",
                    (site_id, page_id, cluster_id, type_fit, overlap, gsc_evidence, overlap),
                )
            inserted += 1
    return inserted


def run_sync(fixtures_dir=FIXTURES_DIR, site=MOCK_SITE, force_refresh=False):
    """Full per-site sync; returns a dict of row counts. Idempotent."""
    from urllib.parse import quote  # noqa: F401  (ensures parity with adapters)

    counts = {}
    conn = database.get_connection()
    try:
        site_id = ensure_site(conn, site)

        gsc = get_gsc_adapter(config={
            "gsc.mode": "mock", "gsc.mock_fixtures_dir": fixtures_dir,
            "gsc.site_url": site["gsc_property"],
        })
        openseo = get_openseo_adapter(config={
            "openseo.mode": "mock", "openseo.mock_fixtures_dir": fixtures_dir,
        })
        from connectors.costlog import log_cost
        openseo._on_fetch_complete = lambda capability, params, result: log_cost(
            conn, site_id, f"openseo_{capability}",
            call_type=capability,
            # Provider-reported cost rides on every ok result (adapter adds
            # result["cost"] from the raw envelope); never cost=None anymore.
            cost=result.get("cost"),
            metadata={"params": params},
        )
        shopify = get_shopify_adapter(config={
            "shopify.mode": "mock", "shopify.mock_fixtures_dir": fixtures_dir,
        })
        ga4 = get_ga4_adapter(config={
            "ga4.mode": "mock", "ga4.mock_fixtures_dir": fixtures_dir,
        })

        counts["search_performance"] = sync_search_performance(conn, gsc, site_id, site)
        counts["pages"] = sync_pages_from_shopify(conn, shopify, site_id, site["domain"])
        counts["page_business_performance"] = sync_page_business_performance(conn, ga4, site_id)

        # ── keyword_volume TTL gate ──────────────────────────────────
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ttl_keyword_volume_days, ttl_serp_days "
                "FROM site_config WHERE site_id = %s",
                (site_id,),
            )
            ttl_row = cur.fetchone()
        kw_ttl = ttl_row[0] if ttl_row else 30
        serp_ttl = ttl_row[1] if ttl_row else 7

        if not force_refresh and _is_fresh(conn, site_id, "keyword_volume", kw_ttl):
            print("[sync] keyword_volume: skipped -- fresh snapshot within TTL")
            counts["keyword_clusters"] = 0
            counts["keyword_clusters_updated"] = 0
        else:
            kw = openseo.fetch("keyword_volume", {
                "keywords": [
                    "wireless noise cancelling headphones",
                    "best budget noise cancelling headphones",
                    "noise cancelling headphones sale",
                    "best wireless headphones for travel",
                    "bose quietcomfort comparison",
                ],
                "date": _days_before(9),
            })
            counts["keyword_clusters"] = sync_keyword_clusters(conn, openseo, site_id, [kw] if kw.get("ok") else [])
            counts["keyword_clusters_updated"] = sync_keyword_volumes(conn, openseo, site_id, [kw] if kw.get("ok") else [])

        # ── SERP TTL gate ────────────────────────────────────────────
        if not force_refresh and _is_fresh(conn, site_id, "serp", serp_ttl):
            print("[sync] serp: skipped -- fresh snapshot within TTL")
            counts["openseo_serp_snapshots"] = 0
        else:
            serp = openseo.fetch("serp", {
                "query": "wireless noise cancelling headphones", "limit": 10, "geo": "us",
            })
            counts["openseo_serp_snapshots"] = sync_serp_snapshots(conn, openseo, site_id, [serp] if serp.get("ok") else [])

        counts["crawl_audit_updated"] = sync_crawl_audit(conn, openseo, site_id, site["domain"])
        counts["catalogue_coverage"] = sync_catalogue_coverage(conn, site_id, site["domain"])
        counts["page_query_match_scores"] = sync_page_query_match_scores(conn, site_id)
        counts["openseo_serp_snapshots"] = counts.get("openseo_serp_snapshots", 0)  # Generator 1 feeds on these

        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    counts = run_sync()
    print(json.dumps(counts, indent=2))
    return counts


if __name__ == "__main__":
    main()
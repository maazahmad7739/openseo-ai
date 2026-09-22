"""Daily sync orchestrator (plan/09 Data Flow 1) — offline in mock mode.

Per-site sequence:
  1. GSC search_analytics → search_performance (upsert on UNIQUE constraint)
  2. Shopify products + collections → pages (catalogue crawl stand-in)
  3. GA4 page_performance → page_business_performance (upsert)
  4. OpenSEO keyword_volume → keyword_clusters.search_volume (existing clusters)

Connector modes resolve via the central INTEGRATION_MODE switch (connectors/
mode.py) with per-connector legacy env vars as fallback; when a live mode is
requested but no credentials are present, each adapter falls back to mock
instead of failing the whole sync. Every fetch is idempotent via ON CONFLICT
upserts, so re-running the sync is safe.
"""

import os
import sys
import json
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from connectors.openseo import get_openseo_adapter, OpenseoConfigError  # noqa: E402
from connectors.gsc import get_gsc_adapter, GscConfigError  # noqa: E402
from connectors.shopify import get_shopify_adapter, ShopifyError  # noqa: E402
from connectors.ga4 import get_ga4_adapter, Ga4Error  # noqa: E402
from connectors.url_normalize import canonicalize_url  # noqa: E402

import db as database  # noqa: E402

FIXTURES_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "tests", "fixtures"
))

# Single anchor for the mock universe (fix 3.6): every fixture date in this
# file is derived relative to FIXTURE_ANCHOR_DATE so the whole dataset moves
# as one block and never drifts apart. The GSC fixture itself spans the 5
# weeks ending on the anchor; GA4 covers the anchor's last two days; SERP is
# snapshotted on the anchor. Live mode uses the real clock (or an explicit
# reference_date); the anchor is only the fallback for mock fixtures.
FIXTURE_ANCHOR_DATE = "2026-09-10"

# Live-crawl/task polling budget for the on_page crawl flow (openseo adapter).
CRAWL_READY_ATTEMPTS = 30
CRAWL_READY_INTERVAL_SECONDS = 5


def _reference_window(reference_date, live, days_back):
    """Return (start, end) ISO date strings covering `days_back` days.

    Live mode uses the real clock (today) or an explicit reference_date;
    mock mode falls back to the fixture anchor so the sandbox dataset stays
    coherent. This is the single date-decoupling seam (fix: anchor only in
    mock).
    """
    if reference_date:
        end = date.fromisoformat(str(reference_date)[:10])
    elif live:
        end = date.today()
    else:
        end = date.fromisoformat(FIXTURE_ANCHOR_DATE)
    start = end - timedelta(days=days_back)
    return str(start), str(end)


def _days_before(days):
    """Back-compat helper: anchor-relative date (mock universe only)."""
    anchor = date.fromisoformat(FIXTURE_ANCHOR_DATE)
    return str(anchor - timedelta(days=days))


def _build_adapter(factory, key, config, fixtures_dir, error_types):
    """Build a connector adapter honoring env-driven mode.

    Per-call config wins over environment (connectors resolve mode via
    resolve_mode()). If a live mode is requested but no credentials are
    configured, the adapter factory raises a typed config error — we fall
    back to the mock adapter so the sync never dies on a missing secret.
    """
    cfg = dict(config)
    cfg[f"{key}.mock_fixtures_dir"] = fixtures_dir
    try:
        return factory(config=cfg)
    except error_types as exc:
        print(f"[sync] {key}: live mode unavailable ({exc}) — falling back to mock", flush=True)
        cfg = dict(config)
        cfg[f"{key}.mode"] = "mock"
        cfg[f"{key}.mock_fixtures_dir"] = fixtures_dir
        return factory(config=cfg)


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

# Set-based match-score computation (performance rewrite of the O(pages x
# clusters) Python loop): one INSERT..SELECT per site, executed by the engine
# instead of the app layer. Chunked (MATCH_SCORE_CHUNK) so a large-catalogue
# backfill never holds one transaction long enough to block API reads, with a
# statement_timeout as the loud-failure guard against pathological catalogues.
MATCH_SCORE_CHUNK = 500
MATCH_SCORE_STATEMENT_TIMEOUT_MS = 120000

# Semantics preserved EXACTLY from the former Python loop (verified row-for-row
# against it on the fixture DB before switching callers; see plan/09 §match-scores):
#   terms = words len>3 from lower(primary_keyword)
#   hay = lower("{title or ''} {url}")
#   overlap = matching_terms / n_terms, substring match, rounded to 2dp (the
#     NUMERIC(3,2) column storage semantics; the old column write rounded too)
#   type_fit = 1.0 when intent='commercial' AND page_type='collection' else 0.4
#   gsc_evidence = 1.0 when the page's URL appears in search_performance
#   aggregate = 0.30*type_fit + 0.30*overlap + 0.25*gsc + 0.15*overlap (rounded 2dp,
#     recomputed identically by the page_query_match_scores generated column)
#   pre-gate: overlap >= MIN_TERM_OVERLAP; post-gate: score >= tier page_match_threshold
#   tie-break: score DESC, page_id ASC — the Python loop's "first max wins"
#     depended on unordered heap order (non-deterministic between runs); the
#     explicit page_id ASC tie-break is the deliberate, stable replacement.
#   rounding: numeric (exact half-up, matching the NUMERIC(3,2) column + generated
#     aggregate), replacing the Python float path which lost epsilon on scores
#     landing exactly on a half-cent boundary (e.g. 0.595: float→0.59, numeric→0.60).
#     One borderline cluster flips from unrecorded to recorded with the exact
#     value the generated column always computed — a corrected latent bug.
# Weights live in the generated column on page_query_match_scores (single
# source); this SQL mirrors them only to select the best page pre-insert.
# {cluster_scope} is the optional cluster restriction for the scoped recompute
# path; the full pass substitutes it with an always-true filter so the
# placeholder count stays identical in both paths.
_MATCH_SCORE_SQL = """
WITH cluster_terms AS (
    SELECT kc.cluster_id, kc.site_id, kc.intent,
           array_agg(DISTINCT t.w) AS terms,
           count(DISTINCT t.w)::int AS n_terms
    FROM keyword_clusters kc
    CROSS JOIN LATERAL unnest(regexp_split_to_array(lower(kc.primary_keyword), '\\s+')) AS t(w)
    WHERE kc.site_id = %(site_id)s::uuid AND length(t.w) > 3
      AND kc.cluster_id = ANY(%(cluster_ids)s::uuid[])
    GROUP BY kc.cluster_id, kc.site_id, kc.intent
),
scored AS (
    SELECT ct.cluster_id, ct.site_id, ct.n_terms, ct.terms,
           p.page_id, p.page_type, p.url,
           (EXISTS (SELECT 1 FROM search_performance sp
                    WHERE sp.site_id = p.site_id AND sp.page_url = p.url))::int AS gsc_evidence,
           CASE WHEN ct.intent = 'commercial' AND p.page_type = 'collection'
                THEN 1.0::float ELSE 0.4::float END AS type_fit,
           lower(coalesce(p.title, '') || ' ' || p.url) AS hay
    FROM cluster_terms ct
    JOIN pages p ON p.site_id = ct.site_id
),
overlap AS (
    SELECT s.cluster_id, s.page_id, s.gsc_evidence, s.type_fit, s.n_terms,
           count(*) FILTER (WHERE position(t.w IN s.hay) > 0)::float / s.n_terms AS overlap_raw
    FROM scored s
    CROSS JOIN LATERAL unnest(s.terms) AS t(w)
    GROUP BY s.cluster_id, s.page_id, s.gsc_evidence, s.type_fit, s.n_terms
),
gated AS (
    SELECT o.*,
           round(o.overlap_raw::numeric, 2) AS overlap,
           round((0.30 * o.type_fit + 0.30 * o.overlap_raw
                  + 0.25 * o.gsc_evidence + 0.15 * o.overlap_raw)::numeric, 2) AS score
    FROM overlap o
    WHERE o.overlap_raw >= {min_overlap}
),
best AS (
    SELECT DISTINCT ON (cluster_id) cluster_id, page_id, type_fit, overlap, gsc_evidence, score
    FROM gated
    ORDER BY cluster_id, score DESC, page_id ASC
)
INSERT INTO page_query_match_scores
    (site_id, page_id, cluster_id, intent_page_type_score,
     catalogue_match_score, gsc_evidence_score, semantic_content_score)
SELECT %(site_id)s::uuid, b.page_id, b.cluster_id, b.type_fit, b.overlap, b.gsc_evidence, b.overlap
FROM best b
JOIN site_config sc ON sc.site_id = %(site_id)s::uuid
JOIN threshold_tiers tt ON tt.tier = sc.catalogue_size_tier
WHERE b.score >= tt.{threshold_column}
ON CONFLICT (page_id, cluster_id) DO UPDATE SET
    intent_page_type_score = EXCLUDED.intent_page_type_score,
    catalogue_match_score = EXCLUDED.catalogue_match_score,
    gsc_evidence_score = EXCLUDED.gsc_evidence_score,
    semantic_content_score = EXCLUDED.semantic_content_score
"""

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


def sync_search_performance(conn, gsc, site_id, site, window=None):
    start, end = window or _reference_window(None, False, 7)
    result = gsc.fetch("search_analytics", {
        "site_url": site["gsc_property"],
        "start_date": start,
        "end_date": end,
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
            "url": canonicalize_url(c["url"], domain),
            "page_type": "collection",
            "title": c["title"],
            "indexable": bool(c["published"]),
        })
    product_urls = []
    for p in products["data"]:
        url = canonicalize_url(f"https://{domain}/products/{p['handle']}")
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


def sync_page_business_performance(conn, ga4, site_id, domain="example.com", window=None):
    start, end = window or _reference_window(None, False, 1)
    result = ga4.fetch("page_performance", {
        "start_date": start,
        "end_date": end,
    })
    if not result.get("ok"):
        print(f"[sync] ga4.page_performance skipped: {result.get('error')}")
        return 0
    rows = []
    for r in result["data"]:
        url = canonicalize_url(f"https://{domain}{r['page_path']}")
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


def sync_serp_snapshots(conn, openseo, site_id, queries, skip=False, reference_date=None, live=False):
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
    snapshot_date = _reference_window(reference_date, live, 0)[1]
    with conn.cursor() as cur:
        for r in queries[0].get("data", []):
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


def sync_crawl_audit(conn, openseo, site_id, domain):
    """OpenSEO weekly crawl pull → pages.* crawl/audit columns (plan/09 Data Flow 1 step 5)."""
    result = openseo.fetch("crawl_audit", {"site": domain})
    if not result.get("ok"):
        print(f"[sync] openseo.crawl_audit skipped: {result.get('error')}")
        return 0
    updated = 0
    with conn.cursor() as cur:
        for page in result["data"]:
            url = canonicalize_url(page.get("url"), domain)
            canonical = canonicalize_url(page.get("canonical"), domain) if page.get("canonical") else None
            cur.execute(
                "UPDATE pages SET "
                "status_code = %s, indexable = %s, canonical_url = %s, template = %s, "
                "crawl_depth = %s, internal_links_in = %s, internal_links_out = %s, "
                "raw_html_hash = %s, rendered_html_hash = %s, render_status = %s, "
                "has_structured_data = %s, is_orphan = %s, last_crawled_at = now() "
                "WHERE site_id = %s AND url = %s",
                (page.get("status_code"), page.get("indexable"), canonical,
                 page.get("template"), page.get("crawl_depth"), page.get("internal_links_in"),
                 page.get("internal_links_out"), page.get("raw_html_hash"),
                 page.get("rendered_html_hash"), page.get("render_status"),
                 page.get("structured_data"), page.get("is_orphan"), site_id, url),
            )
            if cur.rowcount == 0 and url:
                cur.execute(
                    "INSERT INTO pages (site_id, url, page_type, title, indexable, "
                    "status_code, canonical_url, template, crawl_depth, internal_links_in, "
                    "internal_links_out, raw_html_hash, rendered_html_hash, render_status, "
                    "has_structured_data, is_orphan, last_crawled_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
                    "ON CONFLICT (site_id, url_hash) DO NOTHING",
                    (site_id, url, page.get("page_type") or "unknown",
                     url, page.get("indexable"), page.get("status_code"),
                     canonical, page.get("template"), page.get("crawl_depth"),
                     page.get("internal_links_in"), page.get("internal_links_out"),
                     page.get("raw_html_hash"), page.get("rendered_html_hash"),
                     page.get("render_status"), page.get("structured_data"), page.get("is_orphan")),
                )
            updated += 1
    return updated


def sync_catalogue_coverage(conn, site_id, domain, shopify=None):
    """Priority 3.7 job: compute catalogue_coverage per cluster (any_variant rule).

    `shopify` may be the adapter already resolved by run_sync (so a live
    deployment reuses its mode/credentials); when omitted the function
    constructs a mock adapter so the job stays runnable standalone.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cluster_id, primary_keyword FROM keyword_clusters WHERE site_id = %s",
            (site_id,),
        )
        clusters = cur.fetchall()
    if shopify is None:
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


def sync_page_query_match_scores(conn, site_id, cluster_ids=None, chunk_size=MATCH_SCORE_CHUNK):
    """Priority 3.2 job: page×cluster match scores (semantic scoring stand-in).

    Set-based rewrite of the former O(pages x clusters) Python loop: the whole
    computation runs as one INSERT..SELECT (chunked by cluster for the backfill
    path) executed by Postgres — same inputs, same outputs, ~100x less app-side
    work and no long-running app-transaction locks.

    catalogue/semantic components use keyword overlap between the cluster's
    primary keyword and page title+url; gsc_evidence_score is 1.0 only when
    the page actually ranks for the query in search_performance (real GSC
    evidence). Scores below the tier-resolved page_match_threshold are NOT
    recorded (fix 3.7: no local 0.60 copy) — a weak/unknown match means
    Generator 1 may legitimately propose a missing page for the cluster.
    The overlap pre-gate (0.5) is a named constant: MIN_TERM_OVERLAP.

    cluster_ids: optional scope restriction (scoped recompute — only clusters
    whose coverage/keywords changed in this sync, instead of the full recompute).
    None = all clusters for the site (first sync, force-refresh, backfill).

    chunk_size: clusters processed per statement/transaction chunk so a large
    catalogue backfill never holds a long transaction. A statement_timeout
    guards each chunk so a pathological catalogue fails loudly, never locks.
    """
    match_threshold = tier_threshold(conn, site_id, "page_match_threshold")
    sql = (
        _MATCH_SCORE_SQL
        .replace("{min_overlap}", str(MIN_TERM_OVERLAP))
        .replace("{threshold_column}", "page_match_threshold")
    )
    # Full-pass scope: every cluster for the site. The scoped/chunked path
    # narrows to the caller-provided cluster_ids (same statement, different params).
    if cluster_ids is not None:
        inserted = 0
        ids = [str(c) for c in cluster_ids]
        for start in range(0, len(ids), chunk_size):
            chunk = ids[start:start + chunk_size]
            with conn.cursor() as cur:
                cur.execute(
                    f"SET LOCAL statement_timeout = {MATCH_SCORE_STATEMENT_TIMEOUT_MS}")
                cur.execute(sql, {"site_id": site_id, "cluster_ids": chunk})
                inserted += cur.rowcount
            conn.commit()
        return inserted
    # Full pass: single statement (still bounded by statement_timeout).
    # cluster_ids = all cluster ids for the site (ANY() over the full list is
    # the always-true scope; keeps ONE statement shape for both paths).
    all_ids = None
    with conn.cursor() as cur:
        cur.execute("SELECT cluster_id FROM keyword_clusters WHERE site_id = %s::uuid", (site_id,))
        all_ids = [str(r[0]) for r in cur.fetchall()]
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {MATCH_SCORE_STATEMENT_TIMEOUT_MS}")
            cur.execute(sql, {"site_id": site_id, "cluster_ids": all_ids})
            inserted = cur.rowcount
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise


def run_sync(fixtures_dir=FIXTURES_DIR, site=None, force_refresh=False, reference_date=None):
    """Full per-site sync; returns a dict of row counts. Idempotent.

    `site` may be a site_config-shaped dict (site_name, domain, gsc_property,
    shopify_domain, ga4_property_id, catalogue_size_tier, min_in_stock_products)
    built from the DB, or None to resolve the first configured site (falling
    back to MOCK_SITE when none exists, which keeps the sandbox demo working).
    `reference_date` overrides the clock (used by jobs for backfills); in live
    mode without it, real "today" drives all windows (anchor only in mock).
    """
    from urllib.parse import quote  # noqa: F401  (ensures parity with adapters)

    counts = {}
    conn = database.get_connection()
    try:
        site = site or _resolve_default_site(conn)
        site_id = ensure_site(conn, site)

        gsc = _build_adapter(
            get_gsc_adapter, "gsc",
            {"gsc.site_url": site["gsc_property"]}, fixtures_dir, (GscConfigError,),
        )
        # OpenSEO is the paid-data path: mode is env/config-driven (default
        # mock) and falls back to mock when no credential is resolvable.
        openseo = _build_adapter(
            get_openseo_adapter, "openseo",
            {}, fixtures_dir, (OpenseoConfigError,),
        )
        from connectors.costlog import log_cost
        openseo._on_fetch_complete = lambda capability, params, result: log_cost(
            conn, site_id, f"openseo_{capability}",
            call_type=capability,
            # Provider-reported cost rides on every ok result (adapter adds
            # result["cost"] from the raw envelope); never cost=None anymore.
            cost=result.get("cost"),
            metadata={"params": params},
        )
        shopify = _build_adapter(
            get_shopify_adapter, "shopify",
            {"shopify.domain": site["shopify_domain"]}, fixtures_dir, (ShopifyError,),
        )
        ga4 = _build_adapter(
            get_ga4_adapter, "ga4",
            {"ga4.property_id": site["ga4_property_id"]}, fixtures_dir, (Ga4Error,),
        )

        # Any connector in live mode uses the real clock (or reference_date);
        # only a fully-mock sync anchors to the fixture universe.
        live = any(not a.mock_mode for a in (gsc, shopify, ga4, openseo))

        counts["search_performance"] = sync_search_performance(
            conn, gsc, site_id, site, _reference_window(reference_date, live, 7))
        counts["pages"] = sync_pages_from_shopify(conn, shopify, site_id, site["domain"])
        counts["page_business_performance"] = sync_page_business_performance(
            conn, ga4, site_id, site["domain"], _reference_window(reference_date, live, 1))

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
                "date": _reference_window(reference_date, live, 9)[0],
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
            counts["openseo_serp_snapshots"] = sync_serp_snapshots(
                conn, openseo, site_id, [serp] if serp.get("ok") else [],
                reference_date=reference_date, live=live,
            )

        counts["crawl_audit_updated"] = sync_crawl_audit(conn, openseo, site_id, site["domain"])
        counts["catalogue_coverage"] = sync_catalogue_coverage(conn, site_id, site["domain"], shopify)
        # Scoped recompute: only clusters whose coverage/keyword data changed in
        # THIS sync (their catalogue_coverage was rewritten) get a fresh match
        # score. Full pass only when no scores exist yet (first sync / backfill).
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM page_query_match_scores WHERE site_id = %s::uuid",
                (site_id,),
            )
            scores_exist = cur.fetchone()[0] > 0
        if scores_exist:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT cluster_id FROM catalogue_coverage WHERE site_id = %s::uuid",
                    (site_id,),
                )
                covered = {str(r[0]) for r in cur.fetchall()}
            counts["page_query_match_scores"] = sync_page_query_match_scores(
                conn, site_id, cluster_ids=sorted(covered))
        else:
            counts["page_query_match_scores"] = sync_page_query_match_scores(conn, site_id)
        counts["openseo_serp_snapshots"] = counts.get("openseo_serp_snapshots", 0)  # Generator 1 feeds on these

        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _resolve_default_site(conn):
    """First configured site from site_config, else the mock-site fallback.

    This is the only remaining place that touches MOCK_SITE: real deployments
    have ≥1 row in site_config and daily_sync iterates every row; an empty
    site_config falls back to the sandbox site so the demo keeps working.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT site_name, domain, gsc_property, shopify_domain, "
            "ga4_property_id, catalogue_size_tier, min_in_stock_products "
            "FROM site_config ORDER BY created_at LIMIT 1",
        )
        row = cur.fetchone()
    if not row:
        return dict(MOCK_SITE)
    site = dict(zip(("site_name", "domain", "gsc_property", "shopify_domain",
                     "ga4_property_id", "catalogue_size_tier",
                     "min_in_stock_products"), row))
    if site.get("min_in_stock_products") is None:
        site["min_in_stock_products"] = 1
    return site


def main():
    counts = run_sync()
    print(json.dumps(counts, indent=2))
    return counts


if __name__ == "__main__":
    main()
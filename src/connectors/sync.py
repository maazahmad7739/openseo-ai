"""Daily sync orchestrator (plan/09 Data Flow 1) — offline in mock mode.

Per-site sequence:
  1. GSC search_analytics → search_performance (upsert on UNIQUE constraint)
  2. Shopify products + collections → pages (catalogue crawl stand-in)
  3. GA4 page_performance → page_business_performance (upsert)
  4. OpenSEO keyword_volume → keyword_clusters.search_volume (existing clusters)

Connector modes resolve PER-SITE from site_config.integration_mode
(connectors/mode.py resolve_site_mode) with the global INTEGRATION_MODE env
as fallback. plan/31: a live/sandbox site whose credentials are missing
FAILS LOUDLY (SiteModeError) — mock fixture data is never silently injected
into a live run. 'import' sites (Helium) skip connector fetches entirely so
operator-imported rows are never overwritten by mock fixtures. Every fetch
is idempotent via ON CONFLICT upserts, so re-running the sync is safe.
"""

import os
import sys
import json
import html
import re
from datetime import date, timedelta
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from connectors.openseo import get_openseo_adapter, OpenseoConfigError  # noqa: E402
from connectors.gsc import (  # noqa: E402
    get_gsc_adapter, GscConfigError, URL_INSPECTION_DAILY_QUOTA,
)
from connectors.shopify import get_shopify_adapter, ShopifyError  # noqa: E402
from connectors.ga4 import get_ga4_adapter, Ga4Error  # noqa: E402
from connectors.mode import resolve_site_mode, adapter_mode  # noqa: E402
from connectors.url_normalize import canonicalize_url  # noqa: E402
from audit.structured_data import html_has_structured_data  # noqa: E402
from audit.url_pattern import template_from_url  # noqa: E402

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

# Keyword research: per-site target keywords come from site_config
# (target_keywords TEXT[]; plan/31). Empty/NULL = keyword/SERP steps are
# skipped cleanly (they are paid DataForSEO calls) — never the old
# Aurora headphone list.
TARGET_KEYWORDS_COLUMN = "target_keywords"


class SiteModeError(Exception):
    """Raised when a live/sandbox site cannot build a live adapter.

    plan/31: fail-closed. The old behavior silently fell back to mock
    fixtures, blending synthetic data into a live sync. A site running
    integration_mode='live'/'sandbox' with missing credentials must halt
    the sync instead.
    """


class SiteRequiredError(Exception):
    """plan/31: site resolution is strictly mandatory. The old
    'ORDER BY created_at LIMIT 1' fallback silently operated on the
    oldest site (Aurora); every caller must now resolve a real site."""


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


def _build_adapter(conn, factory, key, config, fixtures_dir, error_types, site_id,
                   applicable=True):
    """Build a connector adapter honoring the SITE's integration mode.

    plan/31: mode comes from site_config.integration_mode (resolve_site_mode),
    then the global env. Mock/import sites get the offline adapter. A
    live/sandbox site whose credentials are missing raises SiteModeError —
    the silent mock fallback is gone (never blend fixtures into a live run).

    applicable=False (the site lacks the connector's coordinate, e.g. no
    ga4_property_id) returns None cleanly — that connector's sync step is
    skipped, and a live site is never failed for an inapplicable connector.
    """
    if not applicable:
        return None, "not_applicable"
    site_mode = resolve_site_mode(conn, site_id)
    amode = adapter_mode(site_mode)
    cfg = dict(config)
    cfg[f"{key}.mode"] = amode
    cfg[f"{key}.mock_fixtures_dir"] = fixtures_dir
    if amode == "live":
        try:
            return factory(config=cfg), amode
        except error_types as exc:
            raise SiteModeError(
                f"site {site_id} is integration_mode='{site_mode}' but the "
                f"{key} adapter cannot go live: {exc}"
            ) from exc
    try:
        return factory(config=cfg), amode
    except error_types as exc:
        # Offline site, adapter build still failed (e.g. malformed config):
        # fall back to plain mock so the offline demo keeps working.
        print(f"[sync] {key}: adapter build failed ({exc}) — falling back to mock", flush=True)
        cfg = dict(config)
        cfg[f"{key}.mode"] = "mock"
        cfg[f"{key}.mock_fixtures_dir"] = fixtures_dir
        return factory(config=cfg), amode


def _site_connector_applicability(conn, site_id, site):
    """Which connectors does this site actually use (plan/31)?

    GSC needs a gsc_property; GA4 needs a ga4_property_id; Shopify applies
    only to platform='shopify' sites. Missing coordinates skip the
    connector's sync step cleanly — a live site never fails on an
    inapplicable connector.
    """
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT COALESCE(platform, 'shopify') FROM site_config WHERE site_id = %s",
                (site_id,),
            )
            row = cur.fetchone()
        except Exception:
            conn.rollback()
            row = ("shopify",)
    platform = (row[0] if row and row[0] else "shopify")
    return {
        "gsc": bool(site.get("gsc_property")),
        "ga4": bool(site.get("ga4_property_id")),
        "shopify": platform == "shopify",
    }


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


def ensure_site(conn, site=None):
    """Insert a site row once; return site_id (idempotent by domain).

    plan/31: the implicit MOCK_SITE default is removed — callers pass the
    site explicitly. Kept for tests/seed scripts that construct their own
    site dict.
    """
    site = site or MOCK_SITE
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


def sync_search_performance(conn, gsc, site_id, site, window=None, data_source=None):
    """GSC search_analytics -> search_performance (plan/31 GSC fix).

    Two-pass strategy tuned to real GSC API row-visibility behavior:
      Pass 1: dimensions=[date,page], dataState='all' — fresh/unfinalized
              rows only survive at LOW-cardinality groupings (a 5-dim
              date+query+page+country+device grouping on a small/new
              property returns 0 rows even when data exists; adding any
              third dimension to date+page collapses 31 rows to 1).
      Pass 2: dimensions=[date,query], dataState='all' — per-query rows
              when the property has them.
    query is NOT NULL in the DB; page-level rows carry the
    '(not provided/none)' placeholder (the same convention the GSC
    export import already used), query-level rows use their page's best
    match the same way. Both pass merge into one idempotent upsert.
    """
    start, end = window or _reference_window(None, False, 7)
    NOT_PROVIDED = "(not provided/none)"
    result = gsc.fetch("search_analytics", {
        "site_url": site["gsc_property"],
        "start_date": start,
        "end_date": end,
        "dimensions": ["date", "page"],
        # dataState='all': the API default returns only FINALIZED rows; a
        # new/low-traffic property's rows stay unfinalized for days, so
        # default queries return empty. 'all' includes fresh data.
        "data_state": "all",
    })
    if not result.get("ok"):
        print(f"[sync] gsc.search_analytics skipped: {result.get('error')} "
              f"detail: {str(result.get('detail'))[:200]}")
        return 0
    rows = [dict(r, site_id=str(site_id), query=NOT_PROVIDED,
                 country="all", device="all")
            for r in result["data"]]
    # Pass 2: query-level rows (only when the property has any)
    result_q = gsc.fetch("search_analytics", {
        "site_url": site["gsc_property"],
        "start_date": start,
        "end_date": end,
        "dimensions": ["date", "query"],
        "data_state": "all",
    })
    query_rows = []
    if result_q.get("ok"):
        for r in result_q["data"]:
            if not r.get("query"):
                continue
            # Map the query row to the page that earned it (the site's top
            # page within the window that also has data on that date) —
            # the same mapping convention as scripts/import_gethelium_gsc.py.
            page_candidates = {(row["date"], row["page_url"]): row
                               for row in result["data"]}
            match = None
            for (d, purl), row in page_candidates.items():
                if d == r.get("date"):
                    match = row
                    break
            query_rows.append(dict(
                r, site_id=str(site_id),
                page_url=(match or {}).get("page_url"),
                country="all", device="all",
            ))
        # De-dup: drop pass-1 placeholder rows whose (date,page) got a
        # query-level row (query rows are strictly more informative).
    if data_source:
        rows = [dict(r, data_source=data_source) for r in rows]
        query_rows = [dict(r, data_source=data_source) for r in query_rows]
    inserted = database.insert_rows(
        conn, "search_performance", rows,
        conflict_target="site_id, date, query, page_url_hash, country, device",
        update_keys=["clicks", "impressions", "ctr", "position", "data_source"],
    )
    if query_rows:
        inserted += database.insert_rows(
            conn, "search_performance", query_rows,
            conflict_target="site_id, date, query, page_url_hash, country, device",
            update_keys=["clicks", "impressions", "ctr", "position", "data_source"],
        )
    return inserted


def sync_pages_from_shopify(conn, shopify, site_id, domain="example.com", data_source=None):
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
            "meta_description": c.get("meta_description"),
            "body_html": c.get("body_html"),
            # GraphQL writes (productUpdate/collectionUpdate) key on the
            # GID, not the REST numeric id (plan/22 §3). The connector
            # normalization carries the raw REST id; the GID is minted
            # here so generated fixes always have a write target.
            "shopify_gid": _shopify_gid("Collection", c.get("collection_id")),
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
            "meta_description": p.get("meta_description"),
            "body_html": p.get("body_html"),
            "shopify_gid": _shopify_gid("Product", p.get("product_id")),
        })

    inserted = 0
    for row in rows:
        body_text_hash = _body_text_hash(row.get("body_html"))
        with conn.cursor() as cur:
            cur.execute(
                """
            INSERT INTO pages (site_id, url, page_type, title, indexable,
                               meta_description, body_html, body_text_hash,
                               shopify_gid, data_source, has_structured_data,
                               template)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (site_id, url_hash) DO UPDATE SET
                    title = EXCLUDED.title,
                    indexable = EXCLUDED.indexable,
                    meta_description = COALESCE(EXCLUDED.meta_description, pages.meta_description),
                    body_html = COALESCE(EXCLUDED.body_html, pages.body_html),
                    body_text_hash = COALESCE(EXCLUDED.body_text_hash, pages.body_text_hash),
                    shopify_gid = COALESCE(EXCLUDED.shopify_gid, pages.shopify_gid),
                    data_source = COALESCE(EXCLUDED.data_source, pages.data_source),
                    has_structured_data = COALESCE(
                        EXCLUDED.has_structured_data, pages.has_structured_data),
                    template = COALESCE(pages.template, EXCLUDED.template)
            """,
            (site_id, row["url"], row["page_type"], row["title"], row["indexable"],
             row.get("meta_description"), row.get("body_html"), body_text_hash,
             row.get("shopify_gid"), data_source,
             html_has_structured_data(row.get("body_html")),
             template_from_url(row["url"])),
        )
            inserted += 1
    return inserted


def _shopify_gid(resource, raw_id):
    """REST numeric id -> GraphQL GID ('gid://shopify/Product/123').

    The REST admin API returns numeric ids; GraphQL mutations require the
    gid:// form (plan/21 §4). Tolerates inputs that are already GIDs, int
    or str. Returns None when the id is missing/blank (a row without a GID
    is simply not fix-targetable — never fabricated).
    """
    if raw_id is None:
        return None
    raw = str(raw_id).strip()
    if not raw:
        return None
    if raw.startswith("gid://"):
        return raw
    digits = raw.split("?")[0].split("/")[-1]
    if not digits.isdigit():
        return None
    return f"gid://shopify/{resource}/{digits}"


def _body_text_hash(body_html):
    """Phase 2 near-duplicate key (plan/21 §2.2): normalized visible text,
    hashed md5. Empty/None body -> NULL (a null never equals a null)."""
    if not body_html:
        return None
    text = re.sub(r"<[^>]+>", " ", body_html)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip().lower()
    if not text:
        return None
    return database.md5_hash(text)


def sync_page_business_performance(conn, ga4, site_id, domain="example.com", window=None, data_source=None):
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
        if data_source:
            rows[-1]["data_source"] = data_source
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
    """Map SERP snapshot rows into openseo_serp_snapshots (is_self flag from
    domain). Competitor title/snippet persist verbatim (Phase 2 grounding,
    plan/21 §2.1/§2.2); url_pattern = the URL's archetype segment."""
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
                "(site_id, cluster_id, query, result_url, result_domain, position, "
                "is_self, result_title, result_snippet, url_pattern, snapshot_date) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (site_id, query, result_url, snapshot_date) DO NOTHING",
                (site_id, cluster_id, r.get("query"), url,
                 url.split("/")[2] if "://" in url else None,
                 r.get("position"), is_self,
                 r.get("title"), r.get("snippet"),
                 _url_pattern(url), snapshot_date),
            )
            inserted += 1
    return inserted


def _url_pattern(url):
    """URL archetype segment (plan/21 §2.4 inference reuse): the first path
    segment of the URL, lowercase. '/collections/x' -> '/collections/'.
    Uses urlparse so query strings/fragments never leak into the pattern."""
    if not url or "://" not in url:
        return None
    path = urlparse(url).path or ""
    segments = [s for s in path.split("/") if s]
    if not segments:
        return "/"
    return "/" + segments[0].lower() + "/"


def sync_crawl_audit(conn, openseo, site_id, domain, data_source=None):
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
            # plan/32: the provider's template is authoritative; fall back to
            # the inferred archetype only when it reports none, so generator
            # can group by template for provider crawls too.
            template = page.get("template") or template_from_url(url)
            cur.execute(
                "UPDATE pages SET "
                "status_code = %s, indexable = %s, canonical_url = %s, template = %s, "
                "crawl_depth = %s, internal_links_in = %s, internal_links_out = %s, "
                "raw_html_hash = %s, rendered_html_hash = %s, render_status = %s, "
                "has_structured_data = %s, is_orphan = %s, last_crawled_at = now() "
                "WHERE site_id = %s AND url = %s",
                (page.get("status_code"), page.get("indexable"), canonical,
                 template, page.get("crawl_depth"), page.get("internal_links_in"),
                 page.get("internal_links_out"), page.get("raw_html_hash"),
                 page.get("rendered_html_hash"), page.get("render_status"),
                 page.get("structured_data"), page.get("is_orphan"), site_id, url),
            )
            if cur.rowcount == 0 and url:
                cur.execute(
                    "INSERT INTO pages (site_id, url, page_type, title, indexable, "
                    "status_code, canonical_url, template, crawl_depth, internal_links_in, "
                    "internal_links_out, raw_html_hash, rendered_html_hash, render_status, "
                    "has_structured_data, is_orphan, last_crawled_at, data_source) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s) "
                    "ON CONFLICT (site_id, url_hash) DO NOTHING",
                    (site_id, url, page.get("page_type") or "unknown",
                     url, page.get("indexable"), page.get("status_code"),
                     canonical, template, page.get("crawl_depth"),
                     page.get("internal_links_in"), page.get("internal_links_out"),
                     page.get("raw_html_hash"), page.get("rendered_html_hash"),
                     page.get("render_status"), page.get("structured_data"), page.get("is_orphan"),
                     data_source),
                )
            updated += 1
    return updated


# ────────────────────────────────────────────
# GSC URL Inspection (Task 3)
# ────────────────────────────────────────────

INSPECTION_TTL_DAYS = 7
INSPECTION_DEFAULT_RUN_LIMIT = 400
INSPECTION_SERVICE = "gsc_url_inspection"


def _inspection_targets(conn, site_id, limit):
    """Inspection candidates in priority order (2,000/day is the budget):
      1. indexable=false pages (the not-indexed-but-earns-clicks set first,
         ordered by residual GSC clicks DESC)
      2. stale/never-inspected pages (oldest last_inspected_at first)
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.url
            FROM pages p
LEFT JOIN (
                    SELECT page_url_hash, SUM(clicks) AS clicks_28d
                    FROM search_performance
                    WHERE date >= CURRENT_DATE - INTERVAL '28 days'
                      AND site_id = %s
                    GROUP BY page_url_hash
                ) sp ON sp.page_url_hash = p.url_hash
                WHERE p.site_id = %s
                  AND (p.indexable = false
                       OR p.gsc_inspected_at IS NULL
                       OR p.gsc_inspected_at < now() - INTERVAL '%s days')
                ORDER BY p.indexable ASC NULLS LAST,
                         COALESCE(sp.clicks_28d, 0) DESC,
                         COALESCE(p.gsc_inspected_at, 'epoch') ASC
                LIMIT %s
                """,
                (site_id, site_id, INSPECTION_TTL_DAYS, limit),
        )
        return [r[0] for r in cur.fetchall()]


def _inspection_quota_remaining(conn):
    """Calls remaining under the property's documented 2,000/day quota.

    Counted from api_costs rows logged for gsc_url_inspection since UTC
    midnight. The quota is external truth, so this caps even when no
    budget_config row exists.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(calls), 0) FROM api_costs
            WHERE service = %s AND timestamp >= date_trunc('day', now())
            """,
            (INSPECTION_SERVICE,),
        )
        used = int(cur.fetchone()[0] or 0)
    return max(0, URL_INSPECTION_DAILY_QUOTA - used)


def apply_inspection_result(conn, site_id, url, row):
    """One inspection row -> pages update (verdict + coverage + sitemap)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pages SET
                gsc_verdict = %s,
                gsc_coverage_state = %s,
                gsc_robots_txt_state = %s,
                gsc_google_canonical = %s,
                gsc_in_sitemap = %s,
                gsc_inspected_at = now()
            WHERE site_id = %s AND url = %s
            """,
            (row.get("verdict"), row.get("coverage_state"),
             row.get("robots_txt_state"), row.get("google_canonical"),
             row.get("in_sitemap"), site_id, url),
        )


def sync_url_inspection(conn, gsc, site_id, limit=None):
    """GSC URL Inspection pull -> pages.* Google-verdict columns.

    Quota respected via the per-run limit + a shared count against the
    api_costs rows logged for gsc_url_inspection today (UTC). Every
    failure is a typed skip (never-raise adapter contract; never a crash).
    plan/31: a None adapter (inapplicable connector) skips cleanly.
    """
    if gsc is None:
        return 0
    run_limit = limit or INSPECTION_DEFAULT_RUN_LIMIT
    inspected = 0
    for url in _inspection_targets(conn, site_id, run_limit):
        if inspected >= run_limit:
            break
        remaining = _inspection_quota_remaining(conn)
        if remaining <= 0:
            print("[sync] url_inspection: daily property quota reached — "
                  "remaining URLs deferred to the next run", flush=True)
            break
        result = gsc.fetch("url_inspection", {"inspection_url": url})
        if not result.get("ok"):
            print(f"[sync] gsc.url_inspection skipped for {url}: "
                  f"{result.get('error')}")
            continue
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_costs (site_id, service, call_type, calls, cost) "
                "VALUES (%s, %s, 'url_inspection', 1, 0)",
                (site_id, INSPECTION_SERVICE),
            )
        conn.commit()
        row = (result.get("data") or [{}])[0]
        apply_inspection_result(conn, site_id, url, row)
        inspected += 1
    return inspected


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


def run_sync(fixtures_dir=FIXTURES_DIR, site=None, force_refresh=False, reference_date=None,
             site_id=None):
    """Full per-site sync; returns a dict of row counts. Idempotent.

    `site` may be a site_config-shaped dict (site_id, site_name, domain,
    gsc_property, shopify_domain, ga4_property_id, catalogue_size_tier,
    min_in_stock_products, integration_mode, target_keywords) built from the
    DB. plan/31: site resolution is STRICTLY mandatory — `site` (with
    site_id) or `site_id` must resolve a real site_config row; the old
    first-site/mock fallbacks are gone (SiteRequiredError instead).
    `reference_date` overrides the clock (used by jobs for backfills); in live
    mode without it, real "today" drives all windows (anchor only in mock).
    """
    from urllib.parse import quote  # noqa: F401  (ensures parity with adapters)

    counts = {}
    conn = database.get_connection()
    try:
        site, site_id = _resolve_site(conn, site, site_id)
        site_mode = resolve_site_mode(conn, site_id)
        amode = adapter_mode(site_mode)

        # Applicability (plan/31): a live site only fails on connectors it
        # actually uses. Missing site coordinates (no ga4_property_id, no
        # shopify_domain on a non-shopify platform) SKIP that connector
        # step cleanly instead of failing the whole live sync.
        applicable = _site_connector_applicability(conn, site_id, site)

        gsc, gsc_amode = _build_adapter(
            conn, get_gsc_adapter, "gsc",
            {"gsc.site_url": site["gsc_property"]}, fixtures_dir, (GscConfigError,),
            site_id, applicable=applicable["gsc"],
        )
        # OpenSEO is the paid-data path: mode is per-site/env-driven (default
        # mock); live/sandbox sites with missing credentials fail loudly.
        openseo, openseo_amode = _build_adapter(
            conn, get_openseo_adapter, "openseo",
            {}, fixtures_dir, (OpenseoConfigError,),
            site_id, applicable=True,
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
        shopify, shopify_amode = _build_adapter(
            conn, get_shopify_adapter, "shopify",
            {"shopify.domain": site["shopify_domain"]}, fixtures_dir, (ShopifyError,),
            site_id, applicable=applicable["shopify"],
        )
        ga4, ga4_amode = _build_adapter(
            conn, get_ga4_adapter, "ga4",
            {"ga4.property_id": site["ga4_property_id"]}, fixtures_dir, (Ga4Error,),
            site_id, applicable=applicable["ga4"],
        )

        # Any connector in live mode uses the real clock (or reference_date);
        # only a fully-mock sync anchors to the fixture universe.
        live = any(m == "live" for m in (gsc_amode, shopify_amode, ga4_amode, openseo_amode))

        # plan/31 'import' sites (Helium): connector traffic is DISABLED —
        # data arrives via operator file imports, and mock fixtures must
        # never overwrite imported rows. The site's identity row is ensured,
        # then the connector steps are skipped cleanly.
        if site_mode == "import":
            counts["skipped_reason"] = "integration_mode=import (connector traffic disabled; imported data preserved)"
            conn.commit()
            return counts

        if gsc is None:
            counts["search_performance"] = 0
            counts["gsc_skipped"] = "no gsc_property"
        else:
            counts["search_performance"] = sync_search_performance(
                conn, gsc, site_id, site, _reference_window(reference_date, live, 7),
                data_source=_data_source_for(amode, site_mode))
        if shopify is None:
            counts["pages"] = 0
            counts["pages_skipped"] = "not a shopify-platform site"
        else:
            counts["pages"] = sync_pages_from_shopify(conn, shopify, site_id, site["domain"],
                                                      data_source=_data_source_for(shopify_amode, site_mode))
        if ga4 is None:
            counts["page_business_performance"] = 0
            counts["ga4_skipped"] = "no ga4_property_id"
        else:
            counts["page_business_performance"] = sync_page_business_performance(
                conn, ga4, site_id, site["domain"], _reference_window(reference_date, live, 1),
                data_source=_data_source_for(ga4_amode, site_mode))

        # Per-site keyword research targets (plan/31): site_config
        # .target_keywords. None/empty -> the paid keyword/SERP steps are
        # skipped (no hardcoded fallback ever).
        targets = _site_target_keywords(conn, site_id)
        counts["target_keywords"] = len(targets)

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

        if not targets:
            print("[sync] keyword_volume: skipped -- no target_keywords configured for site")
            counts["keyword_clusters"] = 0
            counts["keyword_clusters_updated"] = 0
        elif not force_refresh and _is_fresh(conn, site_id, "keyword_volume", kw_ttl):
            print("[sync] keyword_volume: skipped -- fresh snapshot within TTL")
            counts["keyword_clusters"] = 0
            counts["keyword_clusters_updated"] = 0
        else:
            kw = openseo.fetch("keyword_volume", {
                "keywords": targets,
                "date": _reference_window(reference_date, live, 9)[0],
            })
            counts["keyword_clusters"] = sync_keyword_clusters(conn, openseo, site_id, [kw] if kw.get("ok") else [])
            counts["keyword_clusters_updated"] = sync_keyword_volumes(conn, openseo, site_id, [kw] if kw.get("ok") else [])

        # ── SERP TTL gate ────────────────────────────────────────────
        if not targets:
            print("[sync] serp: skipped -- no target_keywords configured for site")
            counts["openseo_serp_snapshots"] = 0
        elif not force_refresh and _is_fresh(conn, site_id, "serp", serp_ttl):
            print("[sync] serp: skipped -- fresh snapshot within TTL")
            counts["openseo_serp_snapshots"] = 0
        else:
            counts["openseo_serp_snapshots"] = 0
            for query in targets:
                serp = openseo.fetch("serp", {
                    "query": query, "limit": 10, "geo": "us",
                })
                counts["openseo_serp_snapshots"] += sync_serp_snapshots(
                    conn, openseo, site_id, [serp] if serp.get("ok") else [],
                    reference_date=reference_date, live=live,
                )

        counts["crawl_audit_updated"] = sync_crawl_audit(conn, openseo, site_id, site["domain"])
        counts["url_inspection_updated"] = sync_url_inspection(conn, gsc, site_id)
        if shopify is None:
            counts["catalogue_coverage"] = 0
            counts["catalogue_coverage_skipped"] = "not a shopify-platform site"
        else:
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


def _data_source_for(adapter_amode, site_mode):
    """Provenance stamp for rows written by a connector (plan/31).

    live/sandbox adapter -> 'live'; mock adapter -> 'mock' EXCEPT for an
    'import' site: its sync never writes mock-derived rows (the sync skips
    connector fetches for import sites), so the stamp would never fire in
    practice — but if a standalone helper does write, it must never claim
    'mock' for an import tenant.
    """
    if adapter_amode == "live":
        return "live"
    if site_mode == "import":
        return "import"
    return "mock"


def _resolve_site(conn, site, site_id):
    """Strictly mandatory site resolution (plan/31).

    Accepts a site_config-shaped dict (with site_id) or a bare site_id;
    loads the row from the DB. The old 'first configured site'/'mock site'
    fallbacks are removed: a missing site raises SiteRequiredError.
    """
    if site:
        if not site.get("site_id"):
            raise SiteRequiredError(
                "run_sync: site dict must carry site_id (plan/31: no "
                "positional/default-site fallback)")
        site_id = site["site_id"]
    if not site_id:
        raise SiteRequiredError(
            "run_sync: site_id is required (plan/31: no default-site fallback)")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT site_id, site_name, domain, gsc_property, shopify_domain, "
            "ga4_property_id, catalogue_size_tier, min_in_stock_products "
            "FROM site_config WHERE site_id = %s",
            (site_id,),
        )
        row = cur.fetchone()
    if not row:
        raise SiteRequiredError(f"site_config row not found for site_id {site_id}")
    site = dict(zip(("site_id", "site_name", "domain", "gsc_property",
                     "shopify_domain", "ga4_property_id", "catalogue_size_tier",
                     "min_in_stock_products"), row))
    if site.get("min_in_stock_products") is None:
        site["min_in_stock_products"] = 1
    return site, site_id


def _site_target_keywords(conn, site_id):
    """Per-site DataForSEO targets (site_config.target_keywords TEXT[]).

    Returns [] when the column doesn't exist yet (legacy deployment) or the
    list is empty — the keyword/SERP steps are skipped cleanly, never fed
    with hardcoded fixtures. Keywordless sites legitimately skip.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {TARGET_KEYWORDS_COLUMN} FROM site_config WHERE site_id = %s",
                (site_id,),
            )
            row = cur.fetchone()
    except Exception:
        conn.rollback()
        return []
    if not row or not row[0]:
        return []
    return [str(k).strip() for k in row[0] if str(k).strip()]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site_id", required=True,
                        help="site_config.site_id (plan/31: mandatory, no default site)")
    args = parser.parse_args()
    counts = run_sync(site_id=args.site_id)
    print(json.dumps(counts, indent=2))
    return counts


if __name__ == "__main__":
    main()
"""Baseline snapshots (plan/08 §8 BASELINE): target + control group + optional YoY.

Stored at implementation time into measurement_snapshots:
  - target: the page that was changed, previous window of GSC + GA4 data
  - control: comparable unaffected pages (same site, same period, same
    page_type/template) — confound catcher (Priority 3.5)
  - yoy: same period last year, OPTIONAL bonus signal (Priority 3.6); for
    new/low-traffic sites it is simply absent — clean fallback, never an error

For create_page recommendations, baseline lives on the cluster (no target
page exists yet).  The 'before' signal is the cluster-level GSC footprint:
all queries in cluster_queries for this cluster, aggregated across whatever
pages earned impressions for them.  A zero footprint is an honest signal —
the site had no presence for this cluster's demand — not a missing value.
Control group: pages sharing the cluster's recommended_page_type, same
period (same role as page_type control for URL-based recommendations).
"""

import json
import logging
from datetime import date, timedelta

import db as database  # noqa: E402

logger = logging.getLogger("measurement.baseline")

# Control-group snapshot cap (how many comparable pages may feed the aggregate).
CONTROL_GROUP_LIMIT = 20

# Paired control URLs per recommendation (Comparable Unaffected Pages):
# 1 to 2 peer pages. More than 2 dilutes comparability without adding
# statistical power at MVP sample sizes.
CONTROL_URL_COUNT = 2

# A peer page must have earned at least this many impressions over the
# baseline period to count as a usable control (meaningful history, not a
# dead URL whose zero-delta would fake a clean DiD read).
CONTROL_MIN_IMPRESSIONS = 50


def previous_year_same_day(d):
    """Same calendar month/day one year earlier (fix 3.1, leap-safe).

    Feb 29 → Feb 28 on non-leap years; otherwise identical month/day.
    """
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # Feb 29 in a non-leap target year
        return d.replace(year=d.year - 1, day=28)


def control_group_key(page_type, template):
    """Single definition of the control-group key format (fix 3.2).

    baseline.py and measure.py both build the key via this helper; nothing
    else may construct it inline.
    """
    return f"{page_type}|{template}"

WINDOW_LOOKUP_SQL = (
    "SELECT measurement_window_days FROM measurement_window_lookup WHERE action_type = %s"
)


def resolve_window_days(conn, action_type):
    """Window is resolved per action_type from measurement_window_lookup —
    never a hardcoded constant (plan/08 §8 / plan/00 measurement_window_lookup)."""
    with conn.cursor() as cur:
        cur.execute(WINDOW_LOOKUP_SQL, (action_type,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"no measurement window configured for action_type '{action_type}'")
    return row[0]


def _aggregate_metrics(conn, site_id, url_hash, start_date, end_date):
    """GSC + GA4 aggregates for one page over one period (None-safe)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(impressions), 0), COALESCE(SUM(clicks), 0),
                   CASE WHEN SUM(impressions) > 0 THEN SUM(clicks)::NUMERIC / SUM(impressions) END
            FROM search_performance
            WHERE site_id = %s AND page_url_hash = %s AND date BETWEEN %s AND %s
            """,
            (site_id, url_hash, start_date, end_date),
        )
        impressions, clicks, ctr = cur.fetchone()
        cur.execute(
            """
            SELECT COALESCE(SUM(organic_sessions), 0), COALESCE(SUM(orders), 0),
                   COALESCE(SUM(revenue), 0)
            FROM page_business_performance
            WHERE site_id = %s AND page_url_hash = %s AND date BETWEEN %s AND %s
            """,
            (site_id, url_hash, start_date, end_date),
        )
        sessions, orders, revenue = cur.fetchone()
        cur.execute(
            "SELECT AVG(position) FROM search_performance "
            "WHERE site_id = %s AND page_url_hash = %s AND date BETWEEN %s AND %s "
            "AND clicks > 0",
            (site_id, url_hash, start_date, end_date),
        )
        avg_position = cur.fetchone()[0]
    return {
        "impressions": int(impressions or 0),
        "clicks": int(clicks or 0),
        "ctr": float(ctr) if ctr is not None else None,
        "avg_position": float(avg_position) if avg_position is not None else None,
        "organic_sessions": int(sessions or 0),
        "orders": int(orders or 0),
        "revenue": float(revenue or 0),
    }


def _store_snapshot(conn, recommendation_id, snapshot_type, comparison_type,
                    control_group_key, period_start, period_end, metrics,
                    control_group_json=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO measurement_snapshots
                (recommendation_id, snapshot_type, comparison_type, control_group_key,
                 control_group_json, period_start, period_end, impressions, avg_position,
                 ctr, clicks, organic_sessions, orders, revenue)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (recommendation_id, snapshot_type, comparison_type, control_group_key,
             json.dumps(control_group_json) if control_group_json is not None else None,
             period_start, period_end, metrics["impressions"], metrics["avg_position"],
             metrics["ctr"], metrics["clicks"], metrics["organic_sessions"],
             metrics["orders"], metrics["revenue"]),
        )


def _control_candidates(conn, site_id, ptype, template, url_hash,
                        start_date, end_date):
    """Peer URLs for the control group (Comparable Unaffected Pages).

    Same site, same page_type/template as the target, meaningful historical
    impressions in the period, and NO active changes: the peer is not the
    target of any non-rejected recommendation, has no change_log entries,
    and is not itself in an active (approved/in_progress/live/measured)
    measurement workflow. Ordered by impressions (most comparable first).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.url, p.url_hash,
                   COALESCE(SUM(sp.impressions), 0) AS impressions
            FROM pages p
            LEFT JOIN search_performance sp
              ON sp.site_id = p.site_id AND sp.page_url_hash = p.url_hash
             AND sp.date BETWEEN %s AND %s
            WHERE p.site_id = %s AND p.page_type = %s
              AND COALESCE(p.template, '') = COALESCE(%s, '')
              AND p.url_hash <> %s
              AND p.url_hash IN (
                  SELECT page_url_hash FROM search_performance
                  WHERE site_id = %s AND date BETWEEN %s AND %s)
              AND NOT EXISTS (
                  SELECT 1 FROM recommendations r
                  WHERE r.target_url = p.url
                    AND r.status IN ('approved', 'in_progress', 'live', 'measured'))
              AND NOT EXISTS (
                  SELECT 1 FROM change_log cl WHERE cl.url = p.url)
            GROUP BY p.url, p.url_hash
            HAVING COALESCE(SUM(sp.impressions), 0) >= %s
            ORDER BY impressions DESC
            LIMIT %s
            """,
            (start_date, end_date, site_id, ptype, template, url_hash,
             site_id, start_date, end_date, CONTROL_MIN_IMPRESSIONS,
             CONTROL_URL_COUNT),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _control_url_metrics(conn, site_id, control_urls, start_date, end_date):
    """Aggregate metrics for the paired control URLs over one period.

    Returns (agg_metrics, per_url_list). per_url preserves each peer's own
    numbers for traceability; agg feeds the snapshot row. None when a peer
    lost all data (it will be dropped from pairing rather than diluted).
    """
    per_url = []
    agg = {"impressions": 0, "clicks": 0, "organic_sessions": 0,
           "orders": 0, "revenue": 0.0}
    for url, url_hash in control_urls:
        m = _aggregate_metrics(conn, site_id, url_hash, start_date, end_date)
        if m["impressions"] <= 0:
            continue  # peer went dataless — drop, keep the group clean
        per_url.append({"url": url, "url_hash": url_hash, "metrics": m})
        for k in agg:
            agg[k] += m[k]
    agg["ctr"] = agg["clicks"] / agg["impressions"] if agg["impressions"] else None
    agg["avg_position"] = None
    return agg, per_url


NIL_METRICS = {
    "impressions": 0, "clicks": 0, "ctr": None, "avg_position": None,
    "organic_sessions": 0, "orders": 0, "revenue": 0.0,
}


def store_baseline(conn, recommendation_id, implemented_at):
    """Compute and store baseline snapshots for one implemented recommendation.

    Returns dict of stored periods for traceability.

    Degrades gracefully when the target page is missing from `pages` (or its
    page_type/template row is unusable): the target baseline is stored with
    zeroed metrics (missing GSC/GA4 history defaults to 0, not a crash) and
    the control/YoY arms are skipped. Implementation itself must never 500
    because the crawler never saw the URL.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT site_id, action_type, target_url FROM recommendations "
            "WHERE recommendation_id = %s",
            (recommendation_id,),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError(f"unknown recommendation {recommendation_id}")
    site_id, action_type, target_url = row
    if not target_url:
        raise ValueError(f"recommendation {recommendation_id} has no target_url "
                         "(baseline requires an existing page)")
    window_days = resolve_window_days(conn, action_type)

    baseline_end = implemented_at
    baseline_start = baseline_end - timedelta(days=window_days)

    # Missing pages row: store an explicit zeroed baseline instead of raising.
    # degraded=True tells the caller/UI the numbers are placeholder zeros.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT url_hash, page_type, template FROM pages "
            "WHERE site_id = %s AND url = %s",
            (site_id, target_url),
        )
        page_row = cur.fetchone()
    if not page_row:
        logger.warning(
            "baseline degraded: target page %s for recommendation %s is not in "
            "pages table (no crawl/GSC history) — storing zeroed baseline",
            target_url, recommendation_id,
        )
        _store_snapshot(conn, recommendation_id, "baseline", "target", None,
                        baseline_start, baseline_end, dict(NIL_METRICS))
        return {
            "window_days": window_days,
            "baseline_period": [str(baseline_start), str(baseline_end)],
            "target_missing": True,
            "degraded": "target_url not in pages table — baseline zeroed, "
                        "control/YoY skipped",
            "control_group_size": 0,
            "control_urls": [],
            "control_group_key": None,
            "yoy_available": False,
        }
    url_hash, ptype, template = page_row

    metrics = _aggregate_metrics(conn, site_id, url_hash, baseline_start, baseline_end)
    _store_snapshot(conn, recommendation_id, "baseline", "target", None,
                    baseline_start, baseline_end, metrics)

    # Paired control URLs: same page_type/template peers with meaningful
    # history and no active changes (Comparable Unaffected Pages). URLs +
    # per-URL metrics are stored in control_group_json so the post-window
    # capture measures the IDENTICAL set — fair difference-in-differences.
    # Degraded page rows (NULL page_type/template) fall back to '' matching
    # instead of a NoneType crash.
    control_key = control_group_key(ptype or "", template or "")
    control_urls = _control_candidates(conn, site_id, ptype or "", template,
                                       url_hash, baseline_start, baseline_end)
    control_metrics = None
    control_group_json = None
    if control_urls:
        agg, per_url = _control_url_metrics(conn, site_id, control_urls,
                                            baseline_start, baseline_end)
        if per_url:
            control_metrics = agg
            control_group_json = {
                "urls": [{"url": u["url"], "url_hash": u["url_hash"],
                          "metrics": u["metrics"]} for u in per_url],
                "period": [str(baseline_start), str(baseline_end)],
            }
            _store_snapshot(conn, recommendation_id, "baseline", "control", control_key,
                            baseline_start, baseline_end, control_metrics,
                            control_group_json=control_group_json)

    # YoY (optional bonus signal): same period last year; absent cleanly when
    # there is no data (new/low-traffic sites) — never an error. Leap-safe
    # previous-year mapping (fix 3.1).
    yoy_start = previous_year_same_day(baseline_start)
    yoy_end = previous_year_same_day(baseline_end)
    yoy_metrics = _aggregate_metrics(conn, site_id, url_hash, yoy_start, yoy_end)
    if yoy_metrics["impressions"] > 0 or yoy_metrics["organic_sessions"] > 0:
        _store_snapshot(conn, recommendation_id, "baseline", "yoy", None,
                        yoy_start, yoy_end, yoy_metrics)

    return {
        "window_days": window_days,
        "baseline_period": [str(baseline_start), str(baseline_end)],
        "control_group_size": len(control_urls),
        "control_urls": [u["url"] for u in (control_group_json or {}).get("urls", [])],
        "control_group_key": control_key if control_urls else None,
        "yoy_available": bool(yoy_metrics["impressions"] > 0 or yoy_metrics["organic_sessions"] > 0),
    }


def load_baseline(conn, recommendation_id):
    """Return the stored baseline snapshot rows as dicts (or None if absent)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot_type, comparison_type, control_group_key, "
            "control_group_json, period_start, period_end, impressions, avg_position, "
            "ctr, clicks, organic_sessions, orders, revenue "
            "FROM measurement_snapshots "
            "WHERE recommendation_id = %s AND snapshot_type = 'baseline' "
            "ORDER BY comparison_type",
            (recommendation_id,),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


# ────────────────────────────────────────────────────────────────────
# create_page cluster-level measurement (the missing path)
# ────────────────────────────────────────────────────────────────────

NIL_CLUSTER_ID = "00000000-0000-0000-0000-000000000000"


def _aggregate_cluster_metrics(conn, site_id, cluster_id, start_date, end_date):
    """GSC impressions/clicks for all queries in a cluster, all pages.

    This is the site's organic footprint for the cluster's demand — the
    only 'before' signal that exists for a page that hasn't been created
    yet.  GA4 sessions/orders/revenue are not available at query level
    and are set to 0; classification uses GSC clicks as the primary metric
    for create_page recommendations.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(sp.impressions), 0),
                   COALESCE(SUM(sp.clicks), 0),
                   CASE WHEN SUM(sp.impressions) > 0
                        THEN SUM(sp.clicks)::NUMERIC / SUM(sp.impressions) END,
                   AVG(CASE WHEN sp.clicks > 0 THEN sp.position END)
            FROM search_performance sp
            JOIN cluster_queries cq ON cq.query = sp.query
            WHERE cq.cluster_id = %s
              AND sp.site_id = %s
              AND sp.date BETWEEN %s AND %s
            """,
            (cluster_id, site_id, start_date, end_date),
        )
        impressions, clicks, ctr, avg_position = cur.fetchone()
    return {
        "impressions": int(impressions or 0),
        "clicks": int(clicks or 0),
        "ctr": float(ctr) if ctr is not None else None,
        "avg_position": float(avg_position) if avg_position is not None else None,
        "organic_sessions": 0,
        "orders": 0,
        "revenue": 0.0,
    }


def _cluster_control_metrics(conn, site_id, cluster_id, start_date, end_date):
    """Control group: same page_type as the proposed page, excluding cluster queries.

    This catches site-wide/seasonal movement.  The cluster's queries are
    excluded from the control aggregate so the new page's cluster capture
    doesn't leak into the control signal.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT recommended_page_type, primary_keyword FROM keyword_clusters "
            "WHERE cluster_id = %s",
            (cluster_id,),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    rec_page_type = row[0]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(sp.clicks), 0), COALESCE(SUM(sp.impressions), 0)
            FROM search_performance sp
            JOIN pages p ON p.url_hash = sp.page_url_hash AND p.site_id = sp.site_id
            WHERE sp.site_id = %s
              AND p.page_type = %s
              AND sp.date BETWEEN %s AND %s
              AND sp.query NOT IN (
                  SELECT cq.query FROM cluster_queries cq
                  WHERE cq.cluster_id = %s
              )
              AND p.url <> (SELECT COALESCE(r.proposed_url, '') FROM recommendations r
                            WHERE r.cluster_id = %s LIMIT 1)
            """,
            (site_id, rec_page_type, start_date, end_date, cluster_id, cluster_id),
        )
        clicks, impressions = cur.fetchone()
    if not impressions:
        return None
    return {
        "impressions": int(impressions),
        "clicks": int(clicks),
        "ctr": float(clicks) / float(impressions) if impressions else None,
        "avg_position": None,
        "organic_sessions": 0,
        "orders": 0,
        "revenue": 0.0,
    }


def store_cluster_baseline(conn, recommendation_id, implemented_at):
    """Compute and store baseline snapshots for a create_page recommendation.

    The 'before' signal is the cluster-level GSC footprint (all queries
    in cluster_queries, across all pages) — the site's existing organic
    presence for the cluster's demand.  A zero footprint is honest
    (no page competed for this cluster before) and is handled by the
    zero-baseline growth rule in classify.py.

    Returns dict of stored periods for traceability.
    """
    from measurement.baseline import control_group_key as _cgk

    NIL = NIL_CLUSTER_ID

    with conn.cursor() as cur:
        cur.execute(
            "SELECT site_id, action_type, cluster_id FROM recommendations "
            "WHERE recommendation_id = %s",
            (recommendation_id,),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError(f"unknown recommendation {recommendation_id}")
    site_id, action_type, cluster_id = row
    if not cluster_id or str(cluster_id) == NIL:
        raise ValueError(
            f"recommendation {recommendation_id} has no cluster_id "
            "(create_page measurement requires a keyword cluster)"
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM cluster_queries WHERE cluster_id = %s",
            (cluster_id,),
        )
        query_count = cur.fetchone()[0]
    if query_count == 0:
        raise ValueError(
            f"cluster {cluster_id} has no cluster_queries — cannot measure"
        )

    window_days = resolve_window_days(conn, action_type)
    baseline_end = implemented_at
    baseline_start = baseline_end - timedelta(days=window_days)

    # Target snapshot: cluster-level GSC footprint.
    metrics = _aggregate_cluster_metrics(conn, site_id, cluster_id,
                                         baseline_start, baseline_end)
    cluster_key = _cgk("cluster", str(cluster_id))
    _store_snapshot(conn, recommendation_id, "baseline", "target",
                    cluster_key, baseline_start, baseline_end, metrics)

    # Control group: pages sharing the proposed page_type, same period.
    control_metrics = _cluster_control_metrics(conn, site_id, cluster_id,
                                               baseline_start, baseline_end)
    control_key = None
    control_size = 0
    if control_metrics and control_metrics["impressions"] > 0:
        rec_page_type = None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT recommended_page_type FROM keyword_clusters WHERE cluster_id = %s",
                (cluster_id,),
            )
            r = cur.fetchone()
            if r:
                rec_page_type = r[0]
        control_key = _cgk(rec_page_type or "unknown", "cluster")
        _store_snapshot(conn, recommendation_id, "baseline", "control",
                        control_key, baseline_start, baseline_end, control_metrics)
        control_size = 1

    # YoY: same period last year cluster footprint (optional).
    yoy_start = previous_year_same_day(baseline_start)
    yoy_end = previous_year_same_day(baseline_end)
    yoy_metrics = _aggregate_cluster_metrics(conn, site_id, cluster_id,
                                             yoy_start, yoy_end)
    yoy_available = yoy_metrics["impressions"] > 0
    if yoy_available:
        _store_snapshot(conn, recommendation_id, "baseline", "yoy",
                        cluster_key, yoy_start, yoy_end, yoy_metrics)

    return {
        "window_days": window_days,
        "baseline_period": [str(baseline_start), str(baseline_end)],
        "cluster_id": str(cluster_id),
        "cluster_query_count": query_count,
        "control_group_size": control_size,
        "control_group_key": control_key,
        "yoy_available": yoy_available,
    }
"""Off-page discovery (plan/35) - automated opportunity sourcing.

What it does: for every (site_id, platform) leg that an operator ENABLED in
offpage_platform_settings, it builds `site:<host> <target_keyword>` queries
from site_config.target_keywords and feeds them through the tenant's chosen
discovery seams. Each hit becomes an offpage_community + offpage_thread row
(minus any existing (site_id, thread_url)) and a raw `offpage_community`
recommendation that the drafter can pick up.

What it does NOT do:
  * Nothing here enables anything. Communities are minted with
    enabled=false, rules unread (link_policy='unknown'), promotion_allowed
    NULL - the same fail-closed posture as plan/33 SERP discovery. The
    drafter must pass its rule-read + context-sufficiency gates before any
    draft exists, and posting stays a manual operator act.
  * No platform write API and no authenticated scrape (zero identity
    fingerprinting, plan/35 @header). DataForSEO is the existing never-raise
    SERP contract; web_search is the NEW rate-limited seam that ships
    DISABLED (fail-closed): there is no default endpoint, so unless an
    operator wires OFFPAGE_WEB_SEARCH_ENDPOINT (or injects a provider in
    tests) the seam reports skipped and NO search_log row is written.
  * No budget override: the DataForSEO leg still asserts
    connectors.costlog.check_budget(service='openseo_serp') before every
    call - the same gate sync and audit use (plan/23 non-negotiable #5).

Rate limiting (the seam brake, plan/35 @5):
  * Daily ceiling (offpage_platform_settings.discovery_calls_per_day)
    counted in offpage_search_log for today's rows per (site, platform,
    seam).
  * Hourly ceiling via offpage_search_quota's atomic upsert (the
    INSERT..ON CONFLICT..DO UPDATE..WHERE..RETURNING claim pattern from the
    migration - the WHERE re-check runs under the row lock, so concurrent
    legs cannot exceed max_calls; when now() crosses a minute boundary a
    fresh window_start becomes a new row and the counter resets).

Seam contract (injectable for tests):
    seams: {name: fn(site, platform, query, limit) -> envelope}
    envelope: {"ok": bool, "provider": str, "data_source": "live"|"mock",
               "results": [{"url","title","snippet","position"?, "post_kind"?}],
               "http_status": int|None, "latency_ms": int|None,
               "error": str|None}
    OR {"skipped": True, "reason": str} -> nothing is logged or claimed.

Task is idempotent (ON CONFLICT DO NOTHING on every mint) and re-runnable;
it never raises (run() catches per-leg and per-seam failures into the
summary).
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs.locks import job_lock, LOCK_KEYS, already_running

# platform -> domain suffix(es) a discovered URL must carry for this leg.
# The shopify_community pin is absolute; x accepts both x.com and
# twitter.com; medium posts only live under medium.com (not the www top).
HOST_RULES = {
    "reddit": ("reddit.com",),
    "quora": ("quora.com",),
    "medium": ("medium.com",),
    "x": ("x.com", "twitter.com"),
    "shopify_community": ("community.shopify.com",),
}

# Seam metadata: the search_log.provider label per seam.
SEAM_PROVIDERS = {
    "dataforseo": "dataforseo_serp",
    "web_search": "web_search_default",
}

DEFAULT_LIMIT = 20
DEFAULT_HOURLY_CAP = 10
HOURLY_WINDOW_SECONDS = 3600

WEB_SEARCH_ENDPOINT_ENV = "OFFPAGE_WEB_SEARCH_ENDPOINT"
WEB_SEARCH_TOKEN_ENV = "OFFPAGE_WEB_SEARCH_TOKEN"
WEB_SEARCH_TIMEOUT_ENV = "OFFPAGE_WEB_SEARCH_TIMEOUT"


def _utcnow():
    return datetime.now(timezone.utc)


def _host_ok(url, suffixes):
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return any(suffix in host for suffix in suffixes)


def _community_key(url):
    """Deterministic community key from a URL (netloc + first path node).

    'community.shopify.com/c/advertising/...' -> 'community.shopify.com/c/advertising'
    'x.com/SomeVendor/status/...'             -> 'x.com/SomeVendor'
    'medium.com/@acme-publication/...'        -> 'medium.com/@acme-publication'
    Keeps one community row per distinct landing area without asking the
    seam for anything beyond the URL.
    """
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        segs = [s for s in parsed.path.split("/") if s]
        path = "/" + segs[0] if segs else ""
        if len(segs) > 1 and host == "community.shopify.com":
            path = "/" + "/".join(segs[:2]) + "/"
        return f"{host}{path}".rstrip("/")
    except ValueError:
        return "unknown"


def _build_query(keyword, platform):
    host = HOST_RULES[platform][0]
    return f"site:{host} {keyword}"


def _context_source_for(seam):
    return {"dataforseo": "serp", "web_search": "web_search"}.get(seam, "serp")


# ------------------------------------------------------------
# Seam implementations
# ------------------------------------------------------------

def _dataforseo_seam(site, platform, query, limit):
    """One DataForSEO SERP read behind the never-raise adapter + costlog
    budget gate. Envelope per the seam contract."""
    from connectors.openseo import get_openseo_adapter
    from connectors import costlog
    try:
        openseo = get_openseo_adapter()
    except Exception as exc:
        return {"ok": False, "provider": SEAM_PROVIDERS["dataforseo"],
                "data_source": "live", "results": [],
                "http_status": None, "latency_ms": None,
                "error": f"adapter_config: {type(exc).__name__}: {exc}"}
    # check_budget(conn, service) takes the caller's connection; the job's
    # per-leg conn is not threaded through the seam contract, so the gate
    # reads the budget cap on its own (read-only) connection.
    import db as _dbmod
    try:
        budget_conn = _dbmod.get_connection()
        try:
            allowed, reason = costlog.check_budget(
                budget_conn, service="openseo_serp")
        finally:
            budget_conn.close()
    except Exception as exc:
        return {"ok": False, "provider": SEAM_PROVIDERS["dataforseo"],
                "data_source": "live", "results": [],
                "http_status": None, "latency_ms": None,
                "error": f"budget_gate_db: {type(exc).__name__}: {exc}"}
    if not allowed:
        return {"ok": False, "provider": SEAM_PROVIDERS["dataforseo"],
                "data_source": "live", "results": [],
                "http_status": None, "latency_ms": None,
                "error": f"budget_gate: {reason}"}
    started = time.monotonic()
    try:
        result = openseo.fetch("serp", {"query": query, "limit": limit})
    except Exception as exc:
        return {"ok": False, "provider": SEAM_PROVIDERS["dataforseo"],
                "data_source": "live", "results": [],
                "http_status": None,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": f"fetch: {type(exc).__name__}: {exc}"}
    latency_ms = int((time.monotonic() - started) * 1000)
    if not result.get("ok"):
        return {"ok": False, "provider": SEAM_PROVIDERS["dataforseo"],
                "data_source": "live", "results": [],
                "http_status": result.get("status"),
                "latency_ms": latency_ms,
                "error": result.get("error") or result.get("detail") or "provider_error"}
    rows = []
    for row in (result.get("data") or []):
        if isinstance(row, dict) and row.get("url"):
            rows.append({
                "url": row["url"], "title": row.get("title"),
                "snippet": row.get("snippet"),
                "position": row.get("position"),
                "post_kind": row.get("post_kind") or "reply",
            })
    return {"ok": True, "provider": SEAM_PROVIDERS["dataforseo"],
            "data_source": "live", "results": rows,
            "http_status": 200, "latency_ms": latency_ms, "error": None}


def _web_search_seam(site, platform, query, limit):
    """The rate-limited web-search seam (plan/35 §5) - fails closed.

    No endpoint configured => skipped, nothing logged, nothing claimed.
    When OFFPAGE_WEB_SEARCH_ENDPOINT IS set, POSTs the query and reads a
    {results:[{url,title,snippet}]} envelope (plain list accepted too).
    The endpoint is an internal proxy - the seam never talks to a platform
    API, so identity fingerprinting still does not exist.
    """
    import httpx

    endpoint = os.environ.get(WEB_SEARCH_ENDPOINT_ENV)
    if not endpoint:
        return {"skipped": True,
                "reason": f"{WEB_SEARCH_ENDPOINT_ENV} unset - web_search not configured"}
    timeout = float(os.environ.get(WEB_SEARCH_TIMEOUT_ENV) or "15")
    started = time.monotonic()
    headers = {"content-type": "application/json"}
    token = os.environ.get(WEB_SEARCH_TOKEN_ENV)
    if token:
        headers["authorization"] = f"Bearer {token}"
    payload = {"query": query, "platform": platform,
               "seam": "web_search", "limit": limit}
    try:
        resp = httpx.post(endpoint, json=payload, headers=headers,
                          timeout=timeout)
        latency_ms = int((time.monotonic() - started) * 1000)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        status = None
        resp = getattr(exc, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)
        return {"ok": False, "provider": SEAM_PROVIDERS["web_search"],
                "data_source": "live", "results": [],
                "http_status": status,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": f"fetch: {type(exc).__name__}: {exc}"}
    if isinstance(data, dict):
        rows = data.get("results") or data.get("data") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    results = []
    for row in rows:
        if isinstance(row, dict) and row.get("url"):
            results.append({
                "url": row["url"], "title": row.get("title"),
                "snippet": row.get("snippet"),
                "position": row.get("position"),
                "post_kind": row.get("post_kind") or "reply",
            })
    return {"ok": True, "provider": SEAM_PROVIDERS["web_search"],
            "data_source": "live", "results": results,
            "http_status": resp.status_code, "latency_ms": latency_ms,
            "error": None}


_dataforseo_seam.available = lambda site, platform: True


def _web_search_seam_available(site, platform):
    """The web_search seam is only AVAILABLE when an endpoint is configured.
    This cheap pre-check (no network) runs BEFORE the quota claim, so a
    disabled-seam default never burns a window slot or writes a log row."""
    return bool(os.environ.get(WEB_SEARCH_ENDPOINT_ENV))


_web_search_seam.available = _web_search_seam_available


def _default_seams():
    return {"dataforseo": _dataforseo_seam, "web_search": _web_search_seam}


# ------------------------------------------------------------
# Quota + ledger
# ------------------------------------------------------------

def _today_search_count(conn, site_id, platform, seam, now):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM offpage_search_log
            WHERE site_id = %s AND platform = %s AND seam = %s
              AND created_at >= date_trunc('day', %s::timestamptz)
            """,
            (site_id, platform, seam, now),
        )
        return cur.fetchone()[0]


def _claim_quota_call(conn, site_id, platform, now, max_calls):
    """Atomic hourly-window claim. Returns True when the call is allowed.

    INSERT..ON CONFLICT..DO UPDATE..WHERE..RETURNING: the upsert serialises
    on the (site_id, platform, window_start) row lock, the WHERE re-checks
    calls_made < max_calls against the committed value, and the claim only
    counts when it returned. A new minute = a fresh row = a fresh window.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_search_quota
                (site_id, platform, window_start, window_seconds,
                 calls_made, max_calls)
            VALUES (%s, %s, date_trunc('minute', %s::timestamptz),
                    %s, 1, %s)
            ON CONFLICT (site_id, platform, window_start)
            DO UPDATE SET calls_made = offpage_search_quota.calls_made + 1
            WHERE offpage_search_quota.calls_made <
                  offpage_search_quota.max_calls
            RETURNING calls_made
            """,
            (site_id, platform, now, HOURLY_WINDOW_SECONDS, max_calls),
        )
        return cur.fetchone() is not None


def _log_search(conn, site_id, platform, seam, query, envelope,
                result_count, now):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_search_log
                (site_id, platform, seam, query, result_count, provider,
                 http_status, latency_ms, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (site_id, platform, seam, query, result_count,
             envelope.get("provider"), envelope.get("http_status"),
             envelope.get("latency_ms"), now),
        )


# ------------------------------------------------------------
# Minting: community / thread / recommendation
# ------------------------------------------------------------

def _upsert_community(conn, site_id, platform, community_key, data_source,
                      rules_url, now):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_communities
                (site_id, platform, community_key, enabled, data_source,
                 rules_url, created_at, updated_at)
            VALUES (%s, %s, %s, false, %s, %s, %s, %s)
            ON CONFLICT (site_id, platform, community_key) DO NOTHING
            """
            ,
            (site_id, platform, community_key, data_source, rules_url,
             now, now),
        )


def _upsert_thread(conn, site_id, platform, community_key, url, title,
                   snippet, position, context_source, data_source, query,
                   post_kind, now):
    """Returns the thread_id, or None when the thread already exists."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT thread_id FROM offpage_threads
            WHERE site_id = %s AND thread_url = %s
            """,
            (site_id, url),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE offpage_threads SET last_seen_at = %s "
                "WHERE site_id = %s AND thread_url = %s",
                (now, site_id, url),
            )
            return existing[0]
        cur.execute(
            """
            SELECT community_id FROM offpage_communities
            WHERE site_id = %s AND platform = %s AND community_key = %s
            """,
            (site_id, platform, community_key),
        )
        community = cur.fetchone()
        if not community:
            return None
        cur.execute(
            """
            INSERT INTO offpage_threads
                (site_id, community_id, thread_url, title, snippet,
                 thread_score, context_source, context_chars, context_usable,
                 promotion_allowed, first_seen_at, last_seen_at,
                 data_source, discovery_query, post_kind)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL,
                    %s, %s, %s, %s, %s)
            RETURNING thread_id
            """,
            (site_id, community[0], url, title, snippet, position,
             context_source, len(snippet or ""),
             now, now, data_source, query, post_kind),
        )
        return cur.fetchone()[0]


def _cluster_for_keyword(conn, site_id, keyword):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cluster_id FROM keyword_clusters
            WHERE site_id = %s AND primary_keyword = %s
            LIMIT 1
            """,
            (site_id, keyword),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _ensure_cluster(conn, site_id, platform, keyword):
    """Get-or-create the demand cluster for a discovered keyword.

    A discovered thread's recommendation needs a cluster: the grounding
    loader refuses to rank a cluster-less rec, so discovery mints the
    keyword's cluster (and its own keyword row) when the tenant's config
    does not already carry one.
    """
    existing = _cluster_for_keyword(conn, site_id, keyword)
    if existing:
        return existing
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword, keywords,
                                          intent, commercial_intent,
                                          recommended_page_type)
            VALUES (%s, %s, ARRAY[%s], 'commercial', true, 'landing')
            RETURNING cluster_id
            """,
            (site_id, keyword, keyword))
        cluster_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO cluster_queries (cluster_id, query) "
            "SELECT %s, %s WHERE NOT EXISTS ("
            "SELECT 1 FROM cluster_queries WHERE cluster_id = %s AND query = %s)",
            (cluster_id, keyword, cluster_id, keyword))
    return cluster_id


_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def _insert_recommendation(conn, site_id, thread_id, thread_url, cluster_id,
                           keyword, platform):
    """One raw offpage_community recommendation guarded by the natural-key
    unique index (uq_recommendations_candidate), so re-runs are no-ops.
    target_url is the thread permalink (plan/33: the rec's identity), and
    offpage_thread_id carries the row link for the drafter."""
    diagnosis = (f"Off-page opportunity: {platform} thread surfaced "
                 f"for target keyword '{keyword}'")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, status, diagnosis,
                 cluster_id, target_url, proposed_url, offpage_thread_id)
            SELECT %s, 'offpage_community', 'offpage_reply', 'raw',
                   %s, %s, %s, '', %s
            WHERE NOT EXISTS (
                SELECT 1 FROM recommendations
                WHERE site_id = %s
                  AND generator = 'offpage_community'
                  AND action_type = 'offpage_reply'
                  AND COALESCE(cluster_id,
                      '00000000-0000-0000-0000-000000000000'::uuid) =
                      COALESCE(%s, '00000000-0000-0000-0000-000000000000'::uuid)
                  AND COALESCE(target_url, '') = %s
                  AND COALESCE(proposed_url, '') = ''
            )
            RETURNING recommendation_id
            """,
            (site_id, diagnosis, cluster_id, thread_url, thread_id,
             site_id, cluster_id, thread_url),
        )
        return cur.fetchone()[0] if cur.rowcount else None


# ------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------

def run(now=None, conn=None, seams=None, site_filter=None):
    """Discover conversations/topics for every enabled platform leg.

    Returns a summary dict; never raises. `seams` injects providers for
    tests (default: DataForSEO + the fail-closed web-search seam).
    `site_filter` scopes the run to ONE tenant's legs (operator-triggered
    runs pass it; the scheduler omits it for the full sweep).
    """
    own_conn = conn is None
    if own_conn:
        import env as env_loader
        import db as database
        env_loader.load_env_file(quiet=True)
        conn = database.get_connection()
    now = now or _utcnow()
    providers = seams if seams is not None else _default_seams()

    summary = {
        "status": "ok",
        "legs_evaluated": 0,
        "legs_with_calls": 0,
        "queries_run": 0,
        "queries_skipped": 0,
        "threads_created": 0,
        "communities_created": 0,
        "recommendations_created": 0,
        "seams_missing": [],
        "errors": [],
    }
    created_communities = 0

    try:
        with job_lock(conn, LOCK_KEYS["offpage_discovery"]):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ps.site_id, ps.platform, ps.discovery_seams,
                           ps.discovery_calls_per_day, ps.targeting,
                           sc.target_keywords
                    FROM offpage_platform_settings ps
                    JOIN site_config sc ON sc.site_id = ps.site_id
                    WHERE ps.enabled
                      AND ps.discovery_seams IS NOT NULL
                      AND cardinality(ps.discovery_seams) > 0
                      AND (%s::uuid IS NULL OR ps.site_id = %s::uuid)
                    ORDER BY ps.site_id, ps.platform
                    """,
                    (site_filter, site_filter),
                )
                legs = cur.fetchall()
            for leg in legs:
                site_id, platform, seams_col, daily_cap, targeting, \
                    sc_keywords = leg
                # Operator scoping: the settings panel's Targeting JSON
                # carries per-platform discovery keywords; the tenant-level
                # target_keywords remain the fallback (site-wide list).
                if isinstance(targeting, str):
                    try:
                        targeting = json.loads(targeting)
                    except (TypeError, ValueError):
                        targeting = None
                keywords = (targeting or {}).get("keywords") or sc_keywords
                leg_calls = 0
                for seam in seams_col:
                    provider = providers.get(seam)
                    if provider is None:
                        if seam not in summary["seams_missing"]:
                            summary["seams_missing"].append(seam)
                        continue
                    hourly_cap = min(int(daily_cap or DEFAULT_HOURLY_CAP),
                                     DEFAULT_HOURLY_CAP)
                    avail = getattr(provider, "available", None)
                    seam_available = (avail(site_id, platform)
                                      if avail is not None else True)
                    for keyword in keywords or []:
                        if int(daily_cap or 0) > 0 and \
                                leg_calls >= int(daily_cap or 0):
                            summary["queries_skipped"] += 1
                            continue
                        # 0) availability pre-check - a seam with no real
                        #    surface (e.g. web_search without an endpoint)
                        #    never claims quota or writes a ledger row.
                        if not seam_available:
                            summary["queries_skipped"] += 1
                            continue
                        # 1) hourly brake - if the window is full, the whole
                        #    seam gives up for now.
                        if not _claim_quota_call(
                                conn, site_id, platform, now, hourly_cap):
                            break
                        query = _build_query(keyword, platform)
                        try:
                            envelope = provider(site_id, platform, query,
                                                DEFAULT_LIMIT)
                        except Exception as exc:
                            summary["errors"].append(
                                f"seam {seam} raised for {platform}: "
                                f"{type(exc).__name__}: {exc}")
                            continue
                        if envelope.get("skipped"):
                            summary["queries_skipped"] += 1
                            continue
                        leg_calls += 1
                        result_count = len(envelope.get("results") or [])
                        _log_search(conn, site_id, platform, seam, query,
                                    envelope, result_count, now)
                        summary["queries_run"] += 1
                        if not envelope.get("ok"):
                            # A provider-side failure is a REAL diagnostic:
                            # log it (with the adapter's detail) instead of
                            # letting a zero-result envelope masquerade as
                            # "no conversations found".
                            summary["errors"].append(
                                f"serp {seam} failed for {platform} "
                                f"'{query}': "
                                f"{str(envelope.get('error'))[:160]} "
                                f"{str(envelope.get('detail'))[:160]}")
                        self_host = HOST_RULES[platform]
                        data_source = envelope.get("data_source", "live")
                        for res in envelope.get("results") or []:
                            if not _host_ok(res["url"], self_host):
                                continue
                            ckey = _community_key(res["url"])
                            _upsert_community(conn, site_id, platform,
                                              ckey, data_source,
                                              res["url"], now)
                            thread_id = _upsert_thread(
                                conn, site_id, platform, ckey, res["url"],
                                res.get("title"), res.get("snippet"),
                                res.get("position"),
                                _context_source_for(seam), data_source,
                                query, res.get("post_kind") or "reply", now)
                            if thread_id is None:
                                continue
                            summary["threads_created"] += 1
                            cluster_id = _ensure_cluster(
                                conn, site_id, platform, keyword)
                            rec_id = _insert_recommendation(
                                conn, site_id, thread_id, res["url"],
                                cluster_id, keyword, platform)
                            if rec_id:
                                summary["recommendations_created"] += 1
                summary["legs_evaluated"] += 1
                if leg_calls:
                    summary["legs_with_calls"] += 1
            conn.commit()
    except Exception as exc:
        conn.rollback()
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if own_conn:
            conn.close()
    return summary
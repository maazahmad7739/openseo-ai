"""Orchestration for the on-demand audit engine — Phase B: the SERP step
(plan/23 §1 steps 4–5, §5.1–5.3).

The read-only request path: this module runs keyword inference → cache
check → budget gate → live SERP pull → atomic competitor upsert → cost
log, and stamps the session row.

Guarantees:
  * ZERO external spend without a budget check passing FIRST
    (costlog.check_budget, service openseo_serp; env override
    WEEKLY_BUDGET_OPENSEO).
  * The SERP pull goes ONLY through the existing adapter seam:
    OpenseoRestAdapter.fetch("serp", params) — depth clamped 5..20
    (default 10 = COMPETITOR_POSITION_MAX parity, plan/23 §5.2).
  * Competitor rows land in ONE transaction (audit_serp_competitors
    UNIQUE (session_id, result_url)) — a failure mid-write can never
    leave partial rows (plan/23 §6 Phase B test contract).
  * Post-call log_cost with metadata {"source": "audit_engine",
    "session_id": ...} so on-demand spend is distinguishable from
    weekly-batch spend (plan/23 §5.2).
  * Graceful degradation: zero organic rows -> serp_status 'zero_results'
    (grounding degrades to keyword-only, §4.4); provider error ->
    'serp_error'; budget exhausted -> 'budget_exhausted'. Page analysis
    is unaffected in all three cases.
  * Read/write segregation (plan/23 §5.4): this engine writes ONLY
    audit_sessions / audit_page_snapshots / audit_serp_competitors.
"""

import hashlib
import json

import db as database
import env as env_loader
from connectors.costlog import check_budget, log_cost
from connectors.openseo import get_openseo_adapter
from connectors.url_normalize import canonicalize_url

from audit import keyword_infer
from audit.site_resolution import resolve_site

DEPTH_MIN = 5
DEPTH_MAX = 20
DEPTH_DEFAULT = 10

CACHE_TTL_HOURS = 24

SERP_SERVICE = "openseo_serp"
SERP_CALL_TYPE = "serp_live_audit"

SERP_STATUSES = ("pending", "ok", "zero_results", "serp_error",
                 "budget_exhausted")
FETCH_STATUSES = ("pending", "fetched", "page_unreachable", "blocked_robots")


def _url_hash(url):
    return hashlib.md5((url or "").encode("utf-8")).hexdigest()


def clamp_depth(depth):
    """§5.2: depth clamped to 5..20 (cost scales with depth)."""
    try:
        depth = int(depth)
    except (TypeError, ValueError):
        return DEPTH_DEFAULT
    return max(DEPTH_MIN, min(DEPTH_MAX, depth))


def _host_of(url):
    host = (url or "").split("://", 1)[-1].split("/", 1)[0]
    host = host.split(":", 1)[0].lower()
    return host[4:] if host.startswith("www.") else host


# ------------------------------------------------------------
# Session lifecycle
# ------------------------------------------------------------

def create_session(conn, raw_url, mode, site_id=None, domain=None):
    """Session spine row (plan/23 §2.2). Returns session_id (str)."""
    normalized = canonicalize_url(raw_url) or raw_url
    domain = domain or _host_of(raw_url)
    if mode not in ("audit_checklist", "connected"):
        mode = "audit_checklist"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_sessions (requested_url, normalized_url, domain,
                                        mode, site_id)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING session_id
            """,
            (raw_url, normalized, domain, mode, site_id),
        )
        session_id = str(cur.fetchone()[0])
    conn.commit()
    return session_id


def persist_page_snapshot(conn, session_id, snapshot):
    """Phase A ingest hook: snapshot row + session link (fetch_status
    'fetched'). Kept here so Phase B standalone tests can seed a full
    session without the network."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_page_snapshots (session_id, url, status_code,
                title, meta_description, h1, heading_outline, body_html,
                body_text, render_status, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING snapshot_id
            """,
            (session_id, snapshot.get("url"), snapshot.get("status_code"),
             snapshot.get("title"), snapshot.get("meta_description"),
             snapshot.get("h1"),
             json.dumps(snapshot.get("heading_outline") or []),
             snapshot.get("body_html"), snapshot.get("body_text"),
             snapshot.get("render_status") or "not_rendered",
             snapshot.get("content_hash")),
        )
        snapshot_id = str(cur.fetchone()[0])
        cur.execute(
            """
            UPDATE audit_sessions
            SET fetch_status = 'fetched', page_snapshot_id = %s
            WHERE session_id = %s
            """,
            (snapshot_id, session_id),
        )
    conn.commit()
    return snapshot_id


def _update_session_inference(conn, session_id, query, intent):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE audit_sessions
            SET inferred_query = %s, inferred_intent = %s
            WHERE session_id = %s
            """,
            (query, intent, session_id),
        )
    conn.commit()


def _set_serp_status(conn, session_id, status):
    if status not in SERP_STATUSES:
        raise ValueError(f"invalid serp_status {status!r}")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE audit_sessions SET serp_status = %s WHERE session_id = %s",
            (status, session_id),
        )
    conn.commit()


def _set_fetch_status(conn, session_id, status):
    if status not in FETCH_STATUSES:
        raise ValueError(f"invalid fetch_status {status!r}")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE audit_sessions SET fetch_status = %s WHERE session_id = %s",
            (status, session_id),
        )
    conn.commit()


# ------------------------------------------------------------
# 5.1 Query-result caching
# ------------------------------------------------------------

def find_cached_session(conn, url, query, ttl_hours=CACHE_TTL_HOURS):
    """The §5.1 pre-SERP lookup: an identical (url_hash, inferred_query)
    audit inside the TTL window with a successful SERP.

    url_hash is the GENERATED column (md5 of requested_url), matching the
    schema — the lookup never recomputes hashes SQL-side."""
    if not query:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.session_id,
                   EXTRACT(EPOCH FROM (now() - MIN(p.fetched_at))) / 3600.0
            FROM audit_sessions s
            JOIN audit_page_snapshots p USING (session_id)
            WHERE s.url_hash = %s
              AND s.inferred_query = %s
              AND s.serp_status = 'ok'
              AND p.fetched_at > now() - (%s || ' hours')::interval
            GROUP BY s.session_id
            ORDER BY MIN(p.fetched_at) DESC
            LIMIT 1
            """,
            (_url_hash(url), query, str(ttl_hours)),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def cache_hit_age_hours(conn, session_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXTRACT(EPOCH FROM (now() - MAX(p.fetched_at))) / 3600.0
            FROM audit_sessions s
            JOIN audit_page_snapshots p USING (session_id)
            WHERE s.session_id = %s
            """,
            (session_id,),
        )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


# ------------------------------------------------------------
# SERP fetch + competitor ingest
# ------------------------------------------------------------

def url_pattern(result_url):
    """URL archetype segment (plan/00 url_pattern semantics): the first
    non-empty PATH segment wrapped in slashes; '/' for the bare origin."""
    path = (result_url or "").split("://", 1)[-1]
    # drop the host: everything before the first '/' after the authority
    if "/" in path:
        path = path.split("/", 1)[1]
    else:
        path = ""
    path = path.split("?", 1)[0].split("#", 1)[0]
    parts = [seg for seg in path.split("/") if seg]
    if not parts:
        return "/"
    return f"/{parts[0]}/"


def _ingest_serp_rows(conn, session_id, query, domain, serp_rows,
                      depth_limit=None):
    """Atomic competitor upsert (plan/23 §2.3): URL-dedupe (first
    occurrence wins = best position), depth cap, is_self for the audited
    domain. One transaction; on conflict the existing row is refreshed.
    Returns the inserted row count."""
    audited_domain = (domain or "").lower()
    seen_urls = set()
    rows = []
    for row in serp_rows:
        url = row.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        result_domain = (row.get("domain") or "").lower()
        position = int(row.get("position") or 0)
        if depth_limit is not None and position > depth_limit:
            continue
        rows.append({
            "session_id": session_id,
            "query": query,
            "position": position,
            "result_url": url,
            "result_domain": result_domain,
            "is_self": result_domain == audited_domain,
            "title": row.get("title"),
            "snippet": row.get("snippet"),
            "url_pattern": url_pattern(url),
        })
    if not rows:
        return 0
    # Inline upsert (composite conflict target — db.insert_rows wraps its
    # conflict_target in parens, which breaks multi-column targets; the
    # UNIQUE (session_id, result_url) constraint is the dedupe authority).
    sql = """
        INSERT INTO audit_serp_competitors
            (session_id, query, position, result_url, result_domain,
             is_self, title, snippet, url_pattern)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (session_id, result_url) DO UPDATE SET
            position = EXCLUDED.position,
            title = EXCLUDED.title,
            snippet = EXCLUDED.snippet,
            url_pattern = EXCLUDED.url_pattern,
            is_self = EXCLUDED.is_self
    """
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(sql, (session_id, row["query"], row["position"],
                              row["result_url"], row["result_domain"],
                              row["is_self"], row["title"], row["snippet"],
                              row["url_pattern"]))
    conn.commit()
    return len(rows)


def _fetch_serp(adapter, query, depth, location_code=None, language_code=None):
    """One fetch("serp") call — never raises (adapter contract). Returns
    {"ok", "rows", "error", "detail", "cost"}."""
    params = {"query": query, "limit": depth}
    if location_code:
        params["location_code"] = location_code
    if language_code:
        params["language_code"] = language_code
    result = adapter.fetch("serp", params)
    if not result.get("ok"):
        return {"ok": False, "rows": [],
                "error": result.get("error", "provider_error"),
                "detail": result.get("detail"), "cost": None}
    return {"ok": True, "rows": result.get("data") or [], "error": None,
            "detail": None, "cost": result.get("cost")}


# ------------------------------------------------------------
# The orchestration entry
# ------------------------------------------------------------

def run_audit(conn, raw_url, depth=None, refresh=False, location_code=None,
              language_code=None, adapter=None, cache_ttl_hours=CACHE_TTL_HOURS,
              fetch_page=None):
    """Phase B orchestration (§1 lifecycle order):
      0. resolve site (mode) + create the session spine row
      1. keyword inference (deterministic; page copy when `fetch_page`
         is supplied and succeeds — page fields are optional in Phase B
         standalone mode)
      2. cache pre-check (§5.1) unless refresh=True
      3. budget gate (§5.2) BEFORE the SERP call
      4. fetch("serp") through the adapter seam
      5. atomic competitor upsert + log_cost

    `adapter` and `fetch_page` are TEST seams (mock mode / loopback
    fixtures); live callers pass none. Every failure path is a typed key
    in the result dict — never an exception.
    """
    depth = clamp_depth(depth)
    result = {"url": raw_url, "depth": depth,
              "cache": {"hit": False, "snapshot_age_hours": 0},
              "serp": {}, "cost": {}}

    # ---- 0. resolve ----
    try:
        resolution = resolve_site(conn, raw_url)
    except Exception as exc:
        return dict(result, ok=False, error="resolve_failed", detail=str(exc))
    mode = resolution.get("mode", "audit_checklist")
    domain = resolution.get("domain") or _host_of(raw_url)
    session_id = create_session(conn, raw_url, mode,
                                site_id=resolution.get("site_id"),
                                domain=domain)
    result.update({"ok": True, "session_id": session_id, "mode": mode,
                   "domain": domain})

    # ---- 1. keyword inference ----
    page = fetch_page(conn, raw_url) if fetch_page else None
    if page:
        persist_page_snapshot(conn, session_id, page)
        _set_fetch_status(conn, session_id, "fetched")
    inference = keyword_infer.infer(
        raw_url,
        title=(page or {}).get("title"),
        h1=(page or {}).get("h1"),
        body_text=(page or {}).get("body_text"),
    )
    query = inference.get("primary_keyword")
    intent = inference.get("intent", "unknown")
    _update_session_inference(conn, session_id, query, intent)
    result["inferred_query"] = query
    result["inferred_intent"] = intent
    result["inferred_candidates"] = inference.get("candidates") or []
    if not query:
        # nothing inferrable and no URL handle — nothing to query the
        # SERP with; degrade honestly (zero-SERP behavior, §4.4)
        _set_serp_status(conn, session_id, "zero_results")
        result["serp"] = {"query": None, "results_ingested": 0,
                          "zero_results": True, "error": "no_query"}
        result["cost"]["serp_usd"] = None
        return result

    # ---- 2. cache pre-check ----
    if not refresh:
        cached = find_cached_session(conn, raw_url, query,
                                     ttl_hours=cache_ttl_hours)
        if cached:
            age = cache_hit_age_hours(conn, cached)
            _set_serp_status(conn, session_id, "ok")
            _update_session_inference(conn, session_id, query, intent)
            result["cache"] = {"hit": True, "session_id": cached,
                               "snapshot_age_hours": age}
            result["serp"] = {"query": query, "results_ingested": 0,
                              "zero_results": False, "served_from": cached}
            result["cost"]["serp_usd"] = None  # zero spend on a hit
            return result

    # ---- 3. budget gate (BEFORE the call — plan/23 non-negotiable 5) ----
    budget = check_budget(conn, service=SERP_SERVICE)
    if budget is not None:
        _spend, _cap, is_over, _warn, _thr = budget
        if is_over:
            _set_serp_status(conn, session_id, "budget_exhausted")
            return dict(result, ok=False, error="budget_exhausted",
                        serp_status="budget_exhausted",
                        detail="weekly DataForSEO budget exhausted "
                               "(service openseo_serp)")

    # ---- 4. live SERP pull ----
    if adapter is None:
        adapter = get_openseo_adapter()
    serp = _fetch_serp(adapter, query, depth,
                       location_code=location_code,
                       language_code=language_code)

    if not serp["ok"]:
        _set_serp_status(conn, session_id, "serp_error")
        return dict(result, ok=False, error="serp_error",
                    detail=serp.get("error"), serp_status="serp_error",
                    serp={"query": query, "results_ingested": 0,
                          "zero_results": False})

    rows = serp["rows"]
    if not rows:
        # zero organic rows — degrade gracefully to keyword-only (§4.4)
        _set_serp_status(conn, session_id, "zero_results")
        result["cost"]["serp_usd"] = serp.get("cost")
        result["serp"] = {"query": query, "results_ingested": 0,
                          "self_position": None, "zero_results": True}
        return result

    ingested = _ingest_serp_rows(conn, session_id, query, domain, rows,
                                 depth_limit=depth)
    _set_serp_status(conn, session_id, "ok")

    # ---- 5. cost log (AFTER the call, provider-reported cost) ----
    log_cost(conn, site_id=resolution.get("site_id"),
             service=SERP_SERVICE, call_type=SERP_CALL_TYPE,
             calls=1, cost=serp.get("cost"),
             metadata={"source": "audit_engine", "session_id": session_id,
                       "query": query, "depth": depth})
    conn.commit()

    self_row = next((r for r in sorted(rows, key=lambda r: r.get("position") or 0)
                     if (r.get("domain") or "").lower() == (domain or "").lower()),
                    None)
    result["serp"] = {
        "query": query,
        "results_ingested": ingested,
        "self_position": self_row.get("position") if self_row else None,
        "zero_results": ingested == 0,
    }
    result["cost"]["serp_usd"] = serp.get("cost")
    return result


def load_session_result(conn, session_id):
    """GET /api/audit/{session_id} backing query (Phase B slice): the
    persisted session + competitors, zero new spend."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT session_id, requested_url, normalized_url, domain, mode,
                   site_id, inferred_query, inferred_intent, fetch_status,
                   serp_status, created_at, expires_at
            FROM audit_sessions WHERE session_id = %s
            """,
            (session_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    cols = ["session_id", "requested_url", "normalized_url", "domain",
            "mode", "site_id", "inferred_query", "inferred_intent",
            "fetch_status", "serp_status", "created_at", "expires_at"]
    out = dict(zip(cols, row))
    out["session_id"] = str(out["session_id"])
    if out["site_id"] is not None:
        out["site_id"] = str(out["site_id"])
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT query, position, result_url, result_domain, is_self,
                   title, snippet, url_pattern
            FROM audit_serp_competitors WHERE session_id = %s
            ORDER BY position ASC
            """,
            (session_id,),
        )
        out["serp_competitors"] = [
            {"query": r[0], "position": r[1], "result_url": r[2],
             "result_domain": r[3], "is_self": r[4], "title": r[5],
             "snippet": r[6], "url_pattern": r[7]}
            for r in cur.fetchall()]
    return out
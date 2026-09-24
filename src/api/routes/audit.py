"""Audit endpoints (plan/23 §4.1–4.4): on-demand brand-agnostic audits.

The request path is READ-ONLY (plan/23 non-negotiable 4): writes stay in
the audit_* session tables; connected-store execution remains in
jobs/fix_executor.py behind the plan/21 two-gate flow — this router never
touches the Shopify connector.

Endpoints:
  POST /audit/live-url            — run an on-demand audit (§4.1 contract)
  GET  /audit/{session_id}        — idempotent replay of a session (§4.2)
  POST /audit/{session_id}/to-store — connected-mode bridge (§4.3)

Typed error contract (§4.1): every failure is {"error": <code>} with the
documented HTTP status; page-analysis data is returned even when the SERP
step degrades (zero results / budget exhausted), per §4.4.

Rate limiting (§5.3): per-IP token bucket (AUDIT_RATE_LIMIT env, default
10/hour), in-process.
"""

import os
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from api.common import get_conn

router = APIRouter(tags=["audit"])

RATE_LIMIT_PER_HOUR = int(os.environ.get("AUDIT_RATE_LIMIT", "10"))
RATE_LIMIT_ENABLED = os.environ.get("AUDIT_RATE_LIMIT_DISABLED", "") not in ("1", "true")


class LiveAuditRequest(BaseModel):
    url: str
    depth: Optional[int] = None
    location_code: Optional[int] = None
    language_code: Optional[str] = None
    refresh: bool = False


class ToStoreRequest(BaseModel):
    approve: bool = False
    persist_cluster: bool = False


# ------------------------------------------------------------
# §5.3 per-IP token bucket (in-process)
# ------------------------------------------------------------

class _RateLimiter:
    def __init__(self, limit=RATE_LIMIT_PER_HOUR):
        self.limit = limit
        self.buckets = {}

    def allow(self, ip):
        now = time.time()
        window = self.buckets.setdefault(ip, [])
        while window and now - window[0] > 3600:
            window.pop(0)
        if len(window) >= self.limit:
            return False
        window.append(now)
        return True


_rate_limiter = _RateLimiter()


def _client_ip(request):
    try:
        return request.client.host or "unknown"
    except Exception:
        return "unknown"


# ------------------------------------------------------------
# POST /audit/live-url (§4.1)
# ------------------------------------------------------------

# Test seams (mirroring the engine's contract): when set, the endpoint
# uses the injected adapter / fetcher instead of the live paths. Live
# callers never touch these. Module-level seams keep FastAPI's decorated
# handler intact while letting tests inject doubles at both boundaries.
TEST_ADAPTER = None
TEST_FETCH = None


def _resolve_adapter():
    """The adapter for this request: the injected test double when set,
    else the real OpenSEO facade (mock mode inside the adapter)."""
    if TEST_ADAPTER is not None:
        return TEST_ADAPTER
    from connectors.openseo import get_openseo_adapter
    return get_openseo_adapter()


def _resolve_fetch():
    """The page-fetcher for this request: the injected test double when
    set, else the real Phase A ingest (robots + SSRF guarded)."""
    if TEST_FETCH is not None:
        return TEST_FETCH
    from audit.page_fetch import build_page_snapshot
    return lambda conn, u: build_page_snapshot(u)


def run_live_audit(conn, url, depth=None, refresh=False, location_code=None,
                   language_code=None, adapter=None, fetch_page=None,
                   cache_ttl_hours=None):
    """Shared service body (also used by tests to drive the lifecycle).

    `adapter` / `fetch_page` are the test/live seam inputs (the endpoint
    resolves them via _resolve_adapter/_resolve_fetch; direct callers
    inject doubles). Every failure path is a typed key in the result —
    page-fetch failures raise the §4.1 HTTPException contract.
    """
    import audit.engine as engine
    from audit.page_fetch import PageFetchError

    # 1. page ingest (typed errors -> §4.1 contract)
    snapshot = None
    # URL-shape validation FIRST (§4.1 400 contract) — cheap, deterministic,
    # and independent of the fetch seam (a stubbed fetch must not bypass it).
    from audit.page_fetch import validate_url
    try:
        validate_url(url)
    except PageFetchError as exc:
        _page_fetch_failure(conn, url, exc)
    if fetch_page is not None:
        try:
            snapshot = fetch_page(conn, url)
        except PageFetchError as exc:
            _page_fetch_failure(conn, url, exc)
    else:
        from audit.page_fetch import build_page_snapshot
        try:
            snapshot = build_page_snapshot(url)
        except PageFetchError as exc:
            _page_fetch_failure(conn, url, exc)

    # 2. SERP orchestration (cache/budget/degradation inside the engine)
    kwargs = {"adapter": adapter, "fetch_page": lambda c, u: snapshot,
              "location_code": location_code,
              "language_code": language_code}
    if cache_ttl_hours is not None:
        kwargs["cache_ttl_hours"] = cache_ttl_hours
    result = engine.run_audit(conn, url, depth=depth, refresh=refresh,
                              **kwargs)

    # 3. suggestions (Phase C adapter; checklist + connected both preview
    # here — the connected two-gate flow happens via /to-store + the
    # existing /fixes routes, never inline)
    from audit.context_adapter import generate_suggestions
    serp_rows = []
    if result.get("session_id"):
        session = engine.load_session_result(conn, result["session_id"])
        serp_rows = session.get("serp_competitors") or []
    mode = result.get("mode", "audit_checklist")
    suggestions = generate_suggestions(
        conn, snapshot.get("url") or url, mode,
        page={**snapshot, "primary_keyword": result.get("inferred_query"),
              "intent": result.get("inferred_intent")},
        inferred_query=result.get("inferred_query"),
        inferred_intent=result.get("inferred_intent"),
        site_name=resolution_site_name(conn, result.get("session_id")),
        serp_rows=serp_rows,
        site_id=result.get("site_id") if isinstance(result.get("site_id"), str) else None,
    )
    suggestions.pop("mode", None)

    page_out = {
        "status_code": snapshot.get("status_code"),
        "title": snapshot.get("title"),
        "meta_description": snapshot.get("meta_description"),
        "h1": snapshot.get("h1"),
        "heading_outline": snapshot.get("heading_outline") or [],
        "body_text_chars": len(snapshot.get("body_text") or ""),
        "render_status": snapshot.get("render_status"),
    }
    result["page"] = page_out
    result["suggestions"] = suggestions
    return result


def _page_fetch_failure(conn, url, exc):
    """Session spine persists the failure for debugging (§4.4), then the
    typed §4.1 error is raised."""
    import audit.engine as engine
    session_id = engine.create_session(
        conn, url, engine_mode_for_url(conn, url),
        domain=engine_host(conn, url))
    engine._set_fetch_status(conn, session_id,
                             "blocked_robots" if exc.reason == "blocked_robots"
                             else "page_unreachable")
    detail = {"error": exc.reason, "detail": exc.detail,
              "session_id": session_id}
    if exc.reason == "invalid_url":
        raise HTTPException(status_code=400, detail=detail)
    if exc.reason == "blocked_robots":
        raise HTTPException(status_code=422, detail=detail)
    raise HTTPException(status_code=502, detail=detail)


def resolution_site_name(conn, session_id):
    import audit.engine as engine
    session = engine.load_session_result(conn, session_id)
    if session and session.get("site_id"):
        with conn.cursor() as cur:
            cur.execute("SELECT site_name FROM site_config WHERE site_id = %s",
                        (session["site_id"],))
            row = cur.fetchone()
        return row[0] if row else None
    return None


def engine_mode_for_url(conn, url):
    """Mode for the failure-path session row (site resolution best effort)."""
    try:
        from audit.site_resolution import resolve_site
        return resolve_site(conn, url).get("mode", "audit_checklist")
    except Exception:
        return "audit_checklist"


def engine_host(conn, url):
    host = (url or "").split("://", 1)[-1].split("/", 1)[0]
    host = host.split(":", 1)[0].lower()
    return host[4:] if host.startswith("www.") else host


@router.post("/audit/live-url")
def live_url_audit(body: LiveAuditRequest, request=None,
                   conn=Depends(get_conn)):
    if RATE_LIMIT_ENABLED and request is not None:
        ip = _client_ip(request)
        if not _rate_limiter.allow(ip):
            raise HTTPException(
                status_code=429,
                detail={"error": "rate_limited", "retry_after": 60})

    result = run_live_audit(conn, body.url.strip(), depth=body.depth,
                            refresh=body.refresh,
                            location_code=body.location_code,
                            language_code=body.language_code,
                            adapter=_resolve_adapter(),
                            fetch_page=_resolve_fetch())
    # normalize the engine's typed errors into the §4.1 contract
    if not result.get("ok"):
        error = result.get("error")
        if error == "budget_exhausted":
            raise HTTPException(status_code=402, detail={
                "error": "budget_exhausted",
                "session_id": result.get("session_id")})
        raise HTTPException(status_code=502, detail={
            "error": error or "serp_error",
            "detail": result.get("detail"),
            "session_id": result.get("session_id")})
    result["cost"] = result.get("cost") or {}
    return result


# ------------------------------------------------------------
# GET /audit/{session_id} (§4.2)
# ------------------------------------------------------------

@router.get("/audit/{session_id}")
def get_audit(session_id: str, conn=Depends(get_conn)):
    import audit.engine as engine
    from audit.context_adapter import generate_suggestions
    try:
        session_uuid = uuid.UUID(session_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    session = engine.load_session_result(conn, str(session_uuid))
    if not session:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    # expired sessions report 404 (§4.2: 404 when expired/pruned)
    expires = session.get("expires_at")
    if expires:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        if hasattr(expires, "tzinfo") and expires < now:
            raise HTTPException(status_code=404, detail={"error": "expired"})
    suggestions = {}
    if session.get("fetch_status") == "fetched":
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT title, meta_description, h1, body_text,
                       heading_outline, render_status, status_code
                FROM audit_page_snapshots WHERE session_id = %s
                ORDER BY fetched_at DESC LIMIT 1
                """,
                (str(session_uuid),))
            snap_row = cur.fetchone()
        if snap_row:
            page = {
                "title": snap_row[0], "meta_description": snap_row[1],
                "h1": snap_row[2], "body_text": snap_row[3],
                "heading_outline": snap_row[4] or [],
                "render_status": snap_row[5],
                "status_code": snap_row[6],
            }
            suggestions = generate_suggestions(
                conn, session["normalized_url"], session["mode"],
                page=page,
                inferred_query=session.get("inferred_query"),
                inferred_intent=session.get("inferred_intent"),
                serp_rows=session.get("serp_competitors") or [],
                site_id=session.get("site_id"))
    return dict(session, suggestions=suggestions)


# ------------------------------------------------------------
# POST /audit/{session_id}/to-store (§4.3) — connected mode ONLY
# ------------------------------------------------------------

@router.post("/audit/{session_id}/to-store")
def to_store(session_id: str, body: ToStoreRequest,
             conn=Depends(get_conn)):
    import audit.engine as engine
    from audit.context_adapter import upsert_page_for_site
    import json as _json

    try:
        session_uuid = uuid.UUID(session_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    session = engine.load_session_result(conn, str(session_uuid))
    if not session:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if session.get("mode") != "connected" or not session.get("site_id"):
        raise HTTPException(status_code=409, detail={
            "error": "not_connected",
            "detail": "to-store requires a connected-store session "
                      "(domain must match a registered site)"})

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.url, s.title, s.meta_description, s.h1, s.body_html,
                   s.body_text, s.render_status, s.status_code
            FROM audit_page_snapshots s
            WHERE s.session_id = %s
            ORDER BY fetched_at DESC LIMIT 1
            """,
            (str(session_uuid),))
        snap_row = cur.fetchone()
    if not snap_row:
        raise HTTPException(status_code=409, detail={
            "error": "no_page_snapshot",
            "detail": "the session has no page snapshot to bridge"})
    snapshot = {
        "url": snap_row[0], "title": snap_row[1],
        "meta_description": snap_row[2], "h1": snap_row[3],
        "body_html": snap_row[4], "body_text": snap_row[5],
        "render_status": snap_row[6], "status_code": snap_row[7],
        "page_type": "product",
    }
    page_id = upsert_page_for_site(conn, session["site_id"], snapshot)

    # optional: persist the inferred keyword as a cluster (§4.3 step 2)
    cluster_id = None
    if body.persist_cluster and session.get("inferred_query"):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO keyword_clusters (site_id, primary_keyword,
                                              keywords, intent)
                VALUES (%s, %s, %s, %s)
                RETURNING cluster_id
                """,
                (session["site_id"], session["inferred_query"],
                 [session["inferred_query"]],
                 session.get("inferred_intent") or "unknown"))
            cluster_id = str(cur.fetchone()[0])
    conn.commit()

    # recommendation (gate 1): 'proposed' by default; 'approved' only on
    # an explicit operator approve:true (§4.3 — the bridge may not
    # auto-approve).
    status = "approved" if body.approve else "proposed"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url, cluster_id,
                 diagnosis, evidence_json, impact, confidence, effort,
                 status)
            VALUES (%s, 'audit_engine', 'improve_page', %s, %s, %s, %s,
                    'medium', 'high', 'low', %s)
            RETURNING recommendation_id
            """,
            (session["site_id"], session["normalized_url"], cluster_id,
             _json.dumps({"summary": "on-demand audit improvement "
                                     "recommendation"}),
             _json.dumps({"source": "audit_engine",
                          "session_id": session_id}),
             status))
        recommendation_id = str(cur.fetchone()[0])
    conn.commit()

    return {"session_id": session_id, "page_id": page_id,
            "cluster_id": cluster_id,
            "recommendation_id": recommendation_id,
            "recommendation_status": status,
            "next": "POST /recommendations/{id}/fix -> POST /fixes/{id}/approve"
                    if status == "approved" else
                    "operator approves in the queue (gate 1), then diff "
                    "approve (gate 2)"}


# ------------------------------------------------------------
# Rate-limit config endpoint (ops; §5.3 tunable without deploy)
# ------------------------------------------------------------

@router.get("/audit-config")
def audit_config():
    return {"rate_limit_per_hour": RATE_LIMIT_PER_HOUR}


import uuid  # noqa: E402  (kept at module bottom: only the UUID validators use it)
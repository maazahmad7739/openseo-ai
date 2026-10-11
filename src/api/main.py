"""FastAPI application (brief §1.1).

Reuses the pipeline's db.py/env.py — one config path. Every endpoint is
site-scoped or id-scoped; transitions and value checks reuse the same sources
of truth as the pipeline. Serves the built UI SPA (brief §4.1) when present.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api.common import get_conn, require_auth, ensure_env_loaded
from api.models.schemas import HealthOut
from api.routes import queue, decisions, measurements, fixes, audit, offpage

import env as env_loader

app = FastAPI(title="OpenSEO++ Operator API", version="1.0.0")

# Dev-only CORS: the default is single-origin serving (FastAPI mounts ui/dist,
# so the SPA's relative fetch('/queue', ...) calls hit this same server — no
# CORS involved). But if the SPA is ever served on its own origin (e.g.
# `python -m http.server 8080 --directory ui/dist`), the browser still needs
# the API to accept those cross-origin fetch()s. Same-origin requests are
# unaffected by this middleware.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:3010",
        "http://localhost:3010",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    ensure_env_loaded()


@app.on_event("shutdown")
def shutdown():
    # P3: return every pooled connection to the OS on process teardown —
    # Vercel freezes warm instances without this, leaking sockets until
    # the pooler's idle timeout reaps them.
    import db as database
    database.close_pool()


@app.get("/health", response_model=HealthOut)
def health():
    # No DB dependency: on serverless there may be no reachable Postgres, and
    # a raised FastAPI dependency surfaces as HTTP 500. Report degraded instead.
    try:
        import db as database
        conn = database.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            db_status = "connected"
        finally:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        db_status = f"error: {exc}"
    integration_mode = os.environ.get("INTEGRATION_MODE", "mock")
    agent_creds = "configured" if (
        os.environ.get("OLLAMA_API_BASE") and os.environ.get("OLLAMA_API_KEY")
    ) else "not configured"
    return HealthOut(
        status="ok" if db_status == "connected" else "degraded",
        database=db_status,
        integration_mode=integration_mode,
        agent_credentials=agent_creds,
    )


# API path prefixes that carry the auth token. Everything else is the static
# SPA (index.html, app.js, screens/*, lib.js, components/*) — a browser module
# <script>/import cannot attach an Authorization header, so those must stay
# public or the UI breaks the moment API_AUTH_TOKEN is set.
_AUTH_PROTECTED_PREFIXES = (
    "/queue",
    "/pipeline",
    "/recommendations",
    "/measurements",
    "/results",
    "/fixes",
    "/audit",
    "/offpage",
)

_API_PREFIX = "/api"

# GitHub cannot attach our Bearer header, so the webhook is exempted from
# the shared-token check and verifies itself with HMAC-SHA256 instead
# (GITHUB_WEBHOOK_SECRET; src/api/routes/github_sync.py). NOTE:
# "/fixes/github-webhook".startswith("/fixes") is True — the exemption
# must be checked BEFORE the protected-prefix test.
_WEBHOOK_EXEMPT_PATHS = {"/fixes/github-webhook"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path == "/health":
        return await call_next(request)
    path = request.url.path
    if path.startswith(_API_PREFIX):
        path = path[len(_API_PREFIX):] or "/"
    if path.rstrip("/") in _WEBHOOK_EXEMPT_PATHS:
        return await call_next(request)  # verified via HMAC in-route
    if not path.startswith(_AUTH_PROTECTED_PREFIXES):
        return await call_next(request)
    from api.common import API_TOKEN_ENV
    token = os.environ.get(API_TOKEN_ENV)
    if token:
        # require_auth RETURNS the 401 response (never raises): an
        # HTTPException raised inside a BaseHTTPMiddleware escapes the
        # exception middleware and renders as a bare 500.
        unauthorized = require_auth(request.headers.get("Authorization"))
        if unauthorized is not None:
            return unauthorized
    return await call_next(request)


app.include_router(queue.router)
app.include_router(queue.pipeline_router)
app.include_router(decisions.router)
app.include_router(measurements.router)
app.include_router(fixes.router)
app.include_router(audit.router)
app.include_router(offpage.router)
from api.routes.github_sync import router as github_sync_router
app.include_router(github_sync_router)
from api.routes.maintenance import router as maintenance_router  # TEMPORARY
app.include_router(maintenance_router)

# Vercel serves the FastAPI app at /api/* (api/index.py).  The SPA's fetch
# calls target /api/queue…, /api/pipeline…, etc., so the same routers are
# re-mounted under /api so those paths resolve.  include_in_schema=False
# keeps duplicate operations out of /docs.  The bare routes are preserved
# for local uvicorn usage and the Python test suite.
app.include_router(queue.router, prefix=_API_PREFIX, include_in_schema=False)
app.include_router(queue.pipeline_router, prefix=_API_PREFIX, include_in_schema=False)
app.include_router(decisions.router, prefix=_API_PREFIX, include_in_schema=False)
app.include_router(measurements.router, prefix=_API_PREFIX, include_in_schema=False)
app.include_router(fixes.router, prefix=_API_PREFIX, include_in_schema=False)
app.include_router(audit.router, prefix=_API_PREFIX, include_in_schema=False)
app.include_router(offpage.router, prefix=_API_PREFIX, include_in_schema=False)
app.include_router(github_sync_router, prefix=_API_PREFIX, include_in_schema=False)
from api.routes.maintenance import router as _maintenance_router  # TEMPORARY
app.include_router(_maintenance_router, prefix=_API_PREFIX, include_in_schema=False)

# Serve the built operator UI (brief §4.1: one deployable process). The API
# and the SPA share one origin/port, so the UI's relative fetch('/queue', ...)
# calls reach this same server — no CORS, no hardcoded base URL.
_UI_BUILD = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", "..", "ui", "dist"))
if os.path.isdir(_UI_BUILD):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=_UI_BUILD, html=True), name="ui")
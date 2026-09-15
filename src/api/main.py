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
from api.routes import queue, decisions, measurements

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
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    ensure_env_loaded()


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
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path == "/health":
        return await call_next(request)
    if not request.url.path.startswith(_AUTH_PROTECTED_PREFIXES):
        return await call_next(request)
    from api.common import API_TOKEN_ENV
    token = os.environ.get(API_TOKEN_ENV)
    if token:
        require_auth(request.headers.get("Authorization"))
    return await call_next(request)


app.include_router(queue.router)
app.include_router(queue.pipeline_router)
app.include_router(decisions.router)
app.include_router(measurements.router)

# Serve the built operator UI (brief §4.1: one deployable process). The API
# and the SPA share one origin/port, so the UI's relative fetch('/queue', ...)
# calls reach this same server — no CORS, no hardcoded base URL.
_UI_BUILD = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", "..", "ui", "dist"))
if os.path.isdir(_UI_BUILD):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=_UI_BUILD, html=True), name="ui")
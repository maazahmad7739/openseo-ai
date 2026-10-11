"""Shared helpers for the API layer (plan/09 api/ tree).

Reuses the SAME db.py/env.py as the pipeline — no second config path, no
duplicate secret handling (brief §1.1). Endpoints are site-scoped; unknown
transitions/values fail loudly (409/422).
"""

import hashlib
import hmac
import os

import fastapi
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import db as database
import env as env_loader

API_TOKEN_ENV = "API_AUTH_TOKEN"


def get_conn():
    """Pooled connection generator dependency (P3, item 2.2).

    Yields a pooled connection and GUARANTEES rollback + release in
    finally — even when a route handler raises. All 32 Depends(get_conn)
    sites keep their signature: FastAPI treats a generator dependency
    identically at the call sites.

    Rollback-before-release means an aborted handler's open transaction
    (locks, vacuum debris, idle-in-transaction) never leaks into the next
    borrower.
    """
    conn = database.get_pooled_connection()
    try:
        yield conn
    finally:
        database.release_pooled_connection(conn)


def ensure_env_loaded():
    env_loader.load_env_file(quiet=True)


def auth_required_response():
    """The canonical 401 response for the auth middleware.

    The middleware sits OUTSIDE the ExceptionMiddleware, so an HTTPException
    raised there escapes to ServerErrorMiddleware and renders as a bare 500
    (never the promised 401). Middleware therefore RETURNS this response
    instead of raising.
    """
    return JSONResponse(
        status_code=401,
        content={"detail": "invalid or missing API token"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_auth(token_header):
    """Shared-token auth (MVP; brief §4.2 — founder confirmed pattern).

    When API_AUTH_TOKEN is unset, auth is disabled (local dev default).
    Set the env var to enforce the Bearer token check.

    Comparison is timing-safe (hmac.compare_digest on both sides): never
    `!=`/`==` on secrets — string equality short-circuits on the first
    differing byte, leaking the token length/prefix through response-time
    deltas.

    Returns the 401 JSONResponse on failure (never raises — the caller is
    the @app.middleware handler, outside the exception middleware).
    """
    expected = os.environ.get(API_TOKEN_ENV)
    if not expected:
        return None
    supplied = token_header or ""
    # compare_digest needs same-type args; len-difference is not a secret
    # (header sizes are visible anyway), so prefix-compare is safe here.
    expected_bytes = f"Bearer {expected}".encode("utf-8")
    supplied_bytes = supplied.encode("utf-8")[:len(expected_bytes)]
    if not hmac.compare_digest(expected_bytes, supplied_bytes) or \
            len(supplied_bytes) != len(expected_bytes):
        return auth_required_response()
    return None


class StatusTransitionError(HTTPException):
    def __init__(self, current, requested):
        # plan/16: 'raw' is the generator-inserted candidate state. Only the
        # agent promotes raw -> proposed (enriched) or raw -> rejected (reason).
        # The common stale-tab case (row already decided elsewhere) deserves a
        # message an operator can act on rather than a bare transition error.
        if requested in ("approved", "rejected") and current not in ("raw", "proposed"):
            detail = (
                f"decision already recorded: current status is '{current}' "
                "(this recommendation has already left the decision queue)"
            )
        else:
            detail = (
                f"invalid status transition: '{current}' -> '{requested}' "
                "(allowed: raw -> proposed | rejected, proposed -> approved | rejected, "
                "approved -> in_progress, in_progress -> live, live -> measured)"
            )
        super().__init__(status_code=409, detail=detail)


# Single transition table for the whole operator workflow (brief §1.3 + plan/16).
# 'raw' rows are generator candidates awaiting the agent; the agent (not the
# operator) performs raw -> proposed | rejected. proposed is the only
# operator-visible queue state.
TRANSITIONS = {
    "raw": {"proposed", "rejected"},
    "proposed": {"approved", "rejected"},
    "approved": {"in_progress"},
    "in_progress": {"live"},
    "live": {"measured"},
}


def validate_transition(current_status, requested_status):
    allowed = TRANSITIONS.get(current_status, set())
    if requested_status not in allowed:
        raise StatusTransitionError(current_status, requested_status)


def get_recommendation(conn, recommendation_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT recommendation_id, site_id, generator, action_type, target_url, "
            "proposed_url, cluster_id, diagnosis, evidence_json, work_required_json, "
            "impact, confidence, effort, owner, status, assigned_to, result, "
            "measurement_metric, measurement_window_days, measurement_due_at, "
            "created_at, approved_at, implemented_at, measured_at "
            "FROM recommendations WHERE recommendation_id = %s",
            (recommendation_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="recommendation not found")
    columns = [
        "recommendation_id", "site_id", "generator", "action_type", "target_url",
        "proposed_url", "cluster_id", "diagnosis", "evidence_json", "work_required_json",
        "impact", "confidence", "effort", "owner", "status", "assigned_to", "result",
        "measurement_metric", "measurement_window_days", "measurement_due_at",
        "created_at", "approved_at", "implemented_at", "measured_at",
    ]
    return dict(zip(columns, row))
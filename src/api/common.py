"""Shared helpers for the API layer (plan/09 api/ tree).

Reuses the SAME db.py/env.py as the pipeline — no second config path, no
duplicate secret handling (brief §1.1). Endpoints are site-scoped; unknown
transitions/values fail loudly (409/422).
"""

import os

import fastapi
from fastapi import HTTPException
from pydantic import BaseModel

import db as database
import env as env_loader

API_TOKEN_ENV = "API_AUTH_TOKEN"


def get_conn():
    return database.get_connection()


def ensure_env_loaded():
    env_loader.load_env_file(quiet=True)


def require_auth(token_header):
    """Shared-token auth (MVP; brief §4.2 — founder confirmed pattern).

    When API_AUTH_TOKEN is unset, auth is disabled (local dev default).
    Set the env var to enforce the Bearer token check.
    """
    expected = os.environ.get(API_TOKEN_ENV)
    if not expected:
        return
    if token_header != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid or missing API token")


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
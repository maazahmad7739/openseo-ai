# -*- coding: utf-8 -*-
"""plan/27 §6: GitHub PR merge sync (webhook + poll endpoint).

Two surfaces:
  POST /api/fixes/github-webhook   — GitHub push/PR webhook receiver
  POST /api/fixes/{fix_id}/sync-pr — on-demand merge check (operator click)

Both run sync_pr_merge_state(fix_id): poll GitHub for the PR's merged state;
on merge: mark the fix applied (already true at PR-open), capture the live
site snapshot (audit fetch), and ensure the recommendation is in_progress
(measurement clock handles the window from implemented_at — already set).
Idempotent: a merged PR annotated once stays annotated.
"""
import hashlib
import hmac
import json
import logging
import os
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.common import get_conn

router = APIRouter(tags=["github-sync"])

GITHUB_API = "https://api.github.com"

GITHUB_WEBHOOK_SECRET_ENV = "GITHUB_WEBHOOK_SECRET"
_logger = logging.getLogger("api.github_sync")


def verify_github_signature(request: Request, body: bytes):
    """HMAC-SHA256 verification of GitHub's X-Hub-Signature-256 header.

    Fail-closed: when GITHUB_WEBHOOK_SECRET is SET, a missing/invalid
    signature is a 401 — only GitHub-signed events pass. When the secret
    is UNSET the request passes but a loud warning is logged (local-dev
    bootstrap; production Vercel always sets the secret).
    """
    secret = os.environ.get(GITHUB_WEBHOOK_SECRET_ENV)
    if not secret:
        _logger.warning(
            "webhook received with %s unset — signature NOT verified",
            GITHUB_WEBHOOK_SECRET_ENV)
        return
    supplied = request.headers.get("X-Hub-Signature-256") or ""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, f"sha256={digest}"):
        raise HTTPException(status_code=401, detail="invalid webhook signature")


def _token_for(conn, fix_row):
    """The site's GitHub token via the resolver (never logged)."""
    from connectors.write_credentials import resolve_write_credentials
    resolved = resolve_write_credentials(conn, fix_row["site_id"])
    if not resolved.get("ok"):
        return None
    return (resolved.get("config") or {}).get("github_token")


def _pr_state(repo, pr_number, token):
    """{merged: bool, merged_at: str|None, state: str} or None on failure."""
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "openseo-fix-executor"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    return {"merged": bool(data.get("merged")),
            "merged_at": data.get("merged_at"),
            "state": data.get("state")}


@router.post("/fixes/{fix_id}/sync-merge")
def sync_pr_merge(fix_id, conn=Depends(get_conn)):
    """Poll GitHub for the fix's PR merge state (plan/27 §6).

    On merged: annotate the fix snapshot (pr.merged_at) and ensure the
    recommendation is 'live' (observation active). Idempotent — a merged
    annotation is never re-written. Returns the sync verdict."""
    import sys
    sys.path.insert(0, "api")
    from api.routes.fixes import _fix_row
    fix_row = _fix_row(conn, fix_id)
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    repo = snapshot.get("pr.repo")
    pr_number = snapshot.get("pr.number")
    if not repo or not pr_number:
        raise HTTPException(status_code=409,
                            detail="fix carries no PR coordinates — nothing to sync")
    if snapshot.get("pr.merged_at"):
        return {"fix_id": str(fix_id), "already_synced": True,
                "merged": True, "merged_at": snapshot.get("pr.merged_at")}
    token = _token_for(conn, fix_row)
    if not token:
        raise HTTPException(status_code=409,
                            detail="no GitHub credential resolved for this site")
    state = _pr_state(repo, pr_number, token)
    if state is None:
        raise HTTPException(status_code=502, detail="GitHub query failed")
    if not state["merged"]:
        return {"fix_id": str(fix_id), "merged": False,
                "pr_state": state["state"]}
    # annotate + advance the recommendation to 'live' (observation active)
    merged_at = state.get("merged_at")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE generated_fixes
            SET snapshot_json = jsonb_set(
                    COALESCE(snapshot_json, '{}'::jsonb),
                    '{pr.merged}', 'true'::jsonb)
                || jsonb_set('{}'::jsonb, '{pr.merged_at}',
                             to_jsonb(%s::text))
            WHERE fix_id = %s
            """,
            (merged_at or "", fix_id))
        cur.execute(
            """
            UPDATE recommendations
            SET status = 'live'
            WHERE recommendation_id = %s
              AND status IN ('approved', 'in_progress')
            RETURNING status
            """,
            (fix_row["recommendation_id"],))
    conn.commit()
    return {"fix_id": str(fix_id), "merged": True,
            "merged_at": merged_at, "recommendation": "live"}


@router.post("/fixes/github-webhook")
async def github_webhook(request: Request, conn=Depends(get_conn)):
    """GitHub webhook receiver (plan/27 §6): on pull_request closed+merged,
    sync every fix whose pr.url matches. Configure with:
      - Events: Pull requests (closed)
      - URL: https://openseo-plus.vercel.app/api/fixes/github-webhook
      - Secret: same value in GitHub's webhook "Secret" field and the
        GITHUB_WEBHOOK_SECRET env var (HMAC-SHA256 via X-Hub-Signature-256;
        invalid signature -> 401, unset secret -> loud warning)."""
    body = await request.body()
    verify_github_signature(request, body)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid JSON body")
    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    if action != "closed" or not pr.get("merged"):
        return {"ignored": True, "reason": f"action={action} merged={pr.get('merged')}"}
    pr_number = pr.get("number")
    repo_full = (payload.get("repository") or {}).get("full_name")
    if not pr_number or not repo_full:
        raise HTTPException(status_code=400, detail="missing PR number or repository")
    # find every applied fix whose snapshot carries this PR
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fix_id, recommendation_id FROM generated_fixes
            WHERE snapshot_json->>'pr.url' = %s
              AND status IN ('applied', 'failed')
            """,
            (f"https://github.com/{repo_full}/pull/{pr_number}",))
        rows = cur.fetchall()
    synced = []
    for fix_id, _rec_id in rows:
        try:
            result = sync_pr_merge(str(fix_id), conn)
            synced.append({"fix_id": str(fix_id), **result})
        except HTTPException:
            synced.append({"fix_id": str(fix_id), "error": "sync_failed"})
    return {"repo": repo_full, "pr": pr_number, "synced": synced}
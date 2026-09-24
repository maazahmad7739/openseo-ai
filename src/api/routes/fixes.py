"""Fix endpoints (plan/21 Â§3): generate / approve / reject / get / revert.

Second approval gate lives here: recommendation approve (gate 1) then the
operator approves the DIFF (gate 2) before a fix can ever sit 'queued'.
Writes are never free-running in-request: POST /execute and POST /revert are
dev/manual triggers that run the SAME job path as the scheduled executor â€”
under the same per-site advisory lock, so a request and a scheduled run can
never collide. (Vercel's 60s window still cannot own a long write batch;
that stays the executor job's domain.)
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any, List, Optional
from uuid import UUID

from api.common import get_conn, get_recommendation

router = APIRouter(tags=["fixes"])


class FixOut(BaseModel):
    fix_id: UUID
    recommendation_id: UUID
    site_id: UUID
    action_type: str
    sub_type: Optional[str] = None
    target_url: str
    target_entity_ref: str
    payload_json: Any = None
    diff_json: Any = None
    generation_source: str
    status: str
    risk_tier: str
    snapshot_json: Any = None
    rollback_of: Optional[UUID] = None
    executed_at: Optional[str] = None
    verification_status: Optional[str] = None
    error_detail: Optional[str] = None
    created_at: Optional[str] = None
    approved_at: Optional[str] = None
    applied_at: Optional[str] = None


class FixApproveOut(BaseModel):
    fix_id: UUID
    status: str
    weekly_cap: int
    applied_this_week: int


class FixRejectOut(BaseModel):
    fix_id: UUID
    status: str
    rejection_log_id: UUID


class FixGenerateOut(BaseModel):
    fix_id: UUID
    recommendation_id: UUID
    status: str
    created: bool
    diff_json: Any = None


class FixDraftOut(BaseModel):
    """Lazy-draft result: what was generated (or why nothing could be)."""
    drafts: List[dict] = []
    unsupported: List[dict] = []


class FixRejectRequest(BaseModel):
    reason: str


def _fix_row(conn, fix_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fix_id, recommendation_id, site_id, action_type, sub_type,
                   target_url, target_entity_ref, payload_json, diff_json,
                   generation_source, status, risk_tier, snapshot_json,
                   rollback_of, executed_at, verification_status, error_detail,
                   created_at, approved_at, applied_at
            FROM generated_fixes WHERE fix_id = %s
            """,
            (fix_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="fix not found")
    columns = [
        "fix_id", "recommendation_id", "site_id", "action_type", "sub_type",
        "target_url", "target_entity_ref", "payload_json", "diff_json",
        "generation_source", "status", "risk_tier", "snapshot_json",
        "rollback_of", "executed_at", "verification_status", "error_detail",
        "created_at", "approved_at", "applied_at",
    ]
    return dict(zip(columns, row))


def _out(row):
    return FixOut(
        fix_id=row["fix_id"],
        recommendation_id=row["recommendation_id"],
        site_id=row["site_id"],
        action_type=row["action_type"],
        sub_type=row["sub_type"],
        target_url=row["target_url"],
        target_entity_ref=row["target_entity_ref"],
        payload_json=_jsonb(row["payload_json"]),
        diff_json=_jsonb(row["diff_json"]),
        generation_source=row["generation_source"],
        status=row["status"],
        risk_tier=row["risk_tier"],
        snapshot_json=_jsonb(row["snapshot_json"]),
        rollback_of=row["rollback_of"],
        executed_at=_iso(row["executed_at"]),
        verification_status=row["verification_status"],
        error_detail=row["error_detail"],
        created_at=_iso(row["created_at"]),
        approved_at=_iso(row["approved_at"]),
        applied_at=_iso(row["applied_at"]),
    )


def _jsonb(value):
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


# ------------------------------------------------------------
# Generation (gate-1 side effect): POST /recommendations/{id}/fix
# ------------------------------------------------------------

@router.post("/recommendations/{recommendation_id}/fix", response_model=FixGenerateOut)
def generate_fix(recommendation_id, conn=Depends(get_conn)):
    """Turn an approved recommendation into a concrete fix (status='generated').

    Gate 1 must already be recorded (approve) or must be recorded in the same
    operator action; generating requires the recommendation to exist. The
    recommendation status transition itself stays on /approve (TRANSITIONS
    untouched) â€” this endpoint only writes the generated_fixes row.

    Best effort fresh read of the live seo.title (the subject the fix
    rewrites) so the quality gate judges the real value: the shop_domain is
    in site_config and the token in env. No credentials resolved -> the
    gate falls back to pages.title (never a crash; the executor's
    stale-diff guard remains the authority on drift).
    """
    from fixes.generator import (generate_fix_for_recommendation,
                                 FixGenerationError, FixNotSupported)
    from fixes.policy import PolicyBlocked

    get_recommendation(conn, recommendation_id)  # 404 when unknown
    live_title = _fresh_live_seo_title(conn, recommendation_id)
    try:
        result = generate_fix_for_recommendation(conn, recommendation_id,
                                                 live_seo_title=live_title)
    except FixNotSupported as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except PolicyBlocked as exc:
        raise HTTPException(status_code=409, detail=f"fix conflict: {exc.reason}")
    except FixGenerationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return FixGenerateOut(
        fix_id=result["fix_id"],
        recommendation_id=recommendation_id,
        status=result["status"],
        created=result["created"],
        diff_json=result["diff"],
    )


@router.post("/recommendations/{recommendation_id}/meta-fix", response_model=FixGenerateOut)
def generate_meta_fix(recommendation_id, conn=Depends(get_conn)):
    """Phase 2 (plan/21 §2.2): turn an approved recommendation into a
    seo.description fix (status='generated').

    Same gate order as the title path: shared suppressions (protect
    winners / consolidate) run first; the meta quality gate judges the
    LIVE seo.description when a fresh read resolves, else
    pages.meta_description. Policy is NOT enforced here (generation runs
    while execution stays disabled; fix_policy 0 semantics)."""
    from fixes.generator import (generate_meta_fix_for_recommendation,
                                 FixGenerationError, FixNotSupported)
    from fixes.policy import PolicyBlocked

    get_recommendation(conn, recommendation_id)  # 404 when unknown
    live_meta = _fresh_live_meta_description(conn, recommendation_id)
    try:
        result = generate_meta_fix_for_recommendation(conn, recommendation_id,
                                                      live_meta_description=live_meta)
    except FixNotSupported as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except PolicyBlocked as exc:
        raise HTTPException(status_code=409, detail=f"fix conflict: {exc.reason}")
    except FixGenerationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return FixGenerateOut(
        fix_id=result["fix_id"],
        recommendation_id=recommendation_id,
        status=result["status"],
        created=result["created"],
        diff_json=result["diff"],
    )


@router.post("/recommendations/{recommendation_id}/draft-fixes",
             response_model=FixDraftOut)
def draft_fixes(recommendation_id, conn=Depends(get_conn)):
    """Lazy-draft every engine-supported fix for an approved recommendation.

    Called by the drawer's Auto-fix task when no diff exists yet: drafts the
    seo.title fix and (independently) the seo.description fix, tolerating
    per-field failures — a title draft that passes the quality gate while the
    meta draft fails validation is a NORMAL outcome, not an error. Returns
    what was created plus a per-field reason for anything unsupported so the
    UI can say honestly why no diff exists.
    """
    from fixes.generator import (
        generate_fix_for_recommendation, generate_meta_fix_for_recommendation,
        FixGenerationError, FixNotSupported)
    from fixes.policy import PolicyBlocked

    rec = get_recommendation(conn, recommendation_id)  # 404 when unknown
    if rec["status"] not in ("approved", "in_progress"):
        raise HTTPException(
            status_code=409,
            detail=f"drafting requires an approved recommendation "
                   f"(current status: '{rec['status']}')",
        )

    drafts: List[dict] = []
    unsupported: List[dict] = []
    for label, fn, kwargs in (
        ("seo.title", generate_fix_for_recommendation,
         {"live_seo_title": _fresh_live_seo_title(conn, recommendation_id)}),
        ("seo.description", generate_meta_fix_for_recommendation,
         {"live_meta_description": _fresh_live_meta_description(conn, recommendation_id)}),
    ):
        try:
            result = fn(conn, recommendation_id, **kwargs_safe(kwargs))
            drafts.append({
                "fix_id": result["fix_id"],
                "sub_type": label,
                "status": result["status"],
                "created": result["created"],
                "diff_json": result["diff"],
            })
        except FixNotSupported as exc:
            unsupported.append({"sub_type": label, "reason": str(exc)})
        except (PolicyBlocked, FixGenerationError) as exc:
            reason = getattr(exc, "reason", None) or str(exc)
            unsupported.append({"sub_type": label, "reason": reason})
    conn.rollback()
    return FixDraftOut(drafts=drafts, unsupported=unsupported)


def kwargs_safe(kwargs):
    return kwargs


@router.post("/recommendations/{recommendation_id}/publish-fix", response_model=FixGenerateOut)
def generate_publish_fix(recommendation_id, conn=Depends(get_conn)):
    """Phase 3 (plan/21 §2.3): approved technical_fix -> publish-state fix.

    Routes ONLY sitemap_index_mismatch/not_indexable whose
    evidence.mechanism_hypothesis is 'publish_state' (plan/03's deterministic
    root-cause chain). Products -> productUpdate(status) (write_products);
    collections -> publishablePublish (write_publications, publication id
    resolved + cached from system_config / the publications query).
    Everything else is a plan_only surface and 422s here.
    """
    from fixes.generator import (generate_fix_for_recommendation,
                                 FixGenerationError, FixNotSupported)
    from fixes.policy import PolicyBlocked

    get_recommendation(conn, recommendation_id)  # 404 when unknown
    try:
        result = generate_fix_for_recommendation(conn, recommendation_id)
    except FixNotSupported as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except PolicyBlocked as exc:
        raise HTTPException(status_code=409, detail=f"fix conflict: {exc.reason}")
    except FixGenerationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return FixGenerateOut(
        fix_id=result["fix_id"],
        recommendation_id=recommendation_id,
        status=result["status"],
        created=result["created"],
        diff_json=result["diff"],
    )


def _fresh_live_seo_title(conn, recommendation_id):
    """One cheap GraphQL read for the gate input; None on anything odd."""
    import os
    from connectors.shopify import ShopifyGraphQLClient
    from jobs.fix_executor import _normalize_shop_domain

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.shopify_domain, p.url FROM recommendations r "
                "JOIN site_config s ON s.site_id = r.site_id "
                "JOIN pages p ON p.site_id = r.site_id AND p.url = r.target_url "
                "WHERE r.recommendation_id = %s",
                (recommendation_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        shop_domain, target_url = row
        token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
        if not shop_domain or not token:
            return None
        client = ShopifyGraphQLClient(shop_domain=_normalize_shop_domain(shop_domain),
                                      access_token=token)
        handle = target_url.rstrip("/").rsplit("/", 1)[-1]
        query = ("query FixGenRead($handle: String!) { productByHandle(handle: $handle) "
                 "{ seo { title } } }")
        result = client.run("productByHandle", query, {"handle": handle})
        if result.get("ok"):
            product = (result.get("data") or {}).get("productByHandle") or {}
            return (product.get("seo") or {}).get("title")
    except Exception:
        return None
    return None


def _fresh_live_meta_description(conn, recommendation_id):
    """Phase 2: same best-effort live read for seo.description (gate input;
    None on anything odd -> gate falls back to pages.meta_description)."""
    import os
    from connectors.shopify import ShopifyGraphQLClient
    from jobs.fix_executor import _normalize_shop_domain

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.shopify_domain, p.url FROM recommendations r "
                "JOIN site_config s ON s.site_id = r.site_id "
                "JOIN pages p ON p.site_id = r.site_id AND p.url = r.target_url "
                "WHERE r.recommendation_id = %s",
                (recommendation_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        shop_domain, target_url = row
        token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
        if not shop_domain or not token:
            return None
        client = ShopifyGraphQLClient(shop_domain=_normalize_shop_domain(shop_domain),
                                      access_token=token)
        handle = target_url.rstrip("/").rsplit("/", 1)[-1]
        query = ("query FixGenMetaRead($handle: String!) { productByHandle(handle: $handle) "
                 "{ seo { description } } }")
        result = client.run("productByHandle", query, {"handle": handle})
        if result.get("ok"):
            product = (result.get("data") or {}).get("productByHandle") or {}
            return (product.get("seo") or {}).get("description")
    except Exception:
        return None
    return None


@router.get("/recommendations/{recommendation_id}/fix", response_model=list[FixOut])
def get_fixes_for_recommendation(recommendation_id, conn=Depends(get_conn)):
    get_recommendation(conn, recommendation_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fix_id FROM generated_fixes WHERE recommendation_id = %s "
            "ORDER BY created_at DESC",
            (recommendation_id,),
        )
        ids = [str(r[0]) for r in cur.fetchall()]
    conn.rollback()
    return [_out(_fix_row(conn, fid)) for fid in ids]


# ------------------------------------------------------------
# Diff approval (gate 2): POST /fixes/{id}/approve | /reject
# ------------------------------------------------------------

@router.post("/fixes/{fix_id}/approve", response_model=FixApproveOut)
def approve_fix(fix_id, conn=Depends(get_conn)):
    from fixes.policy import check_execution_allowed, PolicyBlocked, weekly_applied_count

    row = _fix_row(conn, fix_id)
    if row["status"] != "generated":
        raise HTTPException(
            status_code=409,
            detail=f"approve requires status 'generated' (current: '{row['status']}')",
        )
    try:
        policy = check_execution_allowed(conn, row["site_id"], row["sub_type"])
    except PolicyBlocked as exc:
        # Fail-closed: a disabled sub_type can never reach 'queued'.
        raise HTTPException(status_code=409,
                            detail=f"execution blocked: {exc.reason} {exc.detail}")
    applied = weekly_applied_count(conn, row["site_id"], row["sub_type"])
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE generated_fixes SET status = 'queued', approved_at = now() "
            "WHERE fix_id = %s AND status = 'generated'",
            (fix_id,),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise HTTPException(status_code=409, detail="fix left 'generated' concurrently")
    conn.commit()
    return FixApproveOut(fix_id=fix_id, status="queued",
                         weekly_cap=policy["weekly_cap"],
                         applied_this_week=applied)


@router.post("/fixes/{fix_id}/reject", response_model=FixRejectOut)
def reject_fix(fix_id, body: FixRejectRequest, conn=Depends(get_conn)):
    """Reject the diff (gate 2): fix -> expired + reason -> rejection_log."""
    row = _fix_row(conn, fix_id)
    if row["status"] != "generated":
        raise HTTPException(
            status_code=409,
            detail=f"reject requires status 'generated' (current: '{row['status']}')",
        )
    try:
        with conn.cursor() as cur:
            # No approved_at stamp here — the fix never was approved; the
            # rejection reason lives in error_detail + rejection_log.
            cur.execute(
                "UPDATE generated_fixes SET status = 'expired', error_detail = %s "
                "WHERE fix_id = %s AND status = 'generated'",
                (f"operator rejected: {body.reason}", fix_id),
            )
            cur.execute(
                "INSERT INTO rejection_log (site_id, candidate_id, generator, "
                "primary_keyword, reason, rejected_by) "
                "SELECT site_id, recommendation_id, 'fix_generation', NULL, %s, 'operator' "
                "FROM recommendations WHERE recommendation_id = %s RETURNING id",
                (body.reason, row["recommendation_id"]),
            )
            inserted = cur.fetchone()
            if not inserted:
                # Recommendation vanished between the status reads — nothing
                # to log; roll the whole rejection back and fail loudly.
                conn.rollback()
                raise HTTPException(status_code=409,
                                    detail="recommendation not found for rejection_log")
            log_id = inserted[0]
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        # Never leave the request's transaction aborted (psycopg2 would
        # refuse every later statement on this connection).
        conn.rollback()
        raise
    return FixRejectOut(fix_id=fix_id, status="expired", rejection_log_id=log_id)


@router.get("/fixes/{fix_id}", response_model=FixOut)
def get_fix(fix_id, conn=Depends(get_conn)):
    row = _fix_row(conn, fix_id)
    conn.rollback()  # read-only: leave the request connection clean
    return _out(row)


# ------------------------------------------------------------
# Execution is ALWAYS the executor job's (queued -> applied there).
# This endpoint is a dev/manual trigger that runs the SAME job path â€”
# including the per-site advisory lock (an API request can still collide
# with a running executor; the lock makes them mutually exclusive).
# ------------------------------------------------------------

@router.post("/fixes/{fix_id}/execute")
def execute_fix(fix_id, conn=Depends(get_conn)):
    from jobs.fix_executor import _execute_one, _resolve_shop_config
    from jobs.locks import job_lock, LOCK_KEYS

    row = _fix_row(conn, fix_id)
    if row["status"] != "queued":
        raise HTTPException(
            status_code=409,
            detail=f"execute requires status 'queued' (current: '{row['status']}'); "
                   "approve the diff first (plan/21 two-gate rule)",
        )
    config = _resolve_shop_config(conn, row["site_id"])
    if not config.get("access_token"):
        raise HTTPException(status_code=409, detail="no Shopify credential resolved")
    fix_row = {
        "fix_id": row["fix_id"],
        "recommendation_id": row["recommendation_id"],
        "site_id": row["site_id"],
        "action_type": row["action_type"],
        "sub_type": row["sub_type"],
        "target_url": row["target_url"],
        "target_entity_ref": row["target_entity_ref"],
        "payload_json": row["payload_json"],
        "diff_json": row["diff_json"],
        "risk_tier": row["risk_tier"],
        "rollback_of": None,
    }
    # Same advisory lock the scheduled executor holds (plan/21 Â§5.2): this
    # request and a scheduled run can never double-execute the same site.
    with job_lock(conn, LOCK_KEYS["fix_executor"], site_id=str(row["site_id"])) as got:
        if not got:
            raise HTTPException(status_code=409,
                                detail="fix_executor is already running for this site")
        result = _execute_one(conn, fix_row, config, dry_run=False)
    conn.rollback()  # leave the request's connection clean for the response
    return result


# ------------------------------------------------------------
# Revert (plan/21 Â§5.3): snapshot values -> adapter -> audited row
# ------------------------------------------------------------

@router.post("/fixes/{fix_id}/revert")
def revert_fix_endpoint(fix_id, conn=Depends(get_conn)):
    from jobs.fix_executor import (_resolve_shop_config, revert_fix,
                                   revert_unpublish_before_redirect)
    from jobs.locks import job_lock, LOCK_KEYS

    row = _fix_row(conn, fix_id)
    if row["status"] != "applied":
        raise HTTPException(
            status_code=409,
            detail=f"revert requires status 'applied' (current: '{row['status']}')",
        )
    snapshot = row["snapshot_json"]
    if snapshot is None:
        raise HTTPException(status_code=409, detail="no snapshot stored â€” cannot revert")
    config = _resolve_shop_config(conn, row["site_id"])
    if not config.get("access_token"):
        raise HTTPException(status_code=409, detail="no Shopify credential resolved")
    with job_lock(conn, LOCK_KEYS["fix_executor"], site_id=str(row["site_id"])) as got:
        if not got:
            raise HTTPException(status_code=409,
                                detail="fix_executor is already running for this site")
        # Rollback lineage (Phase 4 item 3): a sequenced unpublish that is
        # still active is undone FIRST (entity reactivated to its pickup
        # state), then the redirect itself is deleted — undoing in strict
        # reverse order keeps every intermediate state consistent.
        unpublish_undo = revert_unpublish_before_redirect(conn, row, config)
        result = revert_fix(conn, row, config, reason="operator")
    conn.rollback()
    if unpublish_undo is not None:
        result["unpublish_undo"] = unpublish_undo
    return result

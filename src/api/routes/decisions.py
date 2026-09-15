"""Decision endpoints (brief §1.3): approve / reject / assign.

Transitions enforced by the shared table in api.common (409 on invalid).
Reject requires a reason; the reason lands in rejection_log — the same log
the agent consumes as few-shot context next run (plan/08 learning loop).
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.common import get_conn, get_recommendation, validate_transition
from api.models.schemas import (
    RejectRequest, RejectOut, ApproveOut, AssignRequest, AssignOut,
)

router = APIRouter(prefix="/recommendations", tags=["decisions"])


def _status_of(conn, recommendation_id):
    rec = get_recommendation(conn, recommendation_id)
    return rec


@router.post("/{recommendation_id}/approve", response_model=ApproveOut)
def approve(recommendation_id, conn=Depends(get_conn)):
    rec = get_recommendation(conn, recommendation_id)
    validate_transition(rec["status"], "approved")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE recommendations SET status = 'approved', approved_at = now() "
            "WHERE recommendation_id = %s",
            (recommendation_id,),
        )
    conn.commit()
    return ApproveOut(recommendation_id=recommendation_id, status="approved")


@router.post("/{recommendation_id}/reject", response_model=RejectOut)
def reject(recommendation_id, body: RejectRequest, conn=Depends(get_conn)):
    rec = get_recommendation(conn, recommendation_id)
    validate_transition(rec["status"], "rejected")
    with conn.cursor() as cur:
        primary_keyword = None
        if rec.get("cluster_id"):
            cur.execute(
                "SELECT primary_keyword FROM keyword_clusters WHERE cluster_id = %s",
                (rec["cluster_id"],),
            )
            krow = cur.fetchone()
            primary_keyword = krow[0] if krow else None
        cur.execute(
            "UPDATE recommendations SET status = 'rejected', rejection_reason = %s, "
            "rejected_at = now() WHERE recommendation_id = %s",
            (body.reason, recommendation_id),
        )
        cur.execute(
            "INSERT INTO rejection_log "
            "(site_id, candidate_id, generator, primary_keyword, reason, rejected_by) "
            "VALUES (%s, %s, %s, %s, %s, 'operator') RETURNING id",
            (rec["site_id"], rec["recommendation_id"], rec["generator"],
             primary_keyword, body.reason),
        )
        log_id = cur.fetchone()[0]
    conn.commit()
    return RejectOut(recommendation_id=recommendation_id, status="rejected",
                     rejection_log_id=log_id)


@router.post("/{recommendation_id}/assign", response_model=AssignOut)
def assign(recommendation_id, body: AssignRequest, conn=Depends(get_conn)):
    rec = get_recommendation(conn, recommendation_id)
    if rec["status"] not in ("proposed", "approved", "in_progress"):
        raise HTTPException(
            status_code=409,
            detail=f"cannot assign in status '{rec['status']}'",
        )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE recommendations SET owner = %s, assigned_to = %s "
            "WHERE recommendation_id = %s",
            (body.owner, body.owner, recommendation_id),
        )
    conn.commit()
    return AssignOut(recommendation_id=recommendation_id, status=rec["status"],
                     owner=body.owner)
"""Queue endpoints (brief §1.2) — the operator queue.

Reuses pipeline truth: ordering mirrors the agent's priority, filters are
typed, limits are parameters (default 5, never hardcoded).

plan/16: this endpoint serves ONLY agent-enriched rows (status='proposed').
Raw generator candidates never appear under any filter combination. A
'proposed' row whose enrichment is empty is a data-integrity anomaly — it is
logged loudly (naming the recommendation_id) instead of silently passing.
"""

import json
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from api.common import get_conn, get_recommendation
from api.models.schemas import (
    QueueOut, QueueDetailOut, RecommendationOut, StaleApprovalsOut, StaleApprovalOut,
    PipelineOut, PipelineItemOut,
)

logger = logging.getLogger("api.queue")

router = APIRouter(prefix="/queue", tags=["queue"])

# Task text patterns the fix engine can execute end-to-end (generated_fixes
# rows exist for these sub_types: 'seo.title', 'seo.description', 'redirect').
# Anything else degrades to 'manual' — theme/Liquid edits, schema code
# injections, sitemap/content work the operator does by hand.
_AUTOMATED_TASK_PATTERNS = (
    "meta description",
    "meta title",
    "page title",
    "title tag",
    "301",
    "redirect",
)


def _classify_execution_type(task: str) -> str:
    lowered = (task or "").lower()
    return "automated" if any(p in lowered for p in _AUTOMATED_TASK_PATTERNS) else "manual"


def enrich_work_tasks(work, fix_rows):
    """Attach execution_type + fix linkage to work_required_json tasks.

    fix_rows: (fix_id, sub_type, status) tuples of this recommendation's
    generated_fixes rows. A task classified 'automated' links to the row by
    sub_type (title task -> seo.title, description task -> seo.description,
    redirect task -> redirect); unmatched automated tasks stay fix-less and
    the UI shows Drafted as the implicit default.
    """
    if not isinstance(work, list):
        return work
    by_sub_type = {}
    for fix_id, sub_type, status in fix_rows or []:
        by_sub_type.setdefault(sub_type, (fix_id, status))
    enriched = []
    for task in work:
        if not isinstance(task, dict):
            enriched.append(task)
            continue
        item = dict(task)
        item["execution_type"] = _classify_execution_type(item.get("task") or "")
        if item["execution_type"] == "automated":
            lowered = (item.get("task") or "").lower()
            if "redirect" in lowered or "301" in lowered:
                sub_type = "redirect"
            elif "meta description" in lowered:
                sub_type = "seo.description"
            elif "description" in lowered:
                sub_type = "seo.description"
            else:
                sub_type = "seo.title"
            fix = by_sub_type.get(sub_type)
            if fix:
                item["fix_id"], item["fix_status"] = str(fix[0]), fix[1]
        enriched.append(item)
    return enriched


def _load_fix_rows(conn, recommendation_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fix_id, sub_type, status FROM generated_fixes "
            "WHERE recommendation_id = %s ORDER BY created_at",
            (recommendation_id,),
        )
        return cur.fetchall()

# Active-pipeline router: operator visibility for approved + in_progress rows
# (ActionQueue renders only 'proposed', Results only 'measured' — and the
# states in between were previously invisible; see bugfix brief).
pipeline_router = APIRouter(tags=["pipeline"])


# How long an 'approved' recommendation may sit unimplemented before it is
# surfaced as stale (UI badge + weekly sweep). One knob, two consumers.
STALE_APPROVAL_DAYS = 7


@router.get("/stale-approvals", response_model=StaleApprovalsOut)
def get_stale_approvals(
    request: Request,
    site_id,
    stale_days: int = Query(STALE_APPROVAL_DAYS, ge=1, le=365),
    conn=Depends(get_conn),
):
    """Approved-but-unimplemented recommendations older than `stale_days`.

    Same priority order as the queue. `stale_days` mirrors the weekly
    sweep's threshold (default 7) so the UI and the notification agree.
    create_page rows are included: they are implemented via the same
    endpoint (cluster-level baseline) and can go stale like any other
    action type.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.recommendation_id, r.site_id, r.generator, r.action_type,
                   r.diagnosis, r.impact, r.status, r.approved_at, r.assigned_to,
                   (%s::date - r.approved_at::date) AS stale_days
            FROM recommendations r
            WHERE r.site_id = %s AND r.status = 'approved'
              AND r.approved_at IS NOT NULL
              AND (%s::date - r.approved_at::date) >= %s
            ORDER BY stale_days DESC, r.approved_at
            """,
            (request.scope.get("today", date.today()), site_id,
             request.scope.get("today", date.today()), stale_days),
        )
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    conn.rollback()
    return StaleApprovalsOut(
        stale_threshold_days=stale_days,
        items=[
            StaleApprovalOut(
                recommendation_id=r["recommendation_id"],
                site_id=r["site_id"],
                generator=r["generator"],
                action_type=r["action_type"],
                diagnosis=r["diagnosis"],
                impact=r["impact"],
                status=r["status"],
                approved_at=r["approved_at"],
                assigned_to=r["assigned_to"],
                stale_days=int(r["stale_days"]),
            )
            for r in rows
        ],
    )


@router.get("/sites")
def list_sites(conn=Depends(get_conn)):
    """Site bootstrap for the login gate (returns the configured sites).

    Read-only and minimal: ids + names only. The queue endpoints themselves
    remain strictly site-scoped (brief §1.1).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT site_id, site_name, domain FROM site_config ORDER BY created_at")
        columns = [d[0] for d in cur.description]
        sites = [dict(zip(columns, r)) for r in cur.fetchall()]
    conn.rollback()
    return {"sites": sites}


@router.get("", response_model=QueueOut)
def get_queue(
    site_id,
    generator: str = None,
    action_type: str = None,
    owner: str = None,
    impact: str = None,
    search_volume_min: int = None,
    search_volume_max: int = None,
    limit: int = Query(5, ge=1, le=100),
    conn=Depends(get_conn),
):
    """Operator queue: proposed recommendations for one site.

    Ordering: agent-priority order — impact (high first), then the
    recommendation's search volume evidence carried in evidence_json when
    present, else creation recency. total_proposed enables "5 of N" display.

    `owner` and `action_type` are optional filters; the literal "all" is the
    UI's explicit no-filter sentinel and is treated as absent.
    """
    with conn.cursor() as cur:
        sql = (
            "SELECT r.recommendation_id, r.site_id, r.generator, r.action_type, "
            "r.target_url, r.proposed_url, r.cluster_id, r.diagnosis, r.impact, "
            "r.confidence, r.effort, r.owner, r.status, r.assigned_to, r.result, "
            "COALESCE((kc.search_volume)::int, 0) AS search_volume, "
            "kc.primary_keyword, kc.commercial_value, "
            "r.evidence_json "
            "FROM recommendations r "
            "LEFT JOIN keyword_clusters kc ON kc.cluster_id = r.cluster_id "
            "WHERE r.site_id = %s AND r.status = 'proposed'"
        )
        params = [site_id]
        if generator and generator != "all":
            sql += " AND r.generator = %s"
            params.append(generator)
        if action_type and action_type != "all":
            sql += " AND r.action_type = %s"
            params.append(action_type)
        if owner and owner != "all":
            # Case-insensitive: rows are seeded/stored as "SEO" (orchestrator)
            # while the UI sends the lowercase option value "seo".
            sql += " AND lower(r.owner) = lower(%s)"
            params.append(owner)
        if impact:
            sql += " AND r.impact = %s"
            params.append(impact)
        if search_volume_min is not None or search_volume_max is not None:
            sql += " AND (%s IS NULL OR COALESCE(kc.search_volume, 0) >= %s)"
            params.extend([search_volume_min, search_volume_min])
            sql += " AND (%s IS NULL OR COALESCE(kc.search_volume, 0) <= %s)"
            params.extend([search_volume_max, search_volume_max])
        sql += (" ORDER BY CASE r.impact WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, "
                "COALESCE(kc.search_volume, 0) DESC, r.created_at DESC LIMIT %s")
        params.append(limit)
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]

        # total_proposed must reflect the SAME filters as the page query, or
        # the "N of M" counter lies the moment a filter is applied.
        count_sql = (
            "SELECT count(*) FROM recommendations r "
            "LEFT JOIN keyword_clusters kc ON kc.cluster_id = r.cluster_id "
            "WHERE r.site_id = %s AND r.status = 'proposed'"
        )
        count_params = [site_id]
        if generator and generator != "all":
            count_sql += " AND r.generator = %s"
            count_params.append(generator)
        if action_type and action_type != "all":
            count_sql += " AND r.action_type = %s"
            count_params.append(action_type)
        if owner and owner != "all":
            count_sql += " AND lower(r.owner) = lower(%s)"
            count_params.append(owner)
        if impact:
            count_sql += " AND r.impact = %s"
            count_params.append(impact)
        if search_volume_min is not None or search_volume_max is not None:
            count_sql += " AND (%s IS NULL OR COALESCE(kc.search_volume, 0) >= %s)"
            count_params.extend([search_volume_min, search_volume_min])
            count_sql += " AND (%s IS NULL OR COALESCE(kc.search_volume, 0) <= %s)"
            count_params.extend([search_volume_max, search_volume_max])
        cur.execute(count_sql, count_params)
        total = cur.fetchone()[0]
    conn.rollback()

    for r in rows:
        # Belt-and-braces integrity check (plan/16 §1.6): a 'proposed' row the
        # agent "validated" but never actually enriched must never reach the
        # operator silently. Enrichment bugs surface as a loud, named warning.
        try:
            work = json.loads(r["work_required_json"]) if r.get("work_required_json") else None
        except (TypeError, ValueError):
            work = None
        empty_work = work in (None, [], "[]")
        if r["status"] == "proposed" and empty_work:
            logger.warning(
                "queue data-integrity anomaly: recommendation %s is 'proposed' with "
                "empty work_required_json (agent enrichment did not run); "
                "see plan/16 raw->proposed lifecycle",
                r["recommendation_id"],
            )

    items = [
        RecommendationOut(
            recommendation_id=r["recommendation_id"],
            site_id=r["site_id"],
            generator=r["generator"],
            action_type=r["action_type"],
            target_url=r["target_url"],
            proposed_url=r["proposed_url"],
            cluster_id=r["cluster_id"],
            diagnosis=r["diagnosis"],
            impact=r["impact"],
            confidence=r["confidence"],
            effort=r["effort"],
            owner=r["owner"],
            status=r["status"],
            assigned_to=r["assigned_to"],
            result=r["result"],
            search_volume=r["search_volume"] if r["search_volume"] else None,
            primary_keyword=r["primary_keyword"],
            commercial_value=r["commercial_value"],
            evidence_json=_parse_jsonb_queue(r.get("evidence_json")),
        )
        for r in rows
    ]
    return QueueOut(site_id=site_id, total_proposed=total, showing=len(items),
                    recommendations=items)


def _parse_jsonb_queue(value):
    """Queue rows may carry raw jsonb; degrade to None on anything odd."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


@router.get("/{recommendation_id}", response_model=QueueDetailOut)
def get_queue_detail(recommendation_id, conn=Depends(get_conn)):
    """Full detail: evidence + work JSON, measurement plan, cluster context."""
    rec = get_recommendation(conn, recommendation_id)

    cluster = None
    if rec.get("cluster_id"):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cluster_id, primary_keyword, keywords, intent, search_volume, "
                "commercial_value FROM keyword_clusters WHERE cluster_id = %s",
                (rec["cluster_id"],),
            )
            crow = cur.fetchone()
        if crow:
            cluster = {
                "cluster_id": str(crow[0]),
                "primary_keyword": crow[1],
                "keywords": crow[2] or [],
                "intent": crow[3],
                "search_volume": crow[4],
                "commercial_value": crow[5],
            }

    window_days = rec.get("measurement_window_days")
    metric = rec.get("measurement_metric")
    measurement_plan = None
    if rec.get("action_type"):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT measurement_window_days, metric FROM measurement_window_lookup "
                "WHERE action_type = %s",
                (rec["action_type"],),
            )
            wrow = cur.fetchone()
            if wrow:
                window_days = window_days or wrow[0]
                # Single source of truth for the displayed metric (plan/16 §3.1):
                # the lookup's per-action_type metric, unless the agent recorded a
                # more specific one on the row. Never a hardcoded second mapping.
                metric = metric or wrow[1]
    measurement_plan = {
        "metric": metric,
        "window_days": window_days,
        "window_source": "measurement_window_lookup"
        if window_days is not None else None,
        "measurement_due_at": rec.get("measurement_due_at"),
    } if window_days is not None or metric else None

    # plan/16 §3.2: label enrichment state honestly. Row has been through the
    # agent iff it is no longer raw (proposed = agent-validated; later states
    # were agent-promoted first).
    enriched = rec["status"] != "raw"

    # Catalogue inventory signals (drawer "Pages & Products Affected" block):
    # per-cluster stock depth straight from the catalogue_coverage job. Present
    # only for cluster-scoped recommendations; NULL degrades to a hidden block.
    catalogue = None
    if rec.get("cluster_id"):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cc.matching_product_count, cc.in_stock_product_count, "
                "cc.average_price, cc.existing_collection_url "
                "FROM catalogue_coverage cc WHERE cc.cluster_id = %s",
                (rec["cluster_id"],),
            )
            crow = cur.fetchone()
        if crow:
            catalogue = {
                "matching_product_count": crow[0],
                "in_stock_product_count": crow[1],
                "average_price": float(crow[2]) if crow[2] is not None else None,
                "existing_collection_url": crow[3],
            }

    # SERP comparison (drawer "Who outranks you" block): the cluster's latest
    # snapshot results, competitor rows first. is_self lets the UI highlight
    # our own listing; positions give the visual rank ladder.
    serp_context = None
    if rec.get("cluster_id"):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (oss.result_url)
                       oss.query, oss.result_url, oss.result_domain,
                       oss.position, oss.is_self, oss.snapshot_date
                FROM openseo_serp_snapshots oss
                WHERE oss.cluster_id = %s
                ORDER BY oss.result_url, oss.snapshot_date DESC
                """,
                (rec["cluster_id"],),
            )
            columns = [d[0] for d in cur.description]
            serp_rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        serp_context = sorted(serp_rows, key=lambda r: (not r["is_self"], r["position"] or 99))

    def _parse_jsonb(value):
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    parsed_work = _parse_jsonb(rec["work_required_json"])
    if rec["status"] == "proposed" and parsed_work in (None, [], "[]"):
        logger.warning(
            "queue data-integrity anomaly: recommendation %s is 'proposed' with "
            "empty work_required_json (agent enrichment did not run); "
            "see plan/16 raw->proposed lifecycle",
            rec["recommendation_id"],
        )

    # Execution split (automated vs manual) + fix linkage per task: the UI
    # renders Auto-fix badges / fix statuses for engine-supported tasks and a
    # manual checklist for the rest. Cheap sub_type lookup on generated_fixes.
    parsed_work = enrich_work_tasks(parsed_work, _load_fix_rows(conn, rec["recommendation_id"]))

    conn.rollback()
    return QueueDetailOut(
        recommendation_id=rec["recommendation_id"],
        site_id=rec["site_id"],
        generator=rec["generator"],
        action_type=rec["action_type"],
        target_url=rec["target_url"],
        proposed_url=rec["proposed_url"],
        cluster_id=rec["cluster_id"],
        diagnosis=rec["diagnosis"],
        evidence_json=_parse_jsonb(rec["evidence_json"]),
        work_required_json=parsed_work,
        impact=rec["impact"],
        confidence=rec["confidence"],
        effort=rec["effort"],
        owner=rec["owner"],
        status=rec["status"],
        enriched=enriched,
        assigned_to=rec["assigned_to"],
        result=rec["result"],
        measurement_metric=rec.get("measurement_metric"),
        measurement_window_days=window_days,
        measurement_due_at=rec.get("measurement_due_at"),
        created_at=rec.get("created_at"),
        approved_at=rec.get("approved_at"),
        implemented_at=rec.get("implemented_at"),
        measured_at=rec.get("measured_at"),
        cluster=cluster,
        measurement_plan=measurement_plan,
        catalogue=catalogue,
        serp_context=serp_context,
    )


@pipeline_router.get("/pipeline", response_model=PipelineOut)
def get_pipeline(
    request: Request,
    site_id,
    conn=Depends(get_conn),
):
    """Active pipeline (3-stage kanban): approved, in_progress, measured-pending.

    Stage mapping: agent-enriched 'proposed' rows land directly in the
    APPROVED column (the pipeline view maps them to status='approved' —
    ready to implement). 'approved' + 'in_progress' rows render natively.
    ActionQueue still renders raw DB 'proposed' rows for rejection flow;
    the pipeline no longer surfaces a separate PROPOSED stage.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.recommendation_id, r.site_id, r.generator, r.action_type,
                   r.target_url, r.proposed_url, r.diagnosis, r.impact, r.status,
                   r.approved_at, r.implemented_at, r.assigned_to,
                   r.measurement_due_at, r.work_required_json,
                   mwl.measurement_window_days,
                   COALESCE((kc.search_volume)::int, 0) AS search_volume,
                   kc.primary_keyword
            FROM recommendations r
            LEFT JOIN measurement_window_lookup mwl ON mwl.action_type = r.action_type
            LEFT JOIN keyword_clusters kc ON kc.cluster_id = r.cluster_id
            WHERE r.site_id = %s
              AND r.status IN ('proposed', 'approved', 'in_progress')
            ORDER BY
                CASE r.status WHEN 'approved' THEN 1 WHEN 'proposed' THEN 1 ELSE 2 END,
                COALESCE(r.implemented_at, r.approved_at, r.created_at) DESC NULLS LAST
            """,
            (site_id,),
        )
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    conn.rollback()

    today = request.scope.get("today", date.today())
    items: list[PipelineItemOut] = []
    for r in rows:
        # 3-stage kanban mapping: agent-validated 'proposed' rows ARE the
        # APPROVED column (land ready-to-implement); raw DB status is
        # unchanged so ActionQueue/reject flow keeps its contract.
        stage = "approved" if r["status"] == "proposed" else r["status"]
        window = r["measurement_window_days"]
        imp = r["implemented_at"]
        days_remaining = None
        if stage == "in_progress" and window and imp:
            imp_date = imp.date() if hasattr(imp, "date") else imp
            due = imp_date + timedelta(days=window)
            days_remaining = max(int((due - today).days), 0)
        items.append(PipelineItemOut(
            recommendation_id=r["recommendation_id"],
            site_id=r["site_id"],
            generator=r["generator"],
            action_type=r["action_type"],
            target_url=r["target_url"],
            proposed_url=r["proposed_url"],
            diagnosis=r["diagnosis"],
            impact=r["impact"],
            status=stage,
            approved_at=r["approved_at"],
            implemented_at=r["implemented_at"],
            assigned_to=r["assigned_to"],
            observation_window_days=window,
            days_remaining=days_remaining,
            measurement_due_at=r.get("measurement_due_at"),
            search_volume=r["search_volume"] if r["search_volume"] else None,
            primary_keyword=r["primary_keyword"],
            work_tasks=enrich_work_tasks(
                _parse_jsonb_queue(r.get("work_required_json")),
                _load_fix_rows(conn, r["recommendation_id"]),
            ),
        ))

    return PipelineOut(
        site_id=site_id,
        total_approved=sum(1 for i in items if i.status == "approved"),
        total_in_progress=sum(1 for i in items if i.status == "in_progress"),
        items=items,
    )
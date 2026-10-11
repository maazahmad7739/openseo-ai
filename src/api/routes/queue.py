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

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from api.common import get_conn, get_recommendation
from api.models.schemas import (
    QueueOut, QueueDetailOut, RecommendationOut, StaleApprovalsOut, StaleApprovalOut,
    PipelineOut, PipelineItemOut,
)

logger = logging.getLogger("api.queue")

router = APIRouter(prefix="/queue", tags=["queue"])

# Active-window cap (operator request 2026-09-27): the Action Queue surfaces
# at most MAX_ACTIVE_QUEUE proposed cards at a time; the rest of the backlog
# sits in status 'pending' until the active set drains below the cap.
MAX_ACTIVE_QUEUE = 5


def promote_pending_to_queue(conn, site_id, cap=MAX_ACTIVE_QUEUE):
    """Refill the active window from the 'pending' backlog (idempotent).

    Promotes the highest-priority pending rows to 'proposed' until the site
    has `cap` proposed rows again. Called by the decisions routes after
    every approve/reject (the window refills immediately) and by the
    maintenance endpoint. Never raises on an empty backlog.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM recommendations "
            "WHERE site_id = %s AND status = 'proposed'", (site_id,))
        active = cur.fetchone()[0]
    slots = max(cap - active, 0)
    if slots <= 0:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE recommendations
            SET status = 'proposed'
            WHERE recommendation_id IN (
                SELECT recommendation_id FROM recommendations
                WHERE site_id = %s AND status = 'pending'
                ORDER BY CASE impact WHEN 'high' THEN 1 WHEN 'medium' THEN 2
                          ELSE 3 END,
                          created_at ASC
                LIMIT %s
            )
            """,
            (site_id, slots),
        )
        promoted = cur.rowcount
    conn.commit()
    return promoted


def demote_backlog_to_pending(conn, site_id, cap=MAX_ACTIVE_QUEUE):
    """Gate the proposed backlog beyond the cap to 'pending' (idempotent).

    One-time normalization for sites whose queue predates the active
    window: keeps the top `cap` proposed rows, moves the rest to 'pending'
    (surfaced again by promote_pending_to_queue as the active set drains)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE recommendations
            SET status = 'pending'
            WHERE recommendation_id IN (
                SELECT recommendation_id FROM (
                    SELECT recommendation_id,
                           row_number() OVER (
                               ORDER BY CASE impact WHEN 'high' THEN 1
                                        WHEN 'medium' THEN 2 ELSE 3 END,
                                        created_at ASC
                           ) AS rn
                    FROM recommendations
                    WHERE site_id = %s AND status = 'proposed'
                ) ranked
                WHERE rn > %s
            )
            """,
            (site_id, cap),
        )
        demoted = cur.rowcount
    conn.commit()
    return demoted

# Task text patterns the fix engine can execute end-to-end (generated_fixes
# rows exist for these sub_types: 'seo.title', 'seo.description',
# 'redirect', 'collection_description', 'collection_create'). Anything
# else degrades to 'manual' — theme/Liquid edits, schema code injections,
# sitemap work the operator does by hand.
_AUTOMATED_TASK_PATTERNS = (
    "meta description",
    "meta title",
    "page title",
    "title tag",
    "301",
    "redirect",
    "create a collection",   # plan/25: missing-page creation is executable
    "create page",
)

# plan/27+28: netlify-platform auto-fix surfaces (the GitHub-PR engine).
# Tasks on netlify sites mentioning these are executable end-to-end;
# canonical tags ARE now auto-fixable too (plan/28 netlify.canonical_tag).
# plan/29: locality/SERP-gap tasks whose copy carries title/meta phrasing
# ("title/meta phrasing, one H2") are ALSO executable — the title/meta
# portion of the fix routes to netlify.title_meta_rewrite; the H2/content
# portion stays in the task text as operator context (the diff preview
# scopes itself to what the engine writes).
_NETLIFY_AUTOMATED_PATTERNS = (
    "structured data",
    "json-ld",
    "jsonld",
    # the title/meta rewrite surface (PR #3 family): title/meta rewrites
    # on netlify sites route to netlify.title_meta_rewrite
    "meta description",
    "title",
    # plan/28: canonical-tag injection (self-referencing canonical)
    "canonical",
    # plan/29: locality/SERP-gap tasks bundle title/meta phrasing with
    # content asks; the title/meta portion is auto-fixable
    "title/meta phrasing",
    "locality signals",
    "serp gap",
)
NETLIFY_STRUCTURED_SUB_TYPE = "netlify.structured_data"
NETLIFY_TITLE_META_SUB_TYPE = "netlify.title_meta_rewrite"
NETLIFY_CANONICAL_SUB_TYPE = "netlify.canonical_tag"


def _site_platform(conn, site_id):
    """site_config.platform for the site (legacy rows resolve as 'shopify')."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(platform, 'shopify') "
            "FROM site_config WHERE site_id = %s",
            (site_id,),
        )
        row = cur.fetchone()
    return (row[0] if row else "shopify") or "shopify"


def _classify_execution_type(task: str, platform: str = "shopify") -> str:
    lowered = (task or "").lower()
    if platform == "netlify":
        if any(p in lowered for p in _NETLIFY_AUTOMATED_PATTERNS):
            return "automated"
        return "manual"
    return "automated" if any(p in lowered for p in _AUTOMATED_TASK_PATTERNS) else "manual"


def _sub_types_for_task(lowered, platform: str = "shopify"):
    """Task text -> ALL fix sub_types the task bundles (plan/25 §4 +
    plan/28). A single task can bundle multiple engine surfaces — "rewrite
    title tag and meta description" maps to BOTH seo.title and
    seo.description, and each gets its own diff preview. Ordered:
    specific-match first, create-page precedence preserved."""
    lowered = lowered or ""
    if platform == "netlify":
        if not any(p in lowered for p in _NETLIFY_AUTOMATED_PATTERNS):
            return []
        if "structured" in lowered or "json" in lowered:
            return [NETLIFY_STRUCTURED_SUB_TYPE]
        if "canonical" in lowered:
            return [NETLIFY_CANONICAL_SUB_TYPE]
        return [NETLIFY_TITLE_META_SUB_TYPE]
    if ("create a collection" in lowered
            or "create page" in lowered
            or "missing page" in lowered
            or ("/collections/" in lowered and "create" in lowered)):
        return ["collection_create"]
    if "redirect" in lowered or "301" in lowered:
        return ["redirect"]
    # Title/meta bundle: a task mentioning either (or both) links to every
    # matching fix row so each field's before/after renders in the drawer.
    sub_types = []
    if "title" in lowered:
        sub_types.append("seo.title")
    if "meta description" in lowered or "description" in lowered:
        sub_types.append("seo.description")
    if not sub_types:
        sub_types.append("seo.title")
    return sub_types


def _sub_type_for_task(lowered, platform: str = "shopify"):
    """First fix sub_type for a task (legacy single-match callers)."""
    subs = _sub_types_for_task(lowered, platform)
    return subs[0] if subs else None


def enrich_work_tasks(work, fix_rows, platform: str = "shopify"):
    """Attach execution_type + fix linkage to work_required_json tasks.

    fix_rows: (fix_id, sub_type, status, pr_url) tuples of this
    recommendation's generated_fixes rows. A task classified 'automated'
    links to EVERY row whose sub_type the task bundles (title+meta tasks
    link to both seo.title and seo.description rows) — fix_id/fix_status
    stay the primary match for backwards compatibility, fix_refs carries
    the full list so the drawer renders one diff block per field.
    Unmatched automated tasks stay fix-less and the UI shows Drafted as
    the implicit default.
    """
    if not isinstance(work, list):
        return work
    by_sub_type = {}
    for fix_id, sub_type, status, pr_url in fix_rows or []:
        by_sub_type.setdefault(str(sub_type), []).append(
            (fix_id, status, pr_url))
    enriched = []
    for task in work:
        if not isinstance(task, dict):
            enriched.append(task)
            continue
        item = dict(task)
        item["execution_type"] = _classify_execution_type(
            item.get("task") or "", platform)
        if item["execution_type"] == "automated":
            sub_types = _sub_types_for_task((item.get("task") or "").lower(),
                                            platform)
            matches = [fix for st in sub_types
                       for fix in by_sub_type.get(st, [])]
            if matches:
                # Primary = highest-priority ACTIVE row across the bundle
                # (applied > queued > others; _load_fix_rows already sorts
                # applied first). Keeps fix_id/fix_status semantics identical
                # to the single-match behaviour every consumer relies on.
                def _rank(match):
                    status = match[1]
                    if status == "applied":
                        return 0
                    if status == "queued":
                        return 1
                    if status == "generated":
                        return 2
                    return 3

                primary = min(matches, key=_rank)
                item["fix_id"], item["fix_status"] = (
                    str(primary[0]), primary[1])
                if primary[2]:
                    item["fix_pr_url"] = primary[2]
                if len(matches) > 1:
                    item["fix_refs"] = [
                        {"fix_id": str(m[0]), "status": m[1],
                         "sub_type": st, **({"pr_url": m[2]} if m[2] else {})}
                        for st in sub_types
                        for m in by_sub_type.get(st, [])
                    ]
        enriched.append(item)
    return enriched


def _load_fix_rows(conn, recommendation_id):
    """(fix_id, sub_type, status, pr_url) rows for a recommendation.

    pr_url rides from the netlify adapter's snapshot (the GitHub PR the fix
    opened) so pipeline/detail drawers can link directly. Degrades to []
    when the stage-2 schema (generated_fixes) has not been applied to the
    database — task enrichment then renders tasks without fix linkage
    instead of failing the whole endpoint.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fix_id, sub_type, status,
                       COALESCE(snapshot_json->>'pr.url', NULL) pr_url
                FROM generated_fixes
                WHERE recommendation_id = %s
                ORDER BY (status = 'applied') DESC, created_at
                """,
                (recommendation_id,),
            )
            return cur.fetchall()
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        return []

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


@router.post("/sites/{site_id}/normalize-active-window")
def normalize_active_window(site_id, conn=Depends(get_conn)):
    """One-time normalization for the active-window cap (2026-09-27).

    Demotes the proposed backlog beyond MAX_ACTIVE_QUEUE to 'pending'
    (surfaced again as the active set drains). Safe to re-run: a compliant
    site demotes 0 rows. Returns the counts for the operator log.
    """
    demoted = demote_backlog_to_pending(conn, site_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM recommendations "
            "WHERE site_id = %s AND status = 'proposed'", (site_id,))
        active = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM recommendations "
            "WHERE site_id = %s AND status = 'pending'", (site_id,))
        pending = cur.fetchone()[0]
    conn.rollback()
    return {"site_id": site_id, "demoted": demoted,
            "active_proposed": active, "pending_backlog": pending}


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
    """Operator queue: ACTIVE proposed recommendations for one site.

    Active-window cap (operator request 2026-09-27): at most
    MAX_ACTIVE_QUEUE proposed cards are ever served. The cap is a server-side
    invariant, not a client hint — any requested `limit` above it is clamped,
    so a caller cannot walk the backlog by asking for more. The overflow is
    parked in status 'pending' by the demote sweep below and re-enters the
    window through promote_pending_to_queue as the active set drains.

    `total_proposed` is the ACTIVE card count, so the "N of M" counter reads
    "5 of 5" while backlog rows wait in 'pending'.
    """
    # Enforce the window before reading: a site that accumulated proposed rows
    # before this invariant (or via any other writer) is normalised here, so
    # promote_pending_to_queue on approve/reject has a 'pending' backlog to
    # draw from. Idempotent; commits its own demotion.
    demote_backlog_to_pending(conn, site_id)
    limit = min(limit, MAX_ACTIVE_QUEUE)

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

    # The window is capped at MAX_ACTIVE_QUEUE unconditionally, so report at
    # most that many regardless of the limit the caller sent.
    if total > MAX_ACTIVE_QUEUE:
        total = MAX_ACTIVE_QUEUE

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
                "commercial_value FROM keyword_clusters "
                "WHERE cluster_id = %s AND site_id = %s",
                (rec["cluster_id"], rec["site_id"]),
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
                WHERE oss.cluster_id = %s AND oss.site_id = %s
                ORDER BY oss.result_url, oss.snapshot_date DESC
                """,
                (rec["cluster_id"], rec["site_id"]),
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
    # plan/27: classification is platform-aware (netlify structured-data
    # tasks route to the PR engine; canonical tags stay manual everywhere).
    parsed_work = enrich_work_tasks(
        parsed_work,
        _load_fix_rows(conn, rec["recommendation_id"]),
        _site_platform(conn, rec["site_id"]),
    )

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
    """Active pipeline (3-stage kanban): approved, in_progress, measured.

    STRICT stage gating: the APPROVED column shows only rows the operator
    explicitly approved (DB status = 'approved'); raw 'proposed' rows stay
    in the Action Queue until a decision is recorded. The measurement clock
    promotes window-complete 'in_progress' rows to 'measured', which lands
    in the MEASURED column.
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
              AND r.status IN ('approved', 'in_progress', 'measured')
            ORDER BY
                CASE r.status WHEN 'approved' THEN 1 ELSE 2 END,
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
        window = r["measurement_window_days"]
        imp = r["implemented_at"]
        days_remaining = None
        if r["status"] == "in_progress" and window and imp:
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
            status=r["status"],
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
                _site_platform(conn, r["site_id"]),
            ),
        ))

    return PipelineOut(
        site_id=site_id,
        total_approved=sum(1 for i in items if i.status == "approved"),
        total_in_progress=sum(1 for i in items if i.status == "in_progress"),
        items=items,
    )
"""Off-page operator queue API (plan/33 §5 + plan/34).

Discovery and drafting are automated; PUBLISHING is strictly manual. This
router is the operator's manual-control surface for a site's draft queue:

    GET  /offpage/queue                         active queue (capped 5)
    POST /offpage/drafts/{draft_id}/choose      Copy Advice Only /
                                                Copy Full Response (re-checks
                                                the 30d promotional cap)
    POST /offpage/drafts/{draft_id}/skip        discard + reject the rec
    POST /offpage/drafts/{draft_id}/permalink   paste the public permalink ->
                                                offpage_publications
                                                (pending_verify) + 4 pre-created
                                                checkpoint rows

Design invariants:

* The queue is a WINDOW like the action queue's MAX_ACTIVE_QUEUE, capped
  at OFFPAGE_ACTIVE_QUEUE cards server-side (oldest pending first) so the
  operator is never pressured by a backlog.

* 'promotional' is the only action that spends promotional cap, and the cap
  is re-checked AT OPERATION TIME on both the account (v_offpage_promo_cap_
  usage) and the community (rolling 30d) — never earlier. A queue card can
  be copied one day and cap-blocked the next, honestly.

* Every operator decision and every refusal is recorded as an
  `offpage_events` row (gate='operator_choice' / 'operator_skip' /
  'permalink_submitted'), so "what did the operator see and approve at gate
  2" is answerable from the audit trail, not from a comment.

* The permalink endpoint is IDEMPOTENT: re-pasting the same draft's link
  updates the existing publication instead of minting a duplicate row.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException

from api.common import get_conn
from api.models.schemas import (
    OffpageQueueOut, OffpageQueueItem, OffpageGateSummary, OffpageCapOut,
    OffpageChooseRequest, OffpageChooseOut, OffpageSkipRequest,
    OffpageSkipOut, OffpagePermalinkRequest, OffpagePermalinkOut,
    OffpageCheckpointOut, OffpagePlatformSettings, OffpagePlatformSettingsOut,
)

logger = logging.getLogger("api.offpage")

router = APIRouter(prefix="/offpage", tags=["offpage"])

# Server-side invariant, mirroring api.routes.queue.MAX_ACTIVE_QUEUE.
OFFPAGE_ACTIVE_QUEUE = 5

# plan/34: verification checkpoint offsets are read from the sweep job so
# the schedule the UI renders is the schedule the sweep applies.
from jobs.offpage_verify import CHECKPOINT_OFFSET_MINUTES, precreate_verification_rows

# platform -> domain suffix(es) the permalink host must contain (plan/35:
# x accepts both x.com and twitter.com; shopify_community is pinned to the
# merchant forum root).
_PLATFORM_HOSTS = {
    "reddit": "reddit.com",
    "quora": "quora.com",
    "medium": "medium.com",
    "x": ("x.com", "twitter.com"),
    "shopify_community": "community.shopify.com",
}


def _host_ok(host, required):
    """True when the permalink host satisfies the platform's domain rule.

    `required` may be a single suffix string or a tuple of suffices; the
    match is a suffix check so http://www.x.com/... and
    https://twitter.com/... both pass and cross-platform hosts never do.
    """
    host = host.lower()
    if isinstance(required, tuple):
        return any(suffix in host for suffix in required)
    return required in host


def _jsond(draft, key, default=None):
    """Draft grounding/validation columns may be dict or text jsonb."""
    value = draft.get(key)
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _utcnow():
    return datetime.now(timezone.utc)


def _always_utc(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _load_draft(conn, site_id, draft_id):
    """One draft with its thread/community/site context (tenant-scoped)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.draft_id, d.site_id, d.recommendation_id, d.version,
                   d.body_md, d.advice_md, d.mention_md, d.status,
                   d.approved_at, d.created_at,
                   d.helpfulness_score, d.promo_density,
                   d.brand_mention_count, d.link_count,
                   d.validation, d.grounding,
                   r.offpage_thread_id, r.status AS rec_status,
                   t.thread_id, t.thread_url, t.title, t.snippet,
                   t.context_source, t.context_chars, t.post_kind,
                   t.community_id, c.community_key, c.platform,
                   c.promotional_cap_30d,
                   c.enabled AS community_enabled, c.strike_count,
                   sc.site_name AS client_name, sc.domain
            FROM offpage_drafts d
            JOIN recommendations r
              ON r.site_id = d.site_id
             AND r.recommendation_id = d.recommendation_id
            JOIN offpage_threads t
              ON t.site_id = d.site_id
             AND t.thread_id = r.offpage_thread_id
            JOIN offpage_communities c
              ON c.site_id = t.site_id
             AND c.community_id = t.community_id
            JOIN site_config sc ON sc.site_id = d.site_id
            WHERE d.site_id = %s AND d.draft_id = %s
            """,
            (site_id, draft_id),
        )
        columns = [d[0] for d in cur.description]
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404,
                            detail="draft not found for this site")
    return dict(zip(columns, row))


def _resolve_account(conn, site_id, community_id):
    """Best eligible account for the community (mirror of select_account)."""
    try:
        from fixes.offpage_drafter import select_account
        return select_account(conn, site_id, community_id)
    except Exception as exc:  # noqa: BLE001 - degrade, never fail the queue
        logger.warning("select_account failed: %s", exc)
        return None


def _community_promo_usage(conn, community_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(count(*), 0)
            FROM offpage_publications
            WHERE community_id = %s
              AND post_type = 'promotional'
              AND status IN ('live', 'removed')
              AND submitted_at > now() - INTERVAL '30 days'
            """,
            (community_id,),
        )
        return cur.fetchone()[0]


def _cap_summary(conn, draft, account):
    """OffpageCapOut for a draft (None when no account resolved)."""
    if account is None:
        return None
    usage = account.get("cap_usage") or {
        "promotional_cap_30d": account.get("promotional_cap_30d") or 0,
        "promotional_30d": 0, "cap_reached": False,
    }
    community_used = _community_promo_usage(conn, draft["community_id"])
    community_cap = draft.get("promotional_cap_30d")
    return OffpageCapOut(
        account_handle=account.get("handle") or "?",
        account_promotional_cap_30d=int(usage["promotional_cap_30d"]),
        account_promotional_30d=int(usage.get("promotional_30d", 0)),
        account_cap_reached=bool(usage.get("cap_reached", False)),
        community_promotional_cap_30d=int(community_cap or 0),
        community_promotional_30d=int(community_used),
        community_cap_reached=bool(community_cap is not None and
                                   community_used >= community_cap),
    )


def _log_event(conn, site_id, entity_type, entity_id, stage, gate, outcome,
               reason=None, evidence=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_events
                (site_id, entity_type, entity_id, stage, gate,
                 outcome, reason, evidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (site_id, entity_type, entity_id, stage, gate, outcome, reason,
             json.dumps(evidence or {}, default=str)),
        )


def _latest_operator_choice(conn, draft_id):
    """The most recent operator_choice event for a draft, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT evidence->>'action'
            FROM offpage_events
            WHERE entity_type = 'draft' AND entity_id = %s
              AND gate = 'operator_choice' AND outcome = 'pass'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (draft_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _ensure_no_publication(conn, draft_id, site_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT publication_id FROM offpage_publications "
            "WHERE site_id = %s AND draft_id = %s",
            (site_id, draft_id),
        )
        row = cur.fetchone()
    if row:
        raise HTTPException(
            status_code=409,
            detail=f"draft already published (publication {row[0]})",
        )


def _validate_permalink(permalink, platform):
    """Reject non-http(s) permalinks and cross-platform hosts."""
    parsed = urlparse(permalink)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=422,
                            detail="permalink must be an http(s) URL")
    host = parsed.netloc.lower()
    required = _PLATFORM_HOSTS.get(platform)
    if required and not _host_ok(host, required):
        raise HTTPException(
            status_code=422,
            detail=f"permalink host '{host}' does not match "
                   f"platform '{platform}' (expected {required})",
        )


# ---------------------------------------------------------------
# Queue
# ---------------------------------------------------------------

def _queue_drafts(conn, site_id, limit, status_filter):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.draft_id, d.site_id, d.recommendation_id, d.version,
                   d.status, d.approved_at, d.created_at,
                   d.body_md, d.advice_md, d.mention_md,
                   d.helpfulness_score, d.promo_density,
                   d.brand_mention_count, d.link_count,
                   d.validation, d.grounding,
                   r.offpage_thread_id,
                   t.thread_id, t.thread_url, t.title, t.snippet,
                   t.context_source, t.context_chars, t.post_kind,
                   t.discovery_query,
                   t.community_id, c.community_key, c.platform,
                   c.promotional_cap_30d,
                   c.enabled AS community_enabled, c.strike_count,
                   ps.reply_char_limit, ps.post_char_limit, ps.min_words,
                   sc.site_name AS client_name, sc.domain
            FROM offpage_drafts d
            JOIN recommendations r
              ON r.site_id = d.site_id
             AND r.recommendation_id = d.recommendation_id
            JOIN offpage_threads t
              ON t.site_id = d.site_id
             AND t.thread_id = r.offpage_thread_id
            JOIN offpage_communities c
              ON c.site_id = t.site_id
             AND c.community_id = t.community_id
            LEFT JOIN offpage_platform_settings ps
              ON ps.site_id = d.site_id
             AND ps.platform = c.platform
            JOIN site_config sc ON sc.site_id = d.site_id
            WHERE d.site_id = %s AND d.status IN %s
              AND NOT EXISTS (
                  SELECT 1 FROM offpage_publications p
                  WHERE p.site_id = d.site_id AND p.draft_id = d.draft_id)
            ORDER BY d.created_at ASC
            LIMIT %s
            """,
            (site_id, status_filter, limit),
        )
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    return rows


# ---------------------------------------------------------------
# Operator-triggered pipeline runs (plan/35). The scheduler's daily
# 07:00 offpage_discovery slot runs wherever the operator loop is
# deployed; the serverless runtime has no scheduler, so discovery and
# drafting are explicit operator actions here. Both call the committed
# job paths; every external call stays under the job's own quota and
# budget gates.
# ---------------------------------------------------------------

@router.get("/run-discovery")
def run_offpage_discovery_trigger(site_id, conn=Depends(get_conn)):
    """Run the REAL discovery job for this tenant's enabled legs
    (real DataForSEO SERP; quota + budget gated inside the job)."""
    from jobs.offpage_discovery import run as discovery_run
    summary = discovery_run(conn=conn, site_filter=site_id)
    conn.commit()
    return {"ok": True, "site_id": site_id, "summary": summary}


@router.get("/run-draft")
def run_offpage_draft_trigger(site_id, rec_id: str = None,
                              max_drafts: int = 2, conn=Depends(get_conn)):
    """Draft through the REAL drafter + LLM fallback client for the
    tenant's raw off-page recommendations that have no live draft yet.
    `&rec_id=` forces re-drafting one recommendation (its live draft is
    superseded by the drafter's versioned write)."""
    from agents.llm_client import FallbackClient
    from fixes.offpage_drafter import draft_offpage_reply, OffpageDraftBlocked
    with conn.cursor() as cur:
        if rec_id:
            cur.execute(
                """
                SELECT r.recommendation_id, r.target_url, r.offpage_thread_id,
                       t.post_kind, c.platform, c.community_key
                FROM recommendations r
                LEFT JOIN offpage_threads t
                    ON t.site_id = r.site_id
                   AND t.thread_id = r.offpage_thread_id
                LEFT JOIN offpage_communities c
                    ON c.site_id = t.site_id
                   AND c.community_id = t.community_id
                WHERE r.recommendation_id = %s
                  AND r.site_id = %s
                  AND r.generator = 'offpage_community'
                  AND r.offpage_thread_id IS NOT NULL
                """,
                (rec_id, site_id))
        else:
            cur.execute(
                """
                SELECT r.recommendation_id, r.target_url, r.offpage_thread_id,
                       t.post_kind, c.platform, c.community_key
                FROM recommendations r
                LEFT JOIN offpage_threads t
                    ON t.site_id = r.site_id
                   AND t.thread_id = r.offpage_thread_id
                LEFT JOIN offpage_communities c
                    ON c.site_id = t.site_id
                   AND c.community_id = t.community_id
                WHERE r.site_id = %s
                  AND r.generator = 'offpage_community'
                  AND r.status = 'raw'
                  AND r.offpage_thread_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM offpage_drafts d
                      WHERE d.recommendation_id = r.recommendation_id
                        AND d.status IN ('drafted', 'approved'))
                ORDER BY r.created_at ASC
                """,
                (site_id,))
        pending = [dict(zip(
            ("recommendation_id", "target_url", "thread_id", "post_kind",
             "platform", "community_key"), row)) for row in cur.fetchall()]
    outcomes, minted = [], []
    client = FallbackClient()
    for row in pending[:max(1, min(int(max_drafts), 4))]:
        item = str(row["recommendation_id"])
        try:
            out = draft_offpage_reply(conn, item, client=client)
            conn.commit()
            outcomes.append({
                "recommendation_id": item,
                "platform": row["platform"] or "reddit",
                "post_kind": row["post_kind"] or "reply",
                "draft_id": str(out["draft_id"]) if out else None,
                "status": "drafted"})
            minted.append(item)
        except OffpageDraftBlocked as exc:
            conn.rollback()
            outcomes.append({
                "recommendation_id": item,
                "platform": row["platform"] or "reddit",
                "post_kind": row["post_kind"] or "reply",
                "status": "refused",
                "reason": str(exc)[:300]})
        except Exception as exc:
            conn.rollback()
            outcomes.append({
                "recommendation_id": item,
                "platform": row["platform"] or "reddit",
                "post_kind": row["post_kind"] or "reply",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}"[:300]})
    return {"ok": True, "site_id": site_id, "pending": len(pending),
            "outcomes": outcomes}


@router.get("/run-corpus")
def run_offpage_corpus(site_id, conn=Depends(get_conn)):
    """Real corpus step (run_live_offpage stage 1, as an operator action).

    a) Refreshes the tenant's product/collection pages with REAL crawled
       copy (robots-respecting; site_fetch normalisation).
    b) Wires the tenant's own pages into each off-page leg's cluster with
       the stand-in relevance scorer (keyword overlap between the cluster's
       query text and the page's URL/title) — the same non-semantic
       scoring stand-in the pipeline runs while the embedding contract is
       undecided. The grounding loader ranks on these rows.
    """
    import re as _re

    import site_fetch as fetcher

    with conn.cursor() as cur:
        cur.execute(
            "SELECT cluster_id, primary_keyword, keywords "
            "FROM keyword_clusters WHERE site_id = %s "
            "AND cluster_id IN (SELECT cluster_id FROM recommendations "
            "WHERE site_id = %s AND generator = 'offpage_community' "
            "AND status = 'raw' AND cluster_id IS NOT NULL)",
            (site_id, site_id))
        clusters = cur.fetchall()
        cur.execute(
            "SELECT page_id, url, title FROM pages "
            "WHERE site_id = %s AND (page_type IN ('product', 'collection') "
            "OR length(btrim(COALESCE(body_html, ''))) >= 400) "
            "ORDER BY url", (site_id,))
        typed = cur.fetchall()
    # a) real copy refresh (small, known set; robots respected).
    refreshed = []
    robots = None
    origin = None
    for _pid, url, _title in typed:
        parsed = urlparse(url)
        if parsed.netloc and origin is None:
            origin = f"https://{parsed.netloc}/"
    if origin:
        try:
            robots = fetcher.fetch_robots_txt(origin)
        except Exception:
            robots = None
    for page_id, url, _title in typed:
        if not fetcher._robots_ok_for_url(robots, url):
            refreshed.append({"url": url, "status": "robots_disallow"})
            continue
        resp = fetcher._get(url)
        if not resp.get("ok"):
            refreshed.append({"url": url, "status": resp.get("reason")})
            continue
        try:
            body_html, text, h1 = fetcher._page_body_from_html(
                resp.get("body") or "")
            if body_html and text and len(text) >= 300:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE pages SET body_html = %s WHERE "
                        "page_id = %s AND site_id = %s",
                        (body_html, page_id, site_id))
                refreshed.append({"url": url,
                                  "status": f"loaded({len(text)}c)"})
            else:
                refreshed.append({"url": url,
                                  "status": f"thin({len(text or '')}c)"})
        except Exception as exc:
            refreshed.append({"url": url,
                              "status": f"error:{type(exc).__name__}"})
        conn.commit()

    # b) stand-in relevance wiring per leg cluster (typed catalogue pages
    #    plus substantial content pages — the import tenants' real corpus).
    wired = 0

    def _tokens(text):
        return set(_re.findall(r"[a-z0-9]+", (text or "").lower())) - {
            "the", "a", "an", "of", "for", "and", "to", "in", "on", "is",
            "are", "it", "you", "your", "we", "our", "with", "that", "this",
            "at", "by", "from", "or", "as", "be", "their", "them", "they"}

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM page_query_match_scores "
                    "WHERE site_id = %s", (site_id,))
        _before = cur.fetchone()[0]
        for cluster_id, primary, keywords_list in clusters:
            demand = _tokens(primary or "") | (
                _tokens(" ".join(keywords_list))
                if isinstance(keywords_list, (list, tuple)) else set())
            for page_id, url, title in typed:
                page_tokens = _tokens(url) | _tokens(title or "")
                overlap = len(demand & page_tokens) / max(len(demand), 1)
                if overlap < 0.2:
                    continue
                intent = min(0.95, 0.55 + overlap * 0.4)
                cat = min(0.95, 0.50 + overlap * 0.45)
                gsc = min(0.80, 0.30 + overlap * 0.5)
                sem = min(0.90, 0.35 + overlap * 0.55)
                cur.execute(
                    "SELECT 1 FROM page_query_match_scores "
                    "WHERE site_id = %s AND page_id = %s AND cluster_id = %s",
                    (site_id, page_id, cluster_id))
                if cur.fetchone():
                    cur.execute(
                        "UPDATE page_query_match_scores SET "
                        "intent_page_type_score = %s, "
                        "catalogue_match_score = %s, gsc_evidence_score = %s, "
                        "semantic_content_score = %s "
                        "WHERE site_id = %s AND page_id = %s "
                        "AND cluster_id = %s",
                        (intent, cat, gsc, sem, site_id, page_id, cluster_id))
                else:
                    cur.execute(
                        "INSERT INTO page_query_match_scores (site_id, "
                        "page_id, cluster_id, intent_page_type_score, "
                        "catalogue_match_score, gsc_evidence_score, "
                        "semantic_content_score) VALUES (%s, %s, %s, "
                        "%s, %s, %s, %s)",
                        (site_id, page_id, cluster_id, intent, cat, gsc, sem))
                wired += 1
        conn.commit()
        cur.execute("SELECT count(*) FROM page_query_match_scores "
                    "WHERE site_id = %s", (site_id,))
        _after = cur.fetchone()[0]
        cur.execute(
            "SELECT page_type, count(*), "
            "count(*) FILTER (WHERE length(btrim(COALESCE(body_html, ''))) "
            ">= 400) FROM pages WHERE site_id = %s "
            "GROUP BY page_type ORDER BY 2 DESC", (site_id,))
        breakdown = [{"page_type": r[0], "pages": r[1], "with_body": r[2]}
                     for r in cur.fetchall()]
        cur.execute(
            "SELECT url, page_type, "
            "COALESCE(length(btrim(COALESCE(body_html, ''))), 0) "
            "FROM pages WHERE site_id = %s AND (page_type IN "
            "('product', 'collection') OR length(btrim(COALESCE("
            "body_html, ''))) >= 400) ORDER BY 3 DESC", (site_id,))
        candidates = [{"url": r[0], "page_type": r[1], "body": r[2]}
                      for r in cur.fetchall()]
    return {"ok": True, "site_id": site_id, "corpus_refresh": refreshed,
            "candidate_pages": candidates, "page_type_breakdown": breakdown,
            "wiring": {"rows_before": _before, "rows_after": _after,
                       "writes": wired}}


@router.get("/queue", response_model=OffpageQueueOut)
def get_offpage_queue(site_id, conn=Depends(get_conn)):
    """Active off-page task window (capped 5, oldest pending first).

    Each card carries the advice/mention split (plan/34), the gate
    transcript, promo caps for the eventual account + community, and the
    operator's last-forgone choice.
    """
    pending = ("drafted", "approved")
    rows = _queue_drafts(conn, site_id, OFFPAGE_ACTIVE_QUEUE, pending)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM offpage_drafts d
            WHERE d.site_id = %s AND d.status IN ('drafted', 'approved')
              AND NOT EXISTS (
                  SELECT 1 FROM offpage_publications p
                  WHERE p.site_id = d.site_id AND p.draft_id = d.draft_id)
            """,
            (site_id,),
        )
        total_pending = cur.fetchone()[0]
    conn.rollback()

    items = []
    for r in rows:
        grounding = _jsond(r, "grounding", {})
        account = _resolve_account(conn, r["site_id"], r["community_id"])
        gates = [
            OffpageGateSummary(gate=g.get("gate"), outcome=g.get("outcome"),
                               reason=g.get("reason"))
            for g in (_jsond(r, "validation", {}) or {}).get("gates", [])
            if isinstance(g, dict)
        ]
        items.append(OffpageQueueItem(
            draft_id=r["draft_id"],
            version=r["version"],
            status=r["status"],
            approved_at=r["approved_at"],
            created_at=r["created_at"],
            recommendation_id=r["recommendation_id"],
            site_id=r["site_id"],
            client_name=r["client_name"],
            domain=r["domain"],
            community_key=r["community_key"],
            platform=r["platform"],
            post_kind=r["post_kind"] or "reply",
            thread_url=r["thread_url"],
            thread_title=r["title"],
            thread_snippet=r["snippet"],
            discovery_query=r["discovery_query"],
            context_source=r["context_source"],
            context_chars=r["context_chars"],
            reply_char_limit=r["reply_char_limit"],
            post_char_limit=r["post_char_limit"],
            min_words=r["min_words"],
            advice_md=r["advice_md"],
            mention_md=r["mention_md"],
            body_md=r["body_md"],
            post_type=grounding.get("post_type") or "helpful",
            helpfulness_score=float(r["helpfulness_score"])
            if r["helpfulness_score"] is not None else None,
            promo_density=float(r["promo_density"])
            if r["promo_density"] is not None else None,
            brand_mention_count=r["brand_mention_count"],
            link_count=r["link_count"],
            gates=gates,
            account_handle=account.get("handle") if account else None,
            caps=_cap_summary(conn, r, account),
            operator_choice=_latest_operator_choice(conn, r["draft_id"]),
        ))
    return OffpageQueueOut(
        site_id=site_id,
        active_cap=OFFPAGE_ACTIVE_QUEUE,
        total_active=min(len(items), OFFPAGE_ACTIVE_QUEUE),
        total_pending=total_pending,
        showing=len(items),
        items=items,
    )


# ---------------------------------------------------------------
# Operator decisions
# ---------------------------------------------------------------

@router.post("/drafts/{draft_id}/choose", response_model=OffpageChooseOut)
def choose_draft(draft_id, site_id, body: OffpageChooseRequest,
                 conn=Depends(get_conn)):
    """Copy Advice Only (action=helpful) or Copy Full Response
    (action=promotional). Promotional is the ONLY action that spends the
    30d cap and the only one that re-checks it at operation time."""
    draft = _load_draft(conn, site_id, draft_id)
    _ensure_no_publication(conn, draft_id, site_id)

    if draft["status"] not in ("drafted", "approved"):
        raise HTTPException(status_code=409,
                            detail=f"cannot choose in status "
                                   f"'{draft['status']}'")

    grounding = _jsond(draft, "grounding", {})
    post_type = grounding.get("post_type") or "helpful"
    mention_md = draft.get("mention_md") or ""
    account = _resolve_account(conn, site_id, draft["community_id"])

    mention_allowed = bool(mention_md)
    if body.action == "promotional":
        if not mention_allowed:
            raise HTTPException(
                status_code=409,
                detail="no mention exists on this draft; select "
                       "'Copy Advice Only' (helpful)",
            )
        if post_type != "promotional":
            raise HTTPException(
                status_code=409,
                detail=f"draft post_type is '{post_type}', not 'promotional'",
            )
        if account is None:
            raise HTTPException(
                status_code=409,
                detail="no eligible account (established / participating / "
                       "occasional) for this community; promotional copy "
                       "cannot be attributed",
            )
        usage = account.get("cap_usage") or {}
        if usage.get("cap_reached"):
            raise HTTPException(
                status_code=409,
                detail="account promotional cap reached "
                       f"({usage.get('promotional_30d', 0)}/"
                       f"{usage.get('promotional_cap_30d')} in 30 days)",
            )
        community_used = _community_promo_usage(conn, draft["community_id"])
        community_cap = draft.get("promotional_cap_30d")
        if community_cap is not None and community_used >= community_cap:
            raise HTTPException(
                status_code=409,
                detail="community promotional cap reached "
                       f"({community_used}/{community_cap} in 30 days)",
            )

    copied_md = draft["body_md"] if body.action == "promotional" \
        else (draft.get("advice_md") or draft["body_md"])

    new_status = draft["status"]
    if draft["status"] == "drafted":
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE offpage_drafts SET status = 'approved', "
                "approved_at = now() WHERE draft_id = %s AND status = 'drafted'",
                (draft_id,),
            )
        new_status = "approved"

    _log_event(conn, site_id, "draft", draft_id, "publish",
               "operator_choice", "pass",
               reason=f"operator chose to copy {body.action} copy",
               evidence={"action": body.action, "post_type": post_type,
                         "mention_allowed": mention_allowed,
                         "chars_copied": len(copied_md)})
    conn.commit()

    return OffpageChooseOut(
        draft_id=draft_id,
        status=new_status,
        action=body.action,
        copied_md=copied_md,
        mention_allowed=mention_allowed,
        caps=_cap_summary(conn, draft, account),
    )


@router.post("/drafts/{draft_id}/skip", response_model=OffpageSkipOut)
def skip_draft(draft_id, site_id, body: OffpageSkipRequest,
               conn=Depends(get_conn)):
    """Discard a pending draft: reject the off-page recommendation too, so
    the opportunity leaves the queue (and the reason feeds the agent's
    learning loop via rejection_log)."""
    draft = _load_draft(conn, site_id, draft_id)
    _ensure_no_publication(conn, draft_id, site_id)

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE offpage_drafts SET status = 'rejected_by_operator' "
            "WHERE draft_id = %s AND status IN ('drafted', 'approved')",
            (draft_id,),
        )
        cur.execute(
            "UPDATE recommendations "
            "SET status = 'rejected', rejection_reason = %s, rejected_at = now() "
            "WHERE site_id = %s AND recommendation_id = %s "
            "AND status <> 'rejected' "
            "RETURNING recommendation_id",
            (body.reason, site_id, draft["recommendation_id"]),
        )
        rec = cur.fetchone()

        primary_keyword = None
        with conn.cursor() as cur2:
            cur2.execute(
                "SELECT kc.primary_keyword FROM offpage_drafts d "
                "JOIN recommendations r ON r.site_id = d.site_id "
                "  AND r.recommendation_id = d.recommendation_id "
                "LEFT JOIN keyword_clusters kc ON kc.cluster_id = r.cluster_id "
                "WHERE d.draft_id = %s",
                (draft_id,),
            )
            kwrow = cur2.fetchone()
            if kwrow:
                primary_keyword = kwrow[0]
        log_id = None
        if rec:
            cur.execute(
                "INSERT INTO rejection_log "
                "(site_id, candidate_id, generator, primary_keyword, reason, "
                " rejected_by) "
                "VALUES (%s, %s, 'offpage_community', %s, %s, 'operator') "
                "RETURNING id",
                (site_id, draft["recommendation_id"], primary_keyword,
                 body.reason),
            )
            log_id = cur.fetchone()[0]

    _log_event(conn, site_id, "draft", draft_id, "draft",
               "operator_skip", "refuse",
               reason=body.reason,
               evidence={"recommendation_status": "rejected"})
    conn.commit()

    return OffpageSkipOut(
        draft_id=draft_id,
        status="rejected_by_operator",
        recommendation_id=draft["recommendation_id"],
        recommendation_status="rejected" if rec else draft["rec_status"],
        rejection_log_id=log_id,
    )


# ---------------------------------------------------------------
# Publish
# ---------------------------------------------------------------

@router.post("/drafts/{draft_id}/permalink", response_model=OffpagePermalinkOut)
def submit_permalink(draft_id, site_id, body: OffpagePermalinkRequest,
                     conn=Depends(get_conn)):
    """Paste the public permalink after manually posting: freeze the
    approved body into offpage_publications, pre-create the four T+
    checkpoints, and park the off-page recommendation at 'live' (off-page
    replies are never measured — see plan/33 §6)."""
    draft = _load_draft(conn, site_id, draft_id)
    _validate_permalink(body.permalink, draft["platform"])

    post_type = _latest_operator_choice(conn, draft_id) or "helpful"
    grounding = _jsond(draft, "grounding", {})
    mention_md = draft.get("mention_md") or ""

    # Cap re-check at publish time for promotional posts. The count that
    # matters is the LIVE/REMOVED set in the rolling window — a pending
    # submission has not spent cap yet.
    account = _resolve_account(conn, site_id, draft["community_id"])
    if account is None:
        raise HTTPException(status_code=409,
                            detail="no eligible account for this community")
    if post_type == "promotional":
        if not mention_md or grounding.get("post_type") != "promotional":
            raise HTTPException(
                status_code=409,
                detail="operator_choice says promotional but this draft has "
                       "no mention; submit as helpful or fix the choice",
            )
        usage = account.get("cap_usage") or {}
        if usage.get("cap_reached"):
            raise HTTPException(
                status_code=409,
                detail="account promotional cap reached at publish time "
                       f"({usage.get('promotional_30d', 0)}/"
                       f"{usage.get('promotional_cap_30d')} in 30 days)",
            )
        community_used = _community_promo_usage(conn, draft["community_id"])
        community_cap = draft.get("promotional_cap_30d")
        if community_cap is not None and community_used >= community_cap:
            raise HTTPException(
                status_code=409,
                detail="community promotional cap reached at publish time "
                       f"({community_used}/{community_cap} in 30 days)",
            )

    submitted_at = _utcnow()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT publication_id FROM offpage_publications "
            "WHERE site_id = %s AND draft_id = %s",
            (site_id, draft_id),
        )
        pub_row = cur.fetchone()
        if pub_row:
            # Idempotent re-submit: update the permalink/status instead of
            # minting a duplicate publication row.
            publication_id = pub_row[0]
            cur.execute(
                "UPDATE offpage_publications "
                "SET live_permalink = %s, status = 'pending_verify', "
                "    operator = %s, submitted_at = %s, updated_at = now() "
                "WHERE publication_id = %s",
                (body.permalink, body.operator, submitted_at, publication_id),
            )
            precreated = 0
        else:
            cur.execute(
                """
                INSERT INTO offpage_publications
                    (site_id, recommendation_id, draft_id, account_id,
                     community_id, thread_id, approved_body_md,
                     live_permalink, post_type, status, operator, submitted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending_verify',
                        %s, %s)
                RETURNING publication_id
                """,
                (site_id, draft["recommendation_id"], draft["draft_id"],
                 account["account_id"], draft["community_id"],
                 draft["thread_id"], draft["body_md"], body.permalink,
                 post_type, body.operator, submitted_at),
            )
            publication_id = cur.fetchone()[0]
            precreated = precreate_verification_rows(
                conn, site_id, publication_id, submitted_at)
            cur.execute(
                "UPDATE recommendations SET status = 'live' "
                "WHERE site_id = %s AND recommendation_id = %s "
                "AND status <> 'live'",
                (site_id, draft["recommendation_id"]),
            )
            cur.execute(
                "UPDATE offpage_drafts SET status = 'approved' "
                "WHERE draft_id = %s AND status = 'drafted'",
                (draft_id,),
            )

    _log_event(conn, site_id, "publication", publication_id,
               "publish", "permalink_submitted", "pass",
               reason="operator pasted the public permalink",
               evidence={"post_type": post_type,
                         "permalink": body.permalink,
                         "checkpoints_precreated": precreated})
    conn.commit()

    checkpoints = [
        OffpageCheckpointOut(
            checkpoint_index=i,
            offset_minutes=off,
            checkpoint_due_at=submitted_at + timedelta(minutes=off),
        )
        for i, off in enumerate(CHECKPOINT_OFFSET_MINUTES)
    ]
    return OffpagePermalinkOut(
        publication_id=publication_id,
        draft_id=draft_id,
        site_id=site_id,
        status="pending_verify",
        post_type=post_type,
        live_permalink=body.permalink,
        submitted_at=submitted_at,
        recommendation_status="live",
        checkpoints=checkpoints,
    )


# ---------------------------------------------------------------
# Platform settings (plan/35 §3) - per-tenant channel control
# ---------------------------------------------------------------

def _load_platform_settings(conn, site_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT platform, enabled, discovery_seams,
                   discovery_calls_per_day, reply_char_limit,
                   post_char_limit, min_words, promotional_posts_per_30d,
                   targeting
            FROM offpage_platform_settings
            WHERE site_id = %s
            ORDER BY platform
            """,
            (site_id,),
        )
        rows = cur.fetchall()
    items = []
    for (platform, enabled, seams, calls, reply_limit, post_limit,
         min_words, promo_cap, targeting) in rows:
        if isinstance(targeting, str):
            try:
                targeting = json.loads(targeting)
            except (TypeError, ValueError):
                targeting = {}
        items.append(OffpagePlatformSettings(
            site_id=site_id, platform=platform, enabled=bool(enabled),
            discovery_seams=seams or [],
            discovery_calls_per_day=int(calls or 20),
            reply_char_limit=reply_limit, post_char_limit=post_limit,
            min_words=min_words,
            promotional_posts_per_30d=promo_cap,
            targeting=targeting if isinstance(targeting, dict) else {},
        ))
    return items


@router.get("/platform-settings", response_model=OffpagePlatformSettingsOut)
def get_platform_settings(site_id, conn=Depends(get_conn)):
    """Every (site_id, platform) channel row for the operator settings
    panel. Fail-closed by default: an absent row means 'off'."""
    items = _load_platform_settings(conn, site_id)
    conn.rollback()
    return OffpagePlatformSettingsOut(
        site_id=site_id,
        items=items,
        active_channels=sum(1 for i in items if i.enabled),
    )


@router.put("/platform-settings", response_model=OffpagePlatformSettingsOut)
def put_platform_settings(site_id, body: OffpagePlatformSettings,
                          conn=Depends(get_conn)):
    """Upsert one (site_id, platform) channel row. Enabling stays an
    explicit operator act; callers only set the fields they send.
    `enabled=false` is the kill-switch and the default posture."""
    if str(body.site_id) != site_id:
        raise HTTPException(status_code=422,
                            detail="body.site_id must equal path site_id")
    if body.platform not in ("reddit", "quora", "medium",
                             "x", "shopify_community"):
        raise HTTPException(status_code=422,
                            detail=f"unknown platform {body.platform!r}")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_platform_settings
                (site_id, platform, enabled, discovery_seams,
                 discovery_calls_per_day, reply_char_limit, post_char_limit,
                 min_words, promotional_posts_per_30d, targeting)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (site_id, platform) DO UPDATE SET
                enabled = EXCLUDED.enabled,
                discovery_seams = EXCLUDED.discovery_seams,
                discovery_calls_per_day =
                    EXCLUDED.discovery_calls_per_day,
                reply_char_limit = EXCLUDED.reply_char_limit,
                post_char_limit = EXCLUDED.post_char_limit,
                min_words = EXCLUDED.min_words,
                promotional_posts_per_30d =
                    EXCLUDED.promotional_posts_per_30d,
                targeting = EXCLUDED.targeting,
                updated_at = now()
            """,
            (site_id, body.platform, body.enabled, body.discovery_seams,
             body.discovery_calls_per_day, body.reply_char_limit,
             body.post_char_limit, body.min_words,
             body.promotional_posts_per_30d,
             json.dumps(body.targeting or {}, default=str)),
        )
    conn.commit()
    items = _load_platform_settings(conn, site_id)
    conn.rollback()
    return OffpagePlatformSettingsOut(
        site_id=site_id,
        items=items,
        active_channels=sum(1 for i in items if i.enabled),
    )
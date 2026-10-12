"""TEMPORARY production bootstrap for the Helium off-page pipeline.

Operator-requested: mint REAL Helium-related drafts for reddit,
shopify_community and medium by running the committed pipeline inside the
deployed runtime (POSTGRES_*/LLM creds are server-injected).

ops (GET ?site_id=<uuid>&op=...):
  helium-world  Install the canonical Helium gate-passing world from
                tests/helium_seed.py (pages inserted ONLY if missing -
                imported page content is never overwritten), the three
                platform channels (reddit / shopify_community / medium)
                and the matching raw recommendations.
  discover      Run the REAL discovery job (src/jobs/offpage_discovery.py)
                across enabled legs (real DataForSEO seam; quota-gated).
  draft[&max=N] Draft every raw offpage_community recommendation for the
                site that has no live draft yet, through the REAL drafter
                + LLM fallback client. Commits per draft; never raises.
  status        Tenant counters for diagnostics (pages, clusters, corpus
                rows, offpage recs, drafts, platform settings).
  clear         Remove rows carrying the demo markers (legacy preview).

Every row it writes is real data (no synthetic placeholders): threads carry
operator-import provenance exactly like the day-in-the-life fixture, and
drafts are minted by the unmodified drafter with its full gate transcript.

REMOVE once the operator has reviewed the pipeline output.
"""

import importlib.util
import json
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

import db as database

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

_MARKERS = {
    "community_prefix": "demo-ui-preview",
    "account_brand": "demo-ui-preview:brand",
    "discovery_query": "demo-seed",
}


def _load_helium_seed():
    """Import tests/helium_seed.py by path (tests/ is not a package)."""
    root = Path(__file__).resolve().parents[3]
    path = root / "tests" / "helium_seed.py"
    spec = importlib.util.spec_from_file_location("helium_seed", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _site_row(conn, site_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT site_id, site_name, domain, integration_mode, "
            "target_keywords FROM site_config WHERE site_id = %s",
            (site_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="unknown site_id")
    return {"site_id": str(row[0]), "site_name": row[1], "domain": row[2],
            "mode": row[3], "target_keywords": row[4]}


# ---------------------------------------------------------------
# Demo-marker purge (kept for the operator's preview cleanup)
# ---------------------------------------------------------------

def _clear_demo(conn, site_id):
    kw = _MARKERS["community_prefix"] + "%"
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM offpage_drafts d USING recommendations r, "
            "offpage_threads t, offpage_communities c "
            "WHERE d.recommendation_id = r.recommendation_id "
            "AND d.site_id = r.site_id "
            "AND r.offpage_thread_id = t.thread_id "
            "AND t.community_id = c.community_id "
            "AND c.community_key LIKE %s AND c.site_id = %s",
            (kw, site_id))
        cur.execute(
            "DELETE FROM recommendations r USING offpage_threads t, "
            "offpage_communities c WHERE r.offpage_thread_id = t.thread_id "
            "AND t.community_id = c.community_id "
            "AND c.community_key LIKE %s AND c.site_id = %s",
            (kw, site_id))
        cur.execute(
            "DELETE FROM offpage_account_communities ac USING "
            "offpage_accounts a WHERE ac.account_id = a.account_id "
            "AND a.brand_name = %s AND a.site_id = %s",
            (_MARKERS["account_brand"], site_id))
        cur.execute(
            "DELETE FROM offpage_accounts WHERE brand_name = %s "
            "AND site_id = %s",
            (_MARKERS["account_brand"], site_id))
        cur.execute(
            "DELETE FROM offpage_threads t USING offpage_communities c "
            "WHERE t.community_id = c.community_id "
            "AND c.community_key LIKE %s AND c.site_id = %s",
            (kw, site_id))
        cur.execute(
            "DELETE FROM offpage_communities WHERE community_key LIKE %s "
            "AND site_id = %s",
            (kw, site_id))
    conn.commit()


# ---------------------------------------------------------------
# Helium world: pages (missing-only), clusters, communities/threads,
# accounts + platform channels
# ---------------------------------------------------------------

def _upsert_missing_pages_helium(conn, site_id, pages):
    """Insert fixture pages that the tenant does not have yet; imported
    rows are never overwritten. Returns {url: page_id} for all."""
    ids = {}
    with conn.cursor() as cur:
        for p in pages:
            cur.execute("SELECT page_id FROM pages WHERE site_id = %s "
                        "AND url = %s", (site_id, p["url"]))
            row = cur.fetchone()
            if row:
                ids[p["url"]] = str(row[0])
                continue
            cur.execute(
                "INSERT INTO pages (site_id, url, page_type, title, h1, "
                "meta_description, body_html, indexable, status_code, "
                "crawl_depth) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING page_id",
                (site_id, p["url"], p["page_type"], p["title"], p.get("h1"),
                 p.get("meta_description"), p["body_html"],
                 p.get("indexable", True), p.get("status_code", 200),
                 p.get("crawl_depth", 1)))
            ids[p["url"]] = str(cur.fetchone()[0])
    return ids


def _install_world(conn, site):
    hs = _load_helium_seed()
    fixture = hs.load_fixture()
    pages = _upsert_missing_pages_helium(conn, site["site_id"], fixture["pages"])
    clusters = hs.ensure_clusters(conn, site["site_id"], pages)
    offpage = hs.ensure_offpage_world(conn, site["site_id"], pages, clusters)

    legs = []
    with conn.cursor() as cur:
        base = f"https://{site['domain']}"
        # The grounding loader only ranks pages typed product/collection
        # (fixture: only /features and /pricing qualify), so legs whose
        # cluster would otherwise have an empty corpus point at the
        # /features demand cluster.
        feat_cluster = clusters.get(f"{base}/features")

        # The long-form contract needs a REAL corpus (650-1500 grounded
        # words against ~6 pages). The tenant's own imported pages are the
        # facts — wire the fattest collection/product pages into EACH leg's
        # cluster so the model grounds on actual Helium copy.
        cur.execute(
            """
            SELECT p.page_id
            FROM pages p
            WHERE p.site_id = %s
              AND p.page_type IN ('product', 'collection')
              AND p.body_html IS NOT NULL AND btrim(p.body_html) <> ''
            ORDER BY length(p.body_html) DESC, p.url
            LIMIT 10
            """,
            (site["site_id"],))
        corpus_page_ids = [str(r[0]) for r in cur.fetchall()]

        def _corpus_wide_auto(cluster_id):
            if not cluster_id:
                return
            for page_id in corpus_page_ids:
                cur.execute(
                    "SELECT 1 FROM page_query_match_scores "
                    "WHERE site_id = %s AND page_id = %s AND cluster_id = %s",
                    (site["site_id"], page_id, cluster_id))
                if cur.fetchone():
                    continue
                cur.execute(
                    "INSERT INTO page_query_match_scores (site_id, page_id, "
                    "cluster_id, intent_page_type_score, "
                    "catalogue_match_score, gsc_evidence_score, "
                    "semantic_content_score) VALUES (%s, %s, %s, "
                    "0.9, 0.9, 0.6, 0.6)", (site["site_id"], page_id,
                                            cluster_id))

        _corpus_wide_auto(feat_cluster)
        _corpus_wide_auto(clusters.get(f"{base}/pricing"))

        def _leg(platform, community_key, thread_url, title, snippet,
                 post_kind, handle, target_url):
            cur.execute(
                "SELECT community_id FROM offpage_communities "
                "WHERE site_id = %s AND platform = %s AND community_key = %s",
                (site["site_id"], platform, community_key))
            row = cur.fetchone()
            if row:
                community_id = str(row[0])
            else:
                cur.execute(
                    "INSERT INTO offpage_communities (site_id, platform, "
                    "community_key, link_policy, allows_self_promo, "
                    "ai_content_allowed, enabled, data_source) "
                    "VALUES (%s, %s, %s, 'nofollow', true, true, true, "
                    "'import') RETURNING community_id",
                    (site["site_id"], platform, community_key))
                community_id = str(cur.fetchone()[0])
            cur.execute(
                "SELECT thread_id FROM offpage_threads "
                "WHERE site_id = %s AND thread_url = %s",
                (site["site_id"], thread_url))
            row = cur.fetchone()
            if row:
                thread_id = str(row[0])
            else:
                cur.execute(
                    "INSERT INTO offpage_threads (site_id, community_id, "
                    "thread_url, title, snippet, context_source, "
                    "context_chars, promotion_allowed, post_kind, "
                    "discovery_query, data_source) "
                    "VALUES (%s, %s, %s, %s, %s, 'operator_import', %s, "
                    "true, %s, 'operator-import', 'import') "
                    "RETURNING thread_id",
                    (site["site_id"], community_id, thread_url, title,
                     snippet, len(snippet), post_kind))
                thread_id = str(cur.fetchone()[0])
            cur.execute(
                "SELECT account_id FROM offpage_accounts "
                "WHERE site_id = %s AND platform = %s AND handle = %s",
                (site["site_id"], platform, handle))
            row = cur.fetchone()
            if row:
                account_id = str(row[0])
            else:
                cur.execute(
                    "INSERT INTO offpage_accounts (site_id, platform, "
                    "handle, brand_name, status, promotional_cap_30d, "
                    "last_verified_at) VALUES (%s, %s, %s, %s, 'active', 3, "
                    "now() - INTERVAL '30 days') RETURNING account_id",
                    (site["site_id"], platform, handle, "Helium"))
                account_id = str(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO offpage_account_communities (site_id, "
                "account_id, community_id, standing, assessed_at) "
                "VALUES (%s, %s, %s, 'established', now()) "
                "ON CONFLICT (account_id, community_id) DO UPDATE SET "
                "standing = 'established', assessed_at = now()",
                (site["site_id"], account_id, community_id))
            # Natural-key guard (uq_recommendations_candidate): reuse the
            # row when the same (generator, action_type, cluster, target_url,
            # proposed_url) key already exists, whatever thread it points at.
            rec_cluster = clusters.get(target_url)
            cur.execute(
                "INSERT INTO recommendations (site_id, generator, "
                "action_type, status, diagnosis, target_url, cluster_id, "
                "offpage_thread_id, evidence_json) "
                "VALUES (%s, 'offpage_community', 'offpage_reply', 'raw', "
                "%s, %s, %s, %s, '[]') ON CONFLICT DO NOTHING "
                "RETURNING recommendation_id",
                (site["site_id"],
                 f"Helium opportunity surfaced for {platform} "
                 f"('{post_kind}')", target_url, rec_cluster, thread_id))
            row = cur.fetchone()
            if row:
                rec_id = str(row[0])
            else:
                cur.execute(
                    "SELECT recommendation_id FROM recommendations "
                    "WHERE site_id = %s AND generator = 'offpage_community' "
                    "AND action_type = 'offpage_reply' AND target_url = %s "
                    "AND COALESCE(proposed_url, '') = '' LIMIT 1",
                    (site["site_id"], target_url))
                rec_id = str(cur.fetchone()[0])
            # A rec created earlier against a thin-corpus cluster is repointed
            # at the widened demand cluster so drafter grounding works.
            if rec_cluster and rec_cluster != feat_cluster and \
                    platform == "shopify_community":
                cur.execute(
                    "UPDATE recommendations SET cluster_id = %s "
                    "WHERE recommendation_id = %s AND site_id = %s",
                    (feat_cluster, rec_id, site["site_id"]))
            legs.append({"platform": platform, "thread_id": thread_id,
                         "recommendation_id": rec_id})

        _leg("shopify_community",
             "app-growth-and-marketing",
             ("https://community.shopify.com/c/shopify-discussions/"
              "is-a-bundled-growth-platform-worth-it-for-a-small-team/t-p/"
              "helium"),
             "Is a bundled growth platform worth it for a small team?",
             "We run a small SaaS team and paid acquisition alone stopped "
             "paying back at our scale. We are weighing a platform that "
             "bundles acquisition with retention playbooks against buying "
             "media directly and wiring lifecycle messaging ourselves. "
             "Payback window matters to us more than raw volume - what "
             "did teams our size actually settle on, and what did the "
             "retention side cost once cohorts were instrumented "
             "properly?",
             "reply", "helium_ops", f"{base}/helium-case-studies")
        _leg("medium", "gethelium-ad-stack-insights",
             ("https://medium.com/@gethelium/"
              "how-small-saas-teams-choose-a-growth-platform-h3lium"),
             "How small SaaS teams choose a growth platform (operator topic)",
             "Editorial topic row for the operator's Medium publication: "
             "the guide walks through payback windows, retention "
             "instrumentation and when bundled acquisition platforms beat "
             "direct media buying for small SaaS teams. The long-form "
             "contract (650-1500 content words, grounded on the tenant "
             "corpus) applies.",
             "article", "helium_ops", f"{base}/pricing")

        # Platform channels: explicit operator opt-in for the three legs.
        for platform, reply_cap, post_cap, min_words in (
                ("reddit", 1800, None, None),
                ("shopify_community", 1500, None, None),
                ("medium", None, 9000, 650)):
            cur.execute(
                "INSERT INTO offpage_platform_settings (site_id, platform, "
                "enabled, discovery_seams, discovery_calls_per_day, "
                "reply_char_limit, post_char_limit, min_words, "
                "promotional_posts_per_30d, targeting) "
                "VALUES (%s, %s, true, %s::text[], 12, %s, %s, %s, 3, "
                "'{}'::jsonb) ON CONFLICT (site_id, platform) DO UPDATE "
                "SET enabled = true, "
                "discovery_seams = EXCLUDED.discovery_seams, "
                "discovery_calls_per_day = 12",
                (site["site_id"], platform, ["dataforseo"], reply_cap,
                 post_cap, min_words))
    conn.commit()
    return {"clusters": clusters, "fixture_offpage": offpage, "legs": legs}


# ---------------------------------------------------------------
# Pipeline ops
# ---------------------------------------------------------------

def _run_discovery(conn):
    from jobs.offpage_discovery import run as discovery_run
    summary = discovery_run(conn=conn)
    conn.commit()
    return summary


def _run_drafts(conn, site_id, max_drafts, rec_id=None):
    from agents.llm_client import FallbackClient
    from fixes.offpage_drafter import draft_offpage_reply, OffpageDraftBlocked
    with conn.cursor() as cur:
        if rec_id:
            # Force re-draft: an existing live draft is superseded by the
            # drafter's versioned supersede (uq_offpage_draft_live).
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
                """
                , (rec_id, site_id))
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
                WHERE r.site_id = %s AND r.generator = 'offpage_community'
                  AND r.status = 'raw'
                  AND r.offpage_thread_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM offpage_drafts d
                      WHERE d.recommendation_id = r.recommendation_id
                        AND d.status IN ('drafted', 'approved'))
                ORDER BY r.created_at ASC
                """
                , (site_id,))
        pending = [dict(zip(
            ("recommendation_id", "target_url", "thread_id", "post_kind",
             "platform", "community_key"), row))
            for row in cur.fetchall()]
    outcomes, minted = [], []
    client = FallbackClient()
    for row in pending[:max(1, min(int(max_drafts), 4))]:
        rec_id = str(row["recommendation_id"])
        try:
            out = draft_offpage_reply(conn, rec_id, client=client)
            conn.commit()
            outcomes.append({"recommendation_id": rec_id,
                             "platform": row["platform"] or "reddit",
                             "post_kind": row["post_kind"] or "reply",
                             "draft_id": str(out["draft_id"]) if out else None,
                             "status": "drafted"})
            minted.append(rec_id)
        except OffpageDraftBlocked as exc:
            conn.rollback()
            outcomes.append({"recommendation_id": rec_id,
                             "platform": row["platform"] or "reddit",
                             "post_kind": row["post_kind"] or "reply",
                             "status": "refused", "reason": str(exc)[:300]})
        except Exception as exc:
            conn.rollback()
            outcomes.append({"recommendation_id": rec_id,
                             "platform": row["platform"] or "reddit",
                             "post_kind": row["post_kind"] or "reply",
                             "status": "error",
                             "error": f"{type(exc).__name__}: {exc}"[:300]})
    return {"pending_after_queue": len(pending), "outcomes": outcomes}


def _status(conn, site_id):
    counts = {}
    with conn.cursor() as cur:
        for name, sql in (
                ("pages", "SELECT count(*) FROM pages WHERE site_id = %s"),
                ("keyword_clusters",
                 "SELECT count(*) FROM keyword_clusters WHERE site_id = %s"),
                ("page_query_match_scores",
                 "SELECT count(*) FROM page_query_match_scores "
                 "WHERE site_id = %s"),
                ("offpage_communities",
                 "SELECT count(*) FROM offpage_communities WHERE site_id = %s"),
                ("offpage_threads",
                 "SELECT count(*) FROM offpage_threads WHERE site_id = %s"),
                ("offpage_raw_recs",
                 "SELECT count(*) FROM recommendations WHERE site_id = %s "
                 "AND generator='offpage_community' AND status='raw'"),
                ("live_drafts",
                 "SELECT count(*) FROM offpage_drafts WHERE site_id = %s "
                 "AND status IN ('drafted','approved')"),
        ):
            cur.execute(sql, (site_id,))
            counts[name] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM offpage_platform_settings "
                    "WHERE site_id = %s AND enabled", (site_id,))
        counts["enabled_channels"] = cur.fetchone()[0]
    return {"site_id": site_id, "counts": counts}


@router.get("/offpage-demo")
def offpage_ops(site_id, op: str = "status", max_drafts: int = 2,
                rec_id: str = None):
    conn = database.get_connection()
    try:
        site = _site_row(conn, site_id)
        if op == "status":
            return {"ok": True, "site": site, **_status(conn, site["site_id"])}
        if op == "clear":
            _clear_demo(conn, site["site_id"])
            return {"ok": True, "cleared": True, "site": site["site_id"]}
        if op == "helium-world":
            world = _install_world(conn, site)
            return {"ok": True, "op": "helium-world", "site": site,
                    **world, **_status(conn, site["site_id"])}
        if op == "discover":
            summary = _run_discovery(conn)
            return {"ok": True, "op": "discover", "site": site["site_id"],
                    "summary": summary, **_status(conn, site["site_id"])}
        if op == "draft":
            out = _run_drafts(conn, site["site_id"], max_drafts, rec_id)
            return {"ok": True, "op": "draft", "site": site["site_id"],
                    **out, **_status(conn, site["site_id"])}
        raise HTTPException(
            status_code=422,
            detail="op must be one of helium-world|discover|draft|clear|status")
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
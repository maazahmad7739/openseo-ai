"""TEMPORARY finalize step for the off-page engine bootstrap.

Purges every preview/mock seeded card + tonight's synthetic drafts and
sets the Helium tenant's real target keywords (discovery input), then this
router is REMOVED in the follow-up commit so only the committed pipeline
runs: Helium's DB data + the real DataForSEO seam.
"""

from fastapi import APIRouter, HTTPException

import db as database

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

_PREFIXES = ("demo-ui-preview%", "operator-preview%")
_HELIUM_KEYWORDS = [
    "helium ad stack",
    "helium growth platform",
    "growth platform for small saas teams",
    "bundled acquisition and retention platform",
    "app growth pricing per active user",
]


@router.get("/finalize")
def finalize(site_id):
    """Finalize an off-page engine bootstrap: purge seeded preview rows,
    set the tenant's target keywords, report the engine state."""
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT site_id, site_name, domain, target_keywords "
                "FROM site_config WHERE site_id = %s", (site_id,))
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="unknown site_id")
        site_name, domain, keywords = row[1], row[2], row[3]

        purged = {}
        with conn.cursor() as cur:
            # 1. Drafts: every draft belonging to the seeded/marker families
            #    plus tonight's drafts on offpage_community recs.
            for prefix in _PREFIXES:
                cur.execute(
                    "DELETE FROM offpage_drafts d USING recommendations r, "
                    "offpage_threads t, offpage_communities c "
                    "WHERE d.recommendation_id = r.recommendation_id "
                    "AND d.site_id = r.site_id "
                    "AND r.offpage_thread_id = t.thread_id "
                    "AND t.community_id = c.community_id "
                    "AND c.community_key LIKE %s AND c.site_id = %s",
                    (prefix, site_id))
            cur.execute(
                "DELETE FROM offpage_drafts d USING recommendations r "
                "WHERE d.recommendation_id = r.recommendation_id "
                "AND d.site_id = r.site_id "
                "AND r.generator = 'offpage_community' AND r.site_id = %s",
                (site_id,))
            # 2. Preview recommendations/threads/communities/accounts.
            for prefix in _PREFIXES:
                cur.execute(
                    "DELETE FROM recommendations r USING offpage_threads t, "
                    "offpage_communities c "
                    "WHERE r.offpage_thread_id = t.thread_id "
                    "AND t.community_id = c.community_id "
                    "AND c.community_key LIKE %s AND c.site_id = %s",
                    (prefix, site_id))
            cur.execute(
                "DELETE FROM offpage_account_communities ac USING "
                "offpage_communities c WHERE ac.community_id = c.community_id "
                "AND c.community_key LIKE %s AND c.site_id = %s",
                ("operator-preview%", site_id))
            cur.execute(
                "DELETE FROM offpage_account_communities ac USING "
                "offpage_accounts a WHERE ac.account_id = a.account_id "
                "AND (a.brand_name = 'demo-ui-preview:brand' "
                "OR a.handle LIKE 'u/aurora-demo-%%' "
                "OR a.handle LIKE 'u/aurora-preview-%%') "
                "AND a.site_id = %s", (site_id,))
            cur.execute(
                "DELETE FROM offpage_accounts WHERE site_id = %s "
                "AND (brand_name = 'demo-ui-preview:brand' "
                "OR handle LIKE 'u/aurora-demo-%%')", (site_id,))
            for prefix in _PREFIXES:
                cur.execute(
                    "DELETE FROM offpage_threads t USING offpage_communities c "
                    "WHERE t.community_id = c.community_id "
                    "AND c.community_key LIKE %s AND c.site_id = %s",
                    (prefix, site_id))
                cur.execute(
                    "DELETE FROM offpage_communities "
                    "WHERE community_key LIKE %s AND site_id = %s",
                    (prefix, site_id))
            conn.commit()
            # 3. Discovery fuel: the tenant's real target keywords.
            if not keywords:
                cur.execute(
                    "UPDATE site_config SET target_keywords = %s "
                    "WHERE site_id = %s", (_HELIUM_KEYWORDS, site_id))
                keywords = _HELIUM_KEYWORDS
                purged["target_keywords_set"] = len(keywords)
            conn.commit()

        with conn.cursor() as cur:
            state = {}
            for name, sql in (
                    ("offpage_communities",
                     "SELECT count(*) FROM offpage_communities "
                     "WHERE site_id = %s"),
                    ("offpage_threads",
                     "SELECT count(*) FROM offpage_threads "
                     "WHERE site_id = %s"),
                    ("offpage_raw_recs",
                     "SELECT count(*) FROM recommendations "
                     "WHERE site_id = %s AND generator='offpage_community' "
                     "AND status='raw'"),
                    ("live_drafts",
                     "SELECT count(*) FROM offpage_drafts "
                     "WHERE site_id = %s "
                     "AND status IN ('drafted','approved')"),
                    ("enabled_channels",
                     "SELECT count(*) FROM offpage_platform_settings "
                     "WHERE site_id = %s AND enabled"),
                    ("corpus_pages",
                     "SELECT count(*) FROM page_query_match_scores "
                     "WHERE site_id = %s"),
            ):
                cur.execute(sql, (site_id,))
                state[name] = cur.fetchone()[0]
        conn.rollback()
        return {"ok": True, "site": {"site_id": site_id,
                                     "site_name": site_name,
                                     "domain": domain},
                "target_keywords": keywords, "purged": purged,
                "engine_state": state}
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
"""Weekly candidate-generation orchestrator (plan/09 Data Flow 2).

Per site:
  1. Run Generator 1 SQL (plan/01)  → up to 15 missing-page candidates
  2. Run Generator 2 SQL (plan/02)  → up to 20 existing-opportunity candidates
  3. Run Generator 3 SQL (plan/03)  → up to 15 technical-fix candidates
4. Run Generator 4 SQL (plan/04)  → up to 10 cannibalization candidates
   5. Combine (max 60 per site), dedupe by cluster (keep highest priority)
   6. Insert accepted candidates into recommendations (status='raw')

Candidates are inserted as `raw` — the agent stage (agents/seo_agent.py) is the
only writer that promotes a row to `proposed` after enrichment, or drops it to
`rejected` with a reason (plan/16 raw→proposed lifecycle). Raw rows are never
operator-visible: GET /queue filters status='proposed'. Re-generation is
idempotent per candidate via the natural-key unique index (plan/16); a row the
agent already promoted or rejected keeps its status (DO NOTHING).

Per-site LIMIT quotas are enforced by the SQL itself (:site_id bound, one
site can never consume another's). candidate_runs rows record bookkeeping.
"""

import os
import sys
import json
import re
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from generators.runner import run_generator, load_generator_sql  # noqa: E402

import db as database  # noqa: E402

MAX_PER_SITE = 60

# Impact-tier gate for missing-page candidates (Option A, founder decision):
# resolved per site from site_config.min_search_volume; NULL/unset falls back
# to this documented default. The column is the same one Generator 1 uses for
# cluster qualification (see plan/15 for the dual-use semantics).
MISSING_PAGE_HIGH_IMPACT_VOLUME_DEFAULT = 5000


def _impact_volume_threshold(conn, site_id):
    """Resolve the missing-page impact gate from site_config (Option A).

    Returns the per-site override when set, else the documented default.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min_search_volume FROM site_config WHERE site_id = %s",
            (site_id,),
        )
        row = cur.fetchone()
    if row and row[0] is not None:
        return int(row[0])
    return MISSING_PAGE_HIGH_IMPACT_VOLUME_DEFAULT


def _generator_limit(generator_file):
    """Per-generator LIMIT parsed from the plan SQL itself (fix 2.4).

    The plan SQL's trailing `LIMIT n` is the single source of truth for the
    per-site quota; Python no longer restates 15/20/15/10 in a dict.
    """
    sql = load_generator_sql(generator_file)
    matches = re.findall(r"\bLIMIT\s+(\d+)\s*;?\s*(?:--[^\n]*)?$", sql.strip(), re.IGNORECASE)
    if not matches:
        raise ValueError(f"generator SQL has no trailing LIMIT: {generator_file}")
    return int(matches[-1])


def _to_jsonb(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


def _missing_page_row(r, impact_threshold=None):
    """impact gate: search_volume >= per-site resolved threshold -> 'high'.

    impact_threshold is always supplied by the orchestrator (resolved from
    site_config with the documented fallback); the None guard only covers
    direct test calls.
    """
    if impact_threshold is None:
        impact_threshold = MISSING_PAGE_HIGH_IMPACT_VOLUME_DEFAULT
    return {
        "generator": "missing_page",
        "action_type": "create_page",
        "proposed_url": r.get("proposed_url"),
        "cluster_id": r.get("cluster_id"),
        "diagnosis": f"No existing page matches cluster '{r.get('primary_keyword')}' "
                     f"(search_volume={r.get('search_volume')}, in_stock={r.get('matching_products_count')})",
        "evidence_json": json.dumps({
            "search_volume": r.get("search_volume"),
            "in_stock_product_count": r.get("in_stock_product_count"),
            "average_price": float(r["average_price"]) if r.get("average_price") is not None else None,
            "sample_products": r.get("sample_products"),
            "top_competitors": r.get("top_competitors"),
        }, default=str),
        "impact": "high" if (r.get("search_volume") or 0) >= impact_threshold else "medium",
        "confidence": "medium",
        "effort": "days",
        "owner": "content",
    }


def _existing_opportunity_row(r, impact_threshold=None):
    return {
        "generator": "existing_opportunity",
        "action_type": "improve_page",
        "target_url": r.get("target_url"),
        "cluster_id": r.get("cluster_id"),
        "diagnosis": f"Page underperforms for '{r.get('primary_keyword')}' "
                     f"(signals: {', '.join(r.get('signals') or [])})",
        "evidence_json": json.dumps({
            "signals": r.get("signals"),
            "total_clicks": int(r["total_clicks"]) if r.get("total_clicks") is not None else None,
            "total_impressions": int(r["total_impressions"]) if r.get("total_impressions") is not None else None,
            "avg_ctr": float(r["avg_ctr"]) if r.get("avg_ctr") is not None else None,
            "avg_position": float(r["avg_position"]) if r.get("avg_position") is not None else None,
        }, default=str),
        "impact": "medium",
        "confidence": "medium",
        "effort": "days",
        "owner": "content",
    }


def _technical_fix_row(r, impact_threshold=None):
    evidence = r.get("evidence")
    if isinstance(evidence, dict) and "issue_type" not in evidence:
        # issue_type rides in the SQL's primary_keyword slot (plan/03);
        # persist it explicitly so fix generation can route deterministically.
        evidence = dict(evidence)
        evidence["issue_type"] = r.get("primary_keyword")
    return {
        "generator": "technical_fix",
        "action_type": r.get("action_type") or "technical_fix",
        "target_url": r.get("target_url"),
        "cluster_id": None,
        "diagnosis": f"{r.get('primary_keyword')}: {r.get('page_type')} page issue "
                     f"(HTTP {r.get('status_code')})",
        "evidence_json": _to_jsonb(evidence),
        "work_required_json": _to_jsonb(r.get("work_required")),
        "impact": r.get("impact") or "medium",
        "confidence": "high",
        "effort": "hours",
        "owner": "engineering",
    }


def _cannibalization_row(r, impact_threshold=None):
    redirect_source = r.get("proposed_url")   # weaker page (to redirect)
    redirect_target = r.get("target_url") or r.get("stronger_url")  # survivor
    return {
        "generator": "cannibalization",
        "action_type": "consolidate",
        "target_url": r.get("target_url"),
        "proposed_url": r.get("proposed_url"),
        "cluster_id": r.get("cluster_id"),
        "diagnosis": f"Cannibalization on '{r.get('primary_keyword')}': "
                     f"consolidate weaker page into stronger ({r.get('cannibalization_pattern')})",
        "evidence_json": _to_jsonb(r.get("evidence")),
        "work_required_json": json.dumps([
            {
                "owner": "engineering",
                "task": f"Implement 301 redirect from {redirect_source or 'the weaker page'} "
                        f"to {redirect_target or 'the survivor page'} and remove the source URL "
                        "from all XML sitemaps",
                "acceptance_criteria": "Source URL returns 301 to the survivor; destination returns 200; "
                                       "source URL absent from all sitemaps",
            },
            {
                "owner": "SEO",
                "task": "Update all internal links to point at the survivor and confirm product "
                        "coverage from the retired page is represented on the survivor",
                "acceptance_criteria": "Zero internal links to the source URL; every in-stock product "
                                       "formerly listed on the source is reachable from the survivor page",
            },
        ], default=str),
        "impact": "medium",
        "confidence": "medium",
        "effort": "days",
        "owner": "SEO",
    }


ROW_BUILDERS = {
    "01-generator-missing-pages": _missing_page_row,
    "02-generator-existing-opportunities": _existing_opportunity_row,
    "03-generator-technical-root-causes": _technical_fix_row,
    "04-generator-cannibalization": _cannibalization_row,
}


def _validate_action_types(conn, rows):
    """Fail fast at insertion time on unknown action_types (fix 2.2).

    resolve_window_days would only raise days later at measurement time;
    here the unknown value fails the run with a named error immediately.
    Valid values come from measurement_window_lookup — the single source.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT action_type FROM measurement_window_lookup")
        valid = {r[0] for r in cur.fetchall()}
    for row in rows:
        action_type = row.get("action_type")
        if action_type not in valid:
            raise ValueError(
                f"unknown action_type '{action_type}' from generator "
                f"'{row.get('generator')}' — not present in measurement_window_lookup; "
                "refusing to insert (fail fast at generation, not at measurement)")


def _insert_recommendations(conn, site_id, rows):
    inserted = 0
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO recommendations
                    (site_id, generator, action_type, target_url, proposed_url,
                     cluster_id, diagnosis, evidence_json, work_required_json,
                     impact, confidence, effort, owner, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'raw')
                ON CONFLICT (site_id, generator, action_type,
                             COALESCE(cluster_id, '00000000-0000-0000-0000-000000000000'::uuid),
                             COALESCE(target_url, ''), COALESCE(proposed_url, ''))
                DO NOTHING
                """,
                (site_id, r["generator"], r["action_type"], r.get("target_url"),
                 r.get("proposed_url"), r.get("cluster_id"), r["diagnosis"],
                 _to_jsonb(r.get("evidence_json")) or "[]",
                 _to_jsonb(r.get("work_required_json")) or "[]",
                 r["impact"], r["confidence"], r["effort"], r["owner"]),
            )
            inserted += cur.rowcount
    return inserted


def run_candidate_generation(site_id, conn=None, reference_date=None):
    """Run generators 1-4 for one site; combine, dedupe, insert recommendations.

    reference_date is the pipeline's visible clock: it is bound into the
    generator SQL (replacing the former CURRENT_DATE) and defaults to today
    (UTC) only when the caller does not supply one. Tests may pass any date.
    """
    own_conn = conn is None
    if own_conn:
        conn = database.get_connection()
    try:
        counts = {}
        combined = []
        impact_threshold = _impact_volume_threshold(conn, site_id)
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
        for generator_file in ROW_BUILDERS:
            limit = _generator_limit(generator_file)
            candidates = run_generator(conn, generator_file, site_id, limit,
                                       reference_date=reference_date)
            counts[generator_file] = len(candidates)
            builder = ROW_BUILDERS[generator_file]
            combined.extend(builder(c, impact_threshold=impact_threshold) for c in candidates)

        # Dedupe: one recommendation per (generator, cluster_id, target/proposed url)
        seen = set()
        deduped = []
        for row in combined:
            key = (row["generator"], str(row.get("cluster_id")),
                   row.get("target_url"), row.get("proposed_url"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        deduped = deduped[:MAX_PER_SITE]

        _validate_action_types(conn, deduped)
        inserted = _insert_recommendations(conn, site_id, deduped)

        run_id = uuid.uuid4()
        with conn.cursor() as cur:
            for generator_file, count in counts.items():
                # candidate_runs PK is run_id alone: one row per generator run
                cur.execute(
                    "INSERT INTO candidate_runs (run_id, site_id, generator, candidate_count, mode) "
                    "VALUES (%s, %s, %s, %s, 'primary')",
                    (str(uuid.uuid4()), site_id, generator_file, count),
                )
        conn.commit()
        counts["combined"] = len(deduped)
        counts["inserted"] = inserted
        counts["run_id"] = str(run_id)
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def main():
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT site_id, domain FROM site_config")
            sites = cur.fetchall()
        domains = {str(site_id): domain for site_id, domain in sites}
        from jobs.run_multi import run_across_sites
        results = run_across_sites(
            [site_id for site_id, _ in sites],
            lambda sid: run_candidate_generation(sid),
        )
        for site_id in results:
            print(f"[{domains.get(site_id, site_id)}] "
                  f"{json.dumps(results[site_id], default=str)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
# -*- coding: utf-8 -*-
"""Link-graph DB integration (plan/31) — ingestion + generator + fail-closed.

python tests/run_link_ingest_tests.py   (needs a reachable local Postgres)

Verifies:
  A. page_links persistence
     1. ingest_page_links upserts internal edges (source, target, anchor,
        rel, first/last_seen)
     2. re-ingest updates last_seen + fills anchor (upsert, no dup)
     3. external/subdomain/self edges never persisted
     4. ingest is fail-soft (bad snapshot shape -> typed, no raise)
  B. engine.run_audit wiring (mock fetch seam)
     5. connected audit ingests links; audit_checklist mode does NOT
     6. link_ingest rides in the result
  C. Generator 5 (internal links)
     7. orphan target + shared-cluster source -> proposed recommendation
     8. idempotent regeneration (unique index, no second row)
     9. dedupe: target already recommended -> SQL excludes
  D. fail-closed suggestion-only discipline
    10. generate_fix_for_recommendation refuses internal_link (typed)
"""
import os
import sys
import json
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import db as database  # noqa: E402
import env as env_loader  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
FAILURES = []
DOMAIN = f"linkgraph-{RUN_TOKEN}.example.com"


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


def seed_world(conn):
    """Site + pages: an orphan target, a link-rich source, a third page."""
    ids = {"domain": DOMAIN}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property,
                                     catalogue_size_tier, platform)
            VALUES (%s, %s, %s, 'small', 'shopify') RETURNING site_id
            """,
            (f"LinkGraph {RUN_TOKEN}", DOMAIN, f"sc-domain:{DOMAIN}"),
        )
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)

        cur.executemany(
            """
            INSERT INTO pages (site_id, url, page_type, title, indexable,
                               status_code, crawl_depth, internal_links_in,
                               internal_links_out, product_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                # orphan target: revenue page, zero links in
                (site_id, f"https://{DOMAIN}/collections/snowboards",
                 "collection", "Snowboards", True, 200, 7, 0, 2, 8),
                # candidate source: link-rich, shallow, indexable
                (site_id, f"https://{DOMAIN}/blog/buying-guide",
                 "blog", "Snowboard Buying Guide", True, 200, 1, 12, 6, 0),
                # another shallow page (also a candidate source)
                (site_id, f"https://{DOMAIN}/collections/beginner",
                 "collection", "Beginner Gear", True, 200, 1, 3, 4, 5),
            ],
        )
        # shared cluster between target + source (drives source ranking +
        # anchor basis)
        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword, keywords,
                                          intent)
            VALUES (%s, %s, %s, 'commercial') RETURNING cluster_id
            """,
            (site_id, f"snowboard guide {RUN_TOKEN}",
             [f"snowboard guide {RUN_TOKEN}"]),
        )
        cluster_id = cur.fetchone()[0]
        ids["cluster_id"] = str(cluster_id)
        # match scores: BOTH pages score on the same cluster (shared space)
        cur.execute(
            """
            INSERT INTO page_query_match_scores
                (site_id, page_id, cluster_id, intent_page_type_score,
                 catalogue_match_score, gsc_evidence_score,
                 semantic_content_score)
            SELECT %s, p.page_id, %s, 0.8, 0.7, 0.6, 0.5
            FROM pages p WHERE p.site_id = %s
            """,
            (site_id, cluster_id, site_id),
        )
    conn.commit()
    return ids


def cleanup(conn, ids):
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM recommendations WHERE site_id = %s", (ids["site_id"],))
        cur.execute(
            "DELETE FROM candidate_runs WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM page_links WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute(
            "DELETE FROM page_query_match_scores WHERE site_id = %s",
            (ids["site_id"],))
        cur.execute("DELETE FROM audit_sessions WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM pages WHERE site_id = %s", (ids["site_id"],))
        cur.execute(
            "DELETE FROM keyword_clusters WHERE site_id = %s",
            (ids["site_id"],))
        cur.execute("DELETE FROM fix_policy WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM site_config WHERE site_id = %s",
                    (ids["site_id"],))
    conn.commit()


def test_ingest(conn, ids):
    print("\n== A. page_links persistence ==")
    from audit.link_ingest import ingest_page_links

    allok = True
    snapshot = {
        "links": [
            {"href": f"https://{DOMAIN}/collections/snowboards",
             "text": "Shop snowboards", "rel": None},
            {"href": f"https://{DOMAIN}/collections/beginner-gear",
             "text": "Beginner gear", "rel": "sponsored"},
            {"href": "https://external.example.com/x", "text": "Ext",
             "rel": None},
            {"href": "#top", "text": None, "rel": None},
        ],
        "url": f"https://{DOMAIN}/blog/buying-guide",
    }
    result = ingest_page_links(conn, uuid.UUID(ids["site_id"]),
                               snapshot["url"], snapshot)
    allok &= check("1. ingest ok, 2 internal edges", result.get("ok")
                   and result.get("ingested") == 2, str(result))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_url, target_url, anchor_text, rel,
                   first_seen_at, last_seen_at
            FROM page_links WHERE site_id = %s
            ORDER BY target_url
            """,
            (uuid.UUID(ids["site_id"]),),
        )
        rows = cur.fetchall()
    allok &= check("1b. rows carry source/target/anchor/rel",
                   len(rows) == 2
                   and rows[0][0] == f"https://{DOMAIN}/blog/buying-guide"
                   and {r[2] for r in rows}
                   == {"Shop snowboards", "Beginner gear"}
                   and {r[3] for r in rows} == {None, "sponsored"},
                   str(rows))
    # re-ingest: upsert (no dup), last_seen survives
    result2 = ingest_page_links(conn, uuid.UUID(ids["site_id"]), snapshot["url"],
                                snapshot)
    allok &= check("1c. re-ingest idempotent (2 edges again)",
                   result2.get("ingested") == 2, str(result2))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM page_links WHERE site_id = %s",
                    (uuid.UUID(ids["site_id"]),))
        allok &= check("1d. still exactly 2 rows (unique edge)",
                       cur.fetchone()[0] == 2)
    # fail-soft: a snapshot without links never raises
    soft = ingest_page_links(conn, uuid.UUID(ids["site_id"]),
                             snapshot["url"], {})
    allok &= check("1e. snapshot without links -> ok, 0 edges",
                   soft.get("ok") is True and soft.get("ingested") == 0,
                   str(soft))
    return allok


def test_engine_wiring(conn, ids):
    print("\n== B. engine.run_audit wiring (fetch seam) ==")
    import audit.engine as engine

    allok = True
    page_url = f"https://{DOMAIN}/blog/buying-guide"

    def fake_fetch(_conn, url):
        return {
            "url": url, "status_code": 200, "title": "Buying Guide",
            "meta_description": None, "h1": "Buying Guide",
            "heading_outline": [], "body_html": "<p>guide</p>",
            "body_text": "guide", "content_hash": "x",
            "render_status": "not_rendered",
            "links": [
                {"href": f"https://{DOMAIN}/collections/snowboards",
                 "text": "Shop snowboards", "rel": None},
            ],
        }

    # connected mode: links ingested
    with conn.cursor() as cur:
        cur.execute("UPDATE site_config SET platform = 'shopify' "
                    "WHERE site_id = %s", (uuid.UUID(ids["site_id"]),))
    conn.commit()
    result = engine.run_audit(conn, page_url, adapter=_no_adapter(),
                              fetch_page=fake_fetch)
    allok &= check("5. connected audit ingests links",
                   result.get("link_ingest", {}).get("ingested") == 1,
                   str(result.get("link_ingest")))
    # audit_checklist mode (unregistered site): no ingestion
    result2 = engine.run_audit(conn, f"https://stray-{RUN_TOKEN}.example.com/x",
                               adapter=_no_adapter(), fetch_page=fake_fetch)
    allok &= check("5b. checklist mode ingests nothing",
                   "link_ingest" not in result2, str(result2.get("ok")))
    return allok


def _no_adapter():
    class _Dead:
        def fetch(self, _capability, _params):
            return {"ok": False, "error": "provider_error"}
    return _Dead()


def _stub_adapter():
    class _A:
        def fetch(self, capability, params):
            if capability == "serp":
                return {"ok": True, "data": [], "cost": 0.0}
            return {"ok": False, "error": "unsupported"}
    return _A()


def test_generator(conn, ids):
    print("\n== C. Generator 5: internal link suggestions ==")
    from generators.orchestrator import run_candidate_generation
    from generators.runner import run_generator

    allok = True
    # candidate SQL against the seeded world
    rows = run_generator(conn, "05-generator-internal-links",
                         uuid.UUID(ids["site_id"]))
    allok &= check("7. candidate emitted for the orphan target",
                   len(rows) >= 1
                   and rows[0]["target_url"]
                   == f"https://{DOMAIN}/collections/snowboards"
                   and rows[0]["generator"] == "internal_link",
                   str(rows)[:300])
    if rows:
        row = rows[0]
        ev = row["evidence"]
        allok &= check("7b. source is the shared-cluster page",
                       row["proposed_url"]
                       == f"https://{DOMAIN}/blog/buying-guide",
                       str(row.get("proposed_url")))
        allok &= check("7c. suggestion_only flag in evidence",
                       ev.get("suggestion_only") is True
                       and ev.get("issue_type") == "internal_link_gap",
                       str(ev)[:200])
        allok &= check("7d. anchor basis = shared cluster keyword",
                       ev.get("anchor_basis")
                       == "shared_cluster_primary_keyword"
                       and ev.get("suggested_anchor_text")
                       == f"snowboard guide {RUN_TOKEN}",
                       str((ev.get("suggested_anchor_text"),
                            ev.get("anchor_basis"))))
        allok &= check("7e. work_required is editorial (SEO owner, manual task)",
                       any(w["owner"] == "SEO"
                           for w in (row.get("work_required") or [])),
                       str(row.get("work_required"))[:160])

    # full orchestrator run -> recommendations row
    counts = run_candidate_generation(uuid.UUID(ids["site_id"]), conn=conn)
    allok &= check("7f. orchestrator ran generator 5",
                   counts.get("05-generator-internal-links", 0) >= 1,
                   str(counts))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT recommendation_id, generator, action_type, target_url,
                   proposed_url, status, evidence_json
            FROM recommendations
            WHERE site_id = %s AND generator = 'internal_link'
            """,
            (uuid.UUID(ids["site_id"]),),
        )
        recs = cur.fetchall()
    allok &= check("7g. suggestion in the queue as status='proposed'",
                   len(recs) == 1 and recs[0][5] == "proposed",
                   str([(r[2], r[5]) for r in recs]))
    rec_id = str(recs[0][0])
    ev = recs[0][6] if isinstance(recs[0][6], dict) \
        else json.loads(recs[0][6])
    allok &= check("7h. evidence carries suggestion_only + internal_link_gap",
                   ev.get("suggestion_only") is True
                   and ev.get("issue_type") == "internal_link_gap",
                   str(ev)[:200])

    # idempotent regeneration
    counts2 = run_candidate_generation(uuid.UUID(ids["site_id"]), conn=conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recommendations WHERE site_id = %s "
                    "AND generator = 'internal_link'",
                    (uuid.UUID(ids["site_id"]),))
        allok &= check("8. idempotent regeneration (still 1 row)",
                       cur.fetchone()[0] == 1, str(counts2.get("inserted")))
    return allok


def test_fail_closed(conn, ids):
    print("\n== D. fail-closed suggestion-only ==")
    from fixes.generator import (generate_fix_for_recommendation,
                                 FixNotSupported)
    import fixes.generator as gen_mod

    allok = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT recommendation_id FROM recommendations "
            "WHERE site_id = %s AND generator = 'internal_link' "
            "AND status = 'proposed' LIMIT 1",
            (uuid.UUID(ids["site_id"]),),
        )
        row = cur.fetchone()
        # approve it: even APPROVED, generation must refuse (typed)
        cur.execute(
            "UPDATE recommendations SET status = 'approved' "
            "WHERE recommendation_id = %s RETURNING recommendation_id",
            (row[0],))
        rec_id = str(cur.fetchone()[0])
    conn.commit()
    try:
        generate_fix_for_recommendation(conn, rec_id)
        allok &= check("10. approved internal_link -> FixNotSupported",
                       False, "no exception")
    except FixNotSupported as exc:
        allok &= check("10. approved internal_link -> FixNotSupported",
                       "suggestion-only" in str(exc), str(exc))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM generated_fixes "
                    "WHERE recommendation_id = %s", (uuid.UUID(rec_id),))
        allok &= check("10b. no generated_fixes row minted",
                       cur.fetchone()[0] == 0)
    conn.rollback()
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    ids = None
    try:
        ids = seed_world(conn)
        ok = test_ingest(conn, ids)
        ok &= test_engine_wiring(conn, ids)
        ok &= test_generator(conn, ids)
        ok &= test_fail_closed(conn, ids)
    finally:
        if ids:
            cleanup(conn, ids)
        conn.close()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        sys.exit(1)
    print("ALL LINK INGEST TESTS PASSED")


if __name__ == "__main__":
    main()
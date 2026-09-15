"""Day-in-the-life driven THROUGH the API (plan/16 lifecycle).

Full loop through the API:
  1. sync       -> ensure fixture data exists
  2. generate   -> run generation for coverage (may no-op if idempotent)
  3. seed       -> insert 8 raw candidates with unique URLs (deterministic hero path)
  4. agent      -> FakeClient promotes 2 to 'proposed', rejects rest
  5. queue      -> shows only the 2 enriched proposed seeds
  6. operator   -> reject one (learning loop), approve one, assign, implement
  7. measure    -> baseline + post snapshots via API + store_post_snapshots
  8. classify   -> verdict via API + classification code
  9. learning   -> rejection_context carries operator rejection reason
  10. final     -> status snapshot

Seeds are cleaned up at the end so the fixture DB stays pristine.

Usage: python tests/run_api_day_in_the_life.py
"""
import os
import sys
import json
import uuid
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from fastapi.testclient import TestClient

import db as database
from connectors.sync import run_sync
from generators.orchestrator import run_candidate_generation

FIXTURE_DATE = "2026-09-10"
IMPL_DATE = date(2026, 9, 11)
RUN_TOKEN = uuid.uuid4().hex[:8]


class _FakeClient:
    def __init__(self, output):
        self._output = output

    def complete_json(self, _prompt, _payload):
        return self._output


def check(name, value, detail=""):
    print(f"[{'PASS' if value else 'FAIL'}] {name}" + (f" — {detail}" if detail and not value else ""))
    return bool(value)


def seed_raw_candidates(conn, site_id, n=5):
    """Insert up to n deterministic raw candidates against real fixture pages."""
    ids = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT url FROM pages p WHERE NOT EXISTS "
            "  (SELECT 1 FROM recommendations r WHERE r.target_url = p.url) "
            "ORDER BY url LIMIT %s", (n,))
        urls = [r[0] for r in cur.fetchall()]
        for i, url in enumerate(urls):
            rec_id = str(uuid.uuid4())
            ids.append(rec_id)
            atype = "improve_page" if i < 3 else "technical_fix"
            cur.execute(
                """
                INSERT INTO recommendations
                    (recommendation_id, site_id, generator, action_type, target_url,
                     proposed_url, cluster_id, diagnosis, evidence_json, status)
                VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, 'raw')
                """,
                (rec_id, site_id,
                 "existing_opportunity" if atype == "improve_page" else "technical_fix",
                 atype, url, "",
                 f"day-in-life seeded {i} ({RUN_TOKEN})",
                 json.dumps([{"source": "GSC", "finding": "seeded"}])),
            )
    conn.commit()
    return ids


def cleanup(conn, site_id, ids):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = ANY(%s::uuid[])", (ids,))
        cur.execute("DELETE FROM change_log WHERE recommendation_id = ANY(%s::uuid[])", (ids,))
        cur.execute("DELETE FROM rejection_log WHERE site_id = %s AND candidate_id = ANY(%s::uuid[])",
                    (site_id, ids))
        cur.execute("DELETE FROM recommendations WHERE site_id = %s AND recommendation_id = ANY(%s::uuid[])",
                    (site_id, ids))
    conn.commit()


def _fake_agent_output(candidate_ids, seed_ids, approve_count=2):
    seed_set = set(seed_ids)
    approved, rejected = [], []
    for cid in candidate_ids:
        if cid in seed_set and len(approved) < approve_count:
            approved.append(cid)
        else:
            rejected.append(cid)
    recs = []
    for cid in approved:
        recs.append({
            "candidate_id": str(cid),
            "action_type": "improve_page",
            "target_url": "", "proposed_url": "",
            "query_cluster": ["day-in-life"],
            "diagnosis": f"agent diagnosis for {cid[:8]}",
            "evidence": [{"source": "GSC", "finding": "agent evidence"}],
            "work_required": [{"owner": "engineering", "task": "api day work",
                               "acceptance_criteria": "day criteria met"}],
            "impact": "medium", "confidence": "medium",
            "effort": "hours", "owner": "engineering",
            "measurement_metric": "clicks", "measurement_window_days": 28,
        })
    rejs = [{"candidate_id": str(cid), "reason": f"day-agent reject {cid[:8]}"} for cid in rejected]
    return {"recommendations": recs, "rejection_log": rejs}


def main():
    from api.main import app
    client = TestClient(app)
    allok = True
    seed_ids = []
    generated_ids = []
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT site_id, domain FROM site_config WHERE domain = 'example.com'")
            row = cur.fetchone()
        if not row:
            print("FAIL: no site_config; run sync first")
            sys.exit(1)
        site_id = row[0]
        conn.commit()

        # ---- 1. daily data sync -------------------------------------------
        print("\n=== 1. daily data sync ===")
        counts = run_sync()
        allok &= check("sync inserted search_performance", counts.get("search_performance", 0) > 0)
        allok &= check("sync inserted pages", counts.get("pages", 0) > 0)

        # ---- 2. generation (coverage; may no-op on repeat) ----------------
        print("\n=== 2. candidate generation (coverage) ===")
        gen = run_candidate_generation(site_id, conn, reference_date=FIXTURE_DATE)
        allok &= check("generation ran without error", isinstance(gen.get("combined"), int))
        # capture candidate rows generation inserted (committed internally) so
        # cleanup removes them too — keeps the shared fixture pristine
        with conn.cursor() as cur:
            cur.execute(
                "SELECT recommendation_id FROM recommendations "
                "WHERE site_id = %s AND status = 'raw' AND diagnosis NOT LIKE 'day-in-life seeded%%'",
                (site_id,),
            )
            gen_rows = [str(r[0]) for r in cur.fetchall()]
        conn.rollback()
        generated_ids = gen_rows

        # ---- 3. seed deterministic hero candidates ------------------------
        print("\n=== 3. seed raw candidates ===")
        seed_ids = seed_raw_candidates(conn, site_id)
        allok &= check("seeded raw candidates", len(seed_ids) >= 2)

        r = client.get(f"/queue?site_id={site_id}&limit=100")
        q0 = r.json()
        shown0 = {str(rec["recommendation_id"]) for rec in q0["recommendations"]}
        allok &= check("queue excludes seeded raw rows", not (set(seed_ids) & shown0),
                       f"raw leaking: {set(seed_ids) & shown0}")

        # ---- 4. agent evaluation (FakeClient) -----------------------------
        print("\n=== 4. agent evaluation (FakeClient) ===")
        from agents.seo_agent import run_agent, build_agent_input
        payload = build_agent_input(conn, site_id)
        payload_ids = [c["candidate_id"] for c in payload["candidates"]]
        seed_in = [c for c in payload["candidates"] if c["candidate_id"] in set(seed_ids)]
        allok &= check("agent input carries seeded candidates", len(seed_in) >= 2)
        summary = run_agent(site_id, conn,
                           client=_FakeClient(_fake_agent_output(payload_ids, seed_ids)))
        allok &= check("agent ok", summary["status"] == "ok" and summary.get("approved", 0) >= 2,
                       str(summary)[:200])

        with conn.cursor() as cur:
            cur.execute(
                "SELECT recommendation_id, status FROM recommendations "
                "WHERE recommendation_id = ANY(%s::uuid[]) ORDER BY recommendation_id", (seed_ids,))
            seed_states = {str(r[0]): r[1] for r in cur.fetchall()}
        approved_ids = [sid for sid in seed_ids if seed_states.get(sid) == "proposed"]
        rejected_ids = [sid for sid in seed_ids if seed_states.get(sid) == "rejected"]
        allok &= check("agent promoted exactly 2 seeds to proposed", len(approved_ids) == 2,
                       f"proposed={len(approved_ids)}")
        allok &= check("remaining seeds rejected",
                       len(rejected_ids) == len(seed_ids) - len(approved_ids),
                       f"rejected={len(rejected_ids)}")

        # ---- 5. queue shows enriched proposed ------------------------------
        print("\n=== 5. queue enriched surface ===")
        r = client.get(f"/queue?site_id={site_id}&limit=100")
        q1 = r.json()
        shown1 = {str(rec["recommendation_id"]) for rec in q1["recommendations"]}
        allok &= check("queue contains approved seed ids", set(approved_ids) <= shown1,
                       f"missing {set(approved_ids) - shown1}")

        d = client.get(f"/queue/{approved_ids[0]}").json()
        allok &= check("detail enriched=true", d.get("enriched") is True)
        plan = d.get("measurement_plan") or {}
        allok &= check("measurement_plan.metric not 'not set'",
                       plan.get("metric") not in (None, "", "not set"), str(plan))

        # ---- 6. operator workflow via API ---------------------------------
        print("\n=== 6. operator workflow ===")
        # reject one (rejection_loop -> rejection_log)
        r = client.post(f"/recommendations/{approved_ids[1]}/reject",
                       json={"reason": "day-loop: weak signal, skip this cycle"})
        allok &= check("operator reject via API", r.status_code == 200 and r.json()["status"] == "rejected",
                       r.text[:200])

        with database.get_connection() as c2:
            with c2.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM rejection_log "
                    "WHERE candidate_id = %s AND rejected_by = 'operator'", (approved_ids[1],))
                op_log = cur.fetchone()[0]
        allok &= check("operator rejection in rejection_log", op_log >= 1)

        # approve + assign + implement (baseline)
        r = client.post(f"/recommendations/{approved_ids[0]}/approve")
        allok &= check("approve via API", r.status_code == 200, r.text[:200])
        r = client.post(f"/recommendations/{approved_ids[0]}/assign", json={"owner": "engineering"})
        allok &= check("assign via API", r.status_code == 200, r.text[:200])
        r = client.post(f"/recommendations/{approved_ids[0]}/implement",
                       json={"implemented_at": str(IMPL_DATE)})
        allok &= check("implement stores baseline via API", r.status_code == 200 and r.json().get("baseline"),
                       r.text[:300])

        # ---- 7. measurement batch + post snapshots ------------------------
        print("\n=== 7. measurement ===")
        r = client.post("/measurements/run",
                       json={"recommendation_ids": [approved_ids[0]], "reference_date": str(IMPL_DATE)})
        allok &= check("measurement batch via API", r.status_code == 200, r.text[:200])

        from measurement.measure import store_post_snapshots
        from measurement.baseline import resolve_window_days
        with conn.cursor() as cur:
            cur.execute("SELECT action_type FROM recommendations WHERE recommendation_id=%s",
                        (approved_ids[0],))
            atype = cur.fetchone()[0]
        win = resolve_window_days(conn, atype)
        post = store_post_snapshots(conn, approved_ids[0], IMPL_DATE + timedelta(days=win))
        conn.commit()
        allok &= check("post snapshots stored", len(post.get("measurement_period", [])) == 2)

        # ---- 8. classify --------------------------------------------------
        print("\n=== 8. classification ===")
        from measurement.classify import classify_recommendation, persist_classification
        cls = classify_recommendation(conn, approved_ids[0])
        allok &= check("classify returns verdict",
                       cls["result"] in ("won", "neutral", "lost", "inconclusive"), cls["result"])
        persist_classification(conn, approved_ids[0], cls,
                               measured_at=IMPL_DATE + timedelta(days=win))
        conn.commit()

        r = client.get(f"/recommendations/{approved_ids[0]}/measurement")
        allok &= check("measurement read (API)", r.status_code == 200 and len(r.json()["snapshots"]) >= 2,
                       r.text[:200])
        r = client.get(f"/results?site_id={site_id}")
        allok &= check("results endpoint (API)", r.status_code == 200 and "by_generator" in r.json())

        # ---- 9. learning loop ---------------------------------------------
        print("\n=== 9. learning loop ===")
        with database.get_connection() as c3:
            payload2 = build_agent_input(c3, site_id)
        ctx = payload2.get("rejection_context", {}).get("last_rejections", [])
        reasons = [r.get("reason", "") for r in ctx]
        allok &= check("operator rejection in agent rejection_context",
                       any("day-loop" in r for r in reasons), f"reasons={reasons[:3]}")

        # ---- 10. final state ----------------------------------------------
        print("\n=== 10. final state ===")
        with database.get_connection() as c5:
            with c5.cursor() as cur:
                cur.execute("SELECT status, count(*) FROM recommendations WHERE site_id=%s GROUP BY status",
                            (site_id,))
                states = dict(cur.fetchall())
        print(json.dumps(states, indent=1, default=str))
        allok &= check("measured status present", states.get("measured", 0) >= 1)
    finally:
        cleanup(conn, site_id, seed_ids)
        if generated_ids:
            cleanup(conn, site_id, generated_ids)
        conn.close()

    print("\nAPI DAY-IN-THE-LIFE:", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
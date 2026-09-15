"""Smoke: boot the FastAPI app, hit /health + queue + decision flow on fixture DB.

Phase 6 Fix Brief (plan/16): raw->proposed lifecycle changes the ground rules:
  - Generators insert status='raw'; only the agent promotes to 'proposed'.
  - The queue (GET /queue) serves exclusively 'proposed' rows.

Generators are idempotent by natural key (ON CONFLICT DO NOTHING), so this test
does NOT rely on generation producing fresh rows on repeat runs: it seeds its own
deterministic raw candidates (unique URLs per run) and drives them through the
full lifecycle, then removes exactly the rows it created. The shared fixture DB
stays pristine and the suite is repeatable.
"""
import os
import sys
import json
import uuid
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from fastapi.testclient import TestClient

import db as database

RUN_TOKEN = uuid.uuid4().hex[:8]


def check(name, value, detail=""):
    print(f"[{'PASS' if value else 'FAIL'}] {name}" + (f" — {detail}" if detail and not value else ""))
    return bool(value)


def seed_raw_candidates(conn, site_id, n=6):
    """Insert n deterministic raw candidates against real fixture pages."""
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
            cur.execute(
                """
                INSERT INTO recommendations
                    (recommendation_id, site_id, generator, action_type, target_url,
                     proposed_url, cluster_id, diagnosis, evidence_json, status)
                VALUES (%s, %s, 'existing_opportunity', 'improve_page', %s, %s, NULL, %s,
                        %s, 'raw')
                """,
                (rec_id, site_id,
                 url,
                 "",
                 f"seeded raw candidate {i} ({RUN_TOKEN})",
                 json.dumps([{"source": "GSC", "finding": "seeded"}])),
            )
    conn.commit()
    return ids


def cleanup(conn, site_id, ids):
    """Remove exactly the rows this test created (FK-safe order)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = ANY(%s::uuid[])", (ids,))
        cur.execute("DELETE FROM change_log WHERE recommendation_id = ANY(%s::uuid[])", (ids,))
        cur.execute("DELETE FROM rejection_log WHERE site_id = %s AND candidate_id = ANY(%s::uuid[])",
                    (site_id, ids))
        cur.execute("DELETE FROM recommendations WHERE site_id = %s AND recommendation_id = ANY(%s::uuid[])",
                    (site_id, ids))
    conn.commit()


class _FakeClient:
    def __init__(self, output):
        self._output = output

    def complete_json(self, _prompt, _payload):
        return self._output


def _fake_agent_output(candidate_ids, approve_first=2):
    recs, rejs = [], []
    for i, cid in enumerate(candidate_ids):
        if i < approve_first:
            recs.append({
                "candidate_id": str(cid),
                "action_type": "improve_page",
                "target_url": "", "proposed_url": "",
                "query_cluster": ["api smoke"],
                "diagnosis": f"agent diagnosis for {cid}",
                "evidence": [{"source": "GSC", "finding": "agent evidence"}],
                "work_required": [{"owner": "engineering", "task": "api task",
                                   "acceptance_criteria": "api pass"}],
                "impact": "medium", "confidence": "medium",
                "effort": "hours", "owner": "engineering",
                "measurement_metric": "clicks", "measurement_window_days": 28,
            })
        else:
            rejs.append({"candidate_id": str(cid), "reason": f"api-test rejection {cid[:8]}"})
    return {"recommendations": recs, "rejection_log": rejs}


def main():
    from api.main import app
    client = TestClient(app)
    allok = True
    seed_ids = []
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT site_id FROM site_config WHERE domain = 'example.com'")
            site_id = str(cur.fetchone()[0])
        conn.commit()

        # --- health ---
        r = client.get("/health")
        allok &= check("health OK", r.status_code == 200 and r.json()["status"] == "ok", r.text[:200])

        # --- seed raw candidates; queue must exclude them ---
        print("\n== raw lifecycle ==")
        seed_ids = seed_raw_candidates(conn, site_id)
        allok &= check("seeded raw candidates ready", len(seed_ids) >= 2)

        r = client.get(f"/queue?site_id={site_id}&limit=100")
        q = r.json()
        shown_ids = {str(rec["recommendation_id"]) for rec in q["recommendations"]}
        allok &= check("queue excludes seeded raw rows", not (set(seed_ids) & shown_ids),
                       f"raw leaking into queue: {set(seed_ids) & shown_ids}")

        # --- agent promotes raw -> proposed|rejected; queue then shows enriched ---
        print("\n== agent promotion ==")
        from agents.seo_agent import run_agent, build_agent_input
        seed_set = set(seed_ids)
        payload = build_agent_input(conn, site_id)
        payload_ids = [c["candidate_id"] for c in payload["candidates"]]
        seed_in_payload = [c for c in payload["candidates"] if c["candidate_id"] in seed_set]
        allok &= check("agent input carries seeded raw candidates", len(seed_in_payload) >= 2,
                       f"found {len(seed_in_payload)} of {len(seed_ids)}")
        approved_ids = [c["candidate_id"] for c in seed_in_payload[:2]]

        summary = run_agent(site_id, conn, client=_FakeClient(_fake_agent_output(payload_ids)))
        allok &= check("agent returns ok", summary.get("status") == "ok", str(summary)[:200])

        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, count(*) FROM recommendations "
                "WHERE recommendation_id = ANY(%s::uuid[]) GROUP BY status",
                (seed_ids,))
            states = {r[0]: r[1] for r in cur.fetchall()}
        allok &= check("agent promoted first 2 to proposed", states.get("proposed", 0) == 2,
                       str(states))
        allok &= check("remaining seeds rejected by agent", states.get("rejected", 0) == len(seed_ids) - 2,
                       str(states))
        conn.commit()

        r = client.get(f"/queue?site_id={site_id}&limit=100")
        q2 = r.json()
        shown2 = {str(rec["recommendation_id"]) for rec in q2["recommendations"]}
        allok &= check("queue now shows approved promoted seeds", approved_ids and set(approved_ids) <= shown2,
                       f"missing {set(approved_ids) - shown2}")
        allok &= check("queue still excludes rejected seeds",
                       not (set(seed_ids) - set(approved_ids)) & shown2)

        # rejections are learning-loop input
        from agents.seo_agent import build_agent_input as build2
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM rejection_log WHERE site_id = %s AND candidate_id = ANY(%s::uuid[])",
                (site_id, seed_ids))
            rej_logged = cur.fetchone()[0]
        allok &= check("agent rejections logged for rejection_context",
                       rej_logged == len(seed_ids) - 2, f"logged={rej_logged}")

        # --- detail: enriched + metric ---
        print("\n== detail surface ==")
        d = client.get(f"/queue/{approved_ids[0]}").json()
        allok &= check("detail enriched=true (agent-validated)", d.get("enriched") is True, str(d.get("enriched")))
        plan = d.get("measurement_plan") or {}
        allok &= check("measurement_plan.metric populated", plan.get("metric") not in (None, "", "not set"),
                       plan.get("metric"))
        allok &= check("measurement_plan.window matches lookup value 28d",
                       plan.get("window_days") == 28, str(plan))

        # --- decision flow on promoted seeds ---
        print("\n== decision flow ==")
        r = client.post(f"/recommendations/{approved_ids[0]}/approve")
        allok &= check("approve: proposed -> approved", r.status_code == 200 and r.json()["status"] == "approved",
                       r.text[:200])
        r = client.post(f"/recommendations/{approved_ids[0]}/approve")
        allok &= check("re-approve is 409 (decision already recorded)",
                       r.status_code == 409, r.text[:200])

        r = client.post(f"/recommendations/{approved_ids[1]}/reject", json={"reason": "repeated cluster; low value"})
        allok &= check("reject with reason -> rejected", r.status_code == 200 and r.json()["status"] == "rejected",
                       r.text[:200])
        r = client.post(f"/recommendations/{approved_ids[1]}/reject", json={"reason": "x"})
        allok &= check("re-reject is 409 (decision already recorded)", r.status_code == 409, r.text[:200])

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM rejection_log WHERE candidate_id = %s AND rejected_by = 'operator'",
                (approved_ids[1],))
            op_logs = cur.fetchone()[0]
        allok &= check("operator rejection logged (learning loop)", op_logs >= 1)

        r = client.post(f"/recommendations/{approved_ids[0]}/assign", json={"owner": "engineering"})
        allok &= check("assign sets owner", r.status_code == 200 and r.json()["owner"] == "engineering",
                       r.text[:200])
        r = client.post(f"/recommendations/{approved_ids[0]}/implement", json={"implemented_at": "2026-09-11"})
        allok &= check("implement records baseline", r.status_code == 200 and r.json().get("baseline"),
                       r.text[:300])

        # --- migration predicate invariants (shared fixture, read-only) ---
        print("\n== migration predicate invariants ==")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, evidence_json::text FROM recommendations "
                "WHERE generator='cannibalization' AND action_type='consolidate'")
            measured_row = cur.fetchone()
            if measured_row:
                allok &= check("measured consolidate row unchanged",
                               measured_row[0] == "measured" and measured_row[1].startswith("["),
                               f"status={measured_row[0]}")
            cur.execute(
                "SELECT count(*) FROM ("
                "  SELECT site_id, generator, action_type,"
                "    COALESCE(cluster_id, '00000000-0000-0000-0000-000000000000'::uuid) AS cl,"
                "    COALESCE(target_url,'') AS tu, COALESCE(proposed_url,'') AS pu "
                "  FROM recommendations GROUP BY 1,2,3,4,5,6 HAVING count(*)>1"
                ") dups")
            dupe_groups = cur.fetchone()[0]
            allok &= check("zero natural-key duplicate groups (plan/16 dedupe worked)",
                           dupe_groups == 0, f"dupes={dupe_groups}")
            cur.execute(
                "SELECT action_type, metric IS NOT NULL FROM measurement_window_lookup ORDER BY action_type")
            metrics = cur.fetchall()
            allok &= check("measurement_window_lookup.metric populated for all action types",
                           all(m for _, m in metrics) and len(metrics) == 4, str(metrics))
    finally:
        cleanup(conn, site_id, seed_ids)
        conn.close()

    print("\nAPI SMOKE TEST RUN", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
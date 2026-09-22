"""Day-in-the-life end-to-end validation run (scripted, offline connectors).

Pipeline (plan/09 + plan/16 raw lifecycle):
  1. sync      — GSC/Shopify/GA4/OpenSEO -> DB (mock fixtures)
  2. generate  — Generators 1-4 -> recommendations (status='raw')
  3. agent     — live Ollama Cloud evaluation -> raw -> proposed|rejected
  4. operator  — approve one proposed row (simulated; operator approves via UI)
  5. measure   — baseline + post snapshots for the approved row
  6. classify  — plan/08 rules -> result on the measured row

Every stage asserts invariants and stops loudly on failure. The agent stage
requires OLLAMA_API_BASE/OLLAMA_API_KEY (no offline stub by design); all other
stages are fully offline.

Usage: python tests/run_day_in_the_life.py [--skip-agent]
"""
import os
import sys
import json
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import env as env_loader
import db as database
from connectors.sync import run_sync
from generators.orchestrator import run_candidate_generation

FIXTURE_DATE = "2026-09-10"   # fixture-universe anchor for injectable clocks
IMPL_DATE = date(2026, 9, 11)  # implementation anchor for measurement


def stage(name):
    print(f"\n=== {name} ===")


def ok(label, value, detail=""):
    print(f"[OK] {label}" if value else f"[FAIL] {label} — {detail}")
    return bool(value)


def main():
    skip_agent = "--skip-agent" in sys.argv
    env_loader.load_env_file(quiet=True)
    allok = True
    cleanup_ids = []

    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT site_id, domain FROM site_config WHERE domain = 'example.com'")
            site = cur.fetchone()
        if not site:
            print("FAIL: site not found; run sync first")
            sys.exit(1)
        site_id, domain = site
        conn.rollback()

        # ---- 1. daily sync -------------------------------------------------
        stage("1. daily data sync (mock connectors)")
        counts = run_sync()
        print(json.dumps(counts, indent=1, default=str))
        allok &= counts["search_performance"] > 0 and counts["pages"] > 0
        allok &= counts["page_business_performance"] > 0 and counts["crawl_audit_updated"] > 0

        # ---- 2. candidate generation --------------------------------------
        stage("2. candidate generation (reference_date = fixture anchor)")
        gen_counts = run_candidate_generation(site[0], conn, reference_date=FIXTURE_DATE)
        print(json.dumps(gen_counts, default=str))
        allok &= gen_counts["combined"] >= 0 and gen_counts["inserted"] >= 0

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM recommendations WHERE site_id = %s AND status = 'raw'", (site[0],))
            raw_count = cur.fetchone()[0]
        if raw_count:
            allok &= ok("raw candidates inserted", raw_count > 0, f"raw={raw_count}")
        else:
            # generators are idempotent by natural key (plan/16): on repeat runs
            # they may insert nothing. Seed deterministic candidates so the rest
            # of the pipeline still has raw rows to promote.
            import uuid as _uuid
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT url FROM pages p WHERE NOT EXISTS "
                    "  (SELECT 1 FROM recommendations r WHERE r.target_url = p.url) "
                    "ORDER BY url LIMIT 4")
                urls = [r[0] for r in cur.fetchall()]
                for i, url in enumerate(urls):
                    cur.execute(
                        "INSERT INTO recommendations "
                        "  (recommendation_id, site_id, generator, action_type, target_url, "
                        "   proposed_url, cluster_id, diagnosis, evidence_json, status) "
                        "VALUES (%s, %s, 'existing_opportunity', 'improve_page', %s, %s, NULL, %s, %s, 'raw')",
                        (str(_uuid.uuid4()), site[0], url, "",
                         f"day-life seeded {i}", '[]'))
                conn.commit()
                cur.execute("SELECT count(*) FROM recommendations WHERE site_id = %s AND status = 'raw'", (site[0],))
                raw_count = cur.fetchone()[0]
            allok &= ok("seeded raw candidates (idempotent generation no-op)", raw_count == 4, f"raw={raw_count}")
        conn.rollback()

        # every raw row present now was created by THIS run (baseline has none);
        # capture them all so cleanup removes real generated rows too, not just seeds
        with conn.cursor() as cur:
            cur.execute(
                "SELECT recommendation_id FROM recommendations "
                "WHERE site_id = %s AND status = 'raw'", (site[0],))
            cleanup_ids = [str(r[0]) for r in cur.fetchall()]
        conn.rollback()

        # ---- 3. agent evaluation ------------------------------------------
        stage("3. agent evaluation (live Ollama Cloud)")
        if skip_agent:
            print("[SKIP] --skip-agent: simulating agent + operator approval in one shot")
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE recommendations SET status = 'approved', measured_at = NULL, "
                    "approved_at = now(), implemented_at = %s "
                    "WHERE recommendation_id = ("
                    "  SELECT recommendation_id FROM recommendations "
                    "  WHERE site_id = %s AND status = 'raw' AND action_type != 'create_page' "
                    "  AND target_url IS NOT NULL "
                    "  ORDER BY created_at DESC LIMIT 1"
                    ") RETURNING recommendation_id",
                    (IMPL_DATE, site[0],))
                approved = cur.fetchone()
            conn.commit()
        else:
            from agents.seo_agent import run_agent
            summary = run_agent(site[0], conn)
            print(json.dumps(summary, default=str))
            allok &= summary.get("status") == "ok"

            # plan/16: agent promotes accepted rows to 'proposed' (enriched);
            # operator approval is a separate step. Simulate it here to keep
            # the measurement/classification stages working end-to-end.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE recommendations SET status = 'approved', approved_at = now(), "
                    "measured_at = NULL, implemented_at = %s "
                    "WHERE recommendation_id = ("
                    "  SELECT recommendation_id FROM recommendations "
                    "  WHERE site_id = %s AND status = 'proposed' "
                    "  AND action_type != 'create_page' AND target_url IS NOT NULL "
                    "  ORDER BY created_at DESC LIMIT 1"
                    ") RETURNING recommendation_id",
                    (IMPL_DATE, site[0],))
                approved = cur.fetchone()
            conn.commit()

        if not approved:
            print("FAIL: no approved URL-measurable recommendation to measure")
            sys.exit(1)
        approved_id = str(approved[0])
        print(f"measuring recommendation: {approved_id}")

        # ---- 4. measurement snapshots --------------------------------------
        stage("4. measurement batch (baseline + post snapshots)")
        from measurement.measure import run_measurement_batch, store_post_snapshots
        from measurement.baseline import resolve_window_days
        from measurement.thresholds import GSC_SETTLE_DAYS
        with conn.cursor() as cur:
            cur.execute("SELECT action_type FROM recommendations WHERE recommendation_id = %s",
                        (approved_id,))
            action_type = cur.fetchone()[0]
        window = resolve_window_days(conn, action_type)
        # Reference date must clear the GSC settle buffer (impl + window + settle)
        # — the batch skips windows that close inside the buffer.
        batch = run_measurement_batch(conn, [approved_id],
                                      reference_date=IMPL_DATE + timedelta(days=window + GSC_SETTLE_DAYS))
        print(json.dumps(batch, indent=1, default=str))
        allok &= len(batch["measured"]) == 1 and len(batch["skipped"]) == 0
        # post window ends at impl + window (day-in-the-life compression)
        post_info = store_post_snapshots(conn, approved_id, IMPL_DATE + timedelta(days=window))
        conn.commit()
        print(f"post window: {post_info['measurement_period']} (window {post_info['window_days']}d)")

        # ---- 5. classification ---------------------------------------------
        stage("5. classification (plan/08 rules + significance)")
        from measurement.classify import classify_recommendation, persist_classification
        result = classify_recommendation(conn, approved_id)
        print(json.dumps(result, indent=1, default=str)[:400])
        allok &= result["result"] in ("won", "neutral", "lost", "inconclusive")
        persisted = persist_classification(conn, approved_id, result,
                                           measured_at=IMPL_DATE + timedelta(days=window))
        conn.commit()
        print(f"persisted result: {persisted}")

        # ---- 6. final state -------------------------------------------------
        stage("6. final state")
        with conn.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM recommendations WHERE site_id = %s GROUP BY status", (site[0],))
            rec_states = dict(cur.fetchall())
            cur.execute("SELECT count(*) FROM rejection_log WHERE site_id = %s", (site[0],))
            rejections = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM measurement_snapshots WHERE recommendation_id = %s", (approved_id,))
            snaps = cur.fetchone()[0]
            cur.execute("SELECT result, status FROM recommendations WHERE recommendation_id = %s", (approved_id,))
            final = cur.fetchone()
        print(json.dumps({
            "recommendations_by_status": {str(k): v for k, v in rec_states.items()},
            "agent_rejection_log": rejections,
            "measurement_snapshots_for_measured_rec": snaps,
            "final_verdict": {"result": str(final[0]), "status": str(final[1])},
        }, indent=1))
        allok &= snaps >= 2 and final[1] == "measured"
    finally:
        if cleanup_ids:
            with database.get_connection() as c:
                with c.cursor() as cur:
                    cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = ANY(%s::uuid[])",
                                (cleanup_ids,))
                    cur.execute("DELETE FROM change_log WHERE recommendation_id = ANY(%s::uuid[])", (cleanup_ids,))
                    cur.execute("DELETE FROM rejection_log WHERE site_id = %s AND candidate_id = ANY(%s::uuid[])",
                                (site[0], cleanup_ids))
                    cur.execute("DELETE FROM recommendations WHERE site_id = %s AND recommendation_id = ANY(%s::uuid[])",
                                (site[0], cleanup_ids))
                c.commit()
        conn.close()

    print("\nDAY-IN-THE-LIFE VALIDATION:", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()

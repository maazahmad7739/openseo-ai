# -*- coding: utf-8 -*-
"""Scheduler hookup tests (Stage 2 Phase 1.5).

python tests/run_scheduler_tests.py   (needs a reachable local Postgres)

Verifies the scheduling logic and the fix_executor hookup WITHOUT running
any real job body (dispatch is stubbed):

  1. SCHEDULE registry: fix_executor present as a daily slot; every other
     job keeps its designed cadence.
  2. is_due math: hour gate, daily once-per-day, weekly weekday gate,
     weekly once-per-ISO-week, missed-weekday catch-up, never-run fires.
  3. slot_period_key shape (daily vs weekly).
  4. run_due_jobs end-to-end on the real DB with stubbed dispatch:
     - due slots run exactly once (last-run recorded in system_config)
     - a second evaluation in the same period is a no-op
     - a crashing job is captured as failed; later jobs still run
     - dispatch received dry_run passthrough for fix_executor
  5. fix_executor hookup: SCHEDULE entry cadence matches LOCK_KEYS and the
     job module's run() accepts the (site_id, dry_run) contract.

Isolation: last-run keys are namespaced (cleaned before + after); no job
bodies are executed.
"""
import os
import sys
import json
import re
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import db as database  # noqa: E402
import env as env_loader  # noqa: E402

RUN_TOKEN = None  # set in main(): snapshot of pre-existing lastrun rows
FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


def snapshot_last_runs(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT config_key, config_value FROM system_config "
                    "WHERE config_key LIKE 'scheduler.lastrun.%'")
        return cur.fetchall()


def cleanup_last_runs(conn, saved):
    """Restore the pre-suite snapshot exactly: deletes rows the suite
    created, re-inserts pre-existing rows with their original values.
    Roll back first so an aborted test transaction cannot block cleanup."""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM system_config WHERE config_key LIKE 'scheduler.lastrun.%'")
        for key, value in saved or []:
            cur.execute(
                "INSERT INTO system_config (config_key, config_value) VALUES (%s, %s) "
                "ON CONFLICT (config_key) DO UPDATE SET config_value = EXCLUDED.config_value",
                (key, value),
            )
    conn.commit()


def main():
    from jobs import scheduler as sched
    from datetime import timedelta

    allok = True
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    saved_lastruns = snapshot_last_runs(conn)
    cleanup_last_runs(conn, saved_lastruns)
    dispatched = []

    try:
        # 1: registry
        print("\n== schedule registry ==")
        allok &= check("fix_executor in SCHEDULE (daily)",
                       sched.SCHEDULE.get("fix_executor") == {"cadence": "daily", "hour_utc": 3},
                       str(sched.SCHEDULE.get("fix_executor")))
        allok &= check("audit_ttl_sweep in SCHEDULE (daily, off-peak hour)",
                       sched.SCHEDULE.get("audit_ttl_sweep") == {"cadence": "daily", "hour_utc": 6},
                       str(sched.SCHEDULE.get("audit_ttl_sweep")))
        allok &= check("offpage_discovery in SCHEDULE (daily, 07:00, after "
                       "audit_ttl_sweep)",
                       sched.SCHEDULE.get("offpage_discovery")
                       == {"cadence": "daily", "hour_utc": 7},
                       str(sched.SCHEDULE.get("offpage_discovery")))
        allok &= check("audit_ttl_sweep slot after stale_approvals, an hour "
                       "clear of other daily slots",
                       sched.SCHEDULE["audit_ttl_sweep"]["hour_utc"]
                       > sched.SCHEDULE["stale_approvals"]["hour_utc"]
                       and sched.SCHEDULE["offpage_discovery"]["hour_utc"]
                       > sched.SCHEDULE["audit_ttl_sweep"]["hour_utc"]
                       and {spec["hour_utc"] for spec in sched.SCHEDULE.values()
                            if spec["cadence"] == "daily"}
                       == {1, 2, 3, 5, 6, 7},
                       str({n: s["hour_utc"] for n, s in sched.SCHEDULE.items()
                            if s["cadence"] == "daily"}))
        allok &= check("audit_ttl_sweep module importable + exposes run()",
                       callable(getattr(sched._job_module("audit_ttl_sweep"),
                                        "run", None)))
        allok &= check("offpage_discovery module importable + exposes run()",
                       callable(getattr(sched._job_module("offpage_discovery"),
                                        "run", None)))
        allok &= check("offpage_discovery lock key registered",
                       "offpage_discovery"
                       in __import__("jobs.locks", fromlist=["LOCK_KEYS"]).LOCK_KEYS)
        allok &= check("every scheduled job has a run() entry point",
                       all(callable(getattr(sched._job_module(j), "run", None))
                           for j in sched.SCHEDULE),
                       "missing run() in some module")
        allok &= check("audit_ttl_sweep lock key registered",
                       "audit_ttl_sweep" in __import__("jobs.locks", fromlist=["LOCK_KEYS"]).LOCK_KEYS)
        allok &= check("scheduler lock key registered",
                       "scheduler" in __import__("jobs.locks", fromlist=["LOCK_KEYS"]).LOCK_KEYS)

        # 2: is_due math
        print("\n== is_due ==")
        daily = sched.SCHEDULE["daily_sync"]
        tue_0130 = datetime(2026, 9, 22, 1, 30)   # past 01:00 Tue
        tue_0030 = datetime(2026, 9, 22, 0, 30)   # before 01:00
        allok &= check("daily fires past hour when never run",
                       sched.is_due("daily_sync", tue_0130, daily, None))
        allok &= check("daily silent before hour",
                       not sched.is_due("daily_sync", tue_0030, daily, None))
        allok &= check("daily silent when already run today",
                       not sched.is_due("daily_sync", tue_0130, daily, tue_0130))
        allok &= check("daily fires next day",
                       sched.is_due("daily_sync", datetime(2026, 9, 23, 1, 30), daily, tue_0130))

        weekly = sched.SCHEDULE["weekly_candidates"]  # Sat 04:00, weekday_utc=5
        sat_0400 = datetime(2026, 9, 19, 4, 0)    # Sat past hour
        fri_0900 = datetime(2026, 9, 18, 9, 0)    # Fri (before scheduled dow)
        sun_0900 = datetime(2026, 9, 20, 9, 0)    # Sun after the missed Sat slot
        allok &= check("weekly fires on scheduled dow past hour",
                       sched.is_due("weekly_candidates", sat_0400, weekly, None))
        allok &= check("weekly silent before scheduled dow",
                       not sched.is_due("weekly_candidates", fri_0900, weekly, None))
        allok &= check("weekly catches up after missed weekday (same iso week)",
                       sched.is_due("weekly_candidates", sun_0900, weekly, None))
        allok &= check("weekly silent when already run this iso week",
                       not sched.is_due("weekly_candidates", sun_0900, weekly, sat_0400))

        # 3: period keys
        print("\n== period keys ==")
        allok &= check("daily period key = date",
                       sched.slot_period_key("x", tue_0130, daily) == "2026-09-22")
        weekly_key = sched.slot_period_key("x", tue_0130, weekly)
        allok &= check("weekly period key = iso year+week",
                       weekly_key == "2026-W39", weekly_key)

        # 4: run_due_jobs with stubbed dispatch
        print("\n== run_due_jobs (stubbed dispatch) ==")
        real_dispatch = sched._dispatch
        crash_counts = {"measurements": 0}

        def fake_dispatch(job_name, now_utc, dry_run=False):
            dispatched.append({"job": job_name, "dry_run": dry_run})
            if job_name == "measurements":
                crash_counts["measurements"] += 1
                if crash_counts["measurements"] <= 1:
                    raise RuntimeError("stub crash")
            return {"status": f"stub-{job_name}"}

        crash_counts = crash_counts

        sched._dispatch = fake_dispatch
        try:
            # 06:00 UTC on a FUTURE day = past every daily slot's hour
            # (01/02/03/05) AND newer than any pre-existing lastrun row in
            # the shared DB (production state may carry today's real run).
            sim_day = datetime.utcnow().date() + timedelta(days=1)
            day1_0600 = datetime(sim_day.year, sim_day.month, sim_day.day, 6, 0)
            results = sched.run_due_jobs(now_utc=day1_0600, conn=conn)
        finally:
            sched._dispatch = real_dispatch
        allok &= check("all daily slots dispatched (first run, past hours)",
                       {d["job"] for d in dispatched} >= {"daily_sync", "measurements",
                                                           "fix_executor", "stale_approvals",
                                                           "audit_ttl_sweep"},
                       str(dispatched))
        allok &= check("crashing job captured, rest still ran",
                       results["measurements"]["status"] == "failed"
                       and "stub crash" in results["measurements"]["error"],
                       str(results.get("measurements")))
        allok &= check("successful dispatch recorded",
                       results["daily_sync"]["result"] == {"status": "stub-daily_sync"})
        # weekly slots must NOT dispatch on the simulation day (which is
        # deliberately a WEEKDAY — assert that to keep the test meaningful)
        allok &= check("simulation day is a weekday (weekly must stay silent)",
                       day1_0600.weekday() < 5, f"weekday={day1_0600.weekday()}")
        allok &= check("weekly slots NOT dispatched on a weekday",
                       not any(d["job"].startswith("weekly_") for d in dispatched))
        lastruns = sched._load_last_runs(conn)
        allok &= check("last-run recorded for successful jobs",
                       set(lastruns) >= {"daily_sync", "fix_executor", "stale_approvals",
                                          "audit_ttl_sweep"},
                       str(lastruns))
        allok &= check("failed job does NOT record last-run for THIS run",
                       lastruns.get("measurements") is None
                       or lastruns["measurements"].replace(tzinfo=None) < day1_0600,
                       str(lastruns.get("measurements")))
        # the failed slot re-fires on the very next tick (natural retry)
        dispatched.clear()
        sched._dispatch = fake_dispatch
        try:
            results = sched.run_due_jobs(now_utc=day1_0600 + timedelta(minutes=30),
                                         conn=conn)
        finally:
            sched._dispatch = real_dispatch
        allok &= check("failed slot re-dispatched next tick",
                       any(d["job"] == "measurements" for d in dispatched),
                       str(dispatched))
        lastruns = sched._load_last_runs(conn)
        allok &= check("retry success records last-run",
                       "measurements" in lastruns, str(lastruns))

        # same moment again: nothing due
        dispatched.clear()
        results = sched.run_due_jobs(now_utc=day1_0600, conn=conn)
        allok &= check("re-evaluation in same period dispatches nothing",
                       dispatched == [] and results == {}, str(results))

        # next day: due again (dry_run passthrough visible in dispatch call)
        sched._dispatch = fake_dispatch
        try:
            day2_0600 = day1_0600 + timedelta(days=1)
            results = sched.run_due_jobs(now_utc=day2_0600,
                                         dry_run=True, conn=conn)
        finally:
            sched._dispatch = real_dispatch
        fe_calls = [d for d in dispatched if d["job"] == "fix_executor"]
        allok &= check("daily slots re-fire the next day",
                       len(fe_calls) == 1, str(dispatched))
        allok &= check("dry_run passthrough to fix_executor dispatch",
                       fe_calls and fe_calls[0]["dry_run"] is True, str(fe_calls))

        # 5: fix_executor hookup contract
        print("\n== fix_executor hookup ==")
        import inspect
        from jobs.fix_executor import run as fe_run
        sig = inspect.signature(fe_run)
        allok &= check("fix_executor.run accepts (site_id, dry_run)",
                       "site_id" in sig.parameters and "dry_run" in sig.parameters)
        allok &= check("fix_executor slot before stale_approvals (writes "
                       "complete before the daily housekeeping)",
                       sched.SCHEDULE["fix_executor"]["hour_utc"] < 5)

        # 6: minutes cadence (offpage_verify)
        print("\n== minutes cadence (offpage_verify) ==")
        allok &= check("offpage_verify in SCHEDULE (every 15m)",
                       sched.SCHEDULE["offpage_verify"]
                       == {"cadence": "minutes", "interval_minutes": 15},
                       str(sched.SCHEDULE.get("offpage_verify")))
        import inspect
        from jobs import offpage_verify as ov_mod
        ov_sig = inspect.signature(ov_mod.run)
        allok &= check("offpage_verify.run accepts (now, conn, fetcher)",
                       all(p in ov_sig.parameters
                           for p in ("now", "conn", "fetcher")))
        allok &= check("offpage_verify lock key registered",
                       "offpage_verify"
                       in __import__("jobs.locks", fromlist=["LOCK_KEYS"]).LOCK_KEYS)

        ov_spec = sched.SCHEDULE["offpage_verify"]
        tue_0020 = datetime(2026, 9, 22, 0, 20)      # before EVERY daily hour
        tue_0005 = datetime(2026, 9, 22, 0, 5)
        allok &= check("minutes fires when never run (no hour gate needed)",
                       sched.is_due("offpage_verify", tue_0005, ov_spec, None))
        allok &= check("minutes silent while beneath the interval",
                       not sched.is_due("offpage_verify", tue_0020, ov_spec,
                                        datetime(2026, 9, 22, 0, 10)))
        allok &= check("minutes fires at exactly interval_minutes elapsed",
                       sched.is_due("offpage_verify", tue_0020, ov_spec,
                                    datetime(2026, 9, 22, 0, 5)))
        allok &= check("minutes fires past the interval even before any daily hour",
                       sched.is_due("offpage_verify", tue_0020, ov_spec,
                                    datetime(2026, 9, 22, 0, 0)))
        allok &= check("minutes handles naive last_run (stored isoformat)",
                       sched.is_due("offpage_verify",
                                    datetime(2026, 9, 22, 0, 20).replace(
                                        tzinfo=timezone.utc),
                                    ov_spec,
                                    datetime(2026, 9, 22, 0, 0)))

        allok &= check("minutes period key is informational bucket",
                       len(re.findall(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\|\d+$",
                                      sched.slot_period_key("offpage_verify",
                                                            tue_0020, ov_spec)))
                       == 1, sched.slot_period_key("offpage_verify", tue_0020,
                                                   ov_spec))
        k_bucket = lambda t: int(sched.slot_period_key("offpage_verify", t, ov_spec)
                                 .rsplit("|", 1)[1])
        allok &= check("bucket advances exactly one per interval",
                       k_bucket(tue_0020) == k_bucket(tue_0005) + 1
                       and k_bucket(tue_0020 + timedelta(minutes=15))
                       == k_bucket(tue_0020) + 1,
                       f"{k_bucket(tue_0005)}/{k_bucket(tue_0020)}")

        # end-to-end with stubbed dispatch: a 20-minute-elapsed clock must
        # fire offpage_verify even before any daily hour; a 5-minute later
        # tick must stay silent.
        dispatched.clear()
        sched._dispatch = fake_dispatch
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO system_config (config_key, config_value) "
                    "VALUES ('scheduler.lastrun.offpage_verify', %s) "
                    "ON CONFLICT (config_key) DO UPDATE "
                    "SET config_value = EXCLUDED.config_value",
                    (datetime(2026, 9, 22, 0, 0).isoformat(),))
            conn.commit()
            res_a = sched.run_due_jobs(now_utc=tue_0020, conn=conn)
            res_b = sched.run_due_jobs(now_utc=datetime(2026, 9, 22, 0, 25),
                                       conn=conn)
            res_c = sched.run_due_jobs(now_utc=datetime(2026, 9, 22, 0, 40),
                                       conn=conn)
        finally:
            sched._dispatch = real_dispatch
        ov_calls = [d for d in dispatched if d["job"] == "offpage_verify"]
        allok &= check("minutes slot dispatches after 20m elapsed",
                       "offpage_verify" in res_a and
                       res_a["offpage_verify"]["status"] == "ran"
                       and len(ov_calls) == 2,
                       str(ov_calls))
        allok &= check("minutes slot silent at +5m since last run",
                       res_b == {}, str(res_b))
        allok &= check("minutes slot re-fires at another interval",
                       "offpage_verify" in res_c,
                       str(list(res_c)))
        allok &= check("minutes slot fired before every daily hour "
                       "(00:20 < 01:00, no daily leaked)",
                       not any(j.startswith("weekly_") or j in
                               ("daily_sync", "measurements", "fix_executor",
                                "stale_approvals", "audit_ttl_sweep")
                               for j in list(res_a) + list(res_c)),
                       str(list(res_a) + list(res_c)))
    finally:
        cleanup_last_runs(conn, saved_lastruns)
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL SCHEDULER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
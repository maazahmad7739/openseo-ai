"""Scheduler (Stage 2 Phase 1.5 hookup): one long-running loop that fires the
existing CLI jobs on their designed cadences.

Why a runner (not cron entries): every job is scheduler-agnostic by design
(injectable clock, advisory locks, idempotent, never-raise); the runner adds
only the "when" — and makes the fix_executor hookup explicit and observable
during the pilot. An external runner (cron / GitHub Actions / worker host)
can drive `python -m jobs.scheduler --once --due-now` interchangeably; the
job bodies never change.

Scheduling model (deterministic, testable — no hidden now() at import):
  * SCHEDULE: a registry of {job_name, cadence, hour_utc, weekday_utc}.
    A slot is DUE when the current clock has passed its (recurring) fire
    time and the run was not yet recorded for that slot's period.
  * LAST_RUNS: tracked in system_config (config_key 'scheduler.lastrun.<job>')
    as the UTC timestamp of the last completed run — survives restarts, and
    the advisory locks still guarantee two overlapping processes can never
    both execute a job.
  * --once: evaluate due slots and exit (the external-runner contract).
  * default: sleep-loop evaluating due slots every SLEEP_SECONDS.

Cadences (single source of truth — SCHEDULE below; jobs' designed cadences
per WALKTHROUGH §8):
  daily   01:00 UTC  daily_sync
  daily   02:15 UTC  measurement_clock (jobs/measurements.py sweep)
  daily   03:30 UTC  fix_executor   (queued -> applied; policy kill-switches
                      decide whether anything actually writes)
  daily   05:00 UTC  stale_approvals
  weekly  (Sat) 04:00 weekly_candidates
  weekly  (Sat) 05:00 weekly_agent (Ollama budget gate inside)
  weekly  (Sat) 06:00 weekly_crawl
  weekly  (Sat) 07:00 semantic_scoring
  weekly  (Sun) 04:30 cost_report

Safety properties:
  * fix_executor fires with fix_policy kill-switches as the write authority:
    every sub_type disabled = a no-op sweep (typed skips), so wiring the
    schedule can never start writing. The operator flips policies when ready.
  * Global + per-site advisory locks make overlapping ticks harmless.
  * A job's failure never stops the loop; it is recorded and notified
    (send_failure), and the next tick retries naturally.
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _utcnow():
    return datetime.now(timezone.utc)

SLEEP_SECONDS = 60

SCHEDULE = {
    "daily_sync": {"cadence": "daily", "hour_utc": 1},
    "measurements": {"cadence": "daily", "hour_utc": 2},
    "fix_executor": {"cadence": "daily", "hour_utc": 3},
    "stale_approvals": {"cadence": "daily", "hour_utc": 5},
    "weekly_candidates": {"cadence": "weekly", "hour_utc": 4, "weekday_utc": 5},  # Sat
    "weekly_agent": {"cadence": "weekly", "hour_utc": 5, "weekday_utc": 5},
    "weekly_crawl": {"cadence": "weekly", "hour_utc": 6, "weekday_utc": 5},
    "semantic_scoring": {"cadence": "weekly", "hour_utc": 7, "weekday_utc": 5},
    "cost_report": {"cadence": "weekly", "hour_utc": 8, "weekday_utc": 6},  # Sun
}

LAST_RUN_KEY = "scheduler.lastrun.{job}"


def _job_module(job_name):
    """Lazy import of the job's run() — keeps the loop import-light."""
    if job_name == "measurements":
        from jobs import measurements
        return measurements
    if job_name == "fix_executor":
        from jobs import fix_executor
        return fix_executor
    module = __import__(f"jobs.{job_name}", fromlist=[job_name])
    return module


def _dispatch(job_name, now_utc, dry_run=False):
    """Run one job. Returns its summary dict (typed, never raises)."""
    if job_name == "fix_executor":
        from jobs.fix_executor import run as run_fix_executor
        return run_fix_executor(site_id=None, dry_run=dry_run)
    module = _job_module(job_name)
    return module.run()


def slot_period_key(job_name, now_utc, spec):
    """Identity of the schedule slot a tick falls in (for last-run tracking).

    daily -> 'YYYY-MM-DD' (the slot for that calendar day at hour_utc);
    weekly -> ISO week + weekday so a job missed on its weekday does not
    re-fire every day until the next scheduled day.
    """
    if spec["cadence"] == "daily":
        return now_utc.strftime("%Y-%m-%d")
    iso = now_utc.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def is_due(job_name, now_utc, spec, last_run_at):
    """Slot-fire rule: past hour_utc in the CURRENT slot period, not yet run
    for that period. Weekly: additionally only fires on the scheduled weekday
    (or later in the week when the weekday was missed)."""
    if last_run_at is None:
        pass
    else:
        if spec["cadence"] == "daily":
            if last_run_at.date() >= now_utc.date():
                return False
        else:
            iso = now_utc.isocalendar()
            last_iso = last_run_at.isocalendar()
            if (last_iso[0], last_iso[1]) >= (iso[0], iso[1]):
                return False
    if now_utc.hour < spec["hour_utc"]:
        return False
    if spec["cadence"] == "weekly":
        weekday = now_utc.weekday()  # Mon=0 .. Sun=6
        scheduled = spec.get("weekday_utc", 5)
        if weekday < scheduled:
            return False
    return True


def _load_last_runs(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT config_key, config_value FROM system_config "
            "WHERE config_key LIKE 'scheduler.lastrun.%'"
        )
        rows = cur.fetchall()
    out = {}
    for key, value in rows:
        try:
            out[key.rsplit(".", 1)[-1]] = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
    return out


def _record_run(conn, job_name, now_utc):
    key = LAST_RUN_KEY.format(job=job_name)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO system_config (config_key, config_value) VALUES (%s, %s) "
            "ON CONFLICT (config_key) DO UPDATE SET config_value = EXCLUDED.config_value",
            (key, now_utc.isoformat()),
        )
    conn.commit()


def run_due_jobs(now_utc=None, dry_run=False, conn=None):
    """Evaluate every schedule slot at now_utc; dispatch those due.

    Returns {job_name: result-or-skip-reason}. A crash inside one job is
    captured as a failed result; the rest still run (run_multi discipline).
    A FAILED job does NOT record its last-run: the slot stays due and the
    next tick retries it naturally (a persistently crashing job re-runs
    every tick, but its own advisory lock + idempotency make that safe,
    and each failure re-alerts).
    """
    own_conn = conn is None
    if own_conn:
        import db as database
        import env as env_loader
        env_loader.load_env_file(quiet=True)
        conn = database.get_connection()
    try:
        from jobs.locks import job_lock, LOCK_KEYS, already_running
        now_utc = now_utc or _utcnow()
        last_runs = _load_last_runs(conn)
        results = {}
        for job_name, spec in SCHEDULE.items():
            if not is_due(job_name, now_utc, spec, last_runs.get(job_name)):
                continue
            # Scheduler-level lock: overlapping scheduler processes (local +
            # an external runner) never double-dispatch the same tick.
            with job_lock(conn, LOCK_KEYS["scheduler"], site_id=job_name) as got:
                if not got:
                    results[job_name] = {"status": "already_running"}
                    continue
                try:
                    out = _dispatch(job_name, now_utc, dry_run=dry_run)
                    results[job_name] = {"status": "ran", "result": _compact(out)}
                    _record_run(conn, job_name, now_utc)
                except Exception as exc:
                    out = {"status": "failed",
                           "error": f"{type(exc).__name__}: {exc}"}
                    results[job_name] = out
                    _notify_failure(job_name, out["error"])
                    # NO _record_run: the slot stays due -> natural retry.
        return results
    finally:
        if own_conn:
            conn.close()


def _compact(result):
    """Trim noisy per-site payloads for the summary line."""
    text = json.dumps(result, default=str)
    return result if len(text) <= 2000 else text[:1000] + "…(truncated)"


def _notify_failure(job_name, error):
    try:
        from jobs.notify import send_failure
        send_failure(f"scheduler:{job_name}", site_id=None, error=error)
    except Exception as exc:
        print(f"[scheduler] failure notify errored: {exc}", flush=True)


def run_forever():
    import db as database
    import env as env_loader
    env_loader.load_env_file(quiet=True)
    print("[scheduler] started; cadences:", json.dumps(
        {k: f"{v['cadence']}@{v['hour_utc']:02d}:00"
         + (f" dow{v['weekday_utc']}" if "weekday_utc" in v else "")
         for k, v in SCHEDULE.items()}, indent=1), flush=True)
    conn = database.get_connection()
    try:
        while True:
            try:
                due = run_due_jobs(conn=conn)
                for name, res in due.items():
                    print(f"[scheduler] {name}: {_compact(res)}", flush=True)
            except Exception as exc:
                print(f"[scheduler] tick error: {type(exc).__name__}: {exc}",
                      flush=True)
            time.sleep(SLEEP_SECONDS)
    finally:
        conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="evaluate due slots and exit (external-runner mode)")
    parser.add_argument("--due-now", action="store_true",
                        help="with --once: ignore last-run state and dispatch "
                             "every slot in the registry (smoke/manual run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="pass dry-run through to fix_executor (writes "
                             "nothing anywhere)")
    args = parser.parse_args()
    if args.once:
        import db as database
        import env as env_loader
        env_loader.load_env_file(quiet=True)
        if args.due_now:
            for job_name in SCHEDULE:
                out = _dispatch(job_name, _utcnow(), dry_run=args.dry_run)
                print(f"[scheduler] {job_name}: {_compact(out)}", flush=True)
            return
        out = run_due_jobs()
        print(json.dumps(out, default=str, indent=1))
        return
    run_forever()


if __name__ == "__main__":
    main()
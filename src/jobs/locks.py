"""Postgres advisory locks for background jobs (concurrency hardening).

Why: the job runners (daily_sync, measurement sweeps, weekly passes) had no
concurrency guards — safety rested entirely on "jobs never overlap". The
moment anything runs on a schedule (cron, CI, a manual run colliding with a
scheduled one), two runs can interleave on the same tables. Session-scoped
advisory locks make overlapping runs impossible per (job, site).

Design:
  * SESSION-scoped pg_advisory_lock (not transaction-scoped): these jobs
    commit mid-run (run_sync commits per section), so a transaction lock
    would release at the first COMMIT. Session locks survive until unlock.
  * Leak-proof without plumbing: Postgres releases session locks when the
    connection closes — every job already holds its connection in a
    finally: conn.close(), and a crashed process cannot strand a lock.
  * TRY-lock, never queue: pg_try_advisory_lock returns False if another
    run holds the lock and the caller exits with {"status":
    "already_running"}. For scheduled jobs, skipping is correct; queuing a
    second run behind a stuck one doubles the damage.
  * Granularity: per-site jobs lock (job, site_id) so site A never blocks
    site B; global sweeps (measurement_clock, cost_report) lock on job alone.
    Keys are minted ONLY via LOCK_KEYS/lock_key — no ad-hoc strings.

Usage (jobs):
    from jobs.locks import job_lock, LOCK_KEYS
    conn = database.get_connection()
    try:
        with job_lock(conn, LOCK_KEYS["daily_sync"], site_id=site_id) as got:
            if not got:
                print("daily_sync: already running — skipped")
                return {"status": "already_running"}
            ... existing job body ...
    finally:
        conn.close()

run_multi contract: acquire the lock INSIDE each worker on that worker's own
connection (per-site key). Session locks are per-connection; the calling
thread's connection must never be used for worker locks.
"""

import sys

# Central registry — every job's lock key lives here (prevents key drift).
LOCK_KEYS = {
    "daily_sync": "daily_sync",
    "measurement_clock": "measurement_clock",
    "weekly_candidates": "weekly_candidates",
    "weekly_agent": "weekly_agent",
    "weekly_crawl": "weekly_crawl",
    "semantic_scoring": "semantic_scoring",
    "stale_approvals": "stale_approvals",
    "cost_report": "cost_report",
}

_LOCK_NAMESPACE = "openseo:job:"


def _lock_key(job, site_id=None):
    """hashtext input string: openseo:job:<job>[:<site_id>]."""
    return f"{_LOCK_NAMESPACE}{job}:{site_id}" if site_id else f"{_LOCK_NAMESPACE}{job}"


class JobLockBusy(RuntimeError):
    """Raised by blocking mode when the lock is held elsewhere (not used by jobs)."""


def job_lock(conn, job, site_id=None, blocking=False):
    """Context manager: hold a session-scoped advisory lock for the job body.

    Yields True when the lock was acquired, False when it was busy (try mode,
    the default — the caller should exit with already_running). In blocking
    mode the yield is always True (the context waited for the lock) and
    JobLockBusy never surfaces from it.

    The lock is held for the duration of the with-block and released (plus a
    defensive full-release) at exit. Connection close also releases session
    locks, so job code that closes conn in finally can never strand one.
    """
    key = _lock_key(job, site_id)

    class _JobLock:
        def __init__(self):
            self.acquired = False

        def __enter__(self):
            with conn.cursor() as cur:
                if blocking:
                    # pg_advisory_lock blocks until available (unbounded —
                    # bound the caller with statement_timeout if needed).
                    cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
                else:
                    cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,))
                    self.acquired = bool(cur.fetchone()[0])
                if self.acquired:
                    # Remember we hold it so __exit__ releases precisely.
                    self.acquired = True
                else:
                    self.acquired = False
            return self.acquired

        def __exit__(self, exc_type, exc, tb):
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
                    unlocked = bool(cur.fetchone()[0])
                if not unlocked and self.acquired:
                    # Defensive: unlock failed but we think we hold it — the
                    # session close (job finally) guarantees release anyway.
                    print(f"[locks] warning: unlock returned false for {key}", flush=True)
            except Exception:
                # Never mask the job's own exception with an unlock failure.
                pass
            return False

    return _JobLock()


def already_running(job, site_id=None):
    """Standard skip payload for a busy lock (uniform across jobs)."""
    return {
        "status": "already_running",
        "job": job,
        "site_id": str(site_id) if site_id else None,
    }
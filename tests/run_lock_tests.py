# -*- coding: utf-8 -*-
"""Advisory-lock concurrency tests (concurrency hardening).

Verifies the job_lock contract on a real Postgres:
  1. try-mode acquire succeeds when free
  2. second connection CANNOT acquire while held
  3. per-site granularity: a different site_id is NOT blocked
  4. release restores availability (retry succeeds)
  5. exception inside the with-block still releases the lock
  6. connection close releases the lock even without explicit unlock
  7. already_running() payload shape

Run: python tests/run_lock_tests.py   (needs a reachable local Postgres)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import db as database  # noqa: E402
from jobs.locks import job_lock, LOCK_KEYS, already_running  # noqa: E402

SITE_A = "11111111-1111-1111-1111-111111111111"
SITE_B = "22222222-2222-2222-2222-222222222222"


def check(name, value, detail=""):
    print(f"[{'PASS' if value else 'FAIL'}] {name}" + (f" — {detail}" if detail and not value else ""))
    return value


def main():
    allok = True
    conn1 = database.get_connection()
    conn2 = database.get_connection()
    try:
        # 1-4: basic try-lock semantics across two connections
        with job_lock(conn1, LOCK_KEYS["daily_sync"], site_id=SITE_A) as got:
            allok &= check("acquire on free lock succeeds", got is True)
            with job_lock(conn2, LOCK_KEYS["daily_sync"], site_id=SITE_A) as got2:
                allok &= check("second connection blocked while held", got2 is False)
            with job_lock(conn2, LOCK_KEYS["daily_sync"], site_id=SITE_B) as got3:
                allok &= check("different site_id NOT blocked", got3 is True)
        with job_lock(conn2, LOCK_KEYS["daily_sync"], site_id=SITE_A) as got4:
            allok &= check("retry succeeds after release", got4 is True)

        # 5: exception inside the with-block releases the lock
        released = False
        try:
            with job_lock(conn1, LOCK_KEYS["weekly_agent"], site_id=SITE_A):
                raise RuntimeError("job body exploded")
        except RuntimeError:
            pass
        with job_lock(conn2, LOCK_KEYS["weekly_agent"], site_id=SITE_A) as got5:
            released = got5 is True
        allok &= check("exception inside job body still releases lock", released)

        # 6: connection close releases a still-held session lock
        conn3 = database.get_connection()
        with job_lock(conn3, LOCK_KEYS["measurement_clock"]) as got6:
            allok &= check("conn3 acquires global lock", got6 is True)
        conn3.close()
        with job_lock(conn1, LOCK_KEYS["measurement_clock"]) as got7:
            allok &= check("lock released by connection close (crash safety)", got7 is True)

        # 7: already_running payload
        payload = already_running("daily_sync", SITE_A)
        allok &= check("already_running payload shape",
                       payload == {"status": "already_running",
                                   "job": "daily_sync",
                                   "site_id": SITE_A}, str(payload))

        # 8: distinct jobs do not collide on the same site
        with job_lock(conn1, LOCK_KEYS["daily_sync"], site_id=SITE_A) as g8:
            with job_lock(conn2, LOCK_KEYS["weekly_candidates"], site_id=SITE_A) as g9:
                allok &= check("different jobs on same site do not collide",
                               g8 is True and g9 is True)
    finally:
        conn1.close()
        conn2.close()

    print("\nLOCK TEST RUN", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
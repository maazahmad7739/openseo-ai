"""TTL sweep for audit session data (plan/23 §2.2, §6 Phase A item 4).

Prunes audit_sessions rows past expires_at; child tables
(audit_page_snapshots, audit_serp_competitors) cascade. Scheduler-driven
for determinism (plan/23 §6 Phase A: "pick scheduler for determinism") —
registered in jobs/scheduler.py SCHEDULE as the daily 06:00 UTC slot;
also runnable standalone:  python -m audit.ttl_sweep

never-raise contract: run() returns a typed summary, a DB outage is
captured as {'ok': False, 'error': ...} like the other jobs.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as database  # noqa: E402


def sweep(conn, now_expr="now()"):
    """DELETE expired sessions; returns {'ok', 'pruned', 'count'} (typed,
    never raises unless the caller passes a broken conn — job wrapper
    catches).

    Commits before returning: the caller may pass a connection it closes
    immediately after (the scheduler wrapper closes in finally), and a
    rollback-on-close must never silently discard the prune.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM audit_sessions
            WHERE expires_at <= {now_expr}::timestamptz
            RETURNING session_id
            """
        )
        pruned = [str(row[0]) for row in cur.fetchall()]
    conn.commit()
    return {"ok": True, "pruned": pruned, "count": len(pruned)}


def run():
    """Scheduler/CLI entry (matches the jobs' run() contract).

    Advisory-locked (LOCK_KEYS['audit_ttl_sweep']) so a manual
    `python -m audit.ttl_sweep` can never overlap the scheduled run; a busy
    lock returns the standard already_running payload. Never-raise: a DB
    outage is captured as {'ok': False, 'error': ...} like the other jobs.
    """
    import env as env_loader
    env_loader.load_env_file(quiet=True)
    try:
        conn = database.get_connection()
    except Exception as exc:
        return {"ok": False, "error": f"db_connect: {exc}"}
    try:
        from jobs.locks import job_lock, LOCK_KEYS, already_running
        with job_lock(conn, LOCK_KEYS["audit_ttl_sweep"]) as got:
            if not got:
                return already_running("audit_ttl_sweep")
            summary = sweep(conn)
            print(f"[audit_ttl_sweep] pruned {summary.get('count', 0)} "
                  f"expired audit session(s)", flush=True)
            return summary
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2))
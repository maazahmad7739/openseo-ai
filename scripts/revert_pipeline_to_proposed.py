# -*- coding: utf-8 -*-
"""One-off production revert: reset implemented recommendations to 'proposed'.

Resets the two rows the operator implemented by mistake on 2026-09-24
(/collections/snowboard-stomp-pad, /products/the-complete-snowboard) back to
their initial 'proposed' state:

  * status  in_progress -> proposed
  * implemented_at, measurement_due_at, assigned_to -> cleared
    (measurement_due_at is recomputed with the agent's own formula:
     now() + measurement_window_days, matching a fresh proposal)
  * associated baseline artifacts removed: change_log implementation
    markers deleted; measurement_snapshots verified empty (there is no
    fix_measurements table -- baselines live in change_log + snapshots)

Run:  python scripts/revert_pipeline_to_proposed.py          (dry-run print)
      python scripts/revert_pipeline_to_proposed.py --apply (commit)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def _parse_env(path):
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip().strip('"')


_apply = "--apply" in sys.argv
_parse_env(os.path.join(ROOT, ".env.vercel-prod"))

import db as database  # noqa: E402

TARGET_URLS = [
    "%/collections/snowboard-stomp-pad",
    "%/products/the-complete-snowboard",
]

conn = database.get_connection()
try:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT recommendation_id, status, action_type, target_url, proposed_url
            FROM recommendations
            WHERE status = 'in_progress'
              AND (target_url LIKE %s OR target_url LIKE %s
                   OR proposed_url LIKE %s OR proposed_url LIKE %s)
            """,
            TARGET_URLS * 2,
        )
        rows = cur.fetchall()
    print("targets:", [(str(r[0])[:8], r[1], r[2], r[3]) for r in rows])
    if len(rows) != 2:
        print("ABORT: expected exactly 2 in_progress rows, found", len(rows))
        sys.exit(1)

    ids = [str(r[0]) for r in rows]
    if not _apply:
        print("DRY RUN — nothing written. Re-run with --apply to commit.")
        sys.exit(0)

    with conn.cursor() as cur:
        # Baseline artifacts first (FK-safe order).
        cur.execute(
            "DELETE FROM change_log WHERE recommendation_id = ANY(%s::uuid[])",
            (ids,),
        )
        deleted_log = cur.rowcount
        cur.execute(
            "DELETE FROM measurement_snapshots WHERE recommendation_id = ANY(%s::uuid[])",
            (ids,),
        )
        deleted_snaps = cur.rowcount
        # Status reset back to the initial proposal state. measurement_due_at
        # is recomputed with the agent's own formula (now + window) so the row
        # reads exactly like a fresh proposal.
        cur.execute(
            """
            UPDATE recommendations SET
                status = 'proposed',
                implemented_at = NULL,
                assigned_to = NULL,
                result = 'pending',
                measured_at = NULL,
                measurement_due_at = now() + (measurement_window_days || ' days')::interval
            WHERE recommendation_id = ANY(%s::uuid[])
              AND status = 'in_progress'
            """,
            (ids,),
        )
        updated = cur.rowcount
    conn.commit()
    print(f"applied: {updated} recommendation(s) -> proposed, "
          f"{deleted_log} change_log row(s) deleted, {deleted_snaps} snapshot row(s) deleted")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT recommendation_id, status, implemented_at, measurement_due_at
            FROM recommendations WHERE recommendation_id = ANY(%s::uuid[])
            """,
            (ids,),
        )
        for r in cur.fetchall():
            print("verify:", str(r[0])[:8], r[1], "impl:", r[2], "due:", r[3])
        cur.execute(
            "SELECT count(*) FROM measurement_snapshots WHERE recommendation_id = ANY(%s::uuid[])",
            (ids,),
        )
        print("remaining baseline rows:", cur.fetchone()[0])
finally:
    conn.close()
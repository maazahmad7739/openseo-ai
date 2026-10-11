"""TEMPORARY maintenance endpoint (prod migration bootstrap).

Applies the ledger-based schema migrations (same semantics as
scripts/migrate.py) inside the deployed runtime, where POSTGRES_* are
injected server-side. DDL-only: reads the committed plan/*.sql files,
no request data is ever interpreted. Idempotent + ledger-guarded.

This router exists ONLY to run migrations against environments that
cannot pull secrets locally (Vercel 'hidden' env vars + managed hosts).
Remove it once the production schema is up to date.
"""

import sys
from pathlib import Path

from fastapi import APIRouter

import db as database

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

LEDGER_TABLE = "schema_migrations"

_NON_MIGRATIONS = {
    "00-schema.sql",
    "01-generator-missing-pages.sql",
    "02-generator-existing-opportunities.sql",
    "03-generator-technical-root-causes.sql",
    "04-generator-cannibalization.sql",
    "05-generator-internal-links.sql",
}


def _sort_key(path):
    try:
        return (int(path.name.split("-", 1)[0]), path.name)
    except ValueError:
        return (10_000, path.name)


@router.get("/migrate")
def run_migrations():
    root = Path(__file__).resolve().parents[3]
    migrations_dir = root / "plan"
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} ("
                        "version TEXT PRIMARY KEY,"
                        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(f"SELECT version FROM {LEDGER_TABLE}")
            applied = {row[0] for row in cur.fetchall()}
        pending = [
            path for path in sorted(migrations_dir.glob("*.sql"), key=_sort_key)
            if path.name not in applied and path.name not in _NON_MIGRATIONS
        ]
        applied_now, failures = [], []
        for path in pending:
            try:
                with conn.cursor() as cur:
                    cur.execute(path.read_text(encoding="utf-8"))
                    cur.execute(
                        f"INSERT INTO {LEDGER_TABLE} (version) VALUES (%s)",
                        (path.name,))
                conn.commit()
                applied_now.append(path.name)
            except Exception as exc:
                conn.rollback()
                failures.append({"migration": path.name, "error": str(exc)[:300]})
        with conn.cursor() as cur:
            cur.execute(f"SELECT version FROM {LEDGER_TABLE}")
            versions = sorted(row[0] for row in cur.fetchall())
        return {
            "ok": not failures,
            "applied": applied_now,
            "failures": failures,
            "ledger_count": len(versions),
            "ledger": versions,
        }
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
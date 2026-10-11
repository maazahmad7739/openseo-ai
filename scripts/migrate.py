"""Ordered schema migration runner for plan/*.sql (Category 2, item 2.3).

Creates and tracks a `schema_migrations` ledger table; applies every
plan/*.sql file in numeric-prefix order that is not yet recorded. First
run on a live DB that predates the ledger is safe: all existing plan
SQL files are written idempotent (IF NOT EXISTS / guarded DO blocks),
so re-apply is a no-op and the ledger simply catches up.

Usage:
    python scripts/migrate.py            # apply everything pending
    npm run migrate                      # same, via package.json script

Env: POSTGRES_* vars (see .env.example); defaults match the docker run
command (localhost:5433, openseo / openseo_local_only).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db as database  # noqa: E402

LEDGER_TABLE = "schema_migrations"
MIGRATIONS_DIR = ROOT / "plan"

# plan SQL files that are NOT schema migrations:
# - 00-schema.sql: legacy DDL predating the ledger, not written
#   idempotent (bare CREATE TABLE / CREATE INDEX); historically applied
#   by db.apply_schema().
# - 01..05-generator-*.sql: per-site generator query TEMPLATES executed at
#   runtime by src/generators/runner.py (run_generator binds :site_id /
#   :reference_date); they are queries, never DDL to apply at deploy time.
NON_MIGRATIONS = {
    "00-schema.sql",
    "01-generator-missing-pages.sql",
    "02-generator-existing-opportunities.sql",
    "03-generator-technical-root-causes.sql",
    "04-generator-cannibalization.sql",
    "05-generator-internal-links.sql",
}


def _sort_key(path):
    """Numeric prefix order: 00-schema.sql, 01-…, 29-…."""
    try:
        return (int(path.name.split("-", 1)[0]), path.name)
    except ValueError:
        return (10_000, path.name)  # unnumbered files sort last, stable


def ensure_ledger(conn):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
                version     TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    conn.commit()


def applied_versions(conn):
    with conn.cursor() as cur:
        cur.execute(f"SELECT version FROM {LEDGER_TABLE}")
        return {row[0] for row in cur.fetchall()}


def apply_migration(conn, path):
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
        cur.execute(
            f"INSERT INTO {LEDGER_TABLE} (version) VALUES (%s)", (path.name,)
        )
    conn.commit()


def main():
    database.get_connection  # touch: fail fast if the module cannot load
    conn = database.get_connection()
    try:
        ensure_ledger(conn)
        applied = applied_versions(conn)
        pending = [
            path
            for path in sorted(MIGRATIONS_DIR.glob("*.sql"), key=_sort_key)
            if path.name not in applied and path.name not in NON_MIGRATIONS
        ]
        if not pending:
            print("migrate: nothing to apply (schema up to date)")
            return 0
        failures = []
        for path in pending:
            try:
                apply_migration(conn, path)
                print(f"apply {path.name}")
            except Exception as exc:  # noqa: BLE001 - report, keep going
                conn.rollback()
                failures.append((path.name, str(exc)))
                print(f"FAIL  {path.name}: {exc}")
        all_sql = sorted(
            p.name for p in MIGRATIONS_DIR.glob("*.sql")
            if p.name not in NON_MIGRATIONS
        )
        for name in (n for n in all_sql if n in applied):
            print(f"skip  {name}")
        for name in sorted(NON_MIGRATIONS):
            print(f"excl  {name} (not a migration)")
        if failures:
            print(f"migrate: {len(failures)} file(s) failed")
            return 1
        print("migrate: done")
        return 0
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
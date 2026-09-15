"""Local Postgres connection + schema setup (Docker, phase 1).

Reads POSTGRES_* env vars (see .env.example); defaults match the docker run
command (localhost:5433, db/user/pass openseo / openseo / openseo_local_only).
"""

import os
import json
import hashlib

DB_PARAMS_DEFAULT = {
    "host": "localhost",
    "port": "5433",
    "dbname": "openseo",
    "user": "openseo",
    "password": "openseo_local_only",
}

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plan", "00-schema.sql")


def _register_uuid_adapter():
    """Adapt uuid.UUID natively so callers may pass uuid objects directly.

    psycopg2 does not adapt Python uuid.UUID by default; the pipeline has
    always passed strings, but test suites and callers pass raw uuid objects
    too.  Registering the standard text adapter once makes both work and is
    psycopg2's canonical approach (psycopg2.extras.register_uuid).
    """
    try:
        import psycopg2.extras
        psycopg2.extras.register_uuid()
    except Exception:
        pass


_register_uuid_adapter()


class DbError(Exception):
    pass


def get_connection(params=None):
    import psycopg2
    merged = dict(DB_PARAMS_DEFAULT)
    for env_key, param_key in (
        ("POSTGRES_HOST", "host"),
        ("POSTGRES_PORT", "port"),
        ("POSTGRES_DB", "dbname"),
        ("POSTGRES_USER", "user"),
        ("POSTGRES_PASSWORD", "password"),
    ):
        value = os.environ.get(env_key)
        if value:
            merged[param_key] = value
    if params:
        merged.update(params)
    return psycopg2.connect(**merged)


def apply_schema(conn=None, schema_path=SCHEMA_PATH):
    if conn is None:
        conn = get_connection()
    with open(schema_path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def md5_hash(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def qmarks(count):
    return ", ".join(["%s"] * count)


def insert_rows(conn, table, rows, conflict_target=None, update_keys=None):
    """Idempotent row insert (ON CONFLICT upsert when conflict_target given)."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({qmarks(len(cols))})"
    if conflict_target:
        sql += f" ON CONFLICT ({conflict_target}) DO UPDATE SET "
        sql += ", ".join(f"{k} = EXCLUDED.{k}" for k in update_keys)
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(sql, [row[c] for c in cols])
    return len(rows)


def load_fixture(name):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tests", "fixtures", name,
    )
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
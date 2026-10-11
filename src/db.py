"""Local Postgres connection + schema setup (Docker, phase 1).

Reads POSTGRES_* env vars (see .env.example); defaults match the docker run
command (localhost:5433, db/user/pass openseo / openseo / openseo_local_only).
"""

import os
import json
import hashlib
import threading

DB_PARAMS_DEFAULT = {
    "host": "localhost",
    "port": "5433",
    "dbname": "openseo",
    "user": "openseo",
    "password": "openseo_local_only",
}

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plan", "00-schema.sql")

# Localhost hosts where sslmode must NOT be forced (local Docker has no TLS).
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


def _db_params(params=None):
    """Resolved connection kwargs (env overrides + sslmode default).

    POSTGRES_SSLMODE wins when set; otherwise managed Postgres (Supabase
    pooler etc. — anything not localhost) defaults to sslmode=require so
    production credentials never cross the network unencrypted. Local
    Docker defaults to no sslmode (the container has none).
    """
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
    sslmode = os.environ.get("POSTGRES_SSLMODE")
    if not sslmode and not sslmode_default_disabled(merged["host"]):
        sslmode = "require"
    if sslmode:
        merged["sslmode"] = sslmode
    return merged


def sslmode_default_disabled(host):
    """True for local/dev hosts where sslmode=require would break the
    connection (local Postgres without TLS)."""
    return (host or "").strip().lower() in _LOCAL_HOSTS


def get_connection(params=None):
    """One fresh connection (standalone scripts, background jobs, tests).

    The API request path uses get_pooled_connection() instead — this stays
    the unchanged contract for the 23+ job/script call sites.
    """
    import psycopg2
    return psycopg2.connect(**_db_params(params))


# ------------------------------------------------------------
# Pooled connections (API request path only) — P3, item 2.2
# ------------------------------------------------------------

_pool = None
_pool_lock = threading.Lock()

# Serverless caution (blueprint §2.2): the pool is per warm instance; keep
# DB_POOL_MAX low (4-8) so Vercel's concurrent lambdas can't exhaust the
# Supabase pooler's connection slots.
DB_POOL_MIN_DEFAULT = 0
DB_POOL_MAX_DEFAULT = 8


def _pool_config():
    def _int_env(name, default):
        try:
            value = int(os.environ.get(name, "") or default)
        except (TypeError, ValueError):
            value = default
        return max(0, value)

    return (_int_env("DB_POOL_MIN", DB_POOL_MIN_DEFAULT),
            _int_env("DB_POOL_MAX", DB_POOL_MAX_DEFAULT))


def _get_pool():
    """Lazily-initialized process-wide ThreadedConnectionPool (locked)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            from psycopg2.pool import ThreadedConnectionPool
            minconn, maxconn = _pool_config()
            _pool = ThreadedConnectionPool(minconn, maxconn,
                                           **_db_params())
        return _pool


def get_pooled_connection():
    """Borrow a connection from the pool (never raises raw PoolError to the
    route: exhausted pools surface as a typed DbError -> HTTP 500 with a
    recognizable message instead of a traceback)."""
    from psycopg2.pool import PoolError
    try:
        return _get_pool().getconn()
    except PoolError as exc:
        raise DbError(f"connection pool exhausted: {exc}") from exc


def release_pooled_connection(conn):
    """Return a borrowed connection to the pool (rollback first: the next
    borrower must never inherit an open transaction). Broken connections
    are discarded via closeall-safe putconn(conn, close=True)."""
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        _get_pool().putconn(conn, close=conn.closed != 0)
    except Exception:
        # putconn failing means the pool itself is broken; force-close so
        # the socket never leaks even if the pool can't reuse it.
        try:
            conn.close()
        except Exception:
            pass


def close_pool():
    """Shutdown hook: close every pooled connection (idempotent)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
            finally:
                _pool = None


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
    """One fresh connection (standalone scripts, background jobs, tests).

    The API request path uses get_pooled_connection() instead — this stays
    the unchanged contract for the 23+ job/script call sites.
    """
    import psycopg2
    return psycopg2.connect(**_db_params(params))


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
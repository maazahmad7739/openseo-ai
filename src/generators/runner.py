"""Shared per-site generator runner.

Loads a generator SQL file from plan/, binds :site_id (named-param style in
the plan files) as a query parameter, and returns the candidate rows as dicts.
Candidates are NOT inserted here — insertion is the orchestrator's job, so
each generator stays a pure read-only producer (plan/09 §Generators).
"""

import os
import re
from datetime import date, datetime, timezone


def datetime_now_utc_date():
    return datetime.now(timezone.utc).date()

PLAN_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "plan"
))


class GeneratorError(Exception):
    pass


def _load_sql(generator_name):
    return load_generator_sql(generator_name)


def load_generator_sql(generator_name):
    path = os.path.join(PLAN_DIR, f"{generator_name}.sql")
    if not os.path.exists(path):
        raise GeneratorError(f"generator SQL not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def run_generator(conn, generator_file, site_id, limit=None, reference_date=None):
    """Execute one generator SQL for one site; returns list[dict].

    :site_id placeholders are rewritten to %(site_id)s and executed with the
    bound value, keeping the plan SQL authoritative. reference_date is bound
    as %(reference_date)s — it replaces the SQL's former CURRENT_DATE so the
    pipeline has no un-injectable wall-clock input; tests may pass any date.
    When omitted, the runner's "today" (UTC) is used and recorded.

    A trailing statement-level LIMIT in the SQL is respected; the `limit` arg
    is accepted for backward compatibility and is ignored when the SQL already
    carries a LIMIT clause (all plan generators do).
    """
    sql = _load_sql(generator_file)
    sql = sql.replace("%", "%%")
    sql = sql.replace(":site_id", "%(site_id)s")
    sql = sql.replace(":reference_date", "%(reference_date)s")
    params = {"site_id": site_id, "reference_date": reference_date or datetime_now_utc_date()}
    # Honor the limit arg only when the SQL carries no LIMIT of its own
    # (fix 3.4 — the parameter is no longer silently ignored).
    has_own_limit = re.search(r"\bLIMIT\s+\d+", sql, re.IGNORECASE) is not None
    if limit is not None and not has_own_limit:
        sql = sql.rstrip().rstrip(";")
        sql += f" LIMIT {int(limit)};"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
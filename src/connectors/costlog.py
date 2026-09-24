"""Cost logging + budget enforcement for paid API calls.

Provides three helpers:
  log_cost()     — insert a row into api_costs
  weekly_spend() — current week's total spend, grouped by service
  check_budget() — compare spend against budget_config cap
"""

import os
from datetime import datetime, timedelta, timezone


def _week_start():
    """Return Monday 00:00 UTC of the current ISO week."""
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def budget_env_var(service):
    """Return the env var that overrides this service's weekly cap."""
    env_map = {
        "openseo_serp": "WEEKLY_BUDGET_OPENSEO",
        "openseo_keyword_volume": "WEEKLY_BUDGET_OPENSEO",
        "openseo_competitors": "WEEKLY_BUDGET_OPENSEO",
        "openseo_backlinks": "WEEKLY_BUDGET_OPENSEO",
        "openseo_crawl_audit": "WEEKLY_BUDGET_OPENSEO",
        "gsc_url_inspection": "WEEKLY_BUDGET_GSC",
        "ollama_chat": "WEEKLY_BUDGET_OLLAMA",
        "*": "WEEKLY_BUDGET_GLOBAL",
    }
    return env_map.get(service, "WEEKLY_BUDGET_GLOBAL")


def _env_cap(service):
    """Read the weekly cap from the service-specific env var, if present."""
    val = os.environ.get(budget_env_var(service))
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def log_cost(conn, site_id, service, call_type=None, calls=1, cost=None,
             tokens_in=None, tokens_out=None, metadata=None):
    """Insert a row into api_costs."""
    import json
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_costs (site_id, service, call_type, calls, cost, tokens_in, tokens_out, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (site_id, service, call_type, calls, cost, tokens_in, tokens_out,
             json.dumps(metadata, default=str) if metadata else None),
        )


def weekly_spend(conn, service=None, site_id=None):
    """Return current week's total spend grouped by service.

    Returns list of (service, total_cost, total_calls) tuples.
    """
    week_start = _week_start()
    conditions = ["timestamp >= %s"]
    params = [week_start]
    if service:
        conditions.append("service = %s")
        params.append(service)
    if site_id:
        conditions.append("site_id = %s")
        params.append(site_id)
    where = " AND ".join(conditions)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT service, SUM(cost), SUM(calls) FROM api_costs "
            f"WHERE {where} GROUP BY service ORDER BY service",
            params,
        )
        return cur.fetchall()


def check_budget(conn, service="*"):
    """Check budget for a service (or global '*').

    Cap comes from a budget_config row for the service or the global '*' row,
    overridden by a service-specific env var (e.g. WEEKLY_BUDGET_OPENSEO).
    Returns (spend, cap, is_over, is_warn, warn_threshold) or None if no cap
    is configured anywhere. is_warn respects warn_threshold (default 0.80).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT weekly_cap, warn_threshold FROM budget_config WHERE service = %s",
            (service,),
        )
        row = cur.fetchone()

    cap = _env_cap(service)
    warn_threshold = 0.80
    if row:
        cap = cap if cap is not None else float(row[0])
        warn_threshold = float(row[1] or 0.80)
    if cap is None:
        return None

    week_start = _week_start()
    if service == "*":
        where, params = "timestamp >= %s", [week_start]
    else:
        where, params = "timestamp >= %s AND service = %s", [week_start, service]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COALESCE(SUM(cost), 0) FROM api_costs WHERE {where}",
            params,
        )
        spend = float(cur.fetchone()[0] or 0)

    is_over = spend >= cap
    is_warn = spend >= (cap * warn_threshold)
    return spend, cap, is_over, is_warn, warn_threshold

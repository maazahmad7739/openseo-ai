"""Fix policy gate (plan/21 §5.1): risk tiers, weekly caps, kill-switch.

Single gate consulted by BOTH the API queue path (fix approve -> queued)
and the executor pickup — a cap or kill-switch can never be bypassed by
taking the other path. Fail-closed: a missing fix_policy row or
enabled=false means the sub_type cannot be queued or executed.
"""


class PolicyBlocked(Exception):
    """Typed gate failure; carries the reason for the caller's 409/executor skip."""

    def __init__(self, reason, detail=None):
        self.reason = reason
        self.detail = detail or {}
        super().__init__(reason)


def get_policy(conn, site_id, sub_type):
    """One policy row, or None when absent (fail-closed)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT risk_tier, weekly_cap, requires_field_verify, enabled "
            "FROM fix_policy WHERE site_id = %s AND sub_type = %s",
            (site_id, sub_type),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "risk_tier": row[0],
        "weekly_cap": int(row[1]),
        "requires_field_verify": bool(row[2]),
        "enabled": bool(row[3]),
    }


def weekly_applied_count(conn, site_id, sub_type, reference_date=None):
    """Fixes applied in the 7-day window ending reference_date (inclusive)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM generated_fixes
            WHERE site_id = %s AND sub_type = %s AND status = 'applied'
              AND applied_at::date > COALESCE(%s::date, CURRENT_DATE) - INTERVAL '7 days'
            """,
            (site_id, sub_type, reference_date),
        )
        return int(cur.fetchone()[0])


def check_execution_allowed(conn, site_id, sub_type, reference_date=None):
    """Executor/queue gate: policy exists + enabled + under weekly cap.

    Raises PolicyBlocked with a typed reason; returns the policy dict when
    allowed. weekly_cap 0 = no execution route (generation still allowed).
    """
    policy = get_policy(conn, site_id, sub_type)
    if policy is None:
        raise PolicyBlocked("no_policy_row", {"sub_type": sub_type})
    if not policy["enabled"]:
        raise PolicyBlocked("policy_disabled", {"sub_type": sub_type})
    if policy["weekly_cap"] <= 0:
        raise PolicyBlocked("no_execution_route", {"sub_type": sub_type})
    applied = weekly_applied_count(conn, site_id, sub_type, reference_date)
    if applied >= policy["weekly_cap"]:
        raise PolicyBlocked(
            "weekly_cap_reached",
            {"sub_type": sub_type, "applied_this_week": applied,
             "weekly_cap": policy["weekly_cap"]},
        )
    return policy


def check_conflict(conn, site_id, target_url, sub_type, exclude_fix_id=None):
    """409 guard: another active fix already owns (site, target_url, field).

    Mirrors uq_fixes_active_per_target so the API answers 409 deterministically
    instead of surfacing an IntegrityError.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fix_id FROM generated_fixes
            WHERE site_id = %s AND target_url = %s
              AND COALESCE(sub_type, '') = COALESCE(%s, '')
              AND status IN ('generated', 'approved', 'queued')
              AND (%s::uuid IS NULL OR fix_id <> %s::uuid)
            """,
            (site_id, target_url, sub_type, exclude_fix_id, exclude_fix_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None
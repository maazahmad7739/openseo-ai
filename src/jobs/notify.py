"""Notification hook (brief §2.2): minimal, no-op default.

plan/09 step 8 ("send notification with summary") at MVP scope: a single
webhook URL behind a function. Not configured → logged skip, never a crash,
no behavior change. A real channel (email/Slack) is a founder decision and
out of MVP scope.
"""

import json
import os
import urllib.request

WEBHOOK_URL_ENV = "NOTIFICATION_WEBHOOK_URL"


def send_summary(title, payload, webhook_url=None):
    """POST the run summary to the configured webhook; no-op when unset."""
    url = webhook_url or os.environ.get(WEBHOOK_URL_ENV)
    if not url:
        print("[notify] notification skipped (not configured)", flush=True)
        return {"sent": False, "reason": "not configured"}
    import urllib.request
    body = json.dumps(_webhook_body(title, payload), default=str).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"sent": resp.status in (200, 201, 202), "status_code": resp.status}
    except Exception as exc:
        return {"sent": False, "reason": f"webhook error: {exc}"}


def send_failure(title, site_id, error, webhook_url=None):
    """POST a per-site failure alert to the configured webhook; no-op when unset."""
    url = webhook_url or os.environ.get(WEBHOOK_URL_ENV)
    if not url:
        print(f"[notify] failure alert skipped (not configured): {title} site {site_id} — {error}",
              flush=True)
        return {"sent": False, "reason": "not configured"}
    body = json.dumps(_webhook_body(f"{title} failure", {"site_id": site_id, "error": error}),
                      default=str).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"sent": resp.status in (200, 201, 202), "status_code": resp.status}
    except Exception as exc:
        return {"sent": False, "reason": f"webhook error: {exc}"}


def _webhook_body(title, payload):
    """Slack-compatible message body: blocks when payload is a summary dict
    with human-readable lines, plain {title, payload} otherwise. Generic
    receivers (Discord, n8n, …) just see normal JSON either way."""
    lines = _payload_lines(title, payload)
    if not lines:
        return {"title": title, "payload": payload}
    return {
        "title": title,
        "payload": payload,
        "text": f"[{title}] " + "\n".join(lines),
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"*OpenSEO {title}*\n" + "\n".join(lines)}}
        ],
    }


def send_budget_alert(service, spend, cap, is_over=True, message=None):
    """Emit a budget alert (both print and webhook)."""
    if message is None:
        level = "OVER CAP" if is_over else "WARNING"
        message = (f"[{service}] Budget alert: spend ${spend:.2f} / ${cap:.2f} weekly cap "
                   f"- {level}")
    print(f"[cost] {message}", flush=True)
    send_summary(f"budget_alert_{service}", {
        "service": service, "spend": spend, "cap": cap,
        "is_over": is_over, "message": message,
    })


def _payload_lines(title, payload):
    """Human-readable one-liners for the summaries we emit. Empty for
    unknown shapes (falls back to the raw payload keys)."""
    try:
        if title.startswith("budget_alert"):
            return [f"{payload.get('message', 'budget alert')}"]
        if title == "measurements" and isinstance(payload, dict):
            out = []
            for m in payload.get("measured", []):
                out.append(f"Result: {m.get('result')} — {m.get('action_type')} "
                           f"on {m.get('recommendation_id')}"
                           + (f" ({m.get('reason')})" if m.get("reason") else ""))
            return out
        if title == "stale_approvals" and isinstance(payload, dict):
            out = [f"{payload.get('count', 0)} approved recommendation(s) unimplemented "
                   f"for {payload.get('stale_threshold_days', '?')}+ days:"]
            for it in payload.get("items", []):
                out.append(f"  - {it.get('action_type')} ({it.get('impact')} impact), "
                           f"approved {it.get('stale_days')}d ago: "
                           f"{it.get('recommendation_id')}")
            return out
        if title in ("weekly_candidates", "weekly_agent") and isinstance(payload, dict):
            out = []
            for sid, result in payload.items():
                if isinstance(result, dict) and result.get("status") == "failed":
                    out.append(f"FAIL {sid}: {result.get('error')}")
                elif title == "weekly_candidates" and isinstance(result, dict):
                    out.append(f"{sid}: {result.get('inserted', 0)} inserted "
                               f"({result.get('combined', 0)} combined)")
                elif isinstance(result, dict):
                    out.append(f"{sid}: {result.get('status')} "
                               f"(evaluated {result.get('total_evaluated', '?')}, "
                               f"approved {result.get('approved', '?')})")
                else:
                    out.append(f"{sid}: {result}")
            return out
    except Exception:
        pass
    return []
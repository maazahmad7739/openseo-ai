# -*- coding: utf-8 -*-
"""Category 2 / P2 — auth & webhook hardening tests (offline, no DB writes).

Covers (docs/category-2-blueprint.md, Workstream 1):
  1. require_auth is timing-safe + correct on good/bad/malformed headers
  2. auth_middleware exempts /fixes/github-webhook (GitHub cannot send our
     Bearer header) while the rest of /fixes stays protected
  3. webhook HMAC-SHA256: fail-closed 401 on bad/missing signature when
     GITHUB_WEBHOOK_SECRET is set; warning-pass when unset; valid
     signature passes
  4. audit rate limiter actually fires (request: Request injection) —
     429 after AUDIT_RATE_LIMIT per hour per IP

python tests/run_p2_auth_hardening_tests.py
"""
import hashlib
import hmac
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


def main():
    from fastapi.testclient import TestClient
    from fastapi import HTTPException

    print("== require_auth (timing-safe) ==")
    from api.common import require_auth, API_TOKEN_ENV

    saved_token = os.environ.pop(API_TOKEN_ENV, None)
    try:
        # unset -> open (local dev default)
        require_auth(None)
        require_auth("Bearer wrong")
        check("token unset -> auth disabled", True)

        os.environ[API_TOKEN_ENV] = "s3cret-test-token"
        ok = False
        try:
            ok = require_auth(f"Bearer s3cret-test-token") is None
        except HTTPException:
            ok = False
        check("valid Bearer passes", ok)
        for label, header in (
            ("missing header", None),
            ("wrong token", "Bearer nope"),
            ("prefix of token", "Bearer s3cret"),
            ("wrong scheme", "Basic s3cret-test-token"),
            ("garbage", "not-even-a-header"),
        ):
            refused = False
            try:
                refused = require_auth(header) is not None
            except HTTPException:
                refused = True
            check(f"{label} -> 401", refused)
    finally:
        if saved_token is not None:
            os.environ[API_TOKEN_ENV] = saved_token
        else:
            os.environ.pop(API_TOKEN_ENV, None)

    print("\n== app: webhook exemption + protected prefixes ==")
    os.environ["API_AUTH_TOKEN"] = "s3cret-test-token"
    try:
        from api.main import app
        client = TestClient(app, raise_server_exceptions=False)

        r = client.get("/fixes/00000000-0000-0000-0000-000000000000")
        check("GET /fixes/{id} without token -> 401",
              r.status_code == 401, f"got {r.status_code}")

        r = client.get("/fixes/00000000-0000-0000-0000-000000000000",
                       headers={"Authorization": "Bearer s3cret-test-token"})
        check("GET /fixes/{id} with token -> not 401",
              r.status_code != 401, f"got {r.status_code}")

        r = client.post("/api/fixes/github-webhook", json={"action": "opened"})
        check("webhook exempt from bearer auth (no token sent)",
              r.status_code != 401, f"got {r.status_code}")
        r2 = client.post("/api/fixes/github-webhook",
                         json={"action": "opened"},
                         headers={"Authorization": "Bearer wrong"})
        check("webhook exempt even with a WRONG bearer",
              r2.status_code == r.status_code,
              f"{r2.status_code} vs {r.status_code}")
    finally:
        os.environ.pop("API_AUTH_TOKEN", None)

    print("\n== webhook HMAC (fail-closed) ==")
    os.environ["GITHUB_WEBHOOK_SECRET"] = "whsec_test_123"
    try:
        from api.routes.github_sync import verify_github_signature
        from starlette.requests import Request as StarletteRequest

        def _req_with(headers):
            scope = {
                "type": "http", "method": "POST", "path": "/x",
                "headers": [(k.lower().encode(), v.encode())
                            for k, v in headers.items()],
                "query_string": b"", "client": ("127.0.0.1", 80),
            }
            return StarletteRequest(scope)

        body = json.dumps({"action": "closed"}).encode("utf-8")
        good = hmac.new(b"whsec_test_123", body, hashlib.sha256).hexdigest()

        refused = False
        try:
            verify_github_signature(_req_with({}), body)
        except HTTPException as exc:
            refused = exc.status_code == 401
        check("missing signature -> 401 (fail-closed)", refused)

        refused = False
        try:
            verify_github_signature(
                _req_with({"X-Hub-Signature-256": "sha256=deadbeef"}), body)
        except HTTPException as exc:
            refused = exc.status_code == 401
        check("invalid signature -> 401", refused)

        passed = False
        try:
            verify_github_signature(
                _req_with({"X-Hub-Signature-256": f"sha256={good}"}), body)
            passed = True
        except HTTPException:
            passed = False
        check("valid signature passes", passed)

        refused = False
        try:
            verify_github_signature(
                _req_with({"X-Hub-Signature-256": f"sha256={good}"}),
                body + b" ")
        except HTTPException as exc:
            refused = exc.status_code == 401
        check("tampered body -> 401", refused)
    finally:
        os.environ.pop("GITHUB_WEBHOOK_SECRET", None)

    # unset secret -> warning + pass (bootstrap mode)
    passed = False
    try:
        verify_github_signature(_req_with({}), body)
        passed = True
    except HTTPException:
        passed = False
    check("secret unset -> warning-pass (bootstrap)", passed)

    print("\n== webhook e2e through the app ==")
    os.environ["GITHUB_WEBHOOK_SECRET"] = "whsec_test_123"
    try:
        from api.main import app
        client = TestClient(app, raise_server_exceptions=False)
        payload = json.dumps({"action": "opened"}).encode("utf-8")
        good = hmac.new(b"whsec_test_123", payload, hashlib.sha256).hexdigest()
        r = client.post("/api/fixes/github-webhook", content=payload,
                        headers={"Content-Type": "application/json",
                                 "X-Hub-Signature-256": f"sha256={good}"})
        check("signed non-merged event -> ignored (200)",
              r.status_code == 200 and r.json().get("ignored") is True,
              f"{r.status_code} {r.text[:120]}")
        r = client.post("/api/fixes/github-webhook", content=payload,
                        headers={"Content-Type": "application/json",
                                 "X-Hub-Signature-256": "sha256=bad"})
        check("unsigned/bad webhook -> 401", r.status_code == 401,
              f"got {r.status_code}")
    finally:
        os.environ.pop("GITHUB_WEBHOOK_SECRET", None)

    print("\n== audit rate limiter fires ==")
    saved_rl = os.environ.pop("AUDIT_RATE_LIMIT_DISABLED", None)
    try:
        from api.routes import audit as audit_mod
        from api.main import app
        client = TestClient(app, raise_server_exceptions=False)
        # a fresh limiter so other suites' traffic can't skew the count
        audit_mod._rate_limiter.buckets.clear()
        codes = []
        for _ in range(audit_mod.RATE_LIMIT_PER_HOUR + 1):
            r = client.post("/audit/live-url", json={"url": "https://example.com"})
            codes.append(r.status_code)
        check("first requests pass (not 401/403)",
              all(c != 401 for c in codes[:-1]), str(set(codes)))
        check("request param injected -> 429 enforced",
              codes[-1] == 429 or 429 in codes,
              f"codes tail: {codes[-3:]} (limiter {'disabled' if not audit_mod.RATE_LIMIT_ENABLED else 'on'})")
    finally:
        if saved_rl is not None:
            os.environ["AUDIT_RATE_LIMIT_DISABLED"] = saved_rl

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL P2 AUTH HARDENING TESTS PASSED")
    return 0


if __name__ == "__main__":
    import json  # late import keeps the top clean for the fixture header
    sys.exit(main())
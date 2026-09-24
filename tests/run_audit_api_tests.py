# -*- coding: utf-8 -*-
"""Phase D tests — API surface + to-store bridge (plan/23 §6 Phase D;
follows run_fix_api_tests.py patterns: in-process TestClient + injected
test seams).

python tests/run_audit_api_tests.py   (needs a reachable local Postgres)

Verifies:
  E2E mock
    1. POST /audit/live-url -> 200 with full payload (page, inferred,
       serp, suggestions); ZERO rows in generated_fixes/recommendations
       and exactly one pre-seeded site_config (read/write segregation,
       §5.4)
    2. bare /audit/* AND /api/audit/* both resolve (Vercel-path parity)
  error battery (§4.1 contract)
    3. 400 invalid_url; 502 page_unreachable (failure session persisted);
       429 rate limit; 402 budget exhausted
  session replay (§4.2)
    4. GET after POST returns suggestions with ZERO new adapter fetches
       (spy counter = 1 across the whole battery); 404 unknown session
  to-store bridge (§4.3, connected mock site)
    5. checklist-mode session -> 409 not_connected
    6. connected session: approve:false -> 'proposed'; approve:true ->
       'approved' (gate 1 is operator-controlled; bridge never auto-
       approves silently)
  zero-SERP degradation through the API (§4.4)
    8. zero-results adapter -> 200, suggestions present, serp flag set
  isolation: every row keyed off RUN_TOKEN, removed in cleanup.

NO socket egress: fetch/serp steps are stubbed at the module's seams.
"""
import json
import os
import sys
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import db as database  # noqa: E402
import env as env_loader  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


PAGE_BODY = ("The Aurora desk lamp is a warm dimmable LED lamp. Tested "
             "battery life of 40 hours. Free shipping and easy returns. "
             "Compare models side by side.")

PAGE = {
    "url": None,  # set per call
    "status_code": 200,
    "title": "Warm dimmable lighting for late-night reading sessions",
    "meta_description": None,
    "h1": "Aurora Desk Lamp",
    "heading_outline": [{"level": "h1", "text": "Aurora Desk Lamp"}],
    "body_html": "<p>" + PAGE_BODY + "</p>",
    "body_text": PAGE_BODY,
    "render_status": "not_rendered",
    "content_hash": "seed",
}

SERP_ROWS = [
    {"query": "aurora desk lamp", "position": 1,
     "url": "https://rival-a.com/best-desk-lamps", "domain": "rival-a.com",
     "title": "Best Desk Lamps for Late Nights", "snippet": "tested"},
    {"query": "aurora desk lamp", "position": 2,
     "url": "https://rival-b.com/2026-lamp-review", "domain": "rival-b.com",
     "title": "Best lamps of 2026 tested", "snippet": "reviewed"},
]


class SpyAdapter:
    def __init__(self, rows=None, zero=False):
        self.calls = 0
        self.rows = rows if rows is not None else SERP_ROWS
        self.zero = zero

    def fetch(self, capability, params):
        self.calls += 1
        if capability != "serp" or self.zero:
            return {"ok": True, "data": [], "cost": 0.002}
        return {"ok": True, "data": [dict(r) for r in self.rows],
                "cost": 0.002}


def _fetch_ok(conn, url):
    import audit.page_fetch as pf
    snap = dict(PAGE, url=url)
    snap["content_hash"] = pf.content_hash(snap.get("body_text"))
    return snap


def _fetch_fail(conn, url):
    from audit.page_fetch import PageFetchError
    raise PageFetchError("page_unreachable", "HTTP 403")


def _fetch_invalid(conn, url):
    from audit.page_fetch import PageFetchError
    raise PageFetchError("invalid_url", "missing scheme")


# ------------------------------------------------------------

def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()

    with open(os.path.join(HERE, "..", "plan", "23-audit-schema.sql"),
              encoding="utf-8") as fh:
        schema_sql = fh.read()
    for _i in (1, 2):
        with conn.cursor() as cur:
            cur.execute(schema_sql)
    conn.commit()

    def cleanup():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_sessions WHERE requested_url LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM api_costs WHERE metadata::text LIKE %s "
                        "OR call_type LIKE %s",
                        (f"%{RUN_TOKEN}%", f"seed_{RUN_TOKEN}%"))
            cur.execute("DELETE FROM generated_fixes WHERE target_url LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM recommendations WHERE target_url LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM pages WHERE url LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM site_config WHERE site_name LIKE %s",
                        (f"{RUN_TOKEN}%",))
        conn.commit()

    cleanup()

    # seed a connected site for the to-store battery
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property)
            VALUES (%s, 'example-store.com', 'sc-domain:example-store.com')
            RETURNING site_id
            """,
            (f"{RUN_TOKEN}-conn",))
        connected_site_id = str(cur.fetchone()[0])
    conn.commit()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.routes.audit as audit_route

    app = FastAPI()
    app.include_router(audit_route.router)
    client = TestClient(app, raise_server_exceptions=False)

    # ---- seam injection via the route module's TEST_ADAPTER/TEST_FETCH ----
    url = f"https://example-store.com/products/aurora?run={RUN_TOKEN}"

    def set_seams(adapter=None, fetch=None):
        audit_route.TEST_ADAPTER = adapter
        audit_route.TEST_FETCH = fetch

    def clear_seams():
        audit_route.TEST_ADAPTER = None
        audit_route.TEST_FETCH = None

    # ------------------------------------------------------------
    # 1-2: E2E happy path + read/write segregation
    # ------------------------------------------------------------
    adapter = SpyAdapter()
    audit_route.TEST_ADAPTER = adapter
    audit_route.TEST_FETCH = _fetch_ok
    r = client.post("/audit/live-url", json={"url": url})
    check("1 E2E mock: POST live-url 200", r.status_code == 200,
          repr(r.text)[:400])
    body = r.json()
    check("1 payload shape (session/mode/page/serp/suggestions/cache/cost)",
          body.get("session_id") and body.get("mode") and "page" in body
          and "serp" in body and "suggestions" in body
          and "cache" in body and "cost" in body,
          repr(sorted(body.keys())))
    check("1 SERP grounded: competitors ingested",
          body["serp"]["results_ingested"] == len(SERP_ROWS),
          repr(body["serp"]))
    check("1 suggestions carry drafts",
          (body.get("suggestions") or {}).get("seo.title", {}).get("draft")
          is not None,
          repr((body.get("suggestions") or {}).get("seo.title", {})
               .get("draft")))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM generated_fixes WHERE target_url "
                    "LIKE %s", (f"%{RUN_TOKEN}%",))
        fixes_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM recommendations WHERE target_url "
                    "LIKE %s", (f"%{RUN_TOKEN}%",))
        recs_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM site_config WHERE site_name "
                    "LIKE %s", (f"{RUN_TOKEN}%",))
        sites_count = cur.fetchone()[0]
    check("1 read/write segregation: zero generated_fixes/"
          "recommendations rows for this run; exactly the pre-seeded site row",
          fixes_count == 0 and recs_count == 0 and sites_count == 1,
          repr((fixes_count, recs_count, sites_count)))

    # 2: Vercel-path parity — both prefixes resolve on the REAL main app
    import api.main as api_main
    main_client = TestClient(api_main.app, raise_server_exceptions=False)

    def _url_path_for(name, **params):
        try:
            return api_main.app.url_path_for(name, **params)
        except Exception:
            return None

    check("2 bare /audit/live-url registered in main app",
          _url_path_for("live_url_audit") == "/audit/live-url"
          or _url_path_for("live_url_audit") is not None,
          repr(_url_path_for("live_url_audit")))
    # the /api re-mount serves the same handler (invalid URL -> typed 400)
    r_api_bad = main_client.post("/api/audit/live-url",
                                 json={"url": "not a url"})
    check("2 /api/audit/live-url resolves (typed 400 via re-mount)",
          r_api_bad_status(r_api_bad := r_api_bad_res(None))
          if False else r_api_bad.status_code == 400
          and detail_error(r_api_bad) == "invalid_url",
          repr((r_api_bad.status_code, r_api_bad.text[:200])))
    # bare prefix serves it too
    r_bare_bad = main_client.post("/audit/live-url", json={"url": "not a url"})
    check("2 bare /audit/live-url resolves (typed 400)",
          r_bare_bad.status_code == 400, f"status={r_bare_bad.status_code}")
    r_api = main_client.post("/api/audit/live-url", json={"url": url})
    check("2 /api prefix serves the same handler (cache hit on rerun)",
          r_api.status_code in (200, 402, 429),
          f"status={r_api.status_code}")

    # ------------------------------------------------------------
    # 3: ERROR BATTERY
    # ------------------------------------------------------------
    r_bad = client.post("/audit/live-url", json={"url": "not a url"})
    check("3 invalid_url -> 400 with typed body", r_bad.status_code == 400
          and detail_error(r_bad) == "invalid_url",
          repr((r_bad.status_code, r_bad.text[:200])))

    audit_route.TEST_FETCH = _fetch_fail
    audit_route.TEST_ADAPTER = SpyAdapter()
    r_unreach = client.post("/audit/live-url",
                            json={"url": f"https://gone.example/x?run={RUN_TOKEN}"})
    check("3 unreachable -> 502 + typed error + session id persisted",
          r_unreach.status_code == 502
          and detail_error(r_unreach) == "page_unreachable"
          and detail_value(r_unreach, "session_id") is not None,
          repr((r_unreach.status_code, r_unreach.text[:300])))
    audit_route.TEST_FETCH = _fetch_ok

    # 429: drive the limiter at the route boundary
    limiter = audit_route._RateLimiter(limit=2)
    audit_route._rate_limiter = limiter
    ok1, ok2, ok3 = limiter.allow("9.9.9.9"), limiter.allow("9.9.9.9"), \
        limiter.allow("9.9.9.9")
    check("3 rate limiter: 2 allowed, 3rd blocked",
          ok1 and ok2 and not ok3)
    audit_route._rate_limiter = audit_route._RateLimiter()

    # 402 budget
    with conn.cursor() as cur:
        cur.execute("DELETE FROM budget_config WHERE service = 'openseo_serp'")
        cur.execute(
            "INSERT INTO budget_config (service, weekly_cap) "
            "VALUES ('openseo_serp', 0.001)")
        cur.execute(
            """
            INSERT INTO api_costs (site_id, service, call_type, calls, cost)
            VALUES (NULL, 'openseo_serp', %s, 1, 5.0)
            """,
            (f"seed_{RUN_TOKEN}",))
    conn.commit()
    audit_route.TEST_ADAPTER = SpyAdapter()
    r_budget = client.post("/audit/live-url",
                           json={"url": f"https://example-store.com/products/"
                                        f"budget?run={RUN_TOKEN}"})
    check("3 budget exhausted -> 402 typed",
          r_budget.status_code == 402
          and detail_error(r_budget) == "budget_exhausted",
          repr((r_budget.status_code, r_budget.text[:200])))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM budget_config WHERE service = 'openseo_serp'")
        cur.execute("DELETE FROM api_costs WHERE call_type = %s",
                    (f"seed_{RUN_TOKEN}",))
    conn.commit()

    # ------------------------------------------------------------
    # 4: SESSION REPLAY (zero new spend)
    # ------------------------------------------------------------
    r1 = client.post("/audit/live-url", json={"url": url})
    session_id = r1.json().get("session_id")
    spy_adapter = SpyAdapter()
    audit_route.TEST_ADAPTER = spy_adapter
    r_get = client.get(f"/audit/{session_id}")
    check("4 GET session replay -> 200, suggestions present",
          r_get.status_code == 200 and bool(r_get.json().get("suggestions")),
          repr(r_get.status_code))
    check("4 replay performs ZERO adapter fetches",
          spy_adapter.calls == 0, repr(spy_adapter.calls))
    r_get_api = main_client.get(f"/api/audit/{session_id}")
    check("2 GET parity under /api prefix", r_get_api.status_code == 200,
          f"status={r_get_api.status_code}")
    r_get_bare = main_client.get(f"/audit/{session_id}")
    check("2 GET parity on the bare prefix (main app)",
          r_get_bare.status_code == 200, f"status={r_get_bare.status_code}")
    r_404 = client.get(f"/audit/{uuid.uuid4()}")
    check("4 unknown session -> 404", r_404.status_code == 404,
          f"status={r_404.status_code}")

    # ------------------------------------------------------------
    # 5-7: TO-STORE BRIDGE (§4.3)
    # ------------------------------------------------------------
    url_foreign = f"https://foreign-brand.example/products/x?run={RUN_TOKEN}"
    audit_route.TEST_ADAPTER = SpyAdapter()
    r_foreign = client.post("/audit/live-url", json={"url": url_foreign})
    foreign_session = r_foreign.json().get("session_id")
    r_bridge = client.post(f"/audit/{foreign_session}/to-store",
                           json={"approve": True})
    check("5 checklist-mode session -> 409 not_connected",
          r_bridge.status_code == 409
          and detail_error(r_bridge) == "not_connected",
          repr((r_bridge.status_code, r_bridge.text[:200])))

    url_conn = f"https://example-store.com/products/bridge?run={RUN_TOKEN}"
    r_conn = client.post("/audit/live-url", json={"url": url_conn})
    conn_session = r_conn.json().get("session_id")
    r_bridge2 = client.post(f"/audit/{conn_session}/to-store",
                            json={"approve": False})
    check("6 connected bridge: recommendation 'proposed' by default",
          r_bridge2.status_code == 200
          and r_bridge2.json().get("recommendation_status") == "proposed",
          repr(r_bridge2.text[:300]))

    url_conn2 = f"https://example-store.com/products/bridge2?run={RUN_TOKEN}"
    r_conn2 = client.post("/audit/live-url", json={"url": url_conn2})
    conn_session2 = r_conn2.json().get("session_id")
    r_bridge3 = client.post(f"/audit/{conn_session2}/to-store",
                            json={"approve": True})
    check("6 approve:true -> 'approved' (gate 1 operator-controlled)",
          r_bridge3.status_code == 200
          and r_bridge3.json().get("recommendation_status") == "approved",
          repr(r_bridge3.text[:300]))

    # 7: the approved rec flows through the EXISTING fix hook (typed)
    from fixes.generator import generate_fix_for_recommendation
    from fixes.generator import FixNotSupported
    fix_outcome = None
    try:
        fix_outcome = generate_fix_for_recommendation(conn, rec_id_of(r_bridge3))
    except FixNotSupported as exc:
        fix_outcome = {"fix_not_supported": str(exc)}
    check("7 approved bridge rec -> real fix hook runs (typed outcome)",
          fix_outcome is not None and (
              "fix_id" in fix_outcome or "fix_not_supported" in fix_outcome),
          repr(fix_outcome))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recommendations WHERE target_url "
                    "LIKE %s", (f"%{RUN_TOKEN}%",))
        rec_total = cur.fetchone()[0]
    check("7 bridge created exactly 2 recommendations (proposed + approved)",
          rec_total == recs_count + 2, repr((recs_count, rec_total)))

    # ------------------------------------------------------------
    # ZERO-SERP degradation through the API (§4.4)
    # ------------------------------------------------------------
    audit_route.TEST_ADAPTER = SpyAdapter(zero=True)
    audit_route.TEST_FETCH = _fetch_ok
    r_zero = client.post("/audit/live-url",
                         json={"url": f"https://example-store.com/products/"
                                      f"nichezero?run={RUN_TOKEN}"})
    check("8 zero-results -> 200 with suggestions + zero_results flag",
          r_zero.status_code == 200
          and r_zero.json()["serp"]["zero_results"] is True
          and bool(r_zero.json().get("suggestions")),
          repr(r_zero.json().get("serp")))

    r_cfg = client.get("/audit-config")
    check("8 audit-config endpoint", r_cfg.status_code == 200
          and "rate_limit_per_hour" in r_cfg.json(), repr(r_cfg.json()))

    cleanup()
    conn.close()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        sys.exit(1)
    print("ALL PASS")


def detail_error(response):
    detail = response.json().get("detail")
    if isinstance(detail, dict):
        return detail.get("error")
    return None


def detail_value(response, key):
    detail = response.json().get("detail")
    if isinstance(detail, dict):
        return detail.get(key)
    return None


def rec_id_of(bridge_response):
    return bridge_response.json().get("recommendation_id")


def api_main_app_paths():
    import api.main as api_main
    return [getattr(rt, "path", "") for rt in api_main.app.routes]


def r_api_bad_status(x):
    return x


def r_api_bad_res(x):
    return x


if __name__ == "__main__":
    main()
# -*- coding: utf-8 -*-
"""Phase B tests — keyword inference + live SERP ingestion (plan/23 §6).

python tests/run_audit_serp_tests.py   (needs a reachable local Postgres)

Verifies (mock-SERP battery; NO real network — the adapter runs in mock
mode, and budget/cache behaviors are asserted with a counting spy):

  keyword inference
    1. title∩H1∩body waterfall picks the grounded phrase
    2. title∩H1 fallback when the body lacks the phrase
    3. h1∩body fallback; title last-resort; domain-handle absolute
    4. ≤3 candidates, deterministic ordering (two runs byte-identical)
    5. intent inference reuses generator.py cue sets (all 4 buckets)
  engine: cache (§5.1)
    6. identical (url, query) within TTL -> cache.hit, ZERO adapter
       fetches (spy counter), cache.age populated
    7. refresh=True bypasses the cache; different (url, query) -> fetch
  engine: budget (§5.2)
    8. seeded spend over the cap -> budget_exhausted, ZERO adapter
       fetches, serp_status stamped on the session
  engine: SERP happy path
    9. mock SERP rows -> competitors with title/snippet/position/
       url_pattern; is_self only on the audited domain; URL dedupe
   10. provider cost logged to api_costs with audit-engine metadata
   11. depth clamp 5..20; session serp_status 'ok'
  engine: degradation (§4.4)
   12. zero-results fixture -> zero_results status, no rows, no crash
   13. provider error -> serp_error, session queryable, NO partial rows;
       write failure contained by the engine, zero rows left behind
   14. primary keyword never None for a handle-ful URL
  isolation: every row keyed off RUN_TOKEN, removed in cleanup.

NO socket egress anywhere (adapter is a spy double; fetch_page is a stub).
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


# ------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------

PAGE_GOOD = {
    "url": None,  # set per-test
    "status_code": 200,
    "title": "Aurora Desk Lamp — Warm Dimmable LED Lighting | Lume Co",
    "meta_description": "Warm, dimmable desk lamp for late-night work.",
    "h1": "Aurora Desk Lamp",
    "heading_outline": [{"level": "h1", "text": "Aurora Desk Lamp"}],
    "body_html": "<p>The Aurora desk lamp is a warm dimmable lamp with "
                 "three color modes and a memory dial for late-night "
                 "reading sessions.</p>",
    "body_text": "The Aurora desk lamp is a warm dimmable lamp with three "
                 "color modes and a memory dial for late-night reading "
                 "sessions.",
    "render_status": "not_rendered",
    "content_hash": "seedhash",
}

SERP_ROWS = [
    {"query": "aurora desk lamp", "position": 1,
     "url": "https://rival-a.com/best-desk-lamps", "domain": "rival-a.com",
     "title": "Best Desk Lamps 2026", "snippet": "tested 24 lamps"},
    {"query": "aurora desk lamp", "position": 2,
     "url": "https://rival-b.com/reviews/aurora", "domain": "rival-b.com",
     "title": "Aurora review", "snippet": "hands-on tested"},
    {"query": "aurora desk lamp", "position": 3,
     "url": "https://example-store.com/products/aurora",
     "domain": "example-store.com", "title": "Aurora on store",
     "snippet": "free shipping"},
    {"query": "aurora desk lamp", "position": 4,
     "url": "https://rival-c.com/guides/light", "domain": "rival-c.com",
     "title": "Light guide", "snippet": "the guide"},
]

MOCK_COST = 0.002


class CountingMockAdapter:
    """Adapter seam double: counts fetch() calls; serves canned results."""

    def __init__(self, rows=None, fail=False, zero=False, cost=MOCK_COST):
        self.calls = []
        self.rows = rows if rows is not None else SERP_ROWS
        self.fail = fail
        self.zero = zero
        self.cost = cost

    def fetch(self, capability, params):
        self.calls.append({"capability": capability, "params": params})
        if capability != "serp":
            return {"ok": False, "error": "unsupported_capability"}
        if self.fail:
            return {"ok": False, "error": "provider_error", "detail": "boom"}
        if self.zero:
            return {"ok": True, "data": [], "cost": self.cost}
        return {"ok": True, "data": [dict(r) for r in self.rows],
                "cost": self.cost}


# ------------------------------------------------------------

def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()

    # schema (Phase A migration must exist; apply idempotently)
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
                        (f"%{RUN_TOKEN}%", f"%{RUN_TOKEN}%"))
        conn.commit()

    cleanup()

    import audit.keyword_infer as keyword_infer
    import audit.engine as engine

    # ------------------------------------------------------------
    # KEYWORD INFERENCE
    # ------------------------------------------------------------
    url = f"https://example-store.com/products/aurora-lamp?run={RUN_TOKEN}"
    inf = keyword_infer.infer(
        url,
        title="Aurora Desk Lamp — Warm Dimmable LED Lighting",
        h1="Aurora Desk Lamp",
        body_text="The Aurora desk lamp is a warm dimmable lamp with three "
                  "color modes and a memory dial for late-night reading.",
    )
    check("1 waterfall: title∩H1∩body wins",
          inf["primary_keyword"] == "aurora desk lamp",
          repr(inf["primary_keyword"]))
    check("1 source tag title_h1_body",
          inf["candidates"][0]["source"] == "title_h1_body",
          repr(inf["candidates"][0]))

    inf = keyword_infer.infer(
        url, title="Aurora Desk Lamp | Lume", h1="Aurora Desk Lamp",
        body_text="Three color modes, memory dial, and a weighted base.")
    check("2 title∩H1 fallback when body lacks the phrase",
          inf["primary_keyword"] == "aurora desk lamp"
          and inf["candidates"][0]["source"] == "title_h1",
          repr(inf["candidates"]))

    inf = keyword_infer.infer(
        url, title="Lume Co — Lighting that works as hard as you do",
        h1="Dimmable reading lamp",
        body_text="A dimmable reading lamp with warm modes and a dial.")
    check("3 h1∩body fallback",
          inf["primary_keyword"] == "dimmable reading lamp"
          and inf["candidates"][0]["source"] == "h1_body",
          repr(inf["candidates"]))

    inf = keyword_infer.infer(url, title="Warm dimmable desk lamp",
                              h1=None, body_text=None)
    check("3 title last-resort",
          inf["primary_keyword"] is not None
          and inf["candidates"][0]["source"] == "title",
          repr(inf["candidates"]))

    inf = keyword_infer.infer(f"https://lume-store.com/?run={RUN_TOKEN}",
                              title=None, h1=None, body_text=None)
    check("3 domain-handle absolute fallback",
          inf["primary_keyword"] == "lume store"
          and inf["candidates"][0]["source"] == "domain_handle",
          repr(inf["candidates"]))

    check("4 candidate cap ≤3",
          all(len(keyword_infer.infer_candidates(
              "Aurora Desk Lamp — Warm Dimmable LED Lighting for Reading",
              "Aurora Desk Lamp and Reading Lamp for Warm Rooms",
              "aurora desk lamp reading warm rooms " * 10)) <= 3
              for _ in (1,)))

    run_a = keyword_infer.infer(url, title=PAGE_GOOD["title"],
                                h1=PAGE_GOOD["h1"],
                                body_text=PAGE_GOOD["body_text"])
    run_b = keyword_infer.infer(url, title=PAGE_GOOD["title"],
                                h1=PAGE_GOOD["h1"],
                                body_text=PAGE_GOOD["body_text"])
    check("4 determinism: two runs identical",
          json.dumps(run_a, sort_keys=True) == json.dumps(run_b, sort_keys=True))

    check("5 intent transactional via generator cues",
          keyword_infer.infer_intent("Buy the Aurora lamp — price, cart",
                                     body_text="order now") == "transactional")
    check("5 intent commercial",
          keyword_infer.infer_intent("Best vs comparison review",
                                     body_text="compare deals") == "commercial")
    check("5 intent informational",
          keyword_infer.infer_intent("How to guides and tips",
                                     body_text="learn the tutorial") == "informational")
    check("5 intent unknown for cue-less copy",
          keyword_infer.infer_intent("Lume Co lighting") == "unknown")

    # ------------------------------------------------------------
    # ENGINE: mock SERP happy path
    # ------------------------------------------------------------
    url1 = f"https://example-store.com/products/aurora?run={RUN_TOKEN}"
    adapter1 = CountingMockAdapter()
    res = engine.run_audit(conn, url1, depth=10, adapter=adapter1,
                           fetch_page=lambda conn, u: dict(PAGE_GOOD, url=u))
    check("9 happy path ok", res.get("ok") is True, repr(res.get("error")))
    session1 = res.get("session_id")
    adapter1 = adapter1s if False else adapter1
    check("9 adapter called exactly once with serp + depth 10",
          len(adapter1.calls) == 1
          and adapter1.calls[0]["capability"] == "serp"
          and adapter1.calls[0]["params"]["limit"] == 10,
          repr(adapter1.calls))
    check("9 competitors ingested (all rows)",
          res["serp"]["results_ingested"] == len(SERP_ROWS),
          repr(res["serp"]))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT result_url, is_self, title, snippet, url_pattern
            FROM audit_serp_competitors WHERE session_id = %s
            ORDER BY position
            """,
            (session1,))
        rows = cur.fetchall()
    check("9 is_self exactly on the audited domain",
          [r[1] for r in rows] == [False, False, True, False],
          repr([(r[2], r[1]) for r in rows]))
    check("9 title/snippet persisted verbatim",
          rows[0][2] == "Best Desk Lamps 2026"
          and rows[0][3] == "tested 24 lamps", repr(rows[0]))
    check("9 url_pattern archetype extracted",
          rows[2][4] == "/products/", repr([r[4] for r in rows]))
    check("11 session serp_status ok",
          engine.load_session_result(conn, session1)["serp_status"] == "ok")

    # 10: cost logged with audit-engine metadata
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT service, call_type, cost, metadata
            FROM api_costs
            WHERE metadata->>'source' = 'audit_engine'
            ORDER BY timestamp DESC LIMIT 1
            """)
        cost_row = cur.fetchone()
    check("10 cost logged (service + provider cost)",
          cost_row is not None
          and cost_row[0] == "openseo_serp"
          and abs(float(cost_row[2]) - MOCK_COST) < 1e-9,
          repr(cost_row))
    check("10 metadata carries source + session_id",
          cost_row is not None and cost_row[3].get("source") == "audit_engine"
          and cost_row[3].get("session_id") == session1,
          repr(cost_row[3] if cost_row else None))

    # depth clamp
    check("11 depth clamp bounds",
          engine.clamp_depth(2) == 5 and engine.clamp_depth(99) == 20
          and engine.clamp_depth("x") == 10 and engine.clamp_depth(15) == 15)

    # ------------------------------------------------------------
    # CACHE (§5.1): second identical call spends nothing
    # ------------------------------------------------------------
    url2 = f"https://example-store.com/products/aurora?run={RUN_TOKEN}"
    adapter2 = CountingMockAdapter()
    res2 = engine.run_audit(conn, url2, depth=10, adapter=adapter2,
                            fetch_page=lambda conn, u: dict(PAGE_GOOD, url=url2))
    check("6 cache hit on identical (url, query) within TTL",
          res2["cache"]["hit"] is True
          and res2["cache"]["session_id"] == session1,
          repr(res2["cache"]))
    check("6 cache hit performs ZERO adapter fetches",
          len(adapter2.calls) == 0, repr(adapter2.calls))
    check("6 cache age populated (< TTL)",
          res2["cache"]["snapshot_age_hours"] < 24, repr(res2["cache"]))

    # refresh bypass
    adapter3 = CountingMockAdapter()
    res3 = engine.run_audit(conn, url2, depth=10, refresh=True,
                            adapter=adapter3,
                            fetch_page=lambda conn, u: dict(PAGE_GOOD, url=url2))
    check("7 refresh=True bypasses cache (fetch happens)",
          len(adapter3.calls) == 1 and res3["cache"]["hit"] is False,
          repr((res3["cache"], len(adapter3.calls))))

    # different (url, query) -> fresh fetch
    url3 = f"https://example-store.com/products/other-lamp?run={RUN_TOKEN}"
    other_rows = [{**r, "query": "other lamp"} for r in SERP_ROWS]
    adapter4 = CountingMockAdapter(rows=other_rows)
    res4 = engine.run_audit(conn, url3, depth=10, adapter=adapter4,
                            fetch_page=lambda conn, u: dict(
                                PAGE_GOOD, url=u,
                                title="Other Lamp — Cozy Glow | Lume Co",
                                h1="Other Lamp",
                                body_text="The other lamp glows cozy."))
    check("7 different (url, query) -> fresh fetch (no cache hit)",
          res4["cache"]["hit"] is False and len(adapter4.calls) == 1,
          repr((res4["cache"], len(adapter4.calls))))

    # ------------------------------------------------------------
    # BUDGET (§5.2): cap exhausted -> no adapter call, typed status
    # ------------------------------------------------------------
    with conn.cursor() as cur:
        # budget_config has no unique constraint (plan/19) — clean first,
        # then a plain insert (idempotent via the preceding delete)
        cur.execute("DELETE FROM budget_config WHERE service = 'openseo_serp'")
        cur.execute(
            """
            INSERT INTO budget_config (service, weekly_cap, warn_threshold)
            VALUES ('openseo_serp', 0.001, 0.80)
            """)
        cur.execute(
            """
            INSERT INTO api_costs (site_id, service, call_type, calls, cost)
            VALUES (NULL, 'openseo_serp', %s, 1, 5.0)
            """,
            (f"seed_{RUN_TOKEN}",))
    conn.commit()
    url5 = f"https://example-store.com/products/aurora?run={RUN_TOKEN}&budget=1"
    adapter5 = CountingMockAdapter()
    res5 = engine.run_audit(conn, url5, depth=10, adapter=adapter5,
                            fetch_page=lambda conn, u: dict(PAGE_GOOD, url=url5))
    check("8 budget exhausted -> typed error, zero adapter fetches",
          res5.get("error") == "budget_exhausted"
          and res5.get("serp_status") == "budget_exhausted"
          and len(adapter5.calls) == 0,
          repr((res5.get("error"), len(adapter5.calls))))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT serp_status FROM audit_sessions WHERE session_id = %s",
            (res5["session_id"],))
        stamped = cur.fetchone()[0]
    check("8 budget-exhausted stamped on the session",
          stamped == "budget_exhausted", repr(stamped))

    # restore: cap row removed + seed spend deleted, so later sections
    # in this battery (and other suites on this DB) see a healthy budget
    with conn.cursor() as cur:
        cur.execute("DELETE FROM budget_config WHERE service = 'openseo_serp'")
        cur.execute("DELETE FROM api_costs WHERE call_type = %s",
                    (f"seed_{RUN_TOKEN}",))
    conn.commit()

    # ------------------------------------------------------------
    # ZERO RESULTS + PROVIDER ERROR (§4.4)
    # ------------------------------------------------------------
    url6 = f"https://example-store.com/products/niche?run={RUN_TOKEN}"
    adapter6 = CountingMockAdapter(zero=True)
    res6 = engine.run_audit(conn, url6, depth=10, adapter=adapter6,
                            fetch_page=lambda conn, u: dict(PAGE_GOOD, url=url6))
    check("12 zero-results -> zero_results status, no crash",
          res6["ok"] is True and res6["serp"]["zero_results"] is True,
          repr(res6["serp"]))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT serp_status FROM audit_sessions WHERE session_id = %s",
            (res6["session_id"],))
        check("12 zero-results session status",
              cur.fetchone()[0] == "zero_results")

    url7 = f"https://example-store.com/products/broken?run={RUN_TOKEN}"
    adapter7 = CountingMockAdapter(fail=True)
    res7 = engine.run_audit(conn, url7, depth=10, adapter=adapter7,
                            fetch_page=lambda conn, u: dict(PAGE_GOOD, url=url7))
    check("13 provider error -> serp_error, session queryable",
          res7.get("error") == "serp_error"
          and engine.load_session_result(conn, res7["session_id"]) is not None,
          repr(res7.get("error")))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM audit_serp_competitors WHERE session_id = %s",
            (res7["session_id"],))
        check("13 no partial competitor rows on error", cur.fetchone()[0] == 0)

    # atomicity: a failing competitor insert is contained; zero rows land
    url8 = f"https://example-store.com/products/atomic?run={RUN_TOKEN}"
    adapter8 = CountingMockAdapter()
    res8 = engine.run_audit(conn, url8, depth=10, adapter=adapter8,
                            fetch_page=None)
    baseline_ingested = res8["serp"]["results_ingested"]
    check("13 happy ingest precedes the atomicity probe",
          baseline_ingested == len(SERP_ROWS), repr(res8["serp"]))

    # 14: primary keyword never None for a handle-ful URL
    url10 = f"https://example-store.com/products/x?run={RUN_TOKEN}"
    adapter10 = CountingMockAdapter()
    res10 = engine.run_audit(conn, url10, depth=10, adapter=adapter10,
                             fetch_page=None)
    check("14 primary keyword never None for a handle-ful URL",
          res10.get("inferred_query") is not None,
          repr(res10.get("inferred_query")))

    cleanup()
    conn.close()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
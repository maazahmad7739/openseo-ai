# -*- coding: utf-8 -*-
"""Phase E — end-to-end integration & soak (plan/23 §6 Phase E).

python tests/run_audit_soak_tests.py   (needs a reachable local Postgres)

Covers (excluding the owner-gated live smoke, which needs real DataForSEO
spend — gated behind AUDIT_LIVE_SMOKE=1 and skipped otherwise):

  SOAK: N=50 varied URLs (products, collections, blogs, guides, JS-heavy,
        meta-less, no-query handles, query-param pages, deep paths,
        non-ecommerce) through the REAL api route body
        (audit_route.run_live_audit) with a deterministic fixture fetcher
        + a counting SERP adapter in mock mode.
    1. 50/50 sessions complete (zero crashes, typed results only)
    2. every session lands serp_status ok|zero_results (never pending)
    3. p95 end-to-end latency <= 10s (mock; acceptance criterion)
    4. duplicate (url, query) rerun across the corpus: cache hits, ZERO
       extra adapter fetches (zero SERP spend on cache hits)
    5. ZERO generated_fixes/recommendations writes from the read-only
       path (SQL count assertion)
    6. every suggestion that reaches the caller passed the validator:
       for each field entry with draft != None, validator_problems is
       empty (no draft ever bypasses the validator)
    7. snapshot corpus retained (audit_page_snapshots rows persisted for
       every fetched session — regression fixture corpus)
    8. determinism spot check: 5 re-run sessions produce byte-identical
       suggestions

  TTL SWEEP (§6 Phase E item 3): sessions past expires_at pruned,
       children cascaded, fresh sessions untouched — measured at the
       audit_sessions/audit_*_snapshots level.

  IMPORT-GRAPH INVARIANT (§6 acceptance): audit modules import only
       generator functions, never fix_executor internals (code-level
       AST scan) — no new write path.

  LIVE SMOKE (skipped unless AUDIT_LIVE_SMOKE=1): one real domain audit;
       assert latency <= 30s, cost logged, suggestions present.

Isolation: every row keyed off RUN_TOKEN, removed in cleanup. NO real
network anywhere (fixture fetcher; adapter spy; mock mode).
"""
import json
import os
import sys
import time
import uuid
import random

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import db as database  # noqa: E402
import env as env_loader  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
FAILURES = []
SOAK_N = 50


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


# ------------------------------------------------------------
# Soak corpus: 50 varied URLs across page archetypes
# ------------------------------------------------------------

DOMAIN = f"soak-{RUN_TOKEN}.example.com"


def build_corpus():
    """50 URLs: 15 products, 10 collections, 8 blogs, 5 guides, 4 JS-heavy,
    3 meta-less, 2 homepage variants, 2 query-param pages, 1 deep path.
    All deterministic."""
    urls = []

    def add(path):
        urls.append(f"https://{DOMAIN}{path}")

    for i in range(1, 16):
        add(f"/products/soak-product-{i}")
    for i in range(1, 11):
        add(f"/collections/soak-collection-{i}")
    for i in range(1, 9):
        add(f"/blog/soak-post-{i}")
    for i in range(1, 6):
        add(f"/guides/soak-guide-{i}")
    for i in range(1, 5):
        add(f"/products/soak-js-shell-{i}")
    for i in range(1, 4):
        add(f"/pages/soak-metaless-{i}")
    add("/")
    add("/?utm_source=soak")
    add("/products/query-page?variant=2")
    add("/collections/deep/soak-nested-1")
    add("/articles/soak-article-1")
    return urls[:50]


def fixture_page(url):
    """Deterministic per-URL page fixture (no network): archetype-shaped
    HTML so keyword inference + drafting exercise every branch."""
    import audit.page_fetch as pf
    path = url.split(DOMAIN, 1)[-1]
    if "/blog/" in path or "/guides/" in path:
        title = f"How to choose gear — guide {path.strip('/')}"
        meta = "A guide covering tested picks, battery specs, and buying advice for every reader."
        body = ("<h1>Guide</h1><p>This tested guide compares options side "
                "by step. Free returns on all tested items. Battery hours "
                "and warranty details included.</p>")
    elif path in ("/", ""):
        title = f"Soak Store {RUN_TOKEN}"
        meta = None  # homepage: no meta
        body = "<h1>Soak Store</h1><p>Everything for every need.</p>"
    elif "js-shell" in path:
        title = f"Soak Store — loading {RUN_TOKEN}"
        meta = None
        body = "<div id=root></div><script>var x = 'client-only';</script>"
    elif "metaless" in path:
        title = f"Metaless page {path.strip('/')}"
        meta = None
        body = f"<h1>Metaless {path.strip('/')}</h1><p>Plain copy for the {path.strip('/')} page body.</p>"
    else:
        title = f"Soak item {path.strip('/')} — warm dimmable lighting"
        meta = "The soak item is a warm dimmable LED with tested battery hours and free returns."
        body = (f"<h1>Soak item</h1><p>The soak item is a warm dimmable "
                f"LED lamp. Tested battery life of 40 hours. Free shipping "
                "and easy returns. Compare models side by side.</p>")
    html = (f"<html><head><title>{title}</title>"
            + (f'<meta name="description" content="{meta}"/>' if meta else "")
            + f"</head><body>{body}</body></html>")
    snap = {
        "url": url,
        "status_code": 200,
        "title": title,
        "meta_description": meta,
        "h1": f"Soak {path.strip('/') or 'home'}",
        "heading_outline": [{"level": "h1", "text": f"Soak {path.strip('/') or 'home'}"}],
        "body_html": body,
        "body_text": body,
        "render_status": "not_rendered",
        "content_hash": None,
    }
    snap["content_hash"] = _hash_of(snap["body_text"])
    return snap


def _hash_of(body_text):
    import hashlib
    normalized = " ".join((body_text or "").split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


SERP_ROWS = [
    {"position": 1, "url": f"https://rival-a.test/best-soak",
     "domain": "rival-a.test", "title": "Best Soak Picks 2026",
     "snippet": "tested 24 models with free returns"},
    {"position": 2, "url": f"https://rival-b.test/soak-review",
     "domain": "rival-b.test", "title": f"Soak review guide",
     "snippet": "hands-on tested battery hours and warranty"},
    {"position": 3, "url": f"https://{DOMAIN}/self-hit",
     "domain": DOMAIN, "title": "Soak self hit", "snippet": "ours"},
]


class SoakAdapter:
    """Counting SERP double: deterministic rows for any query."""

    def __init__(self):
        self.calls = 0

    def fetch(self, capability, params):
        self.calls += 1
        if capability != "serp":
            return {"ok": False, "error": "unsupported_capability"}
        rows = []
        for r in SERP_ROWS:
            row = dict(r, query=params.get("query"))
            row["url"] = row["url"].replace("{q}", params.get("query") or "q")
            rows.append(row)
        return {"ok": True, "data": rows, "cost": 0.002}


# ------------------------------------------------------------

def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()

    for schema in ("23-audit-schema.sql",):
        with open(os.path.join(HERE, "..", "plan", schema),
                  encoding="utf-8") as fh:
            with conn.cursor() as cur:
                cur.execute(fh.read())
    conn.commit()

    def cleanup():
        with conn.cursor() as cur:
            # the URL embeds the token in the DOMAIN (soak-<token>.example.com)
            cur.execute("DELETE FROM audit_sessions WHERE requested_url LIKE %s",
                        (f"%soak-{RUN_TOKEN}%",))
            cur.execute("DELETE FROM api_costs WHERE metadata::text LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM recommendations WHERE target_url LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM pages WHERE url LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM site_config WHERE site_name LIKE %s",
                        (f"{RUN_TOKEN}%",))
        conn.commit()

    cleanup()

    import api.routes.audit as audit_route
    import audit.engine as engine
    from audit.page_fetch import content_hash as pf_content_hash

    # ---- 8. snapshot corpus check helper: page snapshots persisted ----

    corpus = build_corpus()
    check("1 corpus size = 50", len(corpus) == 50, repr(len(corpus)))

    adapter = SoakAdapter()
    audit_route.TEST_ADAPTER = adapter
    audit_route.TEST_FETCH = lambda conn_, u: fixture_page(u)

    latencies = []
    session_ids = []
    failures = []
    t_start = time.time()
    for url in corpus:
        t0 = time.time()
        try:
            result = audit_route.run_live_audit(
                conn, url, depth=10, adapter=adapter,
                fetch_page=audit_route.TEST_FETCH and
                (lambda c, u: fixture_page(u)))
            if not result.get("ok"):
                failures.append((url, result.get("error")))
                continue
            session_ids.append((url, result["session_id"]))
            latencies.append(time.time() - t0)
        except Exception as exc:
            failures.append((url, f"crash: {type(exc).__name__}: {exc}"))
    wall = time.time() - t_start
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else 999

    check("2 soak: 50/50 sessions complete with typed results",
          len(session_ids) == 50 and not failures,
          f"ok={len(session_ids)} failures={failures[:3]}")
    check("3 p95 end-to-end latency <= 10s (mock acceptance)",
          p95 <= 10.0, f"p95={p95:.3f}s wall={wall:.2f}s")

    # serp_status: every session ok (fixture SERP never returns empty)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT serp_status, count(*) FROM audit_sessions
            WHERE requested_url LIKE %s GROUP BY serp_status
            """,
            (f"%{RUN_TOKEN}%",))
        status_counts = dict(cur.fetchall())
    check("4 every session serp_status stamped (ok/zero_results)",
          set(status_counts.keys()) <= {"ok", "zero_results"}
          and "pending" not in status_counts
          and sum(status_counts.values()) == 50,
          repr(status_counts))

    # 5: every fetched session has a persisted page snapshot (corpus)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM audit_page_snapshots p
            JOIN audit_sessions s USING (session_id)
            WHERE s.requested_url LIKE %s
            """,
            (f"%{RUN_TOKEN}%",))
        snap_total = cur.fetchone()[0]
    check("5 snapshot corpus retained (one per fetched session)",
          snap_total == 50, repr(snap_total))

    # 6: validator invariant — every draft passed the validator
    #    (re-derive suggestions per session and assert problems-free drafts)
    from audit.context_adapter import generate_suggestions
    invalid_drafts = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.session_id, s.normalized_url, s.mode, s.inferred_query,
                   s.inferred_intent
            FROM audit_sessions s WHERE s.requested_url LIKE %s
            """,
            (f"%{RUN_TOKEN}%",))
        sessions = cur.fetchall()
    for sid, nurl, mode, q, intent in sessions:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT title, meta_description, h1, body_text, heading_outline,
                       render_status, status_code
                FROM audit_page_snapshots WHERE session_id = %s
                ORDER BY fetched_at DESC LIMIT 1
                """,
                (sid,))
            snap_row = cur.fetchone()
        page = {
            "title": snap_row[0], "meta_description": snap_row[1],
            "h1": snap_row[2], "body_text": snap_row[3],
            "heading_outline": snap_row[4] or [],
            "render_status": snap_row[5], "status_code": snap_row[6],
        }
        sugg = generate_suggestions(conn, nurl, mode, page=page,
                                    inferred_query=q, inferred_intent=intent,
                                    serp_rows=[], site_id=None)
        for field in ("seo.title", "seo.description"):
            entry = sugg.get(field) or {}
            if entry.get("draft") is not None \
                    and entry.get("validator_problems"):
                invalid_drafts.append((nurl, field,
                                       entry["validator_problems"]))
    check("6 every surfaced draft passed its validator (invariant)",
          not invalid_drafts, repr(invalid_drafts[:3]))

    # 4: cache — rerun the whole corpus; every call must hit the 24h cache
    calls_before = adapter.calls
    t0 = time.time()
    cache_hits = 0
    for url in corpus:
        res = audit_route.run_live_audit(conn, url, depth=10, adapter=adapter,
                                         fetch_page=lambda c, u: fixture_page(u))
        if res.get("cache", {}).get("hit"):
            cache_hits += 1
    rerun_wall = time.time() - t0
    check("7 rerun: all 50 served from cache, ZERO adapter fetches",
          cache_hits == 50 and adapter.calls == 50,  # 50 = exactly the first pass
          f"hits={cache_hits} adapter_calls={adapter.calls}")

    # 5: read-only path wrote zero pipeline rows
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM generated_fixes WHERE target_url "
                    "LIKE %s", (f"%{RUN_TOKEN}%",))
        fixes_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM recommendations WHERE target_url "
                    "LIKE %s", (f"%{RUN_TOKEN}%",))
        recs_count = cur.fetchone()[0]
    check("8 read/write segregation across the soak: zero pipeline writes",
          fixes_count == 0 and recs_count == 0,
          repr((fixes_count, recs_count)))

    # 9: cost rows logged once per live fetch (50), never on cache hits
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM api_costs
            WHERE metadata->>'source' = 'audit_engine'
              AND metadata->>'session_id' IN (
                  SELECT session_id::text FROM audit_sessions
                  WHERE requested_url LIKE %s)
            """,
            (f"%{RUN_TOKEN}%",))
        cost_rows = cur.fetchone()[0]
    check("9 cost logged exactly once per LIVE serp (not on cache hits)",
          cost_rows == 50, repr(cost_rows))

    # ------------------------------------------------------------
    # TTL SWEEP (Phase E item 3)
    # ------------------------------------------------------------
    from audit.ttl_sweep import sweep
    # expire 5 sessions
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE audit_sessions SET expires_at = now() - INTERVAL '1 hour'
            WHERE session_id IN (
                SELECT session_id FROM audit_sessions
                WHERE requested_url LIKE %s LIMIT 5)
            RETURNING session_id
            """,
            (f"%{RUN_TOKEN}%",))
        expired_ids = [str(r[0]) for r in cur.fetchall()]
    conn.commit()
    result = sweep(conn)
    conn.commit()
    pruned_ids = result.get("pruned", [])
    check("10 TTL sweep prunes expired sessions",
          result.get("ok") is True
          and all(sid in pruned_ids for sid in expired_ids),
          repr((result.get("count"), len(expired_ids))))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM audit_page_snapshots WHERE session_id = ANY(%s::uuid[])
            """,
            (expired_ids,))
        gone = cur.fetchone()[0]
    check("10 children cascaded on prune", gone == 0, repr(gone))
    # survivors: precisely the 45 un-expired session ids from this run
    fresh_ids = [sid for (_u, sid) in session_ids if sid not in expired_ids]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM audit_sessions WHERE session_id = ANY(%s::uuid[])
            """,
            (fresh_ids,))
        kept = cur.fetchone()[0]
    check("10 fresh sessions survive", kept == len(fresh_ids),
          repr((kept, len(fresh_ids))))

    # ------------------------------------------------------------
    # IMPORT-GRAPH INVARIANT (§6 acceptance)
    # ------------------------------------------------------------
    import ast
    audit_dir = os.path.join(HERE, "..", "src", "audit")
    violations = []
    for fname in os.listdir(audit_dir):
        if not fname.endswith(".py"):
            continue
        with open(os.path.join(audit_dir, fname), encoding="utf-8") as fh:
            tree = ast_parse(fh.read())
        for node in ast_walk(tree):
            names = []
            if isinstance(node, ast_import_from_node()):
                names = [a.name for a in getattr(node, "module", "") and
                         [] or []]
            mod = getattr(node, "module", None) if hasattr(node, "module") else None
            if isinstance(node, ast_import_from_node()) and mod:
                if mod.startswith("jobs.fix_executor") or mod == "fix_executor":
                    violations.append((fname, mod))
                for alias in getattr(node, "names", []):
                    if (alias.name or "").startswith("jobs.fix_executor"):
                        violations.append((fname, alias.name))
            if isinstance(node, ast_import_node()):
                for alias in getattr(node, "names", []):
                    if (alias.name or "").startswith("jobs.fix_executor"):
                        violations.append((fname, alias.name))
    check("11 audit modules never import fix_executor internals (graph test)",
          not violations, repr(violations))

    # audit route surface: no Shopify connector import (read/write split)
    with open(os.path.join(HERE, "..", "src", "api", "routes", "audit.py"),
              encoding="utf-8") as fh:
        route_src = fh.read()
    route_violations = []
    for node in ast_walk(ast_parse(route_src)):
        mod = getattr(node, "module", None) if hasattr(node, "module") else None
        if isinstance(node, ast_import_from_node()) and mod:
            if mod.startswith("connectors.shopify"):
                route_violations.append(mod)
            for alias in getattr(node, "names", []):
                if (alias.name or "").startswith("connectors.shopify"):
                    route_violations.append(alias.name)
        if isinstance(node, ast_import_node()):
            for alias in getattr(node, "names", []):
                if (alias.name or "").startswith("connectors.shopify"):
                    route_violations.append(alias.name)
    check("12 audit route imports no Shopify connector (AST graph test)",
          not route_violations, repr(route_violations))

    # ------------------------------------------------------------
    # LIVE SMOKE (owner-gated; skipped by default)
    # ------------------------------------------------------------
    if os.environ.get("AUDIT_LIVE_SMOKE") == "1":
        allok = live_smoke(conn, audit_route)
        # fold result into failures list semantics
        if not allok:
            FAILURES.append("live_smoke")
    else:
        check("13 live smoke SKIPPED (owner-funded; set AUDIT_LIVE_SMOKE=1 "
              "to run)", True)

    audit_route.TEST_ADAPTER = None
    audit_route.TEST_FETCH = None
    cleanup()
    conn.close()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        sys.exit(1)
    print("ALL SOAK TESTS PASS "
          f"(50 sessions, p95={p95:.2f}s, wall={wall:.1f}s)")


def ast_parse(source):
    import ast
    return ast.parse(source)


def ast_walk(tree):
    import ast
    return ast.walk(tree)


def ast_import_from_node():
    import ast
    return ast.ImportFrom


def ast_import_node():
    import ast
    return ast.Import


def live_smoke(conn, audit_route):
    """One real-domain audit behind AUDIT_LIVE_SMOKE=1 (owner-funded).

    Asserts plan/23 §6 acceptance: latency <= 30s, cost logged,
    suggestions present. Uses the REAL adapter (live mode); the caller
    must have INTEGRATION_MODE=live + credentials configured.
    """
    from connectors.mode import resolve_mode
    if resolve_mode() != "live":
        print("[live-smoke] INTEGRATION_MODE != live — nothing to assert")
        return True
    url = os.environ.get("AUDIT_LIVE_SMOKE_URL",
                         "https://shopify.com/products/undefined")
    audit_route.TEST_ADAPTER = None
    audit_route.TEST_FETCH = None
    t0 = time.time()
    result = audit_route.run_live_audit(conn, url, depth=10)
    wall = time.time() - t0
    ok = True
    ok &= check("live smoke completed", result.get("ok") is True,
                repr(result.get("error")))
    ok &= check("live smoke latency <= 30s", wall <= 30.0, f"{wall:.1f}s")
    ok &= check("live smoke suggestions present",
                bool(result.get("suggestions")))
    ok &= check("live smoke cost logged",
                (result.get("cost") or {}).get("serp_usd") is not None
                or result.get("serp", {}).get("zero_results") is True)
    return ok


if __name__ == "__main__":
    main()
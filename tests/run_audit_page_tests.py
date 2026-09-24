# -*- coding: utf-8 -*-
"""Phase A tests — page ingest + site resolution (plan/23 §6 Phase A).

python tests/run_audit_page_tests.py   (needs a reachable local Postgres)

Verifies:
  parser
    1. static HTML: title / meta / h1 / outline / body extraction
    2. JS-shell page -> not_rendered flag, script content dropped
    3. meta-less page -> meta_description None
    4. heading outline ordering (document order, H1-H3 only)
    5. huge page -> size cap rejected
    6. fallback regex extractor (parser-hostile HTML)
  SSRF
    7. localhost / 127.0.0.1 / 10.x / 169.254.x / 192.168.x rejected;
       DNS resolving into a protected range rejected
  robots
    8. robots disallow -> blocked_robots (page never fetched)
  fetch
    9. non-200 -> typed HTTP error; huge body -> size cap;
       redirect >1 hop -> typed error; invalid URL shapes -> invalid_url
  site resolution
   10. bare domain match -> connected (match_basis domain)
   11. myshopify host match -> connected (match_basis myshopify_host)
   12. www-strip + case-fold still match; port/scheme dropped
   13. subdomain is NOT a match in v1; no-match -> audit_checklist;
       IP-literal URL -> audit_checklist; resolution writes nothing
  schema + session lifecycle (real local Postgres)
   14. plan/23-audit-schema.sql applies idempotently
   15. session + snapshot insert; CHECK constraints reject bad statuses
   16. TTL sweep prunes expired sessions, cascades children, keeps fresh
  isolation: every site_config row and session row keyed off RUN_TOKEN,
  removed in cleanup.

NO SERP/keyword_volume calls anywhere (Phase A = zero external spend).
"""
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

PAGE_STATIC = """<!DOCTYPE html>
<html><head>
<title>Aurora Desk Lamp — Warm LED Lighting | Lume Co</title>
<meta name="description" content="The Aurora desk lamp brings warm, dimmable light to late-night work sessions.">
</head>
<body>
<h1>Aurora Desk Lamp</h1>
<p>The Aurora desk lamp is a warm, dimmable LED lamp built for late-night work.</p>
<h2>Why designers pick it</h2>
<p>Three color modes and a memory dial.</p>
<h3>Specs</h3>
<p>2700-5000K, 800 lumens.</p>
<script>var telemetry = "this must never leak into body text";</script>
<style>.badge { color: red }</style>
</body></html>"""

PAGE_JS_SHELL = """<!DOCTYPE html>
<html><head><title>Lume Co — Loading</title></head>
<body>
<div id="root"></div>
<script>document.write("client-rendered content that a static GET cannot see");</script>
</body></html>"""

PAGE_NO_META = """<html><head><title>Just a Title</title></head>
<body><h1>Heading One</h1><p>Some words.</p></body></html>"""

PAGE_HOSTILE = """<html><head><TITLE>Hostile  Page</TITLE >
</head><body><H2 CLASS="x">Two &amp; More</H2><h4>ignored level</h4>
<p>Text here.</p></body></html>"""

ROBOTS_DISALLOW = """User-agent: OpenSEOAuditBot
Disallow: /private/

User-agent: *
Disallow: /other/
"""

ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"


class _State:
    page = PAGE_STATIC
    status = 200
    robots = ROBOTS_ALLOW


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/robots.txt"):
            body = _State.robots.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/redirect-endless"):
            self.send_response(302)
            self.send_header("Location", "/redirect-endless")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif self.path.startswith("/redirect-once"):
            self.send_response(302)
            self.send_header("Location", "/final")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif self.path.startswith("/final"):
            body = PAGE_STATIC.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        else:
            body = _State.page.encode()
        self.send_response(_State.status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ------------------------------------------------------------

def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()

    # ---- 14: schema applies idempotently (twice) ---------------------
    schema_path = os.path.join(HERE, "..", "plan", "23-audit-schema.sql")
    with open(schema_path, encoding="utf-8") as fh:
        schema_sql = fh.read()
    applied_ok = True
    for _attempt in (1, 2):
        try:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
            conn.commit()
        except Exception as exc:
            applied_ok = False
            conn.rollback()
            check("14 schema applies idempotently", False, str(exc))
            break
    if applied_ok:
        check("14 schema applies idempotently (twice, no error)", True)

    def cleanup():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_sessions WHERE requested_url LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM site_config WHERE site_name LIKE %s",
                        (f"{RUN_TOKEN}%",))
        conn.commit()

    cleanup()

    # ------------------------------------------------------------
    # PARSER
    # ------------------------------------------------------------
    from audit.page_fetch import (PageFetchError, build_page_snapshot,
                                  content_hash, parse_page_html,
                                  ssrf_check_host, validate_url,
                                  MAX_BODY_BYTES)

    parsed = parse_page_html(PAGE_STATIC)
    check("1 title extracted",
          parsed["title"] == "Aurora Desk Lamp — Warm LED Lighting | Lume Co",
          repr(parsed["title"]))
    check("1 meta description extracted",
          bool(parsed["meta_description"])
          and "Aurora desk lamp" in parsed["meta_description"],
          repr(parsed["meta_description"]))
    check("1 h1 extracted", parsed["h1"] == "Aurora Desk Lamp",
          repr(parsed["h1"]))
    check("1 body_text excludes script/style, keeps prose",
          bool(parsed["body_text"])
          and "telemetry" not in parsed["body_text"]
          and "dimmable LED lamp" in parsed["body_text"],
          repr(parsed["body_text"]))
    check("1 body_html non-None", bool(parsed["body_html"]))
    check("1 content_hash stable + input-sensitive",
          content_hash(parsed["body_text"]) == content_hash(parsed["body_text"])
          and content_hash(parsed["body_text"]) != content_hash("different"))

    js = parse_page_html(PAGE_JS_SHELL)
    check("2 JS shell: render_status honestly not_rendered",
          js["render_status"] == "not_rendered")
    check("2 JS shell: client-rendered content absent from body_text",
          not js["body_text"] or "client-rendered" not in js["body_text"],
          repr(js["body_text"]))

    nometa = parse_page_html(PAGE_NO_META)
    check("3 meta-less page: meta_description None",
          nometa["meta_description"] is None,
          repr(nometa["meta_description"]))

    outline = parsed["heading_outline"]
    check("4 heading outline document order",
          [e["level"] for e in outline] == ["h1", "h2", "h3"]
          and outline[0]["text"] == "Aurora Desk Lamp",
          repr(outline))
    hostile = parse_page_html(PAGE_HOSTILE)
    check("4 h4 ignored (H1-H3 only), hostile HTML survives",
          all(e["level"] in ("h1", "h2", "h3")
              for e in hostile["heading_outline"])
          and hostile["title"] == "Hostile Page",
          repr((hostile["title"], hostile["heading_outline"])))

    # meta tag as self-closing / different attr order (startendtag path)
    meta_selfclosing = parse_page_html(
        '<html><head><title>T</title>'
        '<meta content="Desc here" name="description"/></head>'
        '<body><p>x</p></body></html>')
    check("4 meta captured via startendtag (attr order agnostic)",
          meta_selfclosing["meta_description"] == "Desc here",
          repr(meta_selfclosing["meta_description"]))

    # ------------------------------------------------------------
    # SSRF
    # ------------------------------------------------------------
    for bad_host in ("localhost", "127.0.0.1", "10.1.2.3", "169.254.169.254",
                     "192.168.0.5", "0.0.0.0"):
        try:
            ssrf_check_host(bad_host)
            check(f"7 SSRF rejects {bad_host}", False, "no exception")
        except PageFetchError as exc:
            check(f"7 SSRF rejects {bad_host}",
                  exc.reason in ("ssrf_blocked", "invalid_url"),
                  f"reason={exc.reason}")

    import audit.page_fetch as pf
    orig_getaddrinfo = pf.socket.getaddrinfo
    pf.socket.getaddrinfo = lambda *a, **k: [(2, 1, 6, "", ("10.0.0.9", 0))]
    try:
        try:
            ssrf_check_host("rebind.example.com")
            check("7 SSRF rejects DNS->protected range", False, "no exception")
        except PageFetchError as exc:
            check("7 SSRF rejects DNS->protected range",
                  exc.reason == "ssrf_blocked", f"reason={exc.reason}")
    finally:
        pf.socket.getaddrinfo = orig_getaddrinfo

    # URL-shape validation (400 contract)
    for bad in ("", "   ", "not a url", "ftp://example.com/x",
                "javascript:alert(1)", "https://"):
        try:
            validate_url(bad)
            check(f"9 invalid_url for {bad!r}", False, "no exception")
        except PageFetchError as exc:
            check(f"9 invalid_url for {bad!r}", exc.reason == "invalid_url",
                  f"reason={exc.reason}")

    # ------------------------------------------------------------
    # ROBOTS + FETCH against a real loopback server
    # ------------------------------------------------------------
    server, base = start_server()
    try:
        # Loopback fixture server: ssrf_guard=False is the documented
        # TEST-ONLY escape hatch (live callers always guard).
        _State.robots = ROBOTS_DISALLOW
        _State.page = PAGE_STATIC
        _State.status = 200
        try:
            build_page_snapshot(f"{base}/private/x?run={RUN_TOKEN}",
                                ssrf_guard=False)
            check("8 robots disallow -> blocked_robots", False, "no exception")
        except PageFetchError as exc:
            check("8 robots disallow -> blocked_robots",
                  exc.reason == "blocked_robots", f"reason={exc.reason}")

        _State.robots = ROBOTS_ALLOW
        snap = build_page_snapshot(f"{base}/products/aurora?run={RUN_TOKEN}",
                                   ssrf_guard=False)
        check("8 robots allow -> snapshot built",
              snap["status_code"] == 200 and snap["title"] is not None,
              repr(snap.get("title")))
        check("8 snapshot fields complete",
              RUN_TOKEN in snap["url"]
              and isinstance(snap["content_hash"], str)
              and len(snap["content_hash"]) == 64,
              repr(snap["url"]))

        # 5: huge body -> size cap
        _State.page = "x" * (MAX_BODY_BYTES + 1)
        try:
            build_page_snapshot(f"{base}/huge?run={RUN_TOKEN}",
                                ssrf_guard=False)
            check("5 huge page rejected by size cap", False, "no exception")
        except PageFetchError as exc:
            check("5 huge page rejected by size cap",
                  exc.reason == "page_unreachable"
                  and "exceeds" in (exc.detail or ""),
                  f"reason={exc.reason} detail={exc.detail}")

        # 9: non-200 -> typed error carrying the code
        _State.page = "gone"
        _State.status = 403
        try:
            build_page_snapshot(f"{base}/gone?run={RUN_TOKEN}",
                                ssrf_guard=False)
            check("9 non-200 -> typed HTTP error", False, "no exception")
        except PageFetchError as exc:
            check("9 non-200 -> typed HTTP error",
                  exc.reason == "page_unreachable"
                  and "403" in (exc.detail or ""),
                  f"reason={exc.reason} detail={exc.detail}")

        # 9: endless redirect -> rejected by the 1-hop limit
        _State.status = 200
        try:
            build_page_snapshot(f"{base}/redirect-endless?run={RUN_TOKEN}",
                                ssrf_guard=False)
            check("9 redirect >1 hop rejected", False, "no exception")
        except PageFetchError as exc:
            check("9 redirect >1 hop rejected",
                  exc.reason == "page_unreachable"
                  and "redirect" in (exc.detail or ""),
                  f"reason={exc.reason} detail={exc.detail}")

        # 9: exactly 1 hop is allowed
        snap = build_page_snapshot(f"{base}/redirect-once?run={RUN_TOKEN}",
                                   ssrf_guard=False)
        check("9 exactly 1 redirect hop allowed",
              snap["status_code"] == 200 and snap["title"] is not None)

        # guard still ON: a loopback URL via the default builder must be
        # rejected (proves the test hook didn't weaken the live path)
        try:
            build_page_snapshot(f"{base}/products/aurora?run={RUN_TOKEN}")
            check("9 ssrf guard default-ON rejects loopback", False,
                  "no exception")
        except PageFetchError as exc:
            check("9 ssrf guard default-ON rejects loopback",
                  exc.reason == "ssrf_blocked", f"reason={exc.reason}")
    finally:
        server.shutdown()

    # ------------------------------------------------------------
    # SITE RESOLUTION (real site_config rows, isolated by RUN_TOKEN)
    # ------------------------------------------------------------
    import audit.site_resolution as sr

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property)
            VALUES (%s, 'lume-store.com', 'sc-domain:lume-store.com')
            RETURNING site_id
            """,
            (f"{RUN_TOKEN}-domain-site",))
        site_domain_id = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property,
                                     shopify_domain)
            VALUES (%s, 'other-brand.com', 'sc-domain:other-brand.com',
                    'lume.myshopify.com')
            RETURNING site_id
            """,
            (f"{RUN_TOKEN}-shopify-site",))
        site_shopify_id = str(cur.fetchone()[0])
    conn.commit()

    res = sr.resolve_site(conn, "https://lume-store.com/products/aurora")
    check("10 domain match -> connected",
          res["mode"] == "connected" and str(res["site_id"]) == site_domain_id
          and res["match_basis"] == "domain", repr(res))

    res = sr.resolve_site(conn, "https://lume.myshopify.com/products/aurora")
    check("11 myshopify host match -> connected (shopify_domain wins "
          "per §2.1 order)",
          res["mode"] == "connected"
          and str(res["site_id"]) == site_shopify_id
          and res["match_basis"] == "shopify_domain", repr(res))

    res = sr.resolve_site(conn, "https://WWW.Lume-Store.com/Aurora?utm_source=x")
    check("12 www-strip + case-fold still matches (domain basis)",
          res["mode"] == "connected" and res["match_basis"] == "domain",
          repr(res))

    res = sr.resolve_site(conn, "http://lume-store.com:4433/products/aurora")
    check("12 port/scheme dropped in comparison",
          res["mode"] == "connected", repr(res))

    res = sr.resolve_site(conn, "https://shop.lume-store.com/products/aurora")
    check("13 subdomain is NOT a registrable match in v1",
          res["mode"] == "audit_checklist" and res["match_basis"] == "none",
          repr(res))

    res = sr.resolve_site(conn, "https://unknown-brand.example/products/x")
    check("13 no-match -> audit_checklist",
          res["mode"] == "audit_checklist" and res["site_id"] is None
          and res["match_basis"] == "none", repr(res))

    res = sr.resolve_site(conn, "https://192.168.0.5/page")
    check("13 IP-literal URL -> audit_checklist (no registrable domain)",
          res["mode"] == "audit_checklist", repr(res))

    res = sr.resolve_site(conn, "https://stray.myshopify.com/products/x")
    check("13 unregistered myshopify host -> audit_checklist (safe)",
          res["mode"] == "audit_checklist", repr(res))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM site_config WHERE site_name LIKE %s",
                    (f"{RUN_TOKEN}%",))
        cfg_count = cur.fetchone()[0]
    conn.commit()
    check("13 resolution wrote zero site_config rows", cfg_count == 2,
          f"count={cfg_count}")

    # ------------------------------------------------------------
    # SESSION LIFECYCLE + TTL SWEEP (real tables)
    # ------------------------------------------------------------
    from audit.ttl_sweep import sweep

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_sessions (requested_url, normalized_url, domain,
                                        mode, fetch_status)
            VALUES (%s, %s, 'fresh.example', 'audit_checklist', 'fetched')
            RETURNING session_id
            """,
            (f"https://fresh.example/page?run={RUN_TOKEN}",
             f"https://fresh.example/page?run={RUN_TOKEN}"))
        fresh_id = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO audit_page_snapshots (session_id, url, status_code,
                                              title, body_text, content_hash)
            VALUES (%s, %s, 200, 't', 'b', 'h')
            """,
            (fresh_id, f"https://fresh.example/page?run={RUN_TOKEN}"))
        # expired session + children
        cur.execute(
            """
            INSERT INTO audit_sessions (requested_url, normalized_url, domain,
                                        mode, fetch_status, expires_at)
            VALUES (%s, %s, 'expired.example', 'audit_checklist', 'fetched',
                    now() - INTERVAL '1 hour')
            RETURNING session_id
            """,
            (f"https://expired.example/page?run={RUN_TOKEN}",
             f"https://expired.example/page?run={RUN_TOKEN}"))
        expired_id = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO audit_page_snapshots (session_id, url, status_code)
            VALUES (%s, %s, 200)
            """,
            (expired_id, f"https://expired.example/page?run={RUN_TOKEN}"))
        cur.execute(
            """
            INSERT INTO audit_serp_competitors (session_id, query, position,
                                                result_url, result_domain)
            VALUES (%s, 'q', 1, 'https://c.example/x', 'c.example')
            """,
            (expired_id,))
    conn.commit()

    result = sweep(conn)
    conn.commit()
    check("16 TTL sweep ok", result.get("ok") is True, repr(result))
    check("16 pruned exactly the expired session",
          expired_id in result.get("pruned", [])
          and fresh_id not in result.get("pruned", []),
          repr(result.get("pruned")))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM audit_page_snapshots p
            JOIN audit_serp_competitors s USING (session_id)
            WHERE p.session_id = %s
            """,
            (expired_id,))
        gone_children = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM audit_sessions WHERE session_id = %s",
            (fresh_id,))
        kept_fresh = cur.fetchone()[0]
    check("16 children cascade with the session", gone_children == 0,
          f"children={gone_children}")
    check("16 fresh session survives the sweep", kept_fresh == 1)

    # 15: CHECK constraints reject invalid statuses
    def _insert_bad_fetch_status():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_sessions (requested_url, normalized_url,
                                            domain, mode, fetch_status)
                VALUES (%s, %s, 'bad.example', 'audit_checklist', 'nonsense')
                """,
                (f"https://bad.example/x?run={RUN_TOKEN}",
                 f"https://bad.example/x?run={RUN_TOKEN}"))

    try:
        _insert_bad_fetch_status()
        conn.commit()
        check("15 CHECK constraint rejects invalid fetch_status", False)
    except Exception:
        conn.rollback()
        check("15 CHECK constraint rejects invalid fetch_status", True)

    def _insert_bad_mode():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_sessions (requested_url, normalized_url,
                                            domain, mode)
                VALUES (%s, %s, 'bad.example', 'telepathy')
                """,
                (f"https://bad2.example/x?run={RUN_TOKEN}",
                 f"https://bad2.example/x?run={RUN_TOKEN}"))

    try:
        _insert_bad_mode()
        conn.commit()
        check("15 CHECK constraint rejects invalid mode", False)
    except Exception:
        conn.rollback()
        check("15 CHECK constraint rejects invalid mode", True)

    # 16: sweep run() end-to-end (job wrapper, real DB)
    import importlib
    ttl = importlib.import_module("audit.ttl_sweep")
    wrapper = ttl.run()
    check("16 ttl_sweep.run() wrapper typed result",
          isinstance(wrapper, dict) and "ok" in wrapper, repr(wrapper))

    cleanup()
    conn.close()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
# -*- coding: utf-8 -*-
"""Last-mile execution tests — Tasks 1–6 (GID ingestion, redirect adapter,
GSC URL Inspection, competitor headings, sitemap verification, audit route).

python tests/run_last_mile_tests.py   (needs a reachable local Postgres)

Covers:
  Task 1  shopify_gid ingestion via sync_pages_from_shopify (mock fixtures)
  Task 2  redirect adapter payload validation + fake-store create/delete;
          consolidate recommendation -> redirect fix row
  Task 3  url_inspection capability (mock fixture + unsupported + normalizer)
          and sync_url_inspection quota gate
  Task 4  competitor heading outline grounding (fetcher seam, no network)
  Task 5  robots.txt + sitemap.xml parsing + membership flags (local HTTP)
  Task 6  audit route mounted + FastAPI route surface

NO external network: Task 5 spins a loopback HTTP server; everything else
runs on fixtures / adapter seams.
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
sys.path.insert(0, os.path.join(HERE, "..", "src", "connectors"))

import db as database  # noqa: E402
import env as env_loader  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
FAILURES = []
FIXTURES_DIR = os.path.join(HERE, "fixtures")


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


# ------------------------------------------------------------
# Loopback server for Task 5 (robots.txt + sitemap.xml)
# ------------------------------------------------------------

ROBOTS_BODY = (
    "User-agent: *\n"
    "Disallow: /checkout\n"
    "Sitemap: https://loopback.test/sitemap_index.xml\n"
)
SITEMAP_INDEX_BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<sitemap><loc>https://loopback.test/sitemap_products.xml</loc></sitemap>"
    "</sitemapindex>"
)
SITEMAP_PRODUCTS_BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://loopback.test/products/loop-p1</loc></url>"
    "<url><loc>https://loopback.test/products/loop-p2</loc></url>"
    "</urlset>"
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/robots.txt":
            body = ROBOTS_BODY
        elif self.path == "/sitemap_index.xml":
            body = SITEMAP_INDEX_BODY
        elif self.path == "/sitemap_products.xml":
            body = SITEMAP_PRODUCTS_BODY
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()

    # schema (22 + 24 applied idempotently; 00 assumed present)
    for schema in ("22-stage2-phase0-schema.sql",
                   "24-gsc-inspection-and-redirects.sql"):
        with open(os.path.join(HERE, "..", "plan", schema),
                  encoding="utf-8") as fh:
            with conn.cursor() as cur:
                cur.execute(fh.read())
    conn.commit()

    def cleanup():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pages WHERE site_id IN "
                        "(SELECT site_id FROM site_config WHERE domain LIKE %s)",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM generated_fixes WHERE target_url LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM recommendations WHERE diagnosis LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM site_config WHERE domain LIKE %s",
                        (f"%{RUN_TOKEN}%",))
            cur.execute("DELETE FROM api_costs WHERE service = 'gsc_url_inspection' "
                        "AND call_type = 'url_inspection'")
            cur.execute("DELETE FROM audit_sessions WHERE requested_url LIKE %s",
                        (f"%{RUN_TOKEN}%",))
        conn.commit()

    cleanup()

    from connectors.sync import (sync_pages_from_shopify,
                                 _shopify_gid,
                                 sync_url_inspection,
                                 apply_inspection_result,
                                 _inspection_targets,
                                 _inspection_quota_remaining)
    from connectors.shopify import get_shopify_adapter
    from connectors.gsc import get_gsc_adapter
    from fixes.adapters import (ADAPTERS, get_adapter,
                                _redirect_payload_for,
                                URL_REDIRECT_CREATE_MUTATION)
    from fixes.generator import (generate_fix_for_recommendation,
                                 FixNotSupported,
                                 build_redirect_payload,
                                 _redirect_path_part)
    from audit.competitor_headings import (scan_competitor_headings,
                                           content_outline_gaps_with_headings,
                                           heading_to_section)
    import site_fetch
    from site_fetch import (parse_sitemap_xml, fetch_robots_txt,
                            fetch_sitemap, sync_sitemap_membership)

    # ------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------
    shopify = get_shopify_adapter(config={
        "shopify.mode": "mock", "shopify.mock_fixtures_dir": FIXTURES_DIR,
    })
    gsc = get_gsc_adapter(config={
        "gsc.mode": "mock", "gsc.mock_fixtures_dir": FIXTURES_DIR,
        "gsc.site_url": "sc-domain:example.com",
    })

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO site_config (site_name, domain, gsc_property, "
            "shopify_domain, catalogue_size_tier) "
            "VALUES (%s, %s, %s, %s, 'small') RETURNING site_id",
            (f"LastMile {RUN_TOKEN}", f"loopback-{RUN_TOKEN}.test",
             f"sc-domain:loopback-{RUN_TOKEN}.test",
             f"store-{RUN_TOKEN}.myshopify.com"),
        )
        site_id = str(cur.fetchone()[0])
    conn.commit()

    # ------------------------------------------------------------
    # TASK 1: GID ingestion
    # ------------------------------------------------------------
    inserted = sync_pages_from_shopify(conn, shopify, site_id, f"loopback-{RUN_TOKEN}.test")
    check("T1 sync inserted fixture rows", inserted > 0, repr(inserted))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pages WHERE site_id = %s AND shopify_gid LIKE %s",
            (site_id, "gid://shopify/Product/%"))
        products_with_gid = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM pages WHERE site_id = %s AND shopify_gid LIKE %s",
            (site_id, "gid://shopify/Collection/%"))
        collections_with_gid = cur.fetchone()[0]
    check("T1 product GIDs ingested", products_with_gid >= 6,
          repr(products_with_gid))
    check("T1 collection GIDs ingested", collections_with_gid >= 5,
          repr(collections_with_gid))

    check("T1 _shopify_gid minting",
          _shopify_gid("Product", 7812300451) == "gid://shopify/Product/7812300451"
          and _shopify_gid("Product", "gid://shopify/Product/1")
          == "gid://shopify/Product/1"
          and _shopify_gid("Product", None) is None
          and _shopify_gid("Product", "abc") is None)

    # Fix generation no longer raises the gid-less FixNotSupported for a
    # synced page: pick one product page and force a quality failure.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT url, title FROM pages WHERE site_id = %s "
            "AND page_type = 'product' AND shopify_gid IS NOT NULL LIMIT 1",
            (site_id,))
        page_url, page_title = cur.fetchone()

    # ------------------------------------------------------------
    # TASK 2: redirect adapter + consolidate fix branch
    # ------------------------------------------------------------
    check("T2 redirect adapter registered", "redirect" in ADAPTERS
          and callable(ADAPTERS["redirect"]["execute"])
          and callable(ADAPTERS["redirect"]["restore"])
          and callable(ADAPTERS["redirect"]["snapshot"]))

    bad_payload = {"mutation": "productUpdate", "variables": {}}
    try:
        _redirect_payload_for(bad_payload)
        ok = False
    except Exception as exc:
        ok = "unsupported mutation" in str(exc)
    check("T2 payload rejects wrong mutation", ok)

    payload = build_redirect_payload("/collections/old", "/collections/new")
    redirect = _redirect_payload_for(payload)
    check("T2 payload accepts urlRedirectCreate",
          redirect == {"path": "/collections/old", "target": "/collections/new"},
          repr(redirect))

    loop_payload = build_redirect_payload("/x", "/x")
    try:
        _redirect_payload_for(loop_payload)
        ok = False
    except Exception as exc:
        ok = "no-op loop" in str(exc)
    check("T2 payload rejects path==target loop", ok)

    check("T2 _redirect_path_part",
          _redirect_path_part("https://store.test/collections/a?x=1") == "/collections/a"
          and _redirect_path_part("/products/p") == "/products/p"
          and _redirect_path_part("https://store.test/") is None
          and _redirect_path_part(None) is None)

    # End-to-end: consolidate recommendation -> redirect fix row.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendations (site_id, generator, action_type, "
            "target_url, proposed_url, diagnosis, status) "
            "VALUES (%s, 'cannibalization', 'consolidate', %s, %s, %s, 'approved') "
            "RETURNING recommendation_id",
            (site_id, f"https://loopback-{RUN_TOKEN}.test/collections/survivor",
             f"https://loopback-{RUN_TOKEN}.test/collections/weaker",
             f"consolidate {RUN_TOKEN}"),
        )
        rec_id = str(cur.fetchone()[0])
        # Survivor page row (indexable, with GID).
        cur.execute(
            "INSERT INTO pages (site_id, url, page_type, title, indexable, shopify_gid) "
            "VALUES (%s, %s, 'collection', 'Survivor', true, %s)",
            (site_id, f"https://loopback-{RUN_TOKEN}.test/collections/survivor",
             f"gid://shopify/Collection/{RUN_TOKEN}1"),
        )
    conn.commit()
    result = generate_fix_for_recommendation(conn, rec_id)
    check("T2 consolidate -> redirect fix row generated",
          result.get("created") is True and result.get("status") == "generated",
          repr(result))
    fix_id = result.get("fix_id")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sub_type, action_type, risk_tier, target_url, payload_json "
            "FROM generated_fixes WHERE fix_id = %s", (fix_id,))
        row = cur.fetchone()
    check("T2 fix row sub_type=risk tiered high",
          row[0] == "redirect" and row[1] == "consolidate" and row[2] == "high",
          repr(row[:3]))
    payload_json = row[4]
    if isinstance(payload_json, str):
        import json as _json
        payload_json = _json.loads(payload_json)
    check("T2 payload mutation + scope",
          payload_json.get("mutation") == "urlRedirectCreate"
          and payload_json.get("scope_required") == "write_online_store_navigation",
          repr(payload_json))

    # Idempotent regeneration returns the same fix.
    again = generate_fix_for_recommendation(conn, rec_id)
    check("T2 idempotent regeneration", again.get("fix_id") == fix_id
          and again.get("created") is False, repr(again.get("fix_id")))

    # improve_page rows still take the title path (regression check).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendations (site_id, generator, action_type, "
            "target_url, diagnosis, status) "
            "VALUES (%s, 'existing_opportunity', 'improve_page', %s, %s, 'approved') "
            "RETURNING recommendation_id",
            (site_id, page_url, f"improve {RUN_TOKEN}"),
        )
        imp_rec = str(cur.fetchone()[0])
    try:
        generate_fix_for_recommendation(conn, imp_rec)
        path_ok = True
    except FixNotSupported as exc:
        # gid-less is gone; quality-gate rejections are fine but must NOT
        # mention shopify_gid ingestion.
        path_ok = "shopify_gid" not in str(exc)
    check("T2 improve_page path intact (no GID blocker)", path_ok)

    # Redirect adapter unit: dry-run + execute against a fake client,
    # using the REAL payload generated from the consolidate row.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload_json FROM generated_fixes WHERE fix_id = %s",
            (fix_id,))
        payload_json = cur.fetchone()[0]
    if isinstance(payload_json, str):
        import json as _json
        payload_json = _json.loads(payload_json)
    fake_calls = []
    created_id = f"gid://shopify/UrlRedirect/{RUN_TOKEN}9"

    class FakeRedirectClient:
        api_version = "2026-01"

        def run(self, mutation_name, query, variables=None, **_kw):
            fake_calls.append((mutation_name, dict(variables or {})))
            if mutation_name == "urlRedirectCreate":
                return {"ok": True, "data": {"urlRedirectCreate": {
                    "urlRedirect": {"id": created_id, "path": "/collections/weaker",
                                    "target": "/collections/survivor"}}}}
            if mutation_name == "urlRedirect":
                return {"ok": True, "data": {"urlRedirect": {
                    "id": created_id, "path": "/collections/weaker",
                    "target": "/collections/survivor"}}}
            if mutation_name == "urlRedirectDelete":
                return {"ok": True, "data": {"urlRedirectDelete": {
                    "deletedUrlRedirectId": created_id}}}
            return {"ok": False, "outcome": "top_level_error"}

    import fixes.adapters as fix_adapters
    redirect_adapter = fix_adapters.get_adapter("redirect")
    fix_row = {
        "fix_id": fix_id, "recommendation_id": rec_id, "site_id": site_id,
        "action_type": "consolidate", "sub_type": "redirect",
        "target_url": f"https://loopback-{RUN_TOKEN}.test/collections/weaker",
        "target_entity_ref": f"gid://shopify/Collection/{RUN_TOKEN}1",
        "payload_json": payload_json, "diff_json": [], "risk_tier": "high",
        "snapshot_json": None,
    }
    dry = redirect_adapter["execute"](conn, fix_row, {}, dry_run=True)
    check("T2 adapter dry-run typed", dry.get("ok") is True
          and dry.get("outcome") == "dry_run", repr(dry))

    fake = FakeRedirectClient()
    result = redirect_adapter["execute"](conn, fix_row, {},
                                         dry_run=False, client=fake)
    check("T2 adapter create + verify ok",
          result.get("ok") is True and result.get("verified") is True
          and result.get("redirect_id") == created_id,
          repr(result.get("verification_status")))
    check("T2 fake client received create + read-back",
          [c[0] for c in fake_calls] == ["urlRedirectCreate", "urlRedirect"],
          repr(fake_calls))
    check("T2 adapter snapshot_patch carries created id",
          result.get("snapshot_patch", {}).get("redirect.created_id") == created_id,
          repr(result.get("snapshot_patch")))
    merged = dict(fix_row)
    merged["snapshot_json"] = {"redirect.created_id": created_id}
    restore = redirect_adapter["restore"](conn, merged, {},
                                          client=FakeRedirectClient())
    check("T2 adapter rollback deletes created redirect",
          restore.get("ok") is True and restore.get("restored") == created_id,
          repr(restore))
    no_snap = redirect_adapter["restore"](conn, dict(merged, snapshot_json={}),
                                          {}, client=FakeRedirectClient())
    check("T2 rollback refuses without created id",
          no_snap.get("ok") is False and no_snap.get("outcome") == "no_snapshot",
          repr(no_snap))
    no_creds = redirect_adapter["execute"](conn, fix_row, {}, dry_run=False)
    check("T2 no-credential -> typed no_credentials (never raises)",
          no_creds.get("ok") is False
          and no_creds.get("outcome") == "no_credentials",
          repr(no_creds))

    # ------------------------------------------------------------
    # TASK 3: GSC URL Inspection
    # ------------------------------------------------------------
    sup = gsc.supports("url_inspection")
    result = gsc.fetch("url_inspection", {
        "inspection_url": f"https://loopback-{RUN_TOKEN}.test/products/p",
    })
    check("T3 url_inspection supported + mock fixture ok",
          sup and result.get("ok") is True, repr(result.get("error")))
    row = (result.get("data") or [{}])[0]
    check("T3 normalizer verdict fields",
          row.get("verdict") == "PASS"
          and row.get("coverage_state") == "Submitted and indexed"
          and row.get("robots_txt_state") == "ALLOWED"
          and row.get("in_sitemap") is True,
          repr(row))

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pages (site_id, url, page_type, title, indexable) "
            "VALUES (%s, %s, 'product', 'Uninspected', false)",
            (site_id, f"https://loopback-{RUN_TOKEN}.test/products/insp-a"))
        cur.execute(
            "INSERT INTO pages (site_id, url, page_type, title, indexable) "
            "VALUES (%s, %s, 'product', 'Healthy', true)",
            (site_id, f"https://loopback-{RUN_TOKEN}.test/products/insp-b"))
    conn.commit()
    inspected = sync_url_inspection(conn, gsc, site_id, limit=10)
    check("T3 sync_url_inspection inspected target",
          inspected >= 1, repr(inspected))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gsc_verdict, gsc_coverage_state, gsc_in_sitemap "
            "FROM pages WHERE site_id = %s AND url = %s",
            (site_id, f"https://loopback-{RUN_TOKEN}.test/products/insp-a"))
        insp = cur.fetchone()
    check("T3 verdict persisted to pages",
          insp is not None and insp[0] == "PASS"
          and insp[1] == "Submitted and indexed" and insp[2] is True,
          repr(insp))

    # Quota gate: a full day of api_costs calls -> remaining 0.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_costs (site_id, service, call_type, calls, cost) "
            "VALUES (NULL, 'gsc_url_inspection', 'quota_seed', 2000, 0)")
    conn.commit()
    check("T3 quota exhausted -> 0 remaining",
          _inspection_quota_remaining(conn) == 0)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM api_costs WHERE call_type = 'quota_seed'")
    conn.commit()

    # Priority ordering: indexable=false pages come before indexable pages
    # (insp-a was just inspected, but indexable=false keeps it in the pool;
    # insp-b is indexable + fresh, so it must rank behind insp-a).
    targets = _inspection_targets(conn, site_id, 50)
    insp_a = f"https://loopback-{RUN_TOKEN}.test/products/insp-a"
    insp_b = f"https://loopback-{RUN_TOKEN}.test/products/insp-b"
    check("T3 indexable=false prioritized before indexable",
          insp_a in targets and insp_b in targets
          and targets.index(insp_a) < targets.index(insp_b),
          repr(targets[:4]))

    # ------------------------------------------------------------
    # TASK 4: competitor heading grounding (fetcher seam, no network)
    # ------------------------------------------------------------
    def fake_fetcher(url):
        return {"ok": True, "url": url, "h1": "Rival H1",
                "heading_outline": [
                    {"level": "h1", "text": "Rival H1"},
                    {"level": "h2", "text": "How to choose a desk lamp"},
                    {"level": "h2", "text": "Test results"},
                    {"level": "h3", "text": "Battery specs"},
                ]}

    outlines = scan_competitor_headings(
        ["https://rival-a.test/x", "https://rival-b.test/y"],
        fetcher=fake_fetcher)
    check("T4 scan returns outlines for both competitors",
          outlines["ok"] == 2 and len(outlines["outlines"]) == 2, repr(outlines))

    check("T4 heading->section mapping",
          heading_to_section("Test results: 24 lamps") == "Test results / hands-on findings"
          and heading_to_section("FAQ") == "FAQ section"
          and heading_to_section("About us") is None)

    rec_ctx = {"competitor_context": {
        "titles": [{"title": "Rival A", "url": "https://rival-a.test/x"},
                   {"title": "Rival B", "url": "https://rival-b.test/y"}],
        "snippets": [],
    }, "body_text": ""}
    gaps = content_outline_gaps_with_headings(rec_ctx, fetcher=fake_fetcher)
    check("T4 heading-grounded expected sections",
          "Step-by-step walkthrough" in gaps["expected_sections"]
          and "Test results / hands-on findings" in gaps["expected_sections"]
          and gaps["competitor_headings_scanned"] == 2
          and gaps["grounding"] == "competitor_headings+snippet_hooks",
          repr(gaps))
    check("T4 missing sections detected (empty body)",
          "Step-by-step walkthrough" in gaps["missing_sections"],
          repr(gaps["missing_sections"]))

    # Scan yields nothing -> pure snippet fallback.
    gaps_fb = content_outline_gaps_with_headings(
        {"competitor_context": {}}, fetcher=fake_fetcher)
    check("T4 graceful fallback to snippet path",
          gaps_fb.get("grounding") == "snippet_hooks"
          and gaps_fb.get("competitor_headings_scanned") == 0,
          repr(gaps_fb))

    # ------------------------------------------------------------
    # TASK 5: robots.txt + sitemap.xml (loopback server)
    # ------------------------------------------------------------
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        robots = fetch_robots_txt(f"http://127.0.0.1:{port}")
        check("T5 robots fetched + sitemap line parsed",
              robots.get("fetchable") is True
              and robots.get("sitemap_urls") == ["https://loopback.test/sitemap_index.xml"]
              and robots.get("disallow_all") is False,
              repr(robots))

        urls, children = parse_sitemap_xml(SITEMAP_PRODUCTS_BODY)
        check("T5 urlset parse", urls == ["https://loopback.test/products/loop-p1",
                                          "https://loopback.test/products/loop-p2"]
              and children == [], repr((urls, children)))
        idx_urls, idx_children = parse_sitemap_xml(SITEMAP_INDEX_BODY)
        check("T5 index parse -> children", idx_urls_ok(idx_urls, idx_children),
              repr((idx_urls, idx_children)))

        sitemap = fetch_sitemap(f"http://127.0.0.1:{port}")
        check("T5 index -> child flattening",
              sitemap.get("ok") is True
              and len(sitemap.get("urls") or []) == 2
              and sitemap.get("sitemap_url") == f"http://127.0.0.1:{port}/sitemap_index.xml",
              repr(sitemap))

        # Membership pass: seed pages, one in sitemap, one not.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pages (site_id, url, page_type, title, indexable) "
                "VALUES (%s, %s, 'product', 'P1', true), "
                "(%s, %s, 'product', 'Orphan', true)",
                (site_id, f"http://127.0.0.1:{port}/products/loop-p1",
                 site_id, f"http://127.0.0.1:{port}/products/orphan-page"))
        conn.commit()
        summary = sync_sitemap_membership(conn, site_id,
                                          f"http://127.0.0.1:{port}")
        check("T5 membership: sitemap fetchable via robots line",
              summary.get("sitemap_fetchable") is True
              and summary.get("sitemap_url") == f"http://127.0.0.1:{port}/sitemap_index.xml",
              repr(summary))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT in_sitemap FROM pages WHERE site_id = %s "
                "AND url = %s", (site_id, f"http://127.0.0.1:{port}/products/loop-p1"))
            in_sm = cur.fetchone()
            cur.execute(
                "SELECT in_sitemap FROM pages WHERE site_id = %s "
                "AND url = %s", (site_id, f"http://127.0.0.1:{port}/products/orphan-page"))
            orphan = cur.fetchone()
        check("T5 in_sitemap true/false verdict",
              in_sm and in_sm[0] is True and orphan and orphan[0] is False,
              repr((in_sm, orphan)))

        # Unreachable sitemap -> unknown (NULL), not fabricated False.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pages (site_id, url, page_type, title, indexable) "
                "VALUES (%s, %s, 'product', 'X', true)",
                (site_id, f"https://no-sitemap-{RUN_TOKEN}.invalid/products/p"))
        conn.commit()
        summary2 = sync_sitemap_membership(conn, site_id,
                                           f"no-sitemap-{RUN_TOKEN}.invalid")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT in_sitemap FROM pages WHERE site_id = %s "
                "AND url = %s",
                (site_id, f"https://no-sitemap-{RUN_TOKEN}.invalid/products/p"))
            unknown = cur.fetchone()
        check("T5 unfetchable sitemap -> in_sitemap NULL (unknown)",
              summary2.get("sitemap_fetchable") is False
              and unknown is not None and unknown[0] is None,
              repr((summary2, unknown)))
    finally:
        server.shutdown()

    # ------------------------------------------------------------
    # TASK 6: audit route surface
    # ------------------------------------------------------------
    from api.main import app

    def _collect_paths(routes, acc):
        for route in routes:
            path = getattr(route, "path", None)
            if path:
                acc.add(path)
            # This FastAPI version wraps include_router in a lazy
            # _IncludedRouter whose routes hang off original_router.
            original = getattr(route, "original_router", None)
            if original is not None:
                _collect_paths(getattr(original, "routes", []), acc)
            nested = getattr(route, "routes", None)
            if nested:
                _collect_paths(nested, acc)

    paths = set()
    _collect_paths(app.routes, paths)
    check("T6 audit routes mounted",
          "/audit/live-url" in paths and "/audit/{session_id}" in paths,
          repr(sorted(p for p in paths if "audit" in p)))

    cleanup()
    conn.close()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        sys.exit(1)
    print("ALL LAST-MILE TESTS PASS")


def idx_urls_ok(_urls, children):
    return len(children) == 1 and _urls == []


if __name__ == "__main__":
    main()
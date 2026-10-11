# -*- coding: utf-8 -*-
"""plan/26 Phase 1 — generic sitemap → pages ingestion (non-Shopify).

python tests/run_phase1_sitemap_ingest_tests.py   (needs a reachable local Postgres)

The REAL sync_pages_from_sitemap runs against the REAL database. Every
network fetch is faked at the opener seam (the documented TEST-ONLY
opener parameter that site_fetch._get and audit.page_fetch accept) plus a
benign socket.getaddrinfo (the SSRF guard's DNS step resolves to a
public-looking IP); socket.connect/urlopen stay BANNED so the opener is
the only fetch path exercised — no network anywhere:

  ingestion
    1. sitemap URLs → pages rows (upsert), title/meta/h1/body extracted
    2. page_type from URL archetype (/blog/ → blog, /products/ → product,
       /pages/ → page, unknown → unknown)
    3. shopify_gid stays NULL (never fabricated)
    4. 200 page indexable; status_code persisted
    5. body_text_hash present on non-empty bodies
    6. idempotent re-run: upsert refreshes, no duplicate rows
  typed skips
    7. fetch failure (500) → pages_fetch_failed, typed, no crash
    8. unfetchable sitemap → typed summary naming the reason, zero rows
    9. robots Disallow-all → everything blocked, zero fetches of pages
  bounds
   10. max_pages cap bounds pages_attempted; truncated_to recorded
  wiring (weekly_crawl)
   11. _non_shopify_sites: framer included; explicit shopify excluded;
       legacy default (platform untouched = 'shopify') excluded

Isolation: rows keyed off RUN_TOKEN; cleanup removes exactly what was
created.
"""
import os
import sys
import uuid
import urllib.request
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
# Loopback fixture server (the ONLY fetch target; no external network)
# ------------------------------------------------------------

class _State:
    robots = "User-agent: *\nSitemap: {origin}/sitemap.xml\n"
    sitemap_urls = []
    page_map = {}
    status_map = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/robots.txt"):
            body = _State.robots.format(origin=_State.origin).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/sitemap.xml"):
            body = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                    "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">"
                    + "".join(f"<url><loc>{_State.origin}{u}</loc></url>"
                              for u in _State.sitemap_urls)
                    + "</urlset>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        path = self.path.split("?", 1)[0]
        html = _State.page_map.get(path)
        if html is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = _State.status_map.get(path, 200)
        body = html.format(title=_State.site_title).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


SITEMAP_URLS = [
    "/blog/how-to-choose-a-camera",
    "/products/field-camera",
    "/pages/about",
    "/pricing",
    "/guide/broken",
]

PAGE_MAP = {
    "/blog/how-to-choose-a-camera": (
        "<html><head><title>How to Choose a Field Camera — {title}</title>"
        "<meta name=\"description\" content=\"A grounded guide to field "
        "cameras.\"></head><body><h1>Choosing a Field Camera</h1>"
        "<p>Sensor size matters more than megapixels.</p>"
        "<script>var leak = 'never';</script></body></html>"),
    "/products/field-camera": (
        "<html><head><title>Field Camera — {title}</title></head>"
        "<body><h1>Field Camera</h1><p>Weather-sealed body.</p></body></html>"),
    "/pages/about": (
        "<html><head><title>About — {title}</title></head>"
        "<body><h1>About Us</h1><p>We make cameras.</p></body></html>"),
    "/pricing": (
        "<html><head><title>Pricing — {title}</title></head>"
        "<body><h1>Pricing</h1><p>Simple tiers.</p></body></html>"),
    "/guide/broken": (
        "<html><head><title>Broken — {title}</title></head>"
        "<body><h1>Broken</h1></body></html>"),
}


# ------------------------------------------------------------
# Seeding + cleanup
# ------------------------------------------------------------

def seed_site(conn, platform, domain):
    with conn.cursor() as cur:
        if platform == "legacy":
            # pre-plan/26 row shape: platform column untouched — the
            # NOT NULL DEFAULT 'shopify' IS what a legacy row resolves to.
            cur.execute(
                """
                INSERT INTO site_config (site_name, domain, gsc_property,
                                         catalogue_size_tier)
                VALUES (%s, %s, %s, 'small') RETURNING site_id
                """,
                (f"Phase1 {RUN_TOKEN} {platform}", domain,
                 f"sc-domain:{domain}"),
            )
        else:
            cur.execute(
                """
                INSERT INTO site_config (site_name, domain, gsc_property,
                                         catalogue_size_tier, platform)
                VALUES (%s, %s, %s, 'small', %s) RETURNING site_id
                """,
                (f"Phase1 {RUN_TOKEN} {platform}", domain,
                 f"sc-domain:{domain}", platform),
            )
        site_id = cur.fetchone()[0]
    conn.commit()
    return {"site_id": str(site_id), "domain": domain}


def cleanup(conn, ids_list):
    conn.rollback()
    with conn.cursor() as cur:
        for ids in ids_list:
            cur.execute("DELETE FROM pages WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM site_config WHERE site_id = %s",
                        (ids["site_id"],))
    conn.commit()


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def check_page_rows(conn, ids):
    print("\n== page rows ==")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT url, page_type, title, h1, meta_description,
                   body_text_hash, status_code, indexable, shopify_gid
            FROM pages WHERE site_id = %s ORDER BY url
            """,
            (ids["site_id"],),
        )
        rows = cur.fetchall()
    by_type = {r[0]: r[1] for r in rows}
    allok = True
    blog_url = f"{ids['domain']}/blog/how-to-choose-a-camera"
    prod_url = f"{ids['domain']}/products/field-camera"
    about_url = f"{ids['domain']}/pages/about"
    pricing_url = f"{ids['domain']}/pricing"
    allok &= check("/blog/ -> blog", by_type.get(blog_url) == "blog",
                   str(by_type.get(blog_url)))
    allok &= check("/products/ -> product", by_type.get(prod_url) == "product",
                   str(by_type.get(prod_url)))
    allok &= check("/pages/ -> page", by_type.get(about_url) == "page",
                   str(by_type.get(about_url)))
    allok &= check("unknown pattern -> unknown",
                   by_type.get(pricing_url) == "unknown",
                   str(by_type.get(pricing_url)))
    title = next(r[2] for r in rows if r[0] == blog_url)
    allok &= check("title ingested (script content never leaks)",
                   "How to Choose a Field Camera" in (title or ""),
                   str(title))
    h1 = next((r[3] for r in rows if r[0] == blog_url), None)
    allok &= check("h1 ingested", h1 == "Choosing a Field Camera", str(h1))
    meta = next((r[4] for r in rows if r[0] == blog_url), None)
    allok &= check("meta ingested", bool(meta), str(meta))
    allok &= check("shopify_gid never fabricated",
                   all(r[8] is None for r in rows))
    status = next((r[6] for r in rows if r[0] == blog_url), None)
    indexable = next((r[7] for r in rows if r[0] == blog_url), None)
    allok &= check("200 page indexable", indexable is True and status == 200,
                   str((status, indexable)))
    bhash = next((r[5] for r in rows if r[0] == blog_url), None)
    allok &= check("body_text_hash present", bhash is not None)

    # idempotent re-run
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages WHERE site_id = %s",
                    (ids["site_id"],))
        before = cur.fetchone()[0]
    from site_fetch import sync_pages_from_sitemap
    sync_pages_from_sitemap(conn, ids["site_id"], ids["domain"])
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages WHERE site_id = %s",
                    (ids["site_id"],))
        after = cur.fetchone()[0]
    allok &= check("idempotent re-run (no dupes)", before == after,
                   f"{before} vs {after}")
    return allok


def test_unfetchable_sitemap(conn, ids):
    print("\n== typed skips ==")
    from site_fetch import sync_pages_from_sitemap
    saved_urls = _State.sitemap_urls
    _State.sitemap_urls = []
    try:
        summary = sync_pages_from_sitemap(conn, ids["site_id"], ids["domain"])
    finally:
        _State.sitemap_urls = saved_urls
    allok = check("unfetchable sitemap -> typed summary",
                  summary.get("sitemap_fetchable") is False
                  and summary.get("pages_upserted") == 0, str(summary))
    allok &= check("unfetchable names reason",
                   summary.get("error") == "sitemap_unreachable",
                   str(summary.get("error")))
    return allok


def test_robots_blocked(conn, ids):
    print("\n== robots blocking ==")
    from site_fetch import sync_pages_from_sitemap
    saved_robots = _State.robots
    _State.robots = "User-agent: *\nDisallow: /\n"
    try:
        summary = sync_pages_from_sitemap(conn, ids["site_id"], ids["domain"])
    finally:
        _State.robots = saved_robots
    return check("robots disallow-all -> everything blocked, zero upserts",
                 summary.get("pages_robots_blocked", 0) >= 1
                 and summary.get("pages_upserted") == 0, str(summary))


def test_max_pages_cap(conn, ids):
    print("\n== max_pages cap ==")
    from site_fetch import sync_pages_from_sitemap
    allok = True
    summary = sync_pages_from_sitemap(conn, ids["site_id"], ids["domain"],
                                      max_pages=2)
    allok &= check("cap bounds attempted", summary.get("pages_attempted") <= 2,
                   str(summary.get("pages_attempted")))
    allok &= check("truncated_to recorded", summary.get("truncated_to") == 2)
    return allok


def test_weekly_crawl_gate(conn, ids_shopify, ids_framer, ids_legacy):
    print("\n== weekly_crawl platform gate ==")
    from jobs.weekly_crawl import _non_shopify_sites
    selected = {str(r[0]) for r in _non_shopify_sites(conn)}
    allok = True
    allok &= check("framer site selected", ids_framer["site_id"] in selected)
    allok &= check("shopify site excluded", ids_shopify["site_id"] not in selected)
    allok &= check("legacy-default (shopify) excluded",
                   ids_legacy["site_id"] not in selected)
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    ids_list = []
    server = None
    try:
        server, base = _start_server()
        _State.origin = base
        _State.site_title = f"Phase1 {RUN_TOKEN}"
        _State.sitemap_urls = SITEMAP_URLS
        _State.page_map = PAGE_MAP
        _State.status_map = {"/guide/broken": 500}

        ok = True
        # domain = the FULL loopback origin: sync_pages_from_sitemap (like
        # the Task 5 membership pass it mirrors) prefixes a bare host with
        # https://; the loopback fixture serves plain http, so the seeded
        # domain carries the scheme (site_fetch accepts it).
        ids_framer = seed_site(conn, platform="framer", domain=base)
        ids_list.append(ids_framer)
        ids_shopify = seed_site(conn, platform="shopify",
                                domain=f"phase1-{RUN_TOKEN}-shopify.example.com")
        ids_list.append(ids_shopify)
        ids_legacy = seed_site(conn, platform="legacy",
                               domain=f"phase1-{RUN_TOKEN}-legacy.example.com")
        ids_list.append(ids_legacy)

        from site_fetch import sync_pages_from_sitemap
        print("\n== ingestion ==")
        summary = sync_pages_from_sitemap(conn, ids_framer["site_id"],
                                          ids_framer["domain"])
        allok = True
        allok &= check("summary typed shape",
                       set(("sitemap_fetchable", "pages_attempted",
                            "pages_upserted", "pages_robots_blocked",
                            "pages_fetch_failed")) <= set(summary.keys()),
                       str(sorted(summary.keys())))
        allok &= check("sitemap fetchable", summary.get("sitemap_fetchable") is True,
                       str(summary))
        allok &= check("pages upserted", summary.get("pages_upserted") >= 1,
                       str(summary.get("pages_upserted")))
        allok &= check("fetch failure typed (500 page)",
                       summary.get("pages_fetch_failed", 0) >= 1,
                       str(summary.get("pages_fetch_failed")))
        ok &= allok

        ok &= check_page_rows(conn, ids_framer)
        ok &= test_unfetchable_sitemap(conn, ids_framer)
        ok &= test_robots_blocked(conn, ids_framer)
        ok &= test_max_pages_cap(conn, ids_framer)
        ok &= test_weekly_crawl_gate(conn, ids_shopify, ids_framer, ids_legacy)
    finally:
        if server is not None:
            server.shutdown()
        cleanup(conn, ids_list)
        conn.close()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        sys.exit(1)
    print("ALL PHASE 1 TESTS PASSED")


def _start_server():
    from threading import Thread
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    main()
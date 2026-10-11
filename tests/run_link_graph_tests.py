# -*- coding: utf-8 -*-
"""Link-graph unit tests (plan/31) — no DB, no network.

python tests/run_link_graph_tests.py   (unit half; DB half in
run_link_ingest_tests.py)

Covers:
  1. _AuditHTMLParser <a> capture: href + anchor text + rel, document order,
     nested tags, self-closing <a/>, skipped anchors (no href), anchors
     inside script/style dropped
  2. resolve_internal_links: internal kept, external/protocol-relative/
     mailto/tel/javascript/#fragment skipped, self-loops skipped, dedup,
     relative + protocol-relative resolution, rel preserved
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from audit.page_fetch import parse_page_html  # noqa: E402
from audit.link_ingest import resolve_internal_links  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


HTML_LINKS = """
<html><head><title>T</title></head>
<body>
  <h1>Page</h1>
  <a href="/collections/snowboards">Shop snowboards</a>
  <a href="https://shop.example.com/products/board-1" rel="nofollow">Board 1</a>
  <a href="https://external.example.com/page">External</a>
  <a href="/collections/snowboards">Shop snowboards</a>
  <a href="#section">Fragment only</a>
  <a href="mailto:x@example.com">Mail</a>
  <a name="anchor-without-href">No href</a>
  <a href="/blog/post">Read the <b>guide</b> now</a>
  <a href="//shop.example.com/protocol-rel">Protocol relative</a>
  <a href="https://shop.example.com/products/board-2"/>
  <script>document.write('<a href="/in-script">never</a>');</script>
  <p>Text with an <a href="relative-page">inline relative link</a> mid-copy</p>
</body></html>
"""


def test_parser():
    print("\n== parser <a> capture ==")
    parsed = parse_page_html(HTML_LINKS)
    links = parsed.get("links") or []
    hrefs = [l["href"] for l in links]
    check("1. links captured in document order",
          hrefs == ["/collections/snowboards",
                    "https://shop.example.com/products/board-1",
                    "https://external.example.com/page",
                    "/collections/snowboards",
                    "#section", "mailto:x@example.com",
                    "/blog/post", "//shop.example.com/protocol-rel",
                    "https://shop.example.com/products/board-2",
                    "relative-page"],
          str(hrefs))
    by_href = {}
    for l in links:
        by_href.setdefault(l["href"], l)
    check("1b. anchor text captured",
          by_href["/collections/snowboards"]["text"] == "Shop snowboards",
          str(by_href.get("/collections/snowboards")))
    check("1c. nested-tag anchor text flattened",
          by_href["/blog/post"]["text"] == "Read the guide now",
          str(by_href.get("/blog/post")))
    check("1d. rel captured",
          by_href["https://shop.example.com/products/board-1"]["rel"]
          == "nofollow",
          str(by_href.get("https://shop.example.com/products/board-1")))
    check("1e. rel absent -> None",
          by_href["/blog/post"]["rel"] is None,
          str(by_href.get("/blog/post")))
    check("1f. self-closing <a/> captured with no text",
          {"href": "https://shop.example.com/products/board-2",
           "text": None, "rel": None} in links,
          str(links))
    check("1g. anchor without href skipped at parse level",
          all(l["href"] for l in links), str(links))
    check("1h. anchor inside <script> never captured",
          not any("in-script" in l["href"] for l in links), str(links))
    check("1i. existing fields intact (title/h1/outline)",
          parsed["title"] and parsed["h1"]
          and parsed["heading_outline"], str(parsed.get("title")))
    check("1j. render_status still not_rendered",
          parsed["render_status"] == "not_rendered")


def test_resolve():
    print("\n== resolve_internal_links ==")
    page_url = "https://shop.example.com/blog/post"
    links = [
        {"href": "/collections/snowboards", "text": "Shop snowboards",
         "rel": None},
        {"href": "https://WWW.shop.example.com/products/board-1",
         "text": "Board 1", "rel": "nofollow"},
        {"href": "https://external.example.com/page", "text": "Ext",
         "rel": None},
        {"href": "https://sub.shop.example.com/x", "text": "Sub",
         "rel": None},
        {"href": "#top", "text": None, "rel": None},
        {"href": "mailto:a@b.c", "text": None, "rel": None},
        {"href": "javascript:void(0)", "text": None, "rel": None},
        {"href": "ftp://shop.example.com/f", "text": None, "rel": None},
        {"href": "//shop.example.com/protocol-rel", "text": "PR",
         "rel": None},
        {"href": "relative-page", "text": "Rel", "rel": None},
        {"href": "", "text": None, "rel": None},
        {"href": "https://shop.example.com/blog/post#self",
         "text": "self", "rel": None},
        {"href": "/collections/snowboards?utm_source=x", "text": "utm",
         "rel": None},
    ]
    edges, skipped = resolve_internal_links(page_url, links,
                                            "shop.example.com")
    targets = [e["target_url"] for e in edges]
    check("2a. internal absolute kept + www-stripped + trailing normalized",
          "https://shop.example.com/products/board-1" in targets,
          str(targets))
    check("2b. root-relative resolved against the site origin",
          "https://shop.example.com/collections/snowboards" in targets,
          str(targets))
    check("2c. protocol-relative resolved with page scheme",
          "https://shop.example.com/protocol-rel" in targets,
          str(targets))
    check("2d. bare relative resolved",
          "https://shop.example.com/blog/relative-page" in targets
          or "https://shop.example.com/relative-page" in targets,
          str(targets))
    check("2e. external host skipped",
          not any("external.example.com" in t for t in targets),
          str(targets))
    check("2f. subdomain is NOT internal (registrable-domain match only)",
          not any("sub.shop.example.com" in t for t in targets),
          str(targets))
    check("2g. fragment/mailto/javascript/ftp skipped",
          not any(t.endswith("#top") or "mailto" in t or "javascript" in t
                  or "ftp" in t for t in targets), str(targets))
    check("2h. self-loop (page + fragment) skipped",
          "https://shop.example.com/blog/post" not in targets,
          str(targets))
    check("2i. utm params canonicalized away (faceted collapse)",
          "https://shop.example.com/collections/snowboards" in targets
          and not any("utm_" in t for t in targets), str(targets))
    check("2j. dedup: one edge per normalized target",
          len(targets) == len(set(targets)), str(targets))
    check("2k. skipped count accounts for every rejected href",
          skipped == 8, f"skipped={skipped}")
    anchor = next(e["anchor_text"] for e in edges
                  if e["target_url"].endswith("/products/board-1"))
    check("2l. anchor text + rel ride on the edge",
          anchor == "Board 1"
          and next(e["rel"] for e in edges
                   if e["target_url"].endswith("/products/board-1"))
          == "nofollow", str(edges))


def main():
    test_parser()
    test_resolve()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        sys.exit(1)
    print("ALL LINK GRAPH UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
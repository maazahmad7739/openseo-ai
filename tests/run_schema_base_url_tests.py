# -*- coding: utf-8 -*-
"""Schema grounding (plan/27 + plan/30) — JSON-LD must carry the CLIENT's
origin and the CLIENT's own business copy, never hardcoded agency data.

python tests/run_schema_base_url_tests.py   (no DB, no network needed)

Covers _site_base_url precedence + every URL that _page_specific_jsonld /
_breadcrumb_jsonld emit:
  1. target_url origin wins (audit payload / execution context)
  2. site_config.domain fallback (https-prefixed bare domain)
  3. full URL domain value tolerated (scheme already present)
  4. neither -> typed FixNotSupported (never a hardcoded fallback)
  5. unusable site_config.domain (garbage) -> typed FixNotSupported
  6. default (no site_domain): URL-origin grounding alone
  7. breadcrumb Home item = client origin (regression: agency domain)
  8. homepage Organization/WebSite = client origin
  9. site_name missing -> typed refusal (no placeholder org name)
 10. no 'kineticgrowth'/agency copy anywhere in source or payloads
 11. all archetype routes carry the client origin
 12. uppercase / ported / http hosts preserved as-is
 13. plan/30: /services + /pricing ground on the client's business
     profile; serviceType/areaServed/Offer omitted without one
 14. plan/30: no agency marketing copy ('Digital Marketing...', 'IN')
     in any emitted schema block
"""
import os
import sys
import json

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from fixes.generator import (  # noqa: E402
    FixNotSupported,
    _site_base_url,
    _breadcrumb_jsonld,
    _page_specific_jsonld,
)

FAILURES = []

# the forbidden agency domain + a client domain for assertions
AGENCY = "kineticgrowth.co.in"
CLIENT = "acme-studio.com"
# plan/30: forbidden agency marketing copy — serviceType / areaServed /
# Offer.name defaults that belong to the agency, not the client.
AGENCY_COPY = ("Digital Marketing & AI Growth Services",
               "Digital marketing services", "Performance marketing")


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


def expect_base_url(name, target_url, site_domain, expected):
    try:
        got = _site_base_url(target_url, site_domain)
    except FixNotSupported as exc:
        check(name, False, f"FixNotSupported: {exc}")
        return
    check(name, got == expected, f"got {got!r}, want {expected!r}")


def expect_refusal(name, target_url, site_domain):
    try:
        got = _site_base_url(target_url, site_domain)
    except FixNotSupported:
        check(name, True)
        return
    check(name, False, f"expected FixNotSupported, got {got!r}")


def jsonld_for(target_url, site_domain=None, site_name="Acme Studio",
               business_profile=None):
    return json.loads(_page_specific_jsonld(
        target_url=target_url,
        file_path="index.html",
        site_name=site_name,
        page_title="Some Page",
        h1="Some H1",
        site_domain=site_domain,
        business_profile=business_profile,
    ))


def graph_types(parsed):
    graph = parsed.get("@graph") or []
    return [g.get("@type") for g in graph]


def origin_everywhere(parsed):
    """Every url/item value inside the @graph grounds on the client origin.

    Host comparison is parsed, not string-prefixed, so www./port/scheme
    variants of the client domain pass and foreign origins fail."""
    from urllib.parse import urlparse
    bad = []

    def is_client_origin(url):
        host = (urlparse(url).hostname or "").lower()
        return host == CLIENT or host.endswith("." + CLIENT)

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("url", "item", "id", "@id") \
                        and isinstance(value, str) \
                        and value.startswith("http"):
                    if AGENCY in value.lower() or not is_client_origin(value):
                        bad.append((key, value))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(parsed)
    return not bad, bad


def test_base_url_precedence():
    print("\n== _site_base_url precedence ==")
    expect_base_url(
        "1. target_url origin wins over site_domain",
        "https://www.acme-studio.com/services.html", "other.com",
        "https://www.acme-studio.com")
    expect_base_url(
        "2. site_domain fallback https-prefixed",
        None, "acme-studio.com", "https://acme-studio.com")
    expect_base_url(
        "3. site_domain with scheme kept as-is",
        None, "https://acme-studio.com", "https://acme-studio.com")
    expect_refusal("4. neither -> typed FixNotSupported", None, None)
    expect_refusal("4b. empty strings -> typed FixNotSupported", "", "  ")
    expect_refusal(
        "5. garbage site_domain (no netloc) -> typed FixNotSupported",
        None, "not a domain")
    expect_base_url(
        "6. URL-only grounding (default signature)",
        "https://acme-studio.com/blog/post-1", None,
        "https://acme-studio.com")
    expect_base_url(
        "12a. uppercase host normalized, port preserved",
        "https://ACME.Studio.com:8443/x", None, "https://acme.studio.com:8443")
    expect_base_url(
        "12b. http scheme preserved",
        "http://acme-studio.com/", None, "http://acme-studio.com")


def test_breadcrumb():
    print("\n== _breadcrumb_jsonld ==")
    bc = _breadcrumb_jsonld("https://www.acme-studio.com/services.html",
                            "Acme Studio", site_domain="acme-studio.com")
    items = bc["itemListElement"]
    home = items[0]
    check("7. Home item = client origin (regression: agency domain)",
          home["item"] == "https://www.acme-studio.com", str(home))
    ok, bad = origin_everywhere(bc)
    check("7b. no agency origin anywhere in breadcrumb", ok, str(bad))
    # deep path: mid-crumb keeps the full page URL by original design
    bc2 = _breadcrumb_jsonld("https://acme-studio.com/blog/post-1",
                             "Acme Studio")
    mid = bc2["itemListElement"][1]
    check("7c. mid-crumb unchanged (page URL)",
          mid["item"] == "https://acme-studio.com/blog/post-1", str(mid))


def test_page_jsonld_routes():
    print("\n== _page_specific_jsonld routes ==")
    # homepage route
    home = jsonld_for("https://www.acme-studio.com/",
                      site_domain="acme-studio.com")
    check("8. homepage Organization.url = client origin",
          graph_types(home) == ["Organization", "WebSite"]
          and home["@graph"][0]["url"] == "https://www.acme-studio.com"
          and home["@graph"][1]["url"] == "https://www.acme-studio.com",
          json.dumps(home)[:200])
    check("8b. Organization.name = client site_name (no agency fallback)",
          home["@graph"][0]["name"] == "Acme Studio",
          str(home["@graph"][0].get("name")))
    ok, bad = origin_everywhere(home)
    check("8c. no agency origin in homepage graph", ok, str(bad))

    # inner page route
    svc = jsonld_for("https://www.acme-studio.com/services.html",
                     site_domain="acme-studio.com")
    ok, bad = origin_everywhere(svc)
    check("11a. /services route grounds on client origin", ok, str(bad))
    check("11a2. /services breadcrumb present",
          "BreadcrumbList" in graph_types(svc), str(graph_types(svc)))

    for path in ("/contact.html", "/pricing.html", "/results.html",
                 "/why-us.html", "/blog/post-1"):
        page = jsonld_for(f"https://www.acme-studio.com{path}",
                          site_domain="acme-studio.com")
        ok, bad = origin_everywhere(page)
        check(f"11. route {path} grounds on client origin", ok, str(bad))


def test_refusals():
    print("\n== typed refusals ==")
    try:
        _page_specific_jsonld(
            target_url=None, file_path="index.html",
            site_name="Acme Studio", page_title=None, h1=None,
            site_domain=None)
    except FixNotSupported:
        check("10a. no URL + no domain -> FixNotSupported", True)
    except Exception as exc:  # noqa: BLE001
        check("10a. no URL + no domain -> FixNotSupported", False,
              f"wrong error: {exc!r}")
    else:
        check("10a. no URL + no domain -> FixNotSupported", False,
              "no error raised")

    try:
        _page_specific_jsonld(
            target_url="https://www.acme-studio.com/",
            file_path="index.html", site_name="   ", page_title=None,
            h1=None, site_domain="acme-studio.com")
    except FixNotSupported:
        check("9. blank site_name -> typed refusal (no placeholder org)",
              True)
    except Exception as exc:  # noqa: BLE001
        check("9. blank site_name -> typed refusal (no placeholder org)",
              False, f"wrong error: {exc!r}")
    else:
        check("9. blank site_name -> typed refusal (no placeholder org)",
              False, "no error raised")


def find_nodes(parsed, type_name):
    """All @graph nodes with the given @type."""
    return [g for g in (parsed.get("@graph") or [])
            if g.get("@type") == type_name]


def has_agency_copy(parsed):
    """True when any agency marketing string appears anywhere in the payload."""
    text = json.dumps(parsed)
    return [c for c in AGENCY_COPY if c in text]


def test_services_route():
    print("\n== plan/30: /services grounding ==")
    profile = {
        "business_service_types": ["Wedding Photography", "Portrait Studio"],
        "business_areas_served": ["US", "CA"],
    }
    svc = jsonld_for("https://www.acme-studio.com/services.html",
                     site_domain="acme-studio.com",
                     business_profile=profile)
    blocks = find_nodes(svc, "Service")
    check("13a. /services emits a Service block", len(blocks) == 1,
          str(graph_types(svc)))
    svc_block = blocks[0]
    check("13b. serviceType = client's own service types",
          svc_block.get("serviceType") == profile["business_service_types"],
          str(svc_block.get("serviceType")))
    check("13c. areaServed = client's own regions",
          svc_block.get("areaServed") == profile["business_areas_served"],
          str(svc_block.get("areaServed")))
    check("13d. provider grounds on client org + origin",
          svc_block["provider"]["name"] == "Acme Studio"
          and svc_block["provider"]["url"] == "https://www.acme-studio.com",
          str(svc_block.get("provider")))
    ok, bad = origin_everywhere(svc)
    check("13e. all URLs ground on client origin", ok, str(bad))
    check("14a. no agency copy in /services payload",
          not has_agency_copy(svc), json.dumps(svc)[:200])

    # no profile -> fields omitted, never agency defaults
    bare = jsonld_for("https://www.acme-studio.com/services.html",
                      site_domain="acme-studio.com")
    bare_block = find_nodes(bare, "Service")[0]
    check("13f. no profile -> serviceType omitted",
          "serviceType" not in bare_block, str(bare_block))
    check("13g. no profile -> areaServed omitted",
          "areaServed" not in bare_block, str(bare_block))
    check("13h. Service block itself still emitted (grounded fields only)",
          bare_block.get("name") == "Some Page"
          and bare_block.get("url") == "https://www.acme-studio.com/services.html",
          str(bare_block))

    # blank-string entries in the arrays are dropped, not emitted
    blank = jsonld_for(
        "https://www.acme-studio.com/services.html",
        site_domain="acme-studio.com",
        business_profile={"business_service_types": ["  ", ""],
                          "business_areas_served": [None, ""]})
    blank_block = find_nodes(blank, "Service")[0]
    check("13i. blank/None entries dropped -> fields omitted",
          "serviceType" not in blank_block
          and "areaServed" not in blank_block, str(blank_block))


def test_pricing_route():
    print("\n== plan/30: /pricing grounding ==")
    # with profile: Offer grounded on the client's offering
    priced = jsonld_for("https://www.acme-studio.com/pricing.html",
                        site_domain="acme-studio.com",
                        business_profile={
                            "business_offer_name": "Studio Packages",
                            "business_service_types": ["Wedding Photography"]})
    offers = find_nodes(priced, "Offer")
    check("13j. /pricing emits Offer when profile names an offering",
          len(offers) == 1
          and offers[0]["itemOffered"]["name"] == "Studio Packages",
          str(offers))
    check("13k. Offer name is business_offer_name, not the page title",
          offers and offers[0]["itemOffered"]["name"] != "Some Page",
          str(offers))

    # fallback to first service type when offer_name is absent
    fallback = jsonld_for("https://www.acme-studio.com/pricing.html",
                          site_domain="acme-studio.com",
                          business_profile={
                              "business_service_types": ["Wedding Photography"]})
    offers2 = find_nodes(fallback, "Offer")
    check("13l. Offer falls back to first service type",
          len(offers2) == 1
          and offers2[0]["itemOffered"]["name"] == "Wedding Photography",
          str(offers2))

    # no profile -> Offer omitted entirely, WebPage + breadcrumb still emit
    bare = jsonld_for("https://www.acme-studio.com/pricing.html",
                      site_domain="acme-studio.com")
    check("13m. no profile -> NO Offer block (never agency default)",
          find_nodes(bare, "Offer") == [], str(graph_types(bare)))
    check("13n. WebPage + BreadcrumbList still emitted",
          graph_types(bare) == ["WebPage", "BreadcrumbList"],
          str(graph_types(bare)))
    ok, bad = origin_everywhere(bare)
    check("13o. Offer-less /pricing still grounds on client origin",
          ok, str(bad))
    check("14b. no agency copy in /pricing", not has_agency_copy(priced),
          json.dumps(priced)[:200])


def test_no_agency_string():
    print("\n== no agency strings in source of the schema section ==")
    src_path = os.path.join(HERE, "..", "src", "fixes", "generator.py")
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    check("10. 'kineticgrowth' absent from generator.py",
          AGENCY not in src.lower())
    check("10b. 'Kinetic Growth' absent from generator.py",
          "kinetic growth" not in src.lower())
    check("10c. agency serviceType copy absent from generator.py",
          "Digital Marketing & AI Growth Services" not in src)
    check("10d. agency Offer copy absent from generator.py",
          "Digital marketing services" not in src)
    check("10e. hardcoded 'areaServed' region absent from generator.py",
          '"areaServed": "IN"' not in src)
    check("10f. hardcoded agency description absent from generator.py",
          "Performance marketing and AI growth studio" not in src)


def main():
    test_base_url_precedence()
    test_breadcrumb()
    test_page_jsonld_routes()
    test_services_route()
    test_pricing_route()
    test_refusals()
    test_no_agency_string()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        sys.exit(1)
    print("ALL SCHEMA BASE-URL TESTS PASSED")


if __name__ == "__main__":
    main()
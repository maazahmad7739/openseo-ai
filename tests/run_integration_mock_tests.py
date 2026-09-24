import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from connectors.mode import resolve_mode, describe, ModeError  # noqa: E402
from connectors.openseo import get_openseo_adapter  # noqa: E402
from connectors.gsc import get_gsc_adapter  # noqa: E402
from connectors.shopify import get_shopify_adapter  # noqa: E402
from connectors.ga4 import get_ga4_adapter  # noqa: E402

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def check(name, value):
    status = "PASS" if value else "FAIL"
    print(f"[{status}] {name}")
    return value


def run_capability(adapter, capability, params, expected_keys):
    sup = adapter.supports(capability)
    result = adapter.fetch(capability, params)
    ok = result.get("ok") is True
    keys_present = bool(result.get("data")) and all(
        set(row.keys()) == set(expected_keys) for row in result["data"]
    )
    return {
        "supports()": sup,
        "fetch ok": ok,
        "data is list": isinstance(result.get("data"), list),
        "row keys == doc keys": keys_present,
    }, result


def print_capability(name, adapter, capability, params, expected_keys):
    checks, result = run_capability(adapter, capability, params, expected_keys)
    status = "PASS" if all(checks.values()) else "FAIL"
    print(f"[{status}] {name}")
    for check_name, value in checks.items():
        print(f"    {check_name}: {'OK' if value else 'MISS'}")
    for row in result.get("data", [])[:3]:
        print(f"        {row}")
    return all(checks.values())


def main():
    allok = True

    saved_mode = os.environ.pop("INTEGRATION_MODE", None)
    saved_envs = {}
    for name in ("OPENSEO_MOCK_MODE", "GSC_MOCK_MODE", "SHOPIFY_MOCK_MODE", "GA4_MOCK_MODE"):
        saved_envs[name] = os.environ.pop(name, None)

    try:
        print("== central switch (mode.py) ==")
        allok &= check("default mode is mock (no env, no config)", resolve_mode() == "mock")
        allok &= check("describe(mock) mentions zero external calls",
                       "zero external network calls" in describe("mock"))
        allok &= check("describe(live) requires credentials",
                       "requires credentials" in describe("live"))
        try:
            resolve_mode("staging")
            bad = False
        except ModeError:
            bad = True
        allok &= check("invalid mode value raises ModeError", bad)

        central_on = dict(os.environ)
        os.environ["INTEGRATION_MODE"] = "mock"
        allok &= check("central switch env drives all facades",
                       all(
                           resolve_mode(env_names=(name,)) == "mock"
                           for name in ("OPENSEO_MOCK_MODE", "GSC_MOCK_MODE", "SHOPIFY_MOCK_MODE", "GA4_MOCK_MODE")
                       ) or resolve_mode() == "mock")
        os.environ["INTEGRATION_MODE"] = "live"
        allok &= check("central switch live recognized", resolve_mode() == "live")
        os.environ["INTEGRATION_MODE"] = saved_mode if saved_mode is not None else ""
        if saved_mode is None:
            os.environ.pop("INTEGRATION_MODE", None)

        print("\n== per-connector mock fetches (via central switch) ==")
        openseo = get_openseo_adapter(config={
            "openseo.mode": "mock",
            "openseo.mock_fixtures_dir": FIXTURES_DIR,
        })
        allok &= print_capability(
            "openseo.keyword_volume", openseo, "keyword_volume",
            {"keywords": ["wireless noise cancelling headphones"], "date": "2026-09-01"},
            {"keyword", "search_volume", "date", "competition", "cpc"},
        )
        allok &= print_capability(
            "openseo.serp", openseo, "serp",
            {"query": "wireless noise cancelling headphones", "limit": 10},
            {"query", "position", "url", "title", "snippet"},  # §4.2.1 additive "query"
        )

        gsc = get_gsc_adapter(config={
            "gsc.mode": "mock",
            "gsc.mock_fixtures_dir": FIXTURES_DIR,
            "gsc.site_url": "sc-domain:example.com",
        })
        allok &= print_capability(
            "gsc.search_analytics", gsc, "search_analytics",
            {"start_date": "2026-09-03", "end_date": "2026-09-10"},
            {"date", "query", "page_url", "country", "device", "clicks", "impressions", "ctr", "position"},
        )

        shopify = get_shopify_adapter(config={"shopify.mode": "mock", "shopify.mock_fixtures_dir": FIXTURES_DIR})
        allok &= print_capability(
            "shopify.products", shopify, "products", {},
            {"product_id", "title", "handle", "product_type", "status", "tags",
             "meta_description", "body_html",
             "total_inventory", "in_stock", "variants"},
        )
        allok &= print_capability(
            "shopify.collections", shopify, "collections", {},
            {"collection_id", "title", "handle", "collection_type", "published", "url",
             "meta_description", "body_html", "rules"},
        )

        ga4 = get_ga4_adapter(config={"ga4.mode": "mock", "ga4.mock_fixtures_dir": FIXTURES_DIR})
        allok &= print_capability(
            "ga4.page_performance", ga4, "page_performance",
            {"start_date": "2026-09-09", "end_date": "2026-09-10"},
            {"date", "page_path", "organic_sessions", "engaged_sessions", "add_to_carts",
             "checkouts", "orders", "revenue", "conversion_rate"},
        )

        print("\n== live-mode refusal without credentials (per connector) ==")
        for label, fn, mode_key in (
            ("openseo", get_openseo_adapter, "openseo.mode"),
            ("gsc", get_gsc_adapter, "gsc.mode"),
            ("shopify", get_shopify_adapter, "shopify.mode"),
            ("ga4", get_ga4_adapter, "ga4.mode"),
        ):
            refused = False
            try:
                fn(config={mode_key: "live"})
            except Exception:
                refused = True
            allok &= check(f"{label} live mode without credential refuses loudly", refused)

        print("\n== typed unsupported + missing fixture (never a crash) ==")
        unsupported = shopify.fetch("rank_history", {})
        allok &= check("shopify unsupported capability typed",
                       unsupported == {"ok": False, "capability": "rank_history", "error": "unsupported_capability"})
        missing = ga4.fetch("page_performance", {}, )
        ga4_bad = get_ga4_adapter(config={"ga4.mode": "mock", "ga4.mock_fixtures_dir": os.path.join(FIXTURES_DIR, "missing")})
        missing = ga4_bad.fetch("page_performance", {})
        allok &= check("ga4 missing fixture -> provider_error, no crash",
                       missing.get("ok") is False and missing.get("error") == "provider_error")

        print("\n== realistic-universe consistency ==")
        products = shopify.fetch("products", {}).get("data", [])
        collections = shopify.fetch("collections", {}).get("data", [])
        gsc_rows = gsc.fetch("search_analytics", {"start_date": "2026-09-03", "end_date": "2026-09-10"}).get("data", [])
        ga4_rows = ga4.fetch("page_performance", {}).get("data", [])
        gsc_pages = {r["page_url"] for r in gsc_rows}
        coll_urls = {c["url"] for c in collections if c["url"]}
        product_handles = {p["handle"] for p in products}
        allok &= check("GSC pages intersect Shopify collection URLs",
                       bool(gsc_pages & coll_urls))
        coll_paths = {"/collections/" + c["handle"] for c in collections}
        allok &= check("GA4 paths intersect Shopify collection paths",
                       bool({p["page_path"] for p in ga4_rows} & coll_paths))
        allok &= check("at least one product in_stock (matches in_stock rule)",
                       any(p["in_stock"] for p in products))
        allok &= check("one out-of-stock product present (catalogue coverage edge)",
                       any(not p["in_stock"] for p in products))
        allok &= check("unpublished collection present (published edge case)",
                       any(not c["published"] for c in collections))

        print("\n== fixture json sanity ==")
        for fname in ("gsc_search_analytics.json", "gsc_sites.json", "shopify_products.json",
                      "shopify_collections.json", "ga4_page_performance.json",
                      "dataforseo_keyword_volume.json", "dataforseo_serp.json",
                      "dataforseo_competitors.json", "dataforseo_backlinks.json"):
            with open(os.path.join(FIXTURES_DIR, fname), "r", encoding="utf-8") as fh:
                json.load(fh)
        allok &= check("all fixtures parse as JSON", True)
    finally:
        for name, value in saved_envs.items():
            if value is not None:
                os.environ[name] = value
        if saved_mode is not None:
            os.environ["INTEGRATION_MODE"] = saved_mode

    print("\nINTEGRATION MOCK TEST RUN", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
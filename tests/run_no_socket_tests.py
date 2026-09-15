import os
import sys
import socket
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from connectors.openseo import get_openseo_adapter  # noqa: E402
from connectors.gsc import get_gsc_adapter  # noqa: E402
from connectors.shopify import get_shopify_adapter  # noqa: E402
from connectors.ga4 import get_ga4_adapter  # noqa: E402

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class NetworkBanned(Exception):
    pass


def ban_socket_and_urllib():
    """Monkeypatch every outbound-network escape hatch; any use raises."""
    def banned(*args, **kwargs):
        raise NetworkBanned("network access attempted in mock mode")

    socket.socket.connect = banned
    socket.socket.connect_ex = banned
    socket.create_connection = banned
    socket.getaddrinfo = banned
    import urllib.request
    urllib.request.urlopen = banned
    return banned


def main():
    allok = True
    banned = ban_socket_and_urllib()

    saved = {}
    for name in ("INTEGRATION_MODE", "OPENSEO_MOCK_MODE", "GSC_MOCK_MODE",
                 "SHOPIFY_MOCK_MODE", "GA4_MOCK_MODE"):
        saved[name] = os.environ.pop(name, None)

    try:
        openseo = get_openseo_adapter(config={
            "openseo.mode": "mock", "openseo.mock_fixtures_dir": FIXTURES_DIR,
        })
        gsc = get_gsc_adapter(config={
            "gsc.mode": "mock", "gsc.mock_fixtures_dir": FIXTURES_DIR,
            "gsc.site_url": "sc-domain:example.com",
        })
        shopify = get_shopify_adapter(config={
            "shopify.mode": "mock", "shopify.mock_fixtures_dir": FIXTURES_DIR,
        })
        ga4 = get_ga4_adapter(config={
            "ga4.mode": "mock", "ga4.mock_fixtures_dir": FIXTURES_DIR,
        })

        calls = [
            (openseo, "keyword_volume", {"keywords": ["wireless noise cancelling headphones"], "date": "2026-09-01"}),
            (openseo, "serp", {"query": "wireless noise cancelling headphones"}),
            (openseo, "competitors", {"domain": "example.com"}),
            (openseo, "backlinks", {"urls": ["https://example.com/collections/wireless-noise-cancelling-headphones"]}),
            (gsc, "search_analytics", {"start_date": "2026-09-03", "end_date": "2026-09-10"}),
            (gsc, "sites", {}),
            (shopify, "products", {}),
            (shopify, "collections", {}),
            (ga4, "page_performance", {"start_date": "2026-09-09", "end_date": "2026-09-10"}),
        ]
        all_ok = True
        for adapter, capability, params in calls:
            try:
                result = adapter.fetch(capability, params)
                ok = result.get("ok") is True and bool(result.get("data"))
            except NetworkBanned:
                ok = False
            if not ok:
                all_ok = False
                print(f"[FAIL] {adapter.__class__.__name__}.{capability} attempted network or failed")
        print(f"[{'PASS' if all_ok else 'FAIL'}] all mock fetches return fixtures with socket+urllib banned")
        allok &= all_ok

        variant_check = shopify.fetch("products", {}).get("data", [])
        ids = [v.get("variant_id") for p in variant_check for v in p["variants"]]
        ok = all(i is not None for i in ids)
        print(f"[{'PASS' if ok else 'FAIL'}] shopify variant ids present (fixture uses wire key 'id')")
        allok &= ok

        ga4_rows = ga4.fetch("page_performance", {}).get("data", [])
        ok = all(r["date"] and len(r["date"]) == 10 and r["date"][4] == "-" for r in ga4_rows)
        print(f"[{'PASS' if ok else 'FAIL'}] ga4 dates normalized to ISO (YYYY-MM-DD)")
        allok &= ok

        constructed = 0
        for fn in (get_openseo_adapter, get_gsc_adapter, get_shopify_adapter, get_ga4_adapter):
            try:
                fn(config={})
                constructed += 1
            except Exception:
                pass
        ok = constructed == 4
        print(f"[{'PASS' if ok else 'FAIL'}] default mode = mock: all 4 connectors construct with zero credentials ({constructed}/4)")
        allok &= ok
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value

    print("\nNO-SOCKET CONTRACT TEST RUN", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
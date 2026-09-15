import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from connectors.openseo import get_openseo_adapter  # noqa: E402

FIXTURES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures"
)


def run_capability(adapter, capability, params, expected_keys, fail_on):
    sup = adapter.supports(capability)
    result = adapter.fetch(capability, params)
    ok = result.get("ok") is True
    keys_present = bool(result.get("data")) and all(
        set(row.keys()) == set(expected_keys) for row in result["data"]
    )
    failures = [k for k in fail_on if k not in result]
    checks = {
        "supports()": sup,
        "fetch ok": ok,
        "data is list": isinstance(result.get("data"), list),
        "row keys == doc keys": keys_present,
        "no failure keys injected": not failures,
    }
    return checks, result


def print_capability(name, expected_keys, empty_params, params):
    adapter = get_openseo_adapter(
        config={
            "openseo.mock_mode": True,
            "openseo.mock_fixtures_dir": FIXTURES_DIR,
            "openseo.capabilities": ["keyword_volume", "serp", "competitors", "backlinks"],
        }
    )
    checks, result = run_capability(adapter, name, params, expected_keys, [])
    status = "PASS" if all(checks.values()) else "FAIL"
    print(f"[{status}] {name}")
    for check, value in checks.items():
        print(f"    {check}: {'OK' if value else 'MISS'}")
    print(f"    raw rows: {json.dumps(result, indent=2)}")
    return all(checks.values())


def main():
    allok = True

    allok &= print_capability(
        "keyword_volume",
        {"keyword", "search_volume", "date", "competition", "cpc"},
        {},
        {"keywords": ["wireless noise cancelling headphones", "best budget noise cancelling headphones"], "date": "2026-09-01"},
    )

    allok &= print_capability(
        "serp",
        {"query", "position", "url", "title", "snippet"},  # §4.2.1 additive "query"
        {},
        {"query": "wireless noise cancelling headphones", "limit": 10, "geo": "us"},
    )

    allok &= print_capability(
        "competitors",
        {"domain", "overlap_score", "ranking_keywords_count"},
        {"domain": "example.com"},
        {"domain": "example.com"},
    )

    allok &= print_capability(
        "backlinks",
        {"target_url", "source_url", "anchor", "first_seen"},
        {},
        {"urls": ["https://example.com/collections/wireless-noise-cancelling-headphones"], "limit": 100},
    )

    adapter = get_openseo_adapter(
        config={
            "openseo.mock_mode": True,
            "openseo.mock_fixtures_dir": FIXTURES_DIR,
            "openseo.capabilities": ["keyword_volume", "serp"],
        }
    )
    unsupported = adapter.fetch("crawl_audit", {"site": "example.com"})
    expected = {"ok": False, "capability": "crawl_audit", "error": "unsupported_capability"}
    match = unsupported == expected
    print(f"[{'PASS' if match else 'FAIL'}] typed unsupported result: {unsupported}")
    allok &= match

    missing = adapter.fetch("backlinks", {"urls": ["https://example.com"]})
    print(f"[{'PASS' if not missing.get('ok') else 'FAIL'}] unsupported non-fixture capability "
          f"skipped cleanly: error={missing.get('error')}")

    bad_adapter = get_openseo_adapter(
        config={
            "openseo.mock_mode": True,
            "openseo.mock_fixtures_dir": os.path.join(FIXTURES_DIR, "missing"),
        }
    )
    nf = bad_adapter.fetch("keyword_volume", {"keywords": ["x"], "date": "2026-09-01"})
    nf_expected = nf.get("ok") is False and nf.get("error") == "provider_error" and "not found" in (nf.get("detail") or "")
    print(f"[{'PASS' if nf_expected else 'FAIL'}] missing fixture -> provider_error, no crash: {nf}")
    allok &= nf_expected

    print("\nMOCK-MODE TEST RUN", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
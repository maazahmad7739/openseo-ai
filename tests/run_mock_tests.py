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

    # Credential isolation. A mock run must not be able to reach a provider
    # even when .env holds a working key: that combination is what made the
    # drafting suites dial ollama.com and hang for the full client timeout.
    import env as env_mod

    for key, want_secret in (
        ("OLLAMA_API_KEY", True), ("GEMINI_API_KEY", True),
        ("SHOPIFY_ACCESS_TOKEN", True), ("DATAFORSEO_PASSWORD", True),
        ("GSC_SERVICE_ACCOUNT_KEY_JSON", True), ("GITHUB_TOKEN", True),
        ("OLLAMA_MODEL", False), ("SHOPIFY_DOMAIN", False),
        ("NETLIFY_LIVE_WRITES", False),
        # The switch is credential-shaped by marker; it is exempted one
        # layer up, in secret_env_names, not here.
        ("OPENSEO_LOAD_SECRETS", True),
    ):
        got = env_mod._is_secret_key(key)
        mark = "PASS" if got == want_secret else "FAIL"
        print(f"[{mark}] classification {key} -> secret={got} (expected {want_secret})")
        allok &= got == want_secret

    probe = {"OLLAMA_API_KEY": "x", "DATAFORSEO_PASSWORD": "x", "OPENSEO_LOAD_SECRETS": "0"}
    removed = env_mod.scrub_secret_env(probe)
    scrub_ok = (sorted(removed) == ["DATAFORSEO_PASSWORD", "OLLAMA_API_KEY"]
                and "OPENSEO_LOAD_SECRETS" in probe
                and "OLLAMA_API_KEY" not in probe)
    print(f"[{'PASS' if scrub_ok else 'FAIL'}] scrub removes creds but cannot disarm the "
          f"switch: removed={sorted(removed)} remaining={sorted(probe)}")
    allok &= scrub_ok

    # secret_env_names must never report the switch, or a caller that scrubs
    # before loading would delete the control and revert to loading secrets.
    prev = os.environ.get(env_mod.SECRETS_OPT_IN_ENV)
    os.environ[env_mod.SECRETS_OPT_IN_ENV] = "0"
    exempt = env_mod.SECRETS_OPT_IN_ENV not in env_mod.secret_env_names()
    if prev is None:
        os.environ.pop(env_mod.SECRETS_OPT_IN_ENV, None)
    else:
        os.environ[env_mod.SECRETS_OPT_IN_ENV] = prev
    print(f"[{'PASS' if exempt else 'FAIL'}] switch is exempt from its own scrub: {exempt}")
    allok &= exempt

    print("\nMOCK-MODE TEST RUN", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
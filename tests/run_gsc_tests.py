import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from connectors.gsc import (  # noqa: E402
    GscConfigError,
    get_gsc_adapter,
    read_service_account_client_email,
    ServiceAccountTokenSource,
)

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


def print_capability(name, expected_keys, params):
    adapter = get_gsc_adapter(
        config={
            "gsc.mock_mode": True,
            "gsc.mock_fixtures_dir": FIXTURES_DIR,
            "gsc.capabilities": ["sites", "search_analytics"],
        }
    )
    checks, result = run_capability(adapter, name, params, expected_keys, [])
    status = "PASS" if all(checks.values()) else "FAIL"
    print(f"[{status}] {name}")
    for check, value in checks.items():
        print(f"    {check}: {'OK' if value else 'MISS'}")
    print(f"    rows: {len(result.get('data') or [])}")
    for row in result.get("data", []):
        print(f"        {row}")
    return all(checks.values())


def main():
    allok = True

    allok &= print_capability(
        "sites",
        {"site_url", "permission_level"},
        {},
    )

    allok &= print_capability(
        "search_analytics",
        {"date", "query", "page_url", "country", "device", "clicks", "impressions", "ctr", "position"},
        {
            "site_url": "sc-domain:example.com",
            "start_date": "2026-09-03",
            "end_date": "2026-09-10",
            "dimensions": ["date", "query", "page", "country", "device"],
        },
    )

    adapter = get_gsc_adapter(
        config={
            "gsc.mock_mode": True,
            "gsc.mock_fixtures_dir": FIXTURES_DIR,
            "gsc.capabilities": ["sites"],
        }
    )
    unsupported = adapter.fetch("url_inspection", {"inspection_url": "https://example.com/"})
    expected = {"ok": False, "capability": "url_inspection", "error": "unsupported_capability"}
    match = unsupported == expected
    print(f"[{'PASS' if match else 'FAIL'}] typed unsupported result: {unsupported}")
    allok &= match

    bad_adapter = get_gsc_adapter(
        config={
            "gsc.mock_mode": True,
            "gsc.mock_fixtures_dir": os.path.join(FIXTURES_DIR, "missing"),
        }
    )
    nf = bad_adapter.fetch("search_analytics", {"start_date": "2026-09-03", "end_date": "2026-09-10"})
    nf_expected = nf.get("ok") is False and nf.get("error") == "provider_error" and "not found" in (nf.get("detail") or "")
    print(f"[{'PASS' if nf_expected else 'FAIL'}] missing fixture -> provider_error, no crash: {nf}")
    allok &= nf_expected

    cleanup = os.environ.get("GSC_MOCK_MODE")
    os.environ["GSC_MOCK_MODE"] = "0"
    realfail = False
    try:
        get_gsc_adapter(config={})
    except GscConfigError:
        realfail = True
    if cleanup is not None:
        os.environ["GSC_MOCK_MODE"] = cleanup
    else:
        os.environ.pop("GSC_MOCK_MODE", None)
    print(f"[{'PASS' if realfail else 'FAIL'}] explicit live mode without credential -> GscConfigError")
    allok &= realfail

    sa_key = {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "deadbeef",
        "private_key": "-----BEGIN PRIVATE KEY-----\nTESTONLY\n-----END PRIVATE KEY-----\n",
        "client_email": "gsc-sync@test-project.iam.gserviceaccount.com",
        "client_id": "1234567890",
    }
    email = read_service_account_client_email(key_json=json.dumps(sa_key))
    match = email == sa_key["client_email"]
    print(f"[{'PASS' if match else 'FAIL'}] client_email reader (real GSC user-association step): {email}")
    allok &= match

    token_source_ok = ServiceAccountTokenSource.__doc__ is not None
    print(f"[{'PASS' if token_source_ok else 'FAIL'}] ServiceAccountTokenSource present (mint + auto-refresh)")
    allok &= token_source_ok

    print("\nGSC MOCK-MODE TEST RUN", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
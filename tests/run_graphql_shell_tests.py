"""Stage 2 Phase 0 â€” GraphQL caller shell tests (no network; run directly).

python tests/run_graphql_shell_tests.py

Covers plan/21 Â§4/Â§5 behaviors that are testable offline:
  * MUTATION_REGISTRY completeness (every planned write has a verified scope)
  * _ThrottleTracker observe/wait math
  * _classify_graphql_response outcomes (ok / user_errors / access_denied /
    throttled / top_level_error / non-dict)
  * ShopifyGraphQLClient.run() throttle-retry loop against a fake _post
  * probe_scopes() no-credential typed result
  * required_scopes_for_sub_types gate mapping
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

from connectors.shopify import (  # noqa: E402
    ShopifyGraphQLClient, MUTATION_REGISTRY, _classify_graphql_response,
    _ThrottleTracker, required_scopes_for_sub_types, REQUIRED_WRITE_SCOPES,
    granted_covers_required, ANY_OF_SCOPE_GROUPS,
)

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_registry():
    print("mutation registry:")
    expected = {
        "productUpdate", "collectionUpdate", "publishablePublish",
        "publishableUnpublish", "metafieldsSet", "urlRedirectCreate",
        "urlRedirectUpdate", "urlRedirectDelete", "articleUpdate",
        "productCreate", "pageCreate", "collectionCreate",
    }
    check("all planned mutations registered", set(MUTATION_REGISTRY) == expected)
    check("every entry has (scope, doc_url) with 2026-01 URL",
          all(s and "2026-01" in u for s, u in MUTATION_REGISTRY.values()))


def test_throttle_tracker():
    print("throttle tracker:")
    t = _ThrottleTracker()
    t.observe({"cost": {"throttleStatus": {
        "maximumAvailable": 1000, "currentlyAvailable": 100, "restoreRate": 50.0}}})
    check("observe reads bucket", t.maximum == 1000 and t.available == 100)
    check("no wait when budget clear", t.wait_seconds_for(10) == 0.0)
    t.observe({"cost": {"throttleStatus": {"currentlyAvailable": 5, "restoreRate": 50.0}}})
    wait = t.wait_seconds_for(10)
    check("wait math = deficit/restore", abs(wait - 0.1) < 0.01, f"got {wait}")
    t.observe({"cost": {"throttleStatus": {"currentlyAvailable": 0, "restoreRate": 1.0}}})
    check("capped at sleep cap", t.wait_seconds_for(1000) == 20.0)
    t.observe({"cost": {"actualQueryCost": 12}})
    check("actual cost recorded", t.last_actual_cost == 12)


def test_classifier():
    print("response classifier:")
    r = _classify_graphql_response(
        {"data": {"productUpdate": {"product": {"id": "gid://shopify/Product/1"}}}},
        mutation_name="productUpdate")
    check("ok outcome", r["ok"] and r["outcome"] == "ok")
    r = _classify_graphql_response(
        {"data": {"productUpdate": {"userErrors": [{"field": ["title"], "message": "too long"}]}}},
        mutation_name="productUpdate")
    check("user_errors outcome", not r["ok"] and r["outcome"] == "user_errors"
          and r["userErrors"][0]["field"] == ["title"])
    r = _classify_graphql_response(
        {"data": None, "errors": [{"message": "denied",
                                   "extensions": {"code": "ACCESS_DENIED"}}]},
        mutation_name="productUpdate")
    check("access_denied outcome", not r["ok"] and r["outcome"] == "access_denied")
    r = _classify_graphql_response(
        {"errors": [{"message": "slow down", "extensions": {"code": "THROTTLED"}}]},
        mutation_name="productUpdate")
    check("throttled outcome", not r["ok"] and r["outcome"] == "throttled")
    r = _classify_graphql_response(
        {"errors": [{"message": "bad", "extensions": {"code": "variableNotInScope"}}]},
        mutation_name="productUpdate")
    check("unclassified code â†’ top_level_error", not r["ok"] and r["outcome"] == "top_level_error")
    r = _classify_graphql_response("not a dict")
    check("non-dict envelope typed", not r["ok"] and r["outcome"] == "top_level_error")


def test_run_retry_loop():
    print("run() throttle-retry loop (fake _post):")
    client = ShopifyGraphQLClient(shop_domain="example.com", access_token="tok")
    calls = {"n": 0}

    def fake_post_throttled_then_ok(query, variables=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"errors": [{"message": "slow down",
                                "extensions": {"code": "THROTTLED"}}]}
        return {"data": {"productUpdate": {"product": {"id": "gid://shopify/Product/1"}}},
                "extensions": {"cost": {"throttleStatus": {
                    "maximumAvailable": 1000, "currentlyAvailable": 990,
                    "restoreRate": 50.0}, "actualQueryCost": 10}}}

    client._post = fake_post_throttled_then_ok
    started = time.monotonic()
    result = client.run("productUpdate", "mutation { x }")
    elapsed = time.monotonic() - started
    check("throttle retries then ok", result["ok"] and result["outcome"] == "ok"
          and calls["n"] == 3, f"calls={calls['n']}")
    check("backoff waited (1s + 2s)", 2.9 < elapsed < 4.5, f"elapsed={elapsed:.2f}")

    calls["n"] = 0

    def fake_post_always_throttled(query, variables=None):
        calls["n"] += 1
        return {"errors": [{"message": "busy", "extensions": {"code": "THROTTLED"}}]}

    client._post = fake_post_always_throttled
    result = client.run("productUpdate", "mutation { x }", max_throttle_retries=1)
    check("exhausted retries â†’ typed throttled", not result["ok"]
          and result["outcome"] == "throttled", f"calls={calls['n']}")

    client = ShopifyGraphQLClient(shop_domain="example.com", access_token="tok")

    def fake_post_denied(query, variables=None):
        return {"data": None, "errors": [{"message": "unauthorized",
                                          "extensions": {"code": "ACCESS_DENIED"}}]}

    client._post = fake_post_denied
    result = client.run("urlRedirectCreate", "mutation { x }")
    check("access_denied typed from run()", not result["ok"]
          and result["outcome"] == "access_denied")


def test_no_credential_paths():
    print("credential guards:")
    client = ShopifyGraphQLClient()
    r = client.probe_scopes()
    check("probe no-credential typed skip", not r["ok"] and r["error"] == "no_credentials")
    try:
        client.run("productUpdate", "mutation { x }")
        check("run no-credential raises loud", False, "expected ShopifyError")
    except Exception as exc:
        check("run no-credential raises loud (config-error pattern)", "credential" in str(e := str(exc.__class__)).lower() or type(exc).__name__ == "ShopifyError")


def test_scope_gate():
    print("scope gate mapping (registry-derived):")
    check("seo.title needs write_products",
          required_scopes_for_sub_types(["seo.title"]) == ["write_products"])
    check("product_publish is the union (products OR collections)",
          required_scopes_for_sub_types(["product_publish"]) ==
          ["write_products", "write_publications"])
    check("product_publish_product needs write_products ONLY",
          required_scopes_for_sub_types(["product_publish_product"]) ==
          ["write_products"])
    check("collection_publish needs write_publications only",
          required_scopes_for_sub_types(["collection_publish"]) ==
          ["write_publications"])
    check("redirect needs navigation scope",
          required_scopes_for_sub_types(["redirect"]) == ["write_online_store_navigation"])
    check("page_create maps to write_content (registry-derived)",
          required_scopes_for_sub_types(["page_create"]) == ["write_content"])
    check("collection_create maps to write_products (registry-derived, verified)",
          required_scopes_for_sub_types(["collection_create"]) == ["write_products"])
    check("gate set covers all five scopes",
          set(REQUIRED_WRITE_SCOPES) >= {"write_products", "write_publications",
                                         "write_online_store_navigation", "write_content"})
    check("unknown sub_types return [] (caller must log loudly, plan contract)",
          required_scopes_for_sub_types(["bogus_union_probe"]) == [])
    # Registry drift guard: a sub_type referencing an unregistered mutation
    # must raise loudly (scope lists are DERIVED, so they cannot drift past
    # the registry silently).
    import connectors.shopify as sh
    saved = dict(sh.MUTATION_REGISTRY)
    try:
        del sh.MUTATION_REGISTRY["productUpdate"]
        try:
            required_scopes_for_sub_types(["seo.title"])
            check("registry drift raises loud", False, "expected KeyError")
        except KeyError:
            check("registry drift raises loud", True)
    finally:
        sh.MUTATION_REGISTRY.clear()
        sh.MUTATION_REGISTRY.update(saved)


def test_any_of_groups():
    print("any-of scope gate:")
    # the Phase 1.5 real-store case: write_online_store_pages granted,
    # write_content absent -> content/pages any-of SATISFIED
    missing, unsat = granted_covers_required(
        ["write_products", "write_online_store_pages"], ["write_products"])
    check("any-of satisfied by either member",
          missing == [] and unsat == [], f"{missing}/{unsat}")
    # exact mode: caller opts out of any-of and demands the pair explicitly
    missing, unsat = granted_covers_required(
        ["write_products", "write_online_store_pages"],
        ["write_products", "write_content"], any_of_groups=[])
    check("exact mode when caller opts out",
          missing == ["write_content"] and unsat == [], f"{missing}/{unsat}")
    # neither member granted -> group unsatisfied
    missing, unsat = granted_covers_required(["write_products"], [])
    check("any-of reported unsatisfied when neither granted",
          missing == [] and unsat == [["write_content", "write_online_store_pages"]],
          f"{missing}/{unsat}")
    check("any-of group is the verified pair",
          ANY_OF_SCOPE_GROUPS == [frozenset({"write_content",
                                            "write_online_store_pages"})])


def main():
    test_registry()
    test_throttle_tracker()
    test_classifier()
    test_run_retry_loop()
    test_no_credential_paths()
    test_scope_gate()
    test_any_of_groups()
    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL GRAPHQL SHELL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
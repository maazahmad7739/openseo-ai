import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error
from html import unescape as html_unescape

from .mode import resolve_mode
from .url_normalize import canonicalize_url

SHOPIFY_API_BASE_DEFAULT = "https://{shop}.myshopify.com/admin/api/2024-10"
MOCK_MODE_ENV = "SHOPIFY_MOCK_MODE"

ENDPOINTS = {
    "products": "products.json?limit={limit}",
    # collections.json (the merged REST view) returns HTTP 403 on live stores
    # (verified 2026-09-23 on action-seo-test); custom_collections.json +
    # smart_collections.json are the working pair. _get() runs BOTH calls and
    # merges; the normalizer reads smart_collections/custom_collections keys.
    "collections": ("custom_collections.json?limit={limit} "
                    "smart_collections.json?limit={limit}"),
}

MOCK_FIXTURES = {
    "products": "shopify_products.json",
    "collections": "shopify_collections.json",
}

HTTP_TIMEOUT_SECONDS = 60
DEFAULT_CAPABILITIES = ["products", "collections"]


class ShopifyError(Exception):
    pass


class ShopifyUnsupportedCapability(ShopifyError):
    pass


class ShopifyRestAdapter:
    """Per-site Shopify Admin API adapter (REST, offline-mockable).

    Same supports()/fetch() contract as the other connectors. fetch() never
    raises: provider, HTTP, and missing-mock-fixture failures return a typed
    error response that callers treat as "skip and log".
    """

    def __init__(self, shop_domain=None, access_token=None, api_version="2024-10",
                 capabilities=None, mock_mode=None, fixtures_dir="tests/fixtures"):
        self.shop_domain = shop_domain
        self.access_token = access_token
        self.base_url = SHOPIFY_API_BASE_DEFAULT.format(shop=shop_domain or "mock-store")
        self.api_version = api_version
        self.capabilities = list(capabilities) if capabilities else list(DEFAULT_CAPABILITIES)
        self.fixtures_dir = fixtures_dir

        env_mock = os.environ.get(MOCK_MODE_ENV, "").strip().lower() in ("1", "true", "yes", "on")
        self.mock_mode = bool(mock_mode) if mock_mode is not None else env_mock
        if self.mock_mode:
            print(
                "[shopify] MOCK MODE ENABLED - serving Shopify fixtures from "
                f"'{self.fixtures_dir}'; NO real Shopify network calls will be made.",
                flush=True,
            )

    def supports(self, capability):
        return capability in self.capabilities

    def fetch(self, capability, params):
        if not self.supports(capability):
            return {"ok": False, "capability": capability, "error": "unsupported_capability"}
        try:
            raw = self._call(capability, params)
            return self._normalize(capability, raw)
        except ShopifyUnsupportedCapability:
            return {"ok": False, "capability": capability, "error": "unsupported_capability"}
        except ShopifyError as exc:
            return {"ok": False, "capability": capability, "error": "provider_error", "detail": str(exc)}
        except Exception as exc:
            return {"ok": False, "capability": capability, "error": "provider_error", "detail": str(exc)}

    def _call(self, capability, params):
        if capability not in ENDPOINTS:
            raise ShopifyUnsupportedCapability(capability)
        if self.mock_mode:
            return self._load_mock_fixture(capability)
        return self._get(capability, params or {})

    def _load_mock_fixture(self, capability):
        filename = MOCK_FIXTURES.get(capability)
        if not filename:
            raise ShopifyUnsupportedCapability(capability)
        path = os.path.join(self.fixtures_dir, filename)
        if not os.path.exists(path):
            raise ShopifyError(f"mock fixture not found: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _get(self, capability, params):
        if not self.shop_domain:
            raise ShopifyError("no shop_domain configured (per-site Shopify store)")
        if not self.access_token:
            raise ShopifyError("no Shopify access token configured (per-site Admin API token)")
        limit = int(params.get("limit", 250))
        max_pages = int(params.get("max_pages", 100))
        merged = {}
        # A capability may need SEVERAL endpoint calls (the collections
        # capability = custom_collections.json + smart_collections.json, the
        # merged collections.json view 403s on live stores). Endpoints carry
        # them space-separated; every call is paginated and merged.
        endpoint_urls = [u.strip() for u in ENDPOINTS[capability].split(" ") if u.strip()]
        for endpoint_template in endpoint_urls:
            base_url = f"{self.base_url}/{endpoint_template.format(limit=limit)}"
            if params.get("extra_params"):
                extra = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params["extra_params"].items())
                base_url = f"{base_url}&{extra}"
            url = base_url
            fetched_pages = 0
            while url and fetched_pages < max_pages:
                payload = self._get_raw(url)
                for key, values in (payload or {}).items():
                    if isinstance(values, list):
                        merged.setdefault(key, []).extend(values)
                    else:
                        merged.setdefault(key, values)
                fetched_pages += 1
                url = self._next_page_url(payload)
            else:
                if url:
                    print(
                        f"[shopify] {capability}: hit {max_pages} page cap — results may be "
                        "truncated (raise max_pages if the store warrants it)",
                        flush=True,
                    )
        return merged

    def _get_raw(self, url):
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-Shopify-Access-Token", self.access_token)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                self._link_header = resp.headers.get("Link")
                return payload
        except urllib.error.HTTPError as exc:
            raise ShopifyError(f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            raise ShopifyError(f"connection failed: {exc.reason}")

    def _next_page_url(self, payload):
        link = getattr(self, "_link_header", None) or ""
        for part in link.split(","):
            if 'rel="next"' in part:
                url_part = part.split(";", 1)[0].strip().strip("<>")
                if url_part.startswith("http"):
                    return html_unescape(url_part).replace("&amp;", "&")
        return None

    def _normalize(self, capability, raw):
        if capability == "products":
            data = self._normalize_products(raw)
        elif capability == "collections":
            data = self._normalize_collections(raw)
        else:
            raise ShopifyUnsupportedCapability(capability)
        return {"ok": True, "data": data}

    def _normalize_products(self, raw):
        normalized = []
        for item in raw.get("products", []) or []:
            variants = [
                {
                    "variant_id": v.get("id"),
                    "sku": v.get("sku"),
                    "price": v.get("price"),
                    "inventory_quantity": v.get("inventory_quantity", 0),
                }
                for v in item.get("variants", []) or []
            ]
            normalized.append({
                "product_id": str(item.get("id")) if item.get("id") is not None else None,
                "title": item.get("title"),
                "handle": item.get("handle"),
                "product_type": item.get("product_type") or None,
                "status": item.get("status") or "draft",
                "tags": item.get("tags") or "",
                # Phase 2 (plan/21 §1.4/§2.2): meta + body ingestion. Meta
                # description = the storefront snippet Shopify renders when
                # seo.description is blank; never fabricated here.
                "meta_description": (item.get("meta_description")
                                     or (item.get("seo") or {}).get("description")
                                     or None),
                "body_html": item.get("body_html") or None,
                "total_inventory": sum(v.get("inventory_quantity", 0) for v in item.get("variants", []) or []),
                "in_stock": sum(v.get("inventory_quantity", 0) for v in item.get("variants", []) or []) > 0,
                "variants": variants,
            })
        return normalized

    def _normalize_collections(self, raw):
        normalized = []
        for kind, key in (("smart", "smart_collections"), ("custom", "custom_collections")):
            for item in raw.get(key, []) or []:
                normalized.append({
                    "collection_id": str(item.get("id")) if item.get("id") is not None else None,
                    "title": item.get("title"),
                    "handle": item.get("handle"),
                    "collection_type": kind,
                    "published": item.get("published_at") is not None,
                    "url": canonicalize_url(
                        f"https://{self.shop_domain}/collections/{item.get('handle')}"
                    ) if self.shop_domain else None,
                    # Phase 2: collection description = body copy (rendered
                    # into the page); meta_description via body_html parse
                    # is not attempted here (crawl-owned, never fabricated).
                    "meta_description": (item.get("meta_description")
                                         or (item.get("seo") or {}).get("description")
                                         or None),
                    "body_html": (item.get("body_html")
                                  or item.get("description_html")
                                  or item.get("description")
                                  or None),
                    "rules": item.get("rules") or [],
                })
        return normalized


def resolve_admin_token():
    """Admin API access token, honoring the canonical and alias env names.

    Resolution order (first hit wins):
      SHOPIFY_ACCESS_TOKEN → SHOPIFY_ADMIN_ACCESS_TOKEN
    Returns None when neither is set (callers fail with an explicit
    configuration error naming both keys, never a silent half-credential).
    """
    return os.environ.get("SHOPIFY_ACCESS_TOKEN") or os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN")


def resolve_store_domain():
    """Store domain, honoring the canonical and alias env names.

    Resolution order (first hit wins):
      SHOPIFY_DOMAIN → SHOPIFY_STORE_DOMAIN
    """
    return os.environ.get("SHOPIFY_DOMAIN") or os.environ.get("SHOPIFY_STORE_DOMAIN")


def get_shopify_adapter(config=None):
    """Resolve the per-site Shopify credential and return the REST adapter.

    Shopify is per-site (site_config.shopify_domain + Admin API token).
    Mock mode skips credential resolution entirely.
    """
    cfg = config or {}
    mock_mode = _is_mock_mode(cfg)
    shop_domain = (cfg.get("shopify.domain") or resolve_store_domain())
    access_token = (cfg.get("shopify.access_token") or resolve_admin_token())
    fixtures_dir = cfg.get("shopify.mock_fixtures_dir") or os.environ.get("SHOPIFY_MOCK_FIXTURES_DIR") or "tests/fixtures"
    capabilities = cfg.get("shopify.capabilities") or os.environ.get("SHOPIFY_CAPABILITIES") or DEFAULT_CAPABILITIES
    if isinstance(capabilities, str):
        capabilities = [c.strip() for c in capabilities.split(",") if c.strip()]

    if not mock_mode and not (shop_domain and access_token):
        raise ShopifyError(
            "no Shopify credential resolved: set SHOPIFY_DOMAIN and SHOPIFY_ACCESS_TOKEN. "
            f"Run in mock mode (set INTEGRATION_MODE=mock or {MOCK_MODE_ENV}=1) to test "
            "without credentials."
        )
    if mock_mode and not shop_domain:
        shop_domain = "example.com"

    return ShopifyRestAdapter(
        shop_domain=shop_domain,
        access_token=None if mock_mode else access_token,
        capabilities=capabilities,
        mock_mode=mock_mode,
        fixtures_dir=fixtures_dir,
    )


def _is_mock_mode(cfg):
    explicit = cfg.get("shopify.mock_mode")
    if explicit is not None:
        return bool(explicit)
    return resolve_mode(cfg.get("shopify.mode"), (MOCK_MODE_ENV,)) == "mock"


# ============================================================
# Stage 2 Phase 0 — GraphQL Admin caller shell (plan/21 §4)
# ============================================================
# All writes go through ONE _graphql() caller. Pinned to the version in
# system_config ('shopify.api_version', default 2026-01). Every mutation /
# scope pair is recorded in MUTATION_REGISTRY with its verified docs URL —
# build rule: re-verify against the pinned-version docs page at build time,
# never from memory (plan/21 §8 audit trail).
#
# Behavioral differences vs the REST path (plan/21 §5.2):
#   * Rate limiting is a COST BUCKET, not req/sec: throttleStatus carries
#     maximumAvailable / currentlyAvailable / restoreRate. We pre-check
#     available points before a mutation and sleep-until-restore when short.
#   * Errors come back as HTTP 200 + structured errors: mutation-level
#     `userErrors` (field-level) and top-level `errors` (codes like
#     ACCESS_DENIED, THROTTLED). The classifier maps these to typed outcomes.
#   * Reads paginate by cursor (PageInfo{hasNextPage, endCursor}), not Link
#     headers — the REST fetch path is untouched here.

GRAPHQL_PATH = "/admin/api/{version}/graphql.json"

# Response X-Shopify-API-Version drift warning threshold (plan/21 §4).
PINNED_API_VERSION_DEFAULT = "2026-01"

# Cost-bucket parameters from Shopify docs (2026-01 standard bucket).
THROTTLE_DEFAULT_BUDGET = 1000     # maximumAvailable
THROTTLE_RESTORE_RATE = 50.0       # points restored per second
DEFAULT_MUTATION_COST = 10         # typical update mutation; actual cost is read from the response
THROTTLE_SLEEP_CAP_SECONDS = 20    # never sleep unbounded inside one call

# Scope required per mutation name (verified against 2026-01 docs — plan/21 §4).
MUTATION_REGISTRY = {
    # mutation_name: (scope, doc_url)
    "productUpdate": (
        "write_products",
        "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/productUpdate",
    ),
    "collectionUpdate": (
        "write_products",
        "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/collectionUpdate",
    ),
    "metafieldsSet": (
        "write_products",  # same access level as the owner resource (verified)
        "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/metafieldsSet",
    ),
    "urlRedirectCreate": (
        "write_online_store_navigation",
        "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/urlRedirectCreate",
    ),
    "urlRedirectUpdate": (
        "write_online_store_navigation",
        "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/urlRedirectUpdate",
    ),
    "urlRedirectDelete": (
        "write_online_store_navigation",
        "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/urlRedirectDelete",
    ),
    "articleUpdate": (
        "write_content",  # any-of: write_content | write_online_store_pages (verified)
        "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/articleUpdate",
    ),
    "productCreate": (
        "write_products",
        "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/productCreate",
    ),
    "pageCreate": (
        "write_content",  # any-of: write_content | write_online_store_pages (verified)
        "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/pageCreate",
    ),
    # Verified against pinned 2026-01 docs (2026-09-22, Phase 1 cleanup):
    # requires write_products; input: CollectionInput!; created collection is
    # unpublished by default (publishablePublish afterwards, same as
    # productCreate). Store must not be Starter/Retail (same as collectionUpdate).
    "collectionCreate": (
        "write_products",
        "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/collectionCreate",
    ),
    # Re-verified against pinned 2026-01 docs (2026-09-23, Phase 3 build):
    # args id + input: [PublicationInput!]!; scope write_publications; read-back
    # via Publishable.publishedOnPublication(publicationId:).
    "publishablePublish": (
        "write_publications",
        "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/publishablePublish",
    ),
    "publishableUnpublish": (
        "write_publications",
        "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/publishableUnpublish",
    ),
}

# Top-level GraphQL error codes we classify explicitly (plan/21 §4).
GQL_ERROR_CODE_MAP = {
    "access_denied": "access_denied",
    "throttled": "throttled",
}

# Scope introspection: OAuth is UNVERSIONED (Shopify docs) so this stays REST
# and is not subject to the REST product deprecations.
ACCESS_SCOPES_PATH = "/admin/oauth/access_scopes.json"

# Required scope set for the real-store gate (plan/21 §4 consolidated table).
# 'write_content' and 'write_online_store_pages' are any-of alternatives for
# articles/pages mutations (articleUpdate/pageCreate accept EITHER). The
# any-of pair is modeled explicitly (ANY_OF_SCOPE_GROUPS) instead of a flat
# union, so a consumer can never silently demand both alternatives.
# Derived from MUTATION_REGISTRY — every registered mutation's verified scope
# is exactly represented here; no hand-maintained second list.
ANY_OF_SCOPE_GROUPS = [
    frozenset({"write_content", "write_online_store_pages"}),
]
REQUIRED_WRITE_SCOPES = sorted({
    scope for name, (scope, _doc) in MUTATION_REGISTRY.items()
})


def granted_covers_required(granted, required, any_of_groups=None):
    """Scope-gate check with any-of group support (plan/21 §4).

    required: iterable of exact-match scope handles that must all be granted.
    any_of_groups: iterable of groups; a group is satisfied when AT LEAST ONE
    of its members is granted. Defaults to ANY_OF_SCOPE_GROUPS (the verified
    write_content | write_online_store_pages pair) — pass [] to demand every
    scope exactly.

    Returns the list of MISSING exact scopes and UNSATISFIED groups so the
    caller can report precisely what is absent.
    """
    granted = set(granted or [])
    groups = list(any_of_groups if any_of_groups is not None else ANY_OF_SCOPE_GROUPS)
    missing = sorted(s for s in (required or []) if s not in granted)
    unsatisfied_groups = [
        sorted(group) for group in groups if not (set(group) & granted)
    ]
    return missing, unsatisfied_groups


class _ThrottleTracker:
    """Tracks the GraphQL cost bucket between calls (per-adapter instance).

    Pre-mutation budget math: after each response we record
    extensions.cost.throttleStatus; before the next call we compute how long
    to sleep if the recorded available points are short of the expected cost.
    This keeps the executor within Shopify's bucket even when other clients
    (e.g. the daily sync) share it.
    """

    def __init__(self, budget=THROTTLE_DEFAULT_BUDGET, restore_rate=THROTTLE_RESTORE_RATE):
        self.budget = budget
        self.restore_rate = restore_rate
        self.available = budget          # optimistic start; corrected by first response
        self.maximum = budget
        self.last_observed_at = None
        self.last_actual_cost = None

    def observe(self, extensions):
        """Record throttleStatus/actualQueryCost from a response envelope."""
        cost = (extensions or {}).get("cost") or {}
        throttle = cost.get("throttleStatus") or {}
        # A legitimate 0 (empty bucket) must UPDATE the tracker, not fall
        # back to the stale previous value — only None/missing falls back.
        maximum = throttle.get("maximumAvailable")
        available = throttle.get("currentlyAvailable")
        restore_rate = throttle.get("restoreRate")
        if maximum is not None:
            self.maximum = int(maximum)
        if available is not None:
            self.available = int(available)
        if restore_rate is not None:
            self.restore_rate = float(restore_rate)
        self.last_actual_cost = cost.get("actualQueryCost")
        self.last_observed_at = time.monotonic()

    def wait_seconds_for(self, expected_cost):
        """Seconds to sleep so expected_cost fits the bucket; 0 when clear."""
        if expected_cost <= self.available:
            return 0.0
        deficit = expected_cost - self.available
        return min(deficit / max(self.restore_rate, 0.001), THROTTLE_SLEEP_CAP_SECONDS)


def _classify_graphql_response(payload, mutation_name=None):
    """Map a GraphQL envelope to a typed outcome (never-raise contract).

    Returns {ok, outcome, data, userErrors, errors, throttle} where outcome is
    one of: ok | user_errors | access_denied | throttled | top_level_error.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "outcome": "top_level_error",
                "userErrors": [], "errors": [{"message": "non-object response"}]}
    data = payload.get("data")
    top_errors = payload.get("errors") or []
    classified = []
    for err in top_errors:
        ext = err.get("extensions") or {}
        raw_code = ext.get("code", "") or ""
        code = str(raw_code).lower()
        mapped = GQL_ERROR_CODE_MAP.get(code) or GQL_ERROR_CODE_MAP.get(raw_code)
        classified.append({"code": mapped or code or "unclassified",
                           "message": err.get("message", "")})
    codes = {c["code"] for c in classified}
    outcome = "ok"
    if "throttled" in codes:
        outcome = "throttled"
    elif "access_denied" in codes:
        outcome = "access_denied"
    if outcome != "ok":
        return {"ok": False, "outcome": outcome, "userErrors": [],
                "errors": classified, "data": data}
    # Mutation-level userErrors (verified payload shape: {mutation: {..., userErrors}})
    mutation_payload = (data or {}).get(mutation_name) if mutation_name else None
    user_errors = (mutation_payload or {}).get("userErrors") or []
    if user_errors:
        return {"ok": False, "outcome": "user_errors", "userErrors": user_errors,
                "errors": classified, "data": data}
    if data is None and top_errors:
        return {"ok": False, "outcome": "top_level_error", "userErrors": [],
                "errors": classified, "data": None}
    return {"ok": True, "outcome": "ok", "userErrors": [],
            "errors": [], "data": data}


class ShopifyGraphQLClient:
    """GraphQL Admin caller with cost-bucket throttle + typed error outcomes.

    Executed by jobs/fix_executor.py only (never in-request — plan/21 §3).
    Construction never touches the network; every call returns a typed dict.
    """

    def __init__(self, shop_domain=None, access_token=None, api_version=None,
                 timeout=HTTP_TIMEOUT_SECONDS):
        resolved_version = api_version or os.environ.get("SHOPIFY_API_VERSION") \
            or PINNED_API_VERSION_DEFAULT
        self.shop_domain = shop_domain
        self.access_token = access_token
        self.api_version = resolved_version
        self.url = ("https://" + (shop_domain or "mock-store") +
                    GRAPHQL_PATH.format(version=resolved_version))
        self.throttle = _ThrottleTracker()

    def _post(self, query, variables=None):
        if not self.shop_domain or not self.access_token:
            raise ShopifyError(
                "GraphQL caller needs shop_domain and access_token "
                "(no live Shopify credential resolved)")
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Shopify-Access-Token", self.access_token)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8")
                served_version = resp.headers.get("X-Shopify-API-Version")
                payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            # REST-era bug fixed here: read the body so 403/429 detail survives.
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            return {"ok": False, "outcome": "http_error", "http_status": exc.code,
                    "retry_after": resp_retry_after(exc.headers), "detail": detail}
        except urllib.error.URLError as exc:
            return {"ok": False, "outcome": "network_error", "detail": str(exc.reason)}
        if served_version and served_version != self.api_version:
            print(f"[shopify-graphql] WARNING: served version {served_version} "
                  f"!= pinned {self.api_version} (fall-forward detected)", flush=True)
        return payload if isinstance(payload, dict) else {"errors": [{"message": "non-dict envelope"}]}

    def run(self, mutation_name, query, variables=None, expected_cost=DEFAULT_MUTATION_COST,
            max_throttle_retries=3):
        """Execute one mutation/query; throttle-aware; never raises on provider errors.

        Outcome mapping (plan/21 §5):
          ok              → {"ok": True, "data": ...}
          user_errors     → {"ok": False, "outcome": "user_errors", ...}   (fix → failed)
          access_denied   → {"ok": False, "outcome": "access_denied"}  (3-strike disable)
          throttled       → exponential backoff, then typed failure
          http/network    → typed failure (adapter never-raise)
        """
        backoff = 1.0
        for attempt in range(max_throttle_retries + 1):
            wait = self.throttle.wait_seconds_for(expected_cost)
            if wait > 0:
                time.sleep(wait)
            raw = self._post(query, variables)
            if not isinstance(raw, dict) or raw.get("outcome") in ("http_error", "network_error"):
                return raw  # already typed
            self.throttle.observe(raw.get("extensions"))
            result = _classify_graphql_response(raw, mutation_name=mutation_name)
            if result["outcome"] == "throttled" and attempt < max_throttle_retries:
                # Exponential backoff, then re-check the bucket (which also
                # restores while we waited).
                time.sleep(min(backoff, THROTTLE_SLEEP_CAP_SECONDS))
                backoff *= 2
                continue
            result["adapter"] = "shopify_graphql"
            result["api_version"] = self.api_version
            return result
        return {"ok": False, "outcome": "throttled", "userErrors": [],
                "errors": [{"message": "throttle retries exhausted"}],
                "adapter": "shopify_graphql", "api_version": self.api_version}

    def probe_scopes(self):
        """Read-only scope introspection (unversioned OAuth surface).

        Returns {ok, scopes: [...], shop_domain} or {ok: False, error} —
        Phase 0 §7.1 probe; never contains the token itself.
        """
        if not self.shop_domain or not self.access_token:
            return {"ok": False, "error": "no_credentials",
                    "detail": "probe needs SHOPIFY_DOMAIN + SHOPIFY_ACCESS_TOKEN"}
        url = f"https://{self.shop_domain}{ACCESS_SCOPES_PATH}"
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-Shopify-Access-Token", self.access_token)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict):
                return {"ok": False, "error": "malformed_response",
                        "detail": "scope endpoint returned a non-object body"}
            scopes = sorted(s.get("handle", "") for s in payload.get("access_scopes", []) or [])
            return {"ok": True, "scopes": scopes, "shop_domain": self.shop_domain}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"http_{exc.code}",
                    "detail": "token invalid or endpoint unavailable"}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": "network_error", "detail": str(exc.reason)}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": "malformed_response", "detail": str(exc)}


def resp_retry_after(headers):
    try:
        return float((headers or {}).get("Retry-After") or 0)
    except (TypeError, ValueError):
        return 0.0


PUBLICATION_ID_CONFIG_KEY = "shopify.publication_id"
PUBLICATIONS_QUERY = """
query FixPublications {
  publications(first: 10) {
    nodes { id title }
  }
}
"""


def resolve_publication_id(conn, client):
    """Resolve the online-store publication id (plan/21 §1.1).

    Order: system_config cache ('shopify.publication_id') -> one publications
    query -> cache back into system_config. Returns None when the client is
    credential-less or the store returns nothing — callers NEVER fabricate a
    publication id (the publish fix path refuses instead).

    conn: database connection for the system_config read/write (may be None
    in tests — cache is skipped, resolution still works).
    """
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT config_value FROM system_config "
                    "WHERE config_key = %s", (PUBLICATION_ID_CONFIG_KEY,))
                row = cur.fetchone()
            if row and row[0]:
                return str(row[0])
        except Exception:
            pass
    if not client.shop_domain or not client.access_token:
        return None
    result = client.run("publications", PUBLICATIONS_QUERY, {})
    if not result.get("ok"):
        return None
    nodes = ((result.get("data") or {}).get("publications") or {}).get("nodes") or []
    online_store = next((n for n in nodes
                         if (n.get("title") or "").lower() in
                         ("online store", "web", "web sales channel")), None)
    picked = online_store or (nodes[0] if nodes else None)
    publication_id = (picked or {}).get("id")
    if publication_id and conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO system_config (config_key, config_value) "
                    "VALUES (%s, %s) "
                    "ON CONFLICT (config_key) DO UPDATE SET config_value = EXCLUDED.config_value",
                    (PUBLICATION_ID_CONFIG_KEY, publication_id))
            conn.commit()
        except Exception:
            conn.rollback()
    return publication_id


def required_scopes_for_sub_types(sub_types):
    """Required scope(s) per sub_type — executor gate input (plan/21 §4 table).

    Entries with a `mutation` key are DERIVED from MUTATION_REGISTRY (single
    source of truth: scope verified once at build time, both lists can never
    drift). Entries with explicit scope lists are unions (sub_types that may
    touch more than one mutation kind). Unknown sub_types return [] — the
    caller (executor scope probe) must treat that as a diagnostic gap and log
    loudly, never as "no scope needed".
    """
    registry_scope = {name: pair[0] for name, pair in MUTATION_REGISTRY.items()}
    mapping = {
        "seo.title": ["productUpdate"],
        "seo.description": ["productUpdate"],
        "content": ["productUpdate", "collectionUpdate"],
        "product_publish": ["productUpdate", "publishablePublish"],  # products OR collections (union)
        "product_publish_product": ["productUpdate"],  # products only: status write
        "collection_publish": ["publishablePublish"],  # collections only
        "product_unpublish": ["productUpdate"],  # products only: status -> ARCHIVED
        "collection_unpublish": ["publishableUnpublish"],  # collections only
        "article_body_link": ["articleUpdate"],
        "redirect": ["urlRedirectCreate", "urlRedirectUpdate", "urlRedirectDelete"],
        "collection_create": ["collectionCreate"],
        "product_create": ["productCreate"],
        "page_create": ["pageCreate"],
    }
    scopes = set()
    for st in sub_types or []:
        for mutation_name in mapping.get(st, []):
            scope = registry_scope.get(mutation_name)
            if scope is None:
                raise KeyError(
                    f"sub_type {st!r} references mutation {mutation_name!r} "
                    "which is not in MUTATION_REGISTRY — registry and scope "
                    "mapping have drifted")
            scopes.add(scope)
    return sorted(scopes)
import json
import os
import urllib.request
import urllib.error

from .mode import resolve_mode

SHOPIFY_API_BASE_DEFAULT = "https://{shop}.myshopify.com/admin/api/2024-10"
MOCK_MODE_ENV = "SHOPIFY_MOCK_MODE"

ENDPOINTS = {
    "products": "products.json?limit={limit}",
    "collections": "collections.json?limit={limit}",
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
        url = f"{self.base_url}/{ENDPOINTS[capability].format(limit=limit)}"
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-Shopify-Access-Token", self.access_token)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ShopifyError(f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            raise ShopifyError(f"connection failed: {exc.reason}")

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
                    "url": f"https://{self.shop_domain}/collections/{item.get('handle')}" if self.shop_domain else None,
                    "rules": item.get("rules") or [],
                })
        return normalized


def get_shopify_adapter(config=None):
    """Resolve the per-site Shopify credential and return the REST adapter.

    Shopify is per-site (site_config.shopify_domain + Admin API token).
    Mock mode skips credential resolution entirely.
    """
    cfg = config or {}
    mock_mode = _is_mock_mode(cfg)
    shop_domain = cfg.get("shopify.domain") or os.environ.get("SHOPIFY_DOMAIN")
    access_token = cfg.get("shopify.access_token") or os.environ.get("SHOPIFY_ACCESS_TOKEN")
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
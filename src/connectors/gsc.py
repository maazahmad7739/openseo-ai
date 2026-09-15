import json
import os
import urllib.request
import urllib.error
from urllib.parse import quote

from .mode import resolve_mode

GSC_API_BASE_DEFAULT = "https://www.googleapis.com/webmasters/v3"
MOCK_MODE_ENV = "GSC_MOCK_MODE"
SITE_URL_ENV = "GSC_SITE_URL"
ACCESS_TOKEN_ENV = "GSC_ACCESS_TOKEN"
SERVICE_ACCOUNT_KEY_FILE_ENV = "GSC_SERVICE_ACCOUNT_KEY_FILE"
SERVICE_ACCOUNT_KEY_JSON_ENV = "GSC_SERVICE_ACCOUNT_KEY_JSON"
GSC_OAUTH_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"

ENDPOINTS = {
    "sites": "sites",
    "search_analytics": "sites/{site_url}/searchAnalytics/query",
}

MOCK_FIXTURES = {
    "sites": "gsc_sites.json",
    "search_analytics": "gsc_search_analytics.json",
}

HTTP_TIMEOUT_SECONDS = 60

DEFAULT_DIMENSIONS = ["date", "query", "page", "country", "device"]
DEFAULT_CAPABILITIES = ["sites", "search_analytics"]


class GscError(Exception):
    pass


class GscConfigError(GscError):
    pass


class GscUnsupportedCapability(GscError):
    pass


class ServiceAccountTokenSource:
    """Mints and auto-refreshes OAuth tokens from a service-account JSON key.

    The service account must be added as an authorized user on the target GSC
    property (Settings > Users and permissions) with its client_email; then it
    can act with the webmasters.readonly scope. No user interaction involved,
    suitable for unattended daily syncs.
    """

    def __init__(self, key_file=None, key_json=None, scopes=None):
        try:
            import google.auth.transport.requests
            from google.oauth2 import service_account
        except ImportError as exc:
            raise GscConfigError(
                "google-auth is not installed; run: pip install google-auth"
            ) from exc
        self._requests_module = google.auth.transport.requests
        if key_json:
            self._credentials = service_account.Credentials.from_service_account_info(
                json.loads(key_json), scopes=[GSC_OAUTH_SCOPE]
            )
        else:
            if not key_file or not os.path.exists(key_file):
                raise GscConfigError(f"service account key file not found: {key_file}")
            self._credentials = service_account.Credentials.from_service_account_file(
                key_file, scopes=[GSC_OAUTH_SCOPE]
            )
        self.client_email = self._credentials.service_account_email

    def get_access_token(self):
        if not self._credentials.valid:
            self._credentials.refresh(self._requests_module.Request())
        return self._credentials.token


def read_service_account_client_email(key_file=None, key_json=None):
    """Read client_email from a service-account key without network access.

    The returned email must be added as an authorized user on the GSC property
    (Settings > Users and permissions) before the account can pull data.
    """
    try:
        from google.oauth2 import service_account
    except ImportError as exc:
        raise GscConfigError(
            "google-auth is not installed; run: pip install google-auth"
        ) from exc
    if key_json:
        info = json.loads(key_json)
    else:
        if not key_file or not os.path.exists(key_file):
            raise GscConfigError(f"service account key file not found: {key_file}")
        with open(key_file, "r", encoding="utf-8") as fh:
            info = json.load(fh)
    email = info.get("client_email")
    if not email:
        raise GscConfigError(f"key file has no client_email: {key_file}")
    return email


class GscRestAdapter:
    """Per-site Google Search Console REST adapter (plan/09 §4: only GSC is per-site).

    Implements the same supports()/fetch() contract as the OpenSEO adapter.
    fetch() never raises: provider, HTTP, and missing-mock-fixture failures
    return a typed error response that callers treat as "skip and log".
    """

    def __init__(self, base_url=None, site_url=None, access_token=None,
                 token_source=None, capabilities=None, mock_mode=None,
                 fixtures_dir="tests/fixtures"):
        self.base_url = (base_url or GSC_API_BASE_DEFAULT).rstrip("/")
        self.site_url = site_url
        self.access_token = access_token
        self.token_source = token_source
        self.capabilities = list(capabilities) if capabilities else list(ENDPOINTS)
        self.fixtures_dir = fixtures_dir

        env_mock = os.environ.get(MOCK_MODE_ENV, "").strip().lower() in ("1", "true", "yes", "on")
        self.mock_mode = bool(mock_mode) if mock_mode is not None else env_mock
        if self.mock_mode:
            print(
                "[gsc] MOCK MODE ENABLED - serving Google Search Console "
                f"fixtures from '{self.fixtures_dir}'; NO real GSC network calls "
                "will be made.",
                flush=True,
            )

    def supports(self, capability):
        return capability in self.capabilities

    def fetch(self, capability, params):
        if not self.supports(capability):
            return {"ok": False, "capability": capability, "error": "unsupported_capability"}
        try:
            raw = self._call(capability, params)
            return self._normalize(capability, raw, params)
        except GscUnsupportedCapability as exc:
            return {"ok": False, "capability": capability, "error": "unsupported_capability"}
        except GscError as exc:
            return {"ok": False, "capability": capability, "error": "provider_error", "detail": str(exc)}
        except Exception as exc:
            return {"ok": False, "capability": capability, "error": "provider_error", "detail": str(exc)}

    def _call(self, capability, params):
        if capability not in ENDPOINTS:
            raise GscUnsupportedCapability(capability)
        if self.mock_mode:
            return self._load_mock_fixture(capability)
        if capability == "search_analytics":
            return self._query_search_analytics(params)
        return self._get(capability)

    def _load_mock_fixture(self, capability):
        filename = MOCK_FIXTURES.get(capability)
        if not filename:
            raise GscUnsupportedCapability(capability)
        path = os.path.join(self.fixtures_dir, filename)
        if not os.path.exists(path):
            raise GscError(f"mock fixture not found: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _current_access_token(self):
        if self.token_source is not None:
            return self.token_source.get_access_token()
        return self.access_token

    def _request(self, method, url, payload=None):
        headers = {"Authorization": f"Bearer {self._current_access_token()}"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise GscError(f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            raise GscError(f"connection failed: {exc.reason}")

    def _site_path(self, params):
        site_url = params.get("site_url") or self.site_url
        if not site_url:
            raise GscError("no site_url configured for search_analytics")
        return quote(site_url, safe="")

    def _query_search_analytics(self, params):
        path = ENDPOINTS["search_analytics"].format(site_url=self._site_path(params))
        start_date = params.get("start_date") or params.get("startDate")
        end_date = params.get("end_date") or params.get("endDate")
        if not start_date or not end_date:
            raise GscError("search_analytics requires start_date and end_date (YYYY-MM-DD)")
        body = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": params.get("dimensions") or params.get("dimension") or DEFAULT_DIMENSIONS,
            "rowLimit": int(params.get("row_limit") or params.get("rowLimit") or 1000),
            "startRow": int(params.get("start_row") or params.get("startRow") or 0),
            "type": params.get("type", "web"),
        }
        if params.get("dimension_filter_groups"):
            body["dimensionFilterGroups"] = params["dimension_filter_groups"]
        if params.get("data_state") or params.get("dataState"):
            body["dataState"] = params.get("data_state") or params.get("dataState")
        return self._request("POST", f"{self.base_url}/{path}", payload=body)

    def _get(self, capability):
        return self._request("GET", f"{self.base_url}/{ENDPOINTS[capability]}")

    def _normalize(self, capability, raw, params):
        if capability == "sites":
            data = self._normalize_sites(raw)
        elif capability == "search_analytics":
            data = self._normalize_search_analytics(raw, params)
        else:
            raise GscUnsupportedCapability(capability)
        return {"ok": True, "data": data}

    def _normalize_sites(self, raw):
        normalized = []
        for entry in raw.get("siteEntry", []) or []:
            normalized.append({
                "site_url": entry.get("siteUrl"),
                "permission_level": entry.get("permissionLevel"),
            })
        return normalized

    def _normalize_search_analytics(self, raw, params):
        dims = params.get("dimensions") or params.get("dimension") or DEFAULT_DIMENSIONS
        normalized = []
        for row in raw.get("rows", []) or []:
            keys = row.get("keys") or []
            item = {dims[i]: keys[i] for i in range(min(len(keys), len(dims)))}
            page_url = item.pop("page", None) or item.pop("page_url", None)
            clicks = row.get("clicks", 0)
            impressions = row.get("impressions", 0)
            normalized.append({
                "date": item.get("date"),
                "query": item.get("query"),
                "page_url": page_url,
                "country": item.get("country", "all"),
                "device": item.get("device", "all"),
                "clicks": int(clicks) if float(clicks).is_integer() else clicks,
                "impressions": int(impressions) if float(impressions).is_integer() else impressions,
                "ctr": row.get("ctr"),
                "position": row.get("position"),
            })
        return normalized


def get_gsc_adapter(config=None):
    """Resolve the per-site GSC credential and return the REST adapter.

    GSC is the ONLY per-site connector (plan/09 §4): each site carries its own
    gsc_property and its own OAuth grant. Authentication is via a service
    account JSON key (GSC_SERVICE_ACCOUNT_KEY_FILE / GSC_SERVICE_ACCOUNT_KEY_JSON)
    whose client_email must be an authorized user on the property; tokens are
    minted and refreshed automatically. Mock mode (GSC_MOCK_MODE=1) skips
    credential resolution entirely and never touches the secrets store.
    """
    cfg = config or {}
    mock_mode = _is_mock_mode(cfg)
    base_url = cfg.get("gsc.base_url") or _env("GSC_BASE_URL") or GSC_API_BASE_DEFAULT
    site_url = cfg.get("gsc.site_url") or _env(SITE_URL_ENV)
    capabilities = _parse_capabilities(
        cfg.get("gsc.capabilities") or _env("GSC_CAPABILITIES") or DEFAULT_CAPABILITIES
    )
    fixtures_dir = cfg.get("gsc.mock_fixtures_dir") or _env("GSC_MOCK_FIXTURES_DIR") or "tests/fixtures"
    key_file = cfg.get("gsc.service_account_key_file") or _env(SERVICE_ACCOUNT_KEY_FILE_ENV)
    key_json = cfg.get("gsc.service_account_key_json") or _env(SERVICE_ACCOUNT_KEY_JSON_ENV)
    static_token = cfg.get("gsc.access_token") or _env(ACCESS_TOKEN_ENV)

    token_source = None
    access_token = None
    if not mock_mode:
        if key_file or key_json:
            token_source = ServiceAccountTokenSource(key_file=key_file, key_json=key_json)
            print(
                "[gsc] SERVICE ACCOUNT MODE - tokens minted/refreshed via "
                f"{token_source.client_email}; key material never logged.",
                flush=True,
            )
        elif static_token:
            access_token = static_token
        else:
            raise GscConfigError(
                f"no GSC credential resolved: set {SERVICE_ACCOUNT_KEY_FILE_ENV} or "
                f"{SERVICE_ACCOUNT_KEY_JSON_ENV} (service-account JSON key), or "
                f"{ACCESS_TOKEN_ENV} for a static dev token. Run in mock mode "
                f"(set {MOCK_MODE_ENV}=1) to test without credentials."
            )

    return GscRestAdapter(
        base_url=base_url,
        site_url=site_url,
        access_token=access_token,
        token_source=token_source,
        capabilities=capabilities,
        mock_mode=mock_mode,
        fixtures_dir=fixtures_dir,
    )


def _is_mock_mode(cfg):
    explicit = cfg.get("gsc.mock_mode")
    if explicit is not None:
        return bool(explicit)
    return resolve_mode(cfg.get("gsc.mode"), (MOCK_MODE_ENV,)) == "mock"


def _env(name):
    return os.environ.get(name)


def _parse_capabilities(value):
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return list(parsed)
            except ValueError:
                return [c.strip() for c in text.split(",") if c.strip()]
    return list(DEFAULT_CAPABILITIES)
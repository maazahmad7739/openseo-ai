import json
import os
import urllib.request
import urllib.error

from .mode import resolve_mode

GA4_API_BASE_DEFAULT = "https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
MOCK_MODE_ENV = "GA4_MOCK_MODE"

MOCK_FIXTURES = {
    "page_performance": "ga4_page_performance.json",
}

HTTP_TIMEOUT_SECONDS = 60
DEFAULT_CAPABILITIES = ["page_performance"]


class Ga4Error(Exception):
    pass


class Ga4UnsupportedCapability(Ga4Error):
    pass


class Ga4RestAdapter:
    """Per-site GA4 Data API adapter (offline-mockable).

    Same supports()/fetch() contract as the other connectors. fetch() never
    raises: provider, HTTP, and missing-mock-fixture failures return a typed
    error response that callers treat as "skip and log".
    """

    def __init__(self, property_id=None, access_token=None,
                 capabilities=None, mock_mode=None, fixtures_dir="tests/fixtures"):
        self.property_id = property_id
        self.access_token = access_token
        self.capabilities = list(capabilities) if capabilities else list(DEFAULT_CAPABILITIES)
        self.fixtures_dir = fixtures_dir

        env_mock = os.environ.get(MOCK_MODE_ENV, "").strip().lower() in ("1", "true", "yes", "on")
        self.mock_mode = bool(mock_mode) if mock_mode is not None else env_mock
        if self.mock_mode:
            print(
                "[ga4] MOCK MODE ENABLED - serving GA4 fixtures from "
                f"'{self.fixtures_dir}'; NO real GA4 network calls will be made.",
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
        except Ga4UnsupportedCapability:
            return {"ok": False, "capability": capability, "error": "unsupported_capability"}
        except Ga4Error as exc:
            return {"ok": False, "capability": capability, "error": "provider_error", "detail": str(exc)}
        except Exception as exc:
            return {"ok": False, "capability": capability, "error": "provider_error", "detail": str(exc)}

    def _call(self, capability, params):
        if capability not in MOCK_FIXTURES:
            raise Ga4UnsupportedCapability(capability)
        if self.mock_mode:
            return self._load_mock_fixture(capability)
        return self._run_report(capability, params or {})

    def _load_mock_fixture(self, capability):
        filename = MOCK_FIXTURES.get(capability)
        if not filename:
            raise Ga4UnsupportedCapability(capability)
        path = os.path.join(self.fixtures_dir, filename)
        if not os.path.exists(path):
            raise Ga4Error(f"mock fixture not found: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _run_report(self, capability, params):
        if not self.property_id:
            raise Ga4Error("no ga4_property_id configured (per-site GA4 property)")
        if not self.access_token:
            raise Ga4Error("no GA4 access token configured (OAuth token for analytics.readonly)")
        url = GA4_API_BASE_DEFAULT.format(property_id=self.property_id)
        body = {
            "dateRanges": [{
                "startDate": params.get("start_date"),
                "endDate": params.get("end_date"),
            }],
            "dimensions": [{"name": "date"}, {"name": "pagePath"}, {"name": "sessionDefaultChannelGroup"}],
            "metrics": [{"name": "sessions"}, {"name": "engagedSessions"},
                        {"name": "addToCarts"}, {"name": "checkouts"},
                        {"name": "transactions"}, {"name": "totalRevenue"}],
            "dimensionFilter": {
                "filter": {
                    "fieldName": "sessionDefaultChannelGroup",
                    "stringFilter": {"matchType": "EXACT", "value": "Organic Search"},
                }
            },
            "limit": int(params.get("limit", 10000)),
        }
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.access_token}")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise Ga4Error(f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            raise Ga4Error(f"connection failed: {exc.reason}")

    def _normalize(self, capability, raw):
        if capability == "page_performance":
            data = self._normalize_page_performance(raw)
        else:
            raise Ga4UnsupportedCapability(capability)
        return {"ok": True, "data": data}

    def _normalize_page_performance(self, raw):
        normalized = []
        for row in raw.get("rows", []) or []:
            dims = row.get("dimensionValues", []) or []
            mets = row.get("metricValues", []) or []
            values = [m.get("value") for m in mets]
            sessions = int(float(values[0])) if len(values) > 0 and values[0] else 0
            orders = int(float(values[4])) if len(values) > 4 and values[4] else 0
            revenue = float(values[5]) if len(values) > 5 and values[5] else 0.0
            raw_date = dims[0].get("value") if dims else None
            iso_date = (
                f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                if raw_date and len(raw_date) == 8 and raw_date.isdigit() else raw_date
            )
            normalized.append({
                "date": iso_date,
                "page_path": dims[1].get("value") if len(dims) > 1 else None,
                "organic_sessions": sessions,
                "engaged_sessions": int(float(values[1])) if len(values) > 1 and values[1] else 0,
                "add_to_carts": int(float(values[2])) if len(values) > 2 and values[2] else 0,
                "checkouts": int(float(values[3])) if len(values) > 3 and values[3] else 0,
                "orders": orders,
                "revenue": round(revenue, 2),
                "conversion_rate": round(orders / sessions, 4) if sessions else 0.0,
            })
        return normalized


def get_ga4_adapter(config=None):
    """Resolve the per-site GA4 credential and return the REST adapter.

    GA4 is per-site (site_config.ga4_property_id + OAuth token).
    Mock mode skips credential resolution entirely.
    """
    cfg = config or {}
    mock_mode = _is_mock_mode(cfg)
    property_id = cfg.get("ga4.property_id") or os.environ.get("GA4_PROPERTY_ID")
    access_token = cfg.get("ga4.access_token") or os.environ.get("GA4_ACCESS_TOKEN")
    fixtures_dir = cfg.get("ga4.mock_fixtures_dir") or os.environ.get("GA4_MOCK_FIXTURES_DIR") or "tests/fixtures"
    capabilities = cfg.get("ga4.capabilities") or os.environ.get("GA4_CAPABILITIES") or DEFAULT_CAPABILITIES
    if isinstance(capabilities, str):
        capabilities = [c.strip() for c in capabilities.split(",") if c.strip()]

    if not mock_mode and not (property_id and access_token):
        raise Ga4Error(
            "no GA4 credential resolved: set GA4_PROPERTY_ID and GA4_ACCESS_TOKEN. "
            f"Run in mock mode (set INTEGRATION_MODE=mock or {MOCK_MODE_ENV}=1) to test "
            "without credentials."
        )

    return Ga4RestAdapter(
        property_id=property_id,
        access_token=None if mock_mode else access_token,
        capabilities=capabilities,
        mock_mode=mock_mode,
        fixtures_dir=fixtures_dir,
    )


def _is_mock_mode(cfg):
    explicit = cfg.get("ga4.mock_mode")
    if explicit is not None:
        return bool(explicit)
    return resolve_mode(cfg.get("ga4.mode"), (MOCK_MODE_ENV,)) == "mock"
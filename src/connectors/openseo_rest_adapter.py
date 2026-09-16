import json
import os
import urllib.request
import urllib.error

OPENSEO_API_BASE_DEFAULT = "https://api.dataforseo.com"
MOCK_MODE_ENV = "OPENSEO_MOCK_MODE"

ENDPOINTS = {
    "keyword_volume": "v3/keywords_data/google/search_volume/live",
    "serp": "v3/serp/google/organic/live/regular",
    "competitors": "v3/dataforseo_labs/google/competitors_domain/live",
    "backlinks": "v3/backlinks/backlinks/live",
    "crawl_audit": "v3/on_page/summary",
}

MOCK_FIXTURES = {
    "keyword_volume": "dataforseo_keyword_volume.json",
    "serp": "dataforseo_serp.json",
    "competitors": "dataforseo_competitors.json",
    "backlinks": "dataforseo_backlinks.json",
    "crawl_audit": "dataforseo_crawl_audit.json",
}

OK_STATUS_CODES = (20000,)
HTTP_TIMEOUT_SECONDS = 60


class OpenseoError(Exception):
    pass


class OpenseoUnsupportedCapability(OpenseoError):
    pass


class OpenseoRestAdapter:
    """Single shared OpenSEO/REST adapter (plan/11 §2-§4).

    Implements supports()/fetch() only. fetch() never raises: provider, HTTP,
    and missing-mock-fixture failures return a typed error response that
    callers treat as "skip and log", never a crash.
    """

    def __init__(self, base_url=None, capabilities=None, credential=None,
                 mock_mode=None, fixtures_dir="tests/fixtures",
                 on_fetch_complete=None):
        self.base_url = (base_url or OPENSEO_API_BASE_DEFAULT).rstrip("/")
        self.credential = credential
        self.capabilities = list(capabilities) if capabilities else list(ENDPOINTS)
        self.fixtures_dir = fixtures_dir
        self._on_fetch_complete = on_fetch_complete

        env_mock = os.environ.get(MOCK_MODE_ENV, "").strip().lower() in ("1", "true", "yes", "on")
        self.mock_mode = bool(mock_mode) if mock_mode is not None else env_mock
        if self.mock_mode:
            print(
                "[openseo] MOCK MODE ENABLED - serving DataForSEO fixtures from "
                f"'{self.fixtures_dir}'; NO real DataForSEO network calls will be made.",
                flush=True,
            )

    def supports(self, capability):
        return capability in self.capabilities

    def fetch(self, capability, params):
        if not self.supports(capability):
            return {"ok": False, "capability": capability, "error": "unsupported_capability"}
        try:
            raw = self._call(capability, params)
            result = self._normalize(capability, raw, params)
            # Billing truth travels on every successful result: the provider's
            # per-task cost from the raw envelope (smoke test proved the field
            # is present and reliable: $0.09/search_volume, $0.002/serp).
            # log_cost() callers read it instead of logging cost=None.
            if result.get("ok"):
                result["cost"] = self._provider_cost(raw)
                if self._on_fetch_complete:
                    self._on_fetch_complete(capability, params, result)
            return result
        except OpenseoUnsupportedCapability as exc:
            return {"ok": False, "capability": capability, "error": "unsupported_capability"}
        except OpenseoError as exc:
            return {"ok": False, "capability": capability, "error": "provider_error", "detail": str(exc)}
        except Exception as exc:
            return {"ok": False, "capability": capability, "error": "provider_error", "detail": str(exc)}

    @staticmethod
    def _provider_cost(raw):
        """Extract the provider-reported cost from a raw DataForSEO envelope.
        Prefers the per-task cost; falls back to the envelope-level cost."""
        if not raw:
            return None
        for task in raw.get("tasks", []) or []:
            if isinstance(task, dict) and task.get("cost") is not None:
                return task["cost"]
        return raw.get("cost")

    def _call(self, capability, params):
        if capability not in ENDPOINTS:
            raise OpenseoUnsupportedCapability(capability)
        if self.mock_mode:
            return self._load_mock_fixture(capability)
        return self._post(capability, params)

    def _load_mock_fixture(self, capability):
        filename = MOCK_FIXTURES.get(capability)
        if not filename:
            raise OpenseoUnsupportedCapability(capability)
        path = os.path.join(self.fixtures_dir, filename)
        if not os.path.exists(path):
            raise OpenseoError(f"mock fixture not found: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("status_code") not in OK_STATUS_CODES:
            raise OpenseoError(payload.get("status_message", "fixture status_code not 20000"))
        return payload

    def _post(self, capability, params):
        url = f"{self.base_url}/{ENDPOINTS[capability]}"
        body = self._build_request_body(capability, params)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.credential:
            req.add_header("Authorization", f"Basic {self.credential}")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise OpenseoError(f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            raise OpenseoError(f"connection failed: {exc.reason}")
        if payload.get("status_code") not in OK_STATUS_CODES or payload.get("tasks_error", 0) > 0:
            raise OpenseoError(payload.get("status_message", "provider returned errors"))
        return payload

    def _build_request_body(self, capability, params):
        params = params or {}
        location_code = int(params.get("location_code", 2840))
        language_code = params.get("language_code", "en")
        if capability == "keyword_volume":
            keywords = params.get("keywords") or []
            return [{
                "keywords": keywords,
                "location_code": location_code,
                "language_code": language_code,
            }]
        if capability == "serp":
            return [{
                "keyword": params.get("query", ""),
                "location_code": location_code,
                "language_code": language_code,
                "depth": int(params.get("limit", 10)),
            }]
        if capability == "competitors":
            return [{
                "target": params.get("domain", ""),
                "location_code": location_code,
                "language_code": language_code,
                "limit": int(params.get("limit", 100)),
            }]
        if capability == "backlinks":
            urls = params.get("urls") or []
            target = urls[0] if urls else params.get("target", "")
            return [{
                "target": target,
                "mode": "as_is",
                "limit": int(params.get("limit", 100)),
            }]
        if capability == "crawl_audit":
            return [{
                "target": params.get("site", ""),
                "max_crawl_pages": int(params.get("limit", 100)),
            }]
        raise OpenseoUnsupportedCapability(capability)

    def _normalize(self, capability, raw, params):
        if capability == "keyword_volume":
            data = self._normalize_keyword_volume(raw, params)
        elif capability == "serp":
            data = self._normalize_serp(raw, params)
        elif capability == "competitors":
            data = self._normalize_competitors(raw, params)
        elif capability == "backlinks":
            data = self._normalize_backlinks(raw, params)
        elif capability == "crawl_audit":
            data = self._normalize_crawl_audit(raw, params)
        else:
            raise OpenseoUnsupportedCapability(capability)
        return {"ok": True, "data": data}

    def _result_items(self, raw):
        items = []
        for task in raw.get("tasks", []) or []:
            for result in task.get("result", []) or []:
                if isinstance(result, list):
                    items.extend(result)
                else:
                    items.append(result)
        return items

    def _normalize_keyword_volume(self, raw, params):
        normalized = []
        for item in self._result_items(raw):
            normalized.append({
                "keyword": item.get("keyword"),
                "search_volume": item.get("search_volume"),
                "date": params.get("date"),
                "competition": item.get("competition"),
                "cpc": item.get("cpc"),
            })
        return normalized

    def _normalize_serp(self, raw, params):
        normalized = []
        for result in self._result_items(raw):
            for serp_item in result.get("items", []) or []:
                if serp_item.get("type") != "organic":
                    continue
                # NOTE: the row carries "query" (§4.2 additive field, as-built):
                # sync_serp_snapshots() joins each row back to cluster_queries
                # via it, so the fixture-driven sync works without caller-side
                # branching. Consumers needing only §4.2's minimum four keys
                # are unaffected — it is an additive field like 4.1.1's.
                normalized.append({
                    "query": params.get("query"),
                    "position": serp_item.get("rank_absolute"),
                    "url": serp_item.get("url"),
                    "title": serp_item.get("title"),
                    "snippet": serp_item.get("description"),
                })
        return normalized

    def _normalize_competitors(self, raw, params):
        normalized = []
        target_domain = (params or {}).get("domain")
        for result in self._result_items(raw):
            for item in result.get("items", []) or []:
                domain = item.get("domain")
                if target_domain and domain == target_domain:
                    continue
                organic = (item.get("full_domain_metrics") or {}).get("organic") or {}
                normalized.append({
                    "domain": domain,
                    "overlap_score": None,
                    "ranking_keywords_count": organic.get("count"),
                })
        return normalized

    def _normalize_backlinks(self, raw, params):
        normalized = []
        for result in self._result_items(raw):
            for item in result.get("items", []) or []:
                first_seen = item.get("first_seen") or ""
                normalized.append({
                    "target_url": item.get("url_to"),
                    "source_url": item.get("url_from"),
                    "anchor": item.get("anchor"),
                    "first_seen": first_seen[:10] if first_seen else None,
                })
        return normalized

    def _normalize_crawl_audit(self, raw, params):
        normalized = []
        for item in self._result_items(raw):
            for page in (item.get("pages") or item.get("items") or []):
                normalized.append({
                    "url": page.get("url"),
                    "status_code": page.get("status_code"),
                    "indexable": page.get("indexable"),
                    "canonical": page.get("canonical"),
                    "page_type": page.get("page_type"),
                    "template": page.get("template"),
                    "crawl_depth": page.get("crawl_depth"),
                    "internal_links_in": page.get("internal_links_in"),
                    "internal_links_out": page.get("internal_links_out"),
                    "raw_html_hash": page.get("raw_html_hash"),
                    "rendered_html_hash": page.get("rendered_html_hash"),
                    "render_status": page.get("render_status"),
                    "structured_data": page.get("structured_data"),
                    "is_orphan": page.get("is_orphan"),
                })
        return normalized
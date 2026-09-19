import json
import os
import time
import urllib.request
import urllib.error

OPENSEO_API_BASE_DEFAULT = "https://api.dataforseo.com"
MOCK_MODE_ENV = "OPENSEO_MOCK_MODE"

ENDPOINTS = {
    "keyword_volume": "v3/keywords_data/google/search_volume/live",
    "serp": "v3/serp/google/organic/live/regular",
    "competitors": "v3/dataforseo_labs/google/competitors_domain/live",
    "backlinks": "v3/backlinks/backlinks/live",
    "crawl_audit": "v3/on_page/pages",
    "crawl_audit_task_post": "v3/on_page/task_post",
    "crawl_audit_tasks_ready": "v3/on_page/tasks_ready",
}

# on_page/pages is a two-phase endpoint: task_post starts an async crawl and
# returns a task id; tasks_ready polls readiness; pages pulls per-page rows.
# These budgets bound that live flow (unbounded polling costs money and wall
# time). Mock mode never touches them.
CRAWL_READY_ATTEMPTS = int(os.environ.get("CRAWL_READY_ATTEMPTS", "30"))
CRAWL_READY_INTERVAL_SECONDS = float(os.environ.get("CRAWL_READY_INTERVAL_SECONDS", "5"))
CRAWL_PAGE_SIZE = 100
CRAWL_PAGE_LIMIT = 20

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
        if capability == "crawl_audit":
            return self._crawl_audit_pages(params)
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

    def _raw_post(self, endpoint, body):
        url = f"{self.base_url}/{endpoint}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.credential:
            req.add_header("Authorization", f"Basic {self.credential}")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise OpenseoError(f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            raise OpenseoError(f"connection failed: {exc.reason}")

    def _raw_get(self, endpoint):
        url = f"{self.base_url}/{endpoint}"
        req = urllib.request.Request(url, method="GET")
        if self.credential:
            req.add_header("Authorization", f"Basic {self.credential}")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise OpenseoError(f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            raise OpenseoError(f"connection failed: {exc.reason}")

    def _crawl_audit_pages(self, params):
        """Two-phase on_page crawl → per-page rows.

        on_page/pages requires a task id produced by task_post, so live mode
        chains: task_post (target, max_crawl_pages) → poll tasks_ready for the
        task id → pull pages with limit/offset until short pages or cap.
        Returns a synthetic single-task envelope so _normalize_crawl_audit sees
        the same shape as the mock fixture.
        """
        body = self._build_request_body("crawl_audit", params)
        task_payload = self._raw_post(ENDPOINTS["crawl_audit_task_post"], body)
        if task_payload.get("status_code") not in OK_STATUS_CODES or task_payload.get("tasks_error", 0) > 0:
            raise OpenseoError(task_payload.get("status_message", "task_post failed"))
        task_id = None
        for task in task_payload.get("tasks", []) or []:
            if task.get("id"):
                task_id = task["id"]
                break
        if not task_id:
            raise OpenseoError("task_post returned no task id")

        ready = False
        for _ in range(CRAWL_READY_ATTEMPTS):
            ready_payload = self._raw_get(ENDPOINTS["crawl_audit_tasks_ready"])
            if ready_payload.get("status_code") not in OK_STATUS_CODES:
                raise OpenseoError(ready_payload.get("status_message", "tasks_ready failed"))
            if any(task.get("id") == task_id for task in ready_payload.get("tasks", []) or []):
                ready = True
                break
            time.sleep(CRAWL_READY_INTERVAL_SECONDS)
        if not ready:
            raise OpenseoError(f"on_page task {task_id} not ready within "
                               f"{CRAWL_READY_ATTEMPTS} polls")

        items = []
        offset = 0
        max_crawl_pages = int(params.get("limit", CRAWL_PAGE_LIMIT))
        fetched = 0
        while len(items) < max_crawl_pages and fetched < CRAWL_PAGE_LIMIT:
            page_body = [{
                "id": task_id,
                "limit": min(CRAWL_PAGE_SIZE, max_crawl_pages - len(items)),
                "offset": offset,
            }]
            page = self._raw_post(ENDPOINTS["crawl_audit"], page_body)
            if page.get("status_code") not in OK_STATUS_CODES or page.get("tasks_error", 0) > 0:
                raise OpenseoError(page.get("status_message", "pages pull failed"))
            result = (page.get("tasks") or [])
            batch = []
            for task in result:
                for res in task.get("result", []) or []:
                    if isinstance(res, list):
                        batch.extend(res)
                    else:
                        batch.extend(res.get("items", []) or [])
            items.extend(batch)
            fetched += 1
            if not batch:
                break
            offset += len(batch)
        if fetched >= CRAWL_PAGE_LIMIT and len(items) >= max_crawl_pages:
            print(
                f"[openseo] crawl_audit: reached {max_crawl_pages} page cap — "
                "results may be truncated (raise limit or CRAWL_PAGE_LIMIT)",
                flush=True,
            )
        return {
            "status_code": 20000,
            "tasks": [{"id": task_id, "result": [{"items": items}],
                       "cost": task_payload.get("cost")}],
        }

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
                "max_crawl_pages": int(params.get("limit", CRAWL_PAGE_LIMIT)),
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
            # Mock fixture shape: result[].pages[] (top-level keys).
            # Live on_page/pages shape: result[].items[] with per-page
            # status_code/url + nested meta.canonical and link counts.
            pages = item.get("pages") or item.get("items") or []
            for page in pages:
                meta = page.get("meta") or {}
                status_code = page.get("status_code") or meta.get("status_code")
                indexable = page.get("indexable")
                if indexable is None:
                    indexable = status_code == 200
                canonical = page.get("canonical") or meta.get("canonical")
                if isinstance(canonical, dict):
                    canonical = canonical.get("value") or canonical.get("href")
                normalized.append({
                    "url": page.get("url"),
                    "status_code": status_code,
                    "indexable": indexable,
                    "canonical": canonical,
                    "page_type": page.get("page_type") or meta.get("page_type"),
                    "template": page.get("template"),
                    "crawl_depth": page.get("crawl_depth"),
                    "internal_links_in": page.get("internal_links_in")
                        if page.get("internal_links_in") is not None
                        else meta.get("internal_links_count"),
                    "internal_links_out": page.get("internal_links_out")
                        if page.get("internal_links_out") is not None
                        else meta.get("external_links_count"),
                    "raw_html_hash": page.get("raw_html_hash"),
                    "rendered_html_hash": page.get("rendered_html_hash"),
                    "render_status": page.get("render_status"),
                    "structured_data": page.get("structured_data")
                        if page.get("structured_data") is not None
                        else (1 if (meta.get("structured_data") or meta.get("has_structured_data")) else 0),
                    "is_orphan": page.get("is_orphan"),
                })
        return normalized
"""Framer connector (plan/26 Phase 3): the thin write surface for
framer.title / framer.description fix adapters.

Contract mirrors the connector family (supports()/fetch()-style never-raise
typed results; same shape as ShopifyGraphQLClient.run):

  read_page_meta(page_ref)  -> {ok, data: {page_ref, title, description,
                              site_ref}, ...}   — the snapshot source
  update_page_meta(page_ref, title=None, description=None)
                            -> {ok, ...}                       — the write
  probe_auth()              -> {ok, capabilities} or typed error

Modes (central INTEGRATION_MODE switch, connectors/mode.py):
  * mock (default)     — fixtures from tests/fixtures/framer_pages.json,
                         zero network. The adapter layer runs in CI/mock
                         exactly like the Shopify mock path.
  * live               — REAL calls ONLY behind the explicit capability
                         flag FRAMER_LIVE_WRITES=1: the public write
                         surface is UNVERIFIED (plan/26 open question #1).
                         Without the flag, live mode degrades to a typed
                         "live_writes_disabled" failure — fail-closed, no
                         accidental writes against an unverified API.

Credentials: framer_api_key + framer_site_ref from
connectors.write_credentials.resolve_write_credentials (Phase 0).
Values are read but never logged.
"""

import json
import os
import urllib.error
import urllib.request

from .mode import resolve_mode

FRAMER_MOCK_MODE_ENV = "FRAMER_MOCK_MODE"
FRAMER_LIVE_WRITES_ENV = "FRAMER_LIVE_WRITES"

DEFAULT_CAPABILITIES = ["page_meta_read", "page_meta_write"]
HTTP_TIMEOUT_SECONDS = 30


class FramerError(Exception):
    pass


class FramerClient:
    """Framer page-meta read/write; never-raise typed results.

    The URL->page resolution strategy (plan/26 open question #2): the page
    ref IS the canonical URL; read_page_meta/update_page_meta accept it
    verbatim and the backend resolves the live page. The client never
    mints or caches its own ids.
    """

    def __init__(self, framer_api_key=None, framer_site_ref=None,
                 api_base=None, mode=None, fixtures_dir=None,
                 live_writes=None, timeout=HTTP_TIMEOUT_SECONDS):
        self.framer_api_key = framer_api_key
        self.framer_site_ref = framer_site_ref
        self.api_base = (api_base or os.environ.get("FRAMER_API_BASE")
                         or "https://api.framer.com")
        self.fixtures_dir = fixtures_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "tests", "fixtures")
        env_mock = os.environ.get(FRAMER_MOCK_MODE_ENV, "").strip().lower() \
            in ("1", "true", "yes", "on")
        self.mode = mode if mode is not None else resolve_mode(
            None, (FRAMER_MOCK_MODE_ENV,))
        self.mock_mode = self.mode == "mock" or env_mock
        if live_writes is None:
            live_writes = os.environ.get(FRAMER_LIVE_WRITES_ENV, "").strip() \
                .lower() in ("1", "true", "yes", "on")
        self.live_writes = bool(live_writes)
        self.timeout = timeout

    # ------------------------------------------------------------
    # The adapter-facing surface
    # ------------------------------------------------------------

    def read_page_meta(self, page_ref):
        """One page's live meta state -> {ok, data:{page_ref,title,
        description, site_ref}} (typed; never raises)."""
        if self.mock_mode:
            page = self._fixture_page(page_ref)
            if page is None:
                return {"ok": False, "outcome": "not_found",
                        "detail": f"fixture has no page for {page_ref!r}"}
            return {"ok": True, "outcome": "ok",
                    "data": {"page_ref": page_ref,
                             "title": page.get("title"),
                             "description": page.get("description"),
                             "site_ref": self.framer_site_ref}}
        if not self._live_allowed():
            return {"ok": False, "outcome": "live_writes_disabled",
                    "detail": ("Framer live surface unverified (plan/26 "
                               "open question #1) — set FRAMER_LIVE_WRITES=1 "
                               "only after endpoint verification")}
        return self._live_call("GET", page_ref)

    def update_page_meta(self, page_ref, title=None, description=None):
        """Write one page's meta fields (sibling-preserving semantics live
        with the CALLER — this method writes exactly what it is given,
        both fields, like productUpdate.seo)."""
        if self.mock_mode:
            page = self._fixture_page(page_ref)
            if page is None:
                return {"ok": False, "outcome": "not_found",
                        "detail": f"fixture has no page for {page_ref!r}"}
            new = dict(page)
            # WHOLE-OBJECT semantics: the ADAPTER always passes both fields
            # explicitly (sibling discipline), so the mock writes exactly
            # what it receives — including None (clears the field, which is
            # what makes an empty-meta pre-state restorable).
            new["title"] = title
            new["description"] = description
            self._write_fixture_page(page_ref, new)
            return {"ok": True, "outcome": "ok",
                    "data": {"page_ref": page_ref,
                             "title": new.get("title"),
                             "description": new.get("description")}}
        if not self._live_allowed():
            return {"ok": False, "outcome": "live_writes_disabled",
                    "detail": "Framer live writes are flag-gated "
                              "(FRAMER_LIVE_WRITES=1) and unverified"}
        return self._live_call("POST", page_ref,
                               title=title, description=description)

    def probe_auth(self):
        """Executor startup probe (mirrors ShopifyGraphQLClient.probe_scopes).

        Mock mode: fixture-backed ok. Live: flag-gated honest failure (the
        surface is unverified; the policy gate is the only enforcement)."""
        if self.mock_mode:
            fixture = self._load_fixture()
            return {"ok": True, "mode": "mock",
                    "capabilities": ["page_meta_read", "page_meta_write"],
                    "site_ref": self.framer_site_ref}
        if not (self.framer_api_key and self.framer_site_ref):
            return {"ok": False, "error": "no_credentials",
                    "detail": "probe needs FRAMER_API_KEY + "
                              "site_config.framer_site_ref"}
        return {"ok": False, "error": "live_probe_unverified",
                "detail": "Framer live auth probe unverified (plan/26 "
                          "open question #1) — no network call made"}

    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------

    def _live_allowed(self):
        return self.mode == "live" and self.live_writes

    def _live_call(self, verb, page_ref, title=None, description=None):
        """The live HTTP surface. INTENTIONALLY UNVERIFIED: raises nothing,
        returns a typed refusal until the write API is verified (plan/26
        open question #1). Never a guessed endpoint."""
        return {"ok": False, "outcome": "live_writes_disabled",
                "detail": "Framer live endpoint not verified — refusing to "
                          "guess the API shape (verify, then implement)"}

    def _load_fixture(self):
        path = os.path.join(self.fixtures_dir, "framer_pages.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _fixture_pages(self):
        fixture = self._load_fixture()
        if isinstance(fixture, dict) and isinstance(fixture.get("pages"), dict):
            return fixture["pages"]
        return {}

    def _fixture_page(self, page_ref):
        pages = self._fixture_pages()
        # tolerate trailing-slash drift between the sitemap form and the
        # canonical form (the ref is a URL; the fixture keys are URLs)
        page = pages.get(page_ref)
        if page is None and page_ref is not None:
            page = pages.get(page_ref.rstrip("/") or page_ref)
        return page

    def _write_fixture_page(self, page_ref, new_page):
        path = os.path.join(self.fixtures_dir, "framer_pages.json")
        fixture = self._load_fixture() or {}
        if not isinstance(fixture, dict):
            fixture = {}
        pages = fixture.setdefault("pages", {})
        pages[page_ref] = new_page
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(fixture, fh, indent=2, ensure_ascii=False)


def api_base_default():
    return os.environ.get("FRAMER_API_BASE") or "https://api.framer.com"
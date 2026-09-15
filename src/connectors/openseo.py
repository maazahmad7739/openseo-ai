import json
import os

from .openseo_rest_adapter import OpenseoRestAdapter, MOCK_MODE_ENV
from .mode import resolve_mode


OPENSEO_SECRET_VALUE_ENV = "OPENSEO_SECRET_VALUE"
DEFAULT_BASE_URL = "https://api.dataforseo.com"
DEFAULT_CAPABILITIES = ["keyword_volume", "serp", "competitors", "backlinks", "crawl_audit"]
DEFAULT_SECRET_REF = "openseo/company/credential"
DEFAULT_FIXTURES_DIR = "tests/fixtures"


class OpenseoConfigError(Exception):
    pass


def get_openseo_adapter(config=None, secret_value=None):
    """Resolve the ONE global credential and return the REST adapter.

    This is the only code path allowed to resolve the shared OpenSEO secret
    (plan/11 §3 + §7). Mock mode (OPENSEO_MOCK_MODE=1) skips credential
    resolution entirely and never touches the secrets store.
    """
    cfg = config or {}
    mock_mode = _is_mock_mode(cfg)
    base_url = cfg.get("openseo.base_url") or _env("OPENSEO_BASE_URL") or DEFAULT_BASE_URL
    capabilities = _parse_capabilities(
        cfg.get("openseo.capabilities") or _env("OPENSEO_CAPABILITIES") or DEFAULT_CAPABILITIES
    )
    secret_ref = cfg.get("openseo.secret_ref") or _env("OPENSEO_SECRET_REF") or DEFAULT_SECRET_REF
    fixtures_dir = cfg.get("openseo.mock_fixtures_dir") or _env("OPENSEO_MOCK_FIXTURES_DIR") or DEFAULT_FIXTURES_DIR

    credential = None
    if not mock_mode:
        credential = _resolve_credential(secret_ref, secret_value)

    return OpenseoRestAdapter(
        base_url=base_url,
        capabilities=capabilities,
        credential=credential,
        mock_mode=mock_mode,
        fixtures_dir=fixtures_dir,
    )


def _is_mock_mode(cfg):
    explicit = cfg.get("openseo.mock_mode")
    if explicit is not None:
        return bool(explicit)
    return resolve_mode(cfg.get("openseo.mode"), (MOCK_MODE_ENV,)) == "mock"


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


def _resolve_credential(secret_ref, secret_value=None):
    if secret_value is not None:
        return secret_value
    value = _env(OPENSEO_SECRET_VALUE_ENV)
    if value:
        return value
    raise OpenseoConfigError(
        f"openseo.secret_ref '{secret_ref}' could not be resolved: no secrets "
        f"manager configured and {OPENSEO_SECRET_VALUE_ENV} is not set. "
        f"Run in mock mode (set {MOCK_MODE_ENV}=1) to test without a real credential."
    )
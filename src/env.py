"""Load .env into process environment (KEY=VALUE lines; comments ignored).

Policy (fix 3.5):
  - Existing process env vars always win over .env values, so shell overrides
    work. Values are never printed or logged.
  - A missing .env file logs a notice (not silent) but is not an error —
    env vars may legitimately come from the shell/service environment.
  - include_secrets=False (default for offline/mock entry points) skips keys
    matching *_API_KEY / *_SECRET / *TOKEN* so live credentials never enter
    the process environment of a mock run. Agent entry points load with
    include_secrets=True.
"""

import os

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
ENV_PATH_FALLBACKS = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "infra", "open-seo", ".env"),
]

SECRET_MARKERS = ("API_KEY", "SECRET", "TOKEN")


def _is_secret_key(key):
    upper = key.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def load_env_file(path=None, include_secrets=True, quiet=False):
    target = path
    if target is None:
        candidates = [ENV_PATH] + ENV_PATH_FALLBACKS
    else:
        candidates = [path]
    for candidate in candidates:
        if os.path.exists(candidate):
            return _read(candidate, include_secrets)
    if not quiet:
        print(
            "[env] no .env file found (checked project root and fallbacks); "
            "continuing with the existing process environment only.",
            flush=True,
        )
    return 0


def _read(path, include_secrets):
    loaded = 0
    skipped_secrets = 0
    with open(path, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key or key in os.environ:
                continue
            if not include_secrets and _is_secret_key(key):
                skipped_secrets += 1
                continue
            os.environ[key] = value
            loaded += 1
    if skipped_secrets and os.environ.get("INTEGRATION_MODE", "mock") == "mock":
        print(
            f"[env] skipped {skipped_secrets} credential-like key(s) from '{path}' "
            "(mock mode: live secrets stay out of the process environment).",
            flush=True,
        )
    return loaded
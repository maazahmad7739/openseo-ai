"""Load .env into process environment (KEY=VALUE lines; comments ignored).

Policy (fix 3.5):
  - Existing process env vars always win over .env values, so shell overrides
    work. Values are never printed or logged.
  - A missing .env file logs a notice (not silent) but is not an error —
    env vars may legitimately come from the shell/service environment.
  - include_secrets=False skips keys matching SECRET_MARKERS so live
    credentials never enter the process environment of a mock run.
    OPENSEO_LOAD_SECRETS (0/1) overrides it centrally, so callers that never
    pass the flag are still hermetic; agent entry points run with it = 1.
  - include_secrets governs .env only. Keys already exported into the shell
    are untouched unless scrub_environment=True, or the caller scrubs the
    child environment itself via scrub_secret_env().
"""

import os

# Central secrets switch. Unset means "use the caller's include_secrets".
# Set to 0 for a hermetic/mock process; 1 to explicitly opt into live creds.
SECRETS_OPT_IN_ENV = "OPENSEO_LOAD_SECRETS"
_TRUTHY = ("1", "TRUE", "YES", "ON")

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
ENV_PATH_FALLBACKS = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "infra", "open-seo", ".env"),
]

# Markers identify credential-shaped keys. Deliberately broad: a mock run is
# only hermetic if EVERY credential is excluded, and the cost of a miss is a
# live network call to a provider or a live API. PASSWORD and
# SERVICE_ACCOUNT_KEY were added after GSC_SERVICE_ACCOUNT_KEY_JSON and
# DATAFORSEO_PASSWORD were found riding along in .env unfiltered.
SECRET_MARKERS = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PRIVATE_KEY",
    "SERVICE_ACCOUNT_KEY",
    "CREDENTIAL",
)


def _is_secret_key(key):
    upper = key.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def secret_env_names(environ=None):
    """Names in `environ` (default os.environ) that look like credentials.

    Excludes SECRETS_OPT_IN_ENV itself: it contains "SECRET" so it matches,
    but scrubbing the switch would delete the very control that stops
    loading, silently reverting to include_secrets=True. Callers that scrub
    must not be able to disarm themselves.
    """
    source = os.environ if environ is None else environ
    return [key for key in source
            if key != SECRETS_OPT_IN_ENV and _is_secret_key(key)]


def scrub_secret_env(environ=None):
    """Remove credential-shaped keys from a mapping and return the names.

    Takes an explicit mapping rather than only touching os.environ so callers
    that own a throwaway copy (the test runner builds one per subprocess) can
    get a hermetic child without mutating the developer's own shell. Deleting
    from os.environ is opt-in via load_env_file(scrub_environment=True),
    because silently unsetting a variable a caller exported is surprising.
    """
    target = os.environ if environ is None else environ
    removed = secret_env_names(target)
    for key in removed:
        target.pop(key, None)
    return removed


def load_env_file(path=None, include_secrets=True, quiet=False,
                  scrub_environment=False):
    # Central switch. Checked first and it OVERRIDES the include_secrets
    # default, so entry points that never pass the flag are still hermetic.
    # That is the whole point: the mock-mode guarantee must not depend on
    # every one of ~46 call sites remembering to pass an argument. A prior
    # hang came from exactly that — drafting built a live OllamaClient
    # because no caller opted out and a key was present.
    override = os.environ.get(SECRETS_OPT_IN_ENV)
    if override is not None:
        include_secrets = override.strip().lower() in _TRUTHY
    if scrub_environment:
        scrub_secret_env()
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
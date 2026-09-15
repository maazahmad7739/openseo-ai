"""Central integration-mode switch for all third-party connectors.

INTEGRATION_MODE is the ONE switch that governs whether any connector talks
to a live external service or serves offline mock fixtures. Live credentials
are optional and only consulted when mode == "live". No connector code needs
to change when switching: the facades resolve mode here, then build their
adapters accordingly.

Resolution order (first non-empty wins):
  1. explicit config value passed by the caller (per-call override)
  2. INTEGRATION_MODE environment variable
  3. "mock" (safe default: the application NEVER touches the network unless
     a live mode is requested explicitly, per connector)
"""

import os

MODE_ENV = "INTEGRATION_MODE"
VALID_MODES = ("mock", "live")

DEFAULT_MODE = "mock"


class ModeError(Exception):
    pass


def resolve_mode(config_value=None, env_names=None):
    """Resolve the integration mode from config value, env, or default.

    env_names is an optional tuple of legacy per-connector env vars (e.g.
    OPENSEO_MOCK_MODE / GSC_MOCK_MODE) kept so the existing per-connector
    switches keep working during the migration to the central switch.
    """
    if config_value is not None:
        mode = str(config_value).strip().lower()
        if mode in VALID_MODES:
            return mode
        raise ModeError(
            f"invalid integration mode '{config_value}': expected one of {VALID_MODES}"
        )

    central = os.environ.get(MODE_ENV, "").strip().lower()
    if central in VALID_MODES:
        return central
    if central:
        raise ModeError(
            f"invalid {MODE_ENV} '{os.environ.get(MODE_ENV)}': expected one of {VALID_MODES}"
        )

    if env_names:
        for name in env_names:
            value = os.environ.get(name, "").strip().lower()
            if value in ("1", "true", "yes", "on"):
                return "mock"
            if value in ("0", "false", "no", "off"):
                return "live"

    return DEFAULT_MODE


def is_mock(mode=None, config_value=None, env_names=None):
    return resolve_mode(config_value, env_names) == "mock"


def describe(mode):
    if mode == "live":
        return "LIVE - real third-party API calls (requires credentials)"
    return "MOCK - offline fixtures, zero external network calls"
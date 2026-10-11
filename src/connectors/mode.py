"""Central integration-mode switch for all third-party connectors.

The execution mode governs whether any connector talks to a live external
service or serves offline mock fixtures. Live credentials are optional and
only consulted when mode == "live". No connector code needs to change when
switching: the facades resolve mode here, then build their adapters
accordingly.

Resolution order (first non-empty wins):
  1. explicit config value passed by the caller (per-call override)
  2. site_config.integration_mode for the target site (per-tenant, plan/31)
  3. INTEGRATION_MODE environment variable
  4. "mock" (safe default: the application NEVER touches the network unless
     a live mode is requested explicitly, per site)

Site modes (site_config.integration_mode):
  mock    — offline fixtures, zero external network calls (Aurora)
  sandbox — real API calls against a dev/test store (ActionSEO Dev)
  import  — no connector traffic at all; data arrives via operator file
            imports (Helium's GSC PDF export bridge). Treated as offline
            for adapters; syncs skip connector fetches instead of pulling
            fixtures, so imported rows are never overwritten by mocks.
  live    — real API calls against production targets (Kinetic Growth)
"""

import os

MODE_ENV = "INTEGRATION_MODE"
VALID_MODES = ("mock", "sandbox", "import", "live")
# Adapter-compatible modes: 'sandbox' and 'live' both drive real API calls;
# 'sandbox' sites are just live mode against test credentials. 'import' is
# offline-only: no adapter fetches for it (sync skips connector steps).
ADAPTER_LIVE_MODES = ("sandbox", "live")
OFFLINE_MODES = ("mock", "import")

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


def resolve_site_mode(conn, site_id, config_value=None, env_names=None):
    """Resolve the mode for ONE tenant (plan/31): site_config first.

    Hierarchy: explicit config_value > site_config.integration_mode >
    INTEGRATION_MODE env (incl. legacy per-connector envs) > 'mock'.

    conn/site_id may be None (tests, mode-less callers) — resolution then
    degrades to the legacy env/global path unchanged.
    """
    if config_value is not None:
        return resolve_mode(config_value, env_names)
    if conn is not None and site_id is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT integration_mode FROM site_config WHERE site_id = %s",
                (site_id,),
            )
            row = cur.fetchone()
        if row and row[0]:
            mode = str(row[0]).strip().lower()
            if mode in VALID_MODES:
                return mode
            raise ModeError(
                f"invalid site_config.integration_mode '{row[0]}' for site "
                f"{site_id}: expected one of {VALID_MODES}"
            )
    return resolve_mode(None, env_names)


def is_mock(mode=None, config_value=None, env_names=None):
    return resolve_mode(config_value, env_names) in OFFLINE_MODES


def adapter_mode(site_mode):
    """Map a site mode to the connector adapter's mock/live polarity.

    mock/import -> mock (no adapter traffic; import sites skip fetches)
    sandbox/live -> live (real API calls; sandbox = test credentials)
    """
    if site_mode in OFFLINE_MODES:
        return "mock"
    if site_mode in ADAPTER_LIVE_MODES:
        return "live"
    raise ModeError(f"unknown site mode '{site_mode}'")


def describe(mode):
    if mode == "live":
        return "LIVE - real third-party API calls (requires credentials)"
    if mode == "sandbox":
        return "SANDBOX - real API calls against a dev/test store"
    if mode == "import":
        return "IMPORT - connector traffic disabled; operator-imported data only"
    return "MOCK - offline fixtures, zero external network calls"
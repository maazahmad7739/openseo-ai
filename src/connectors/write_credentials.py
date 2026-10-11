"""Multi-connector write-credential resolver (plan/26 Phase 0).

ONE dispatch point for "which write platform does this site use, and what
credentials does it need": the executor and API routes resolve write
config here instead of hardcoding Shopify resolution.

Contract:
  * resolve_write_credentials(conn, site_id) -> typed dict, NEVER raises on
    a missing credential (fail-closed is the CALLER's refusal, not an
    exception here — the executor's existing no-credential refusal branch
    consumes the ok=False shape).
  * platform='shopify' (the DEFAULT for every pre-existing site) returns
    EXACTLY the shape _resolve_shop_config always returned — Shopify
    execution stays byte-identical.
  * platform='framer' returns a Framer credential dict (env FRAMER_API_KEY
    + site_config.framer_site_ref + system_config framer.api_base).
  * platform='netlify' (plan/27) returns a GitHub-PR credential dict
    (env GITHUB_TOKEN + site_config.github_repo/github_branch/
    github_root_dir + system_config github.api_base). Writes are delivered
    as pull requests against github_branch — never live API overwrites.

Credentials are read but never logged: error details name the MISSING
env key, never the value.
"""

import os

VALID_PLATFORMS = ("shopify", "framer", "netlify")

FRAMER_API_KEY_ENV = "FRAMER_API_KEY"
FRAMER_API_BASE_CONFIG_KEY = "framer.api_base"
FRAMER_API_BASE_DEFAULT = "https://api.framer.com"

GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
GITHUB_API_BASE_CONFIG_KEY = "github.api_base"
GITHUB_API_BASE_DEFAULT = "https://api.github.com"

SHOPIFY_API_VERSION_CONFIG_KEY = "shopify.api_version"
SHOPIFY_API_VERSION_DEFAULT = "2026-01"


class CredentialResolutionError(Exception):
    """Raised only for programming errors (unknown platform value in the
    DB); missing credentials are a typed ok=False result, never this."""


def _normalize_shop_domain(domain):
    """site_config may store the bare handle ('my-store'); GraphQL needs
    the myshopify.com FQDN. Identical to the executor's original rule."""
    if not domain:
        return domain
    if "." in domain:
        return domain
    return f"{domain}.myshopify.com"


def _read_site_row(conn, site_id):
    """(platform, shopify_domain, framer_site_ref, github_repo,
    github_branch, github_root_dir) for the site.

    Legacy deployments whose site_config predates plan/26 (no platform
    column) resolve as shopify — the pre-Framer default. plan/27 columns
    are tolerated as missing on legacy deployments the same way."""
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT platform, shopify_domain, framer_site_ref, "
                "github_repo, github_branch, github_root_dir "
                "FROM site_config WHERE site_id = %s",
                (site_id,),
            )
        except Exception:
            conn.rollback()
            cur.execute(
                "SELECT 'shopify', shopify_domain, NULL, NULL, NULL, NULL "
                "FROM site_config WHERE site_id = %s",
                (site_id,),
            )
        row = cur.fetchone()
    if not row:
        raise CredentialResolutionError(f"site_config row not found: {site_id}")
    platform = (row[0] or "shopify") if isinstance(row[0], str) else "shopify"
    return platform, row[1], row[2], row[3], row[4], row[5]


def _config_value(conn, config_key, default=None):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT config_value FROM system_config WHERE config_key = %s",
            (config_key,),
        )
        row = cur.fetchone()
    return (row[0] if row and row[0] else default)


def _resolve_shopify(conn, shopify_domain, api_version=None, site_id=None):
    """The EXACT shopify credential dict the executor always resolved
    (shape and key order preserved — byte-identical consumers).

    plan/31: the access token is PER-SITE when site_config carries one
    (shopify_access_token column — per-tenant secret), falling back to the
    shared SHOPIFY_ACCESS_TOKEN env for legacy single-tenant deployments.
    """
    import os
    shop_domain = _normalize_shop_domain(
        shopify_domain or os.environ.get("SHOPIFY_DOMAIN"))
    if api_version is None:
        api_version = _config_value(
            conn, SHOPIFY_API_VERSION_CONFIG_KEY) or SHOPIFY_API_VERSION_DEFAULT
    access_token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
    if site_id is not None:
        per_site_token = _site_access_token(conn, site_id)
        if per_site_token:
            access_token = per_site_token
    return {
        "platform": "shopify",
        "shop_domain": shop_domain,
        "access_token": access_token,
        "api_version": api_version,
    }


def _site_access_token(conn, site_id):
    """site_config.shopify_access_token (per-tenant, plan/31).

    Tolerates legacy deployments whose site_config predates the column.
    The value is read but never logged.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT shopify_access_token FROM site_config WHERE site_id = %s",
                (site_id,),
            )
            row = cur.fetchone()
    except Exception:
        conn.rollback()
        return None
    return (row[0] or None) if row else None


def _resolve_framer(conn, framer_site_ref):
    """Framer credential dict: env key + site ref + non-sensitive base."""
    import os
    api_key = os.environ.get(FRAMER_API_KEY_ENV)
    api_base = _config_value(conn, FRAMER_API_BASE_CONFIG_KEY) \
        or FRAMER_API_BASE_DEFAULT
    return {
        "platform": "framer",
        "framer_api_key": api_key,
        "framer_site_ref": framer_site_ref,
        "api_base": api_base,
    }


def _resolve_netlify(conn, github_repo, github_branch, github_root_dir):
    """Netlify/static-site credential dict: env PAT + repo coordinates +
    non-sensitive base. The token NEVER rides into logs (detail names the
    env key, never the value)."""
    import os
    api_base = _config_value(conn, GITHUB_API_BASE_CONFIG_KEY) \
        or GITHUB_API_BASE_DEFAULT
    return {
        "platform": "netlify",
        "github_token": os.environ.get(GITHUB_TOKEN_ENV),
        "github_repo": github_repo,
        "github_branch": github_branch or "main",
        "github_root_dir": github_root_dir or "",
        "api_base": api_base,
    }


def _credentials_ok(platform, config):
    """Fail-closed check: the platform's required keys are present."""
    if platform == "shopify":
        return bool(config.get("shop_domain") and config.get("access_token"))
    if platform == "framer":
        return bool(config.get("framer_api_key") and config.get("framer_site_ref"))
    if platform == "netlify":
        return bool(config.get("github_token") and config.get("github_repo"))
    return False


def resolve_write_credentials(conn, site_id, api_version=None):
    """The ONE write-credential dispatch point (plan/26 Phase 0).

    Returns:
      {ok: True, platform, config, detail: None}        — ready to write
      {ok: False, platform, config, outcome, detail}    — fail-closed

    outcome values:
      site_not_found        — no site_config row for site_id
      unknown_platform      — platform value outside the known set
      no_credentials        — the platform's required keys are missing
      shopify_domain_missing— shopify site with no resolvable shop_domain
    detail names the missing ENV KEY (never a value). The 'config' key is
    present in EVERY return shape so the executor can log platform + base
    safely and the adapter layer can still build a mock client for dry
    runs against a partially-configured site.
    """
    try:
        (platform, shopify_domain, framer_site_ref,
         github_repo, github_branch, github_root_dir) = _read_site_row(
            conn, site_id)
    except CredentialResolutionError as exc:
        return {"ok": False, "platform": None, "config": None,
                "outcome": "site_not_found", "detail": str(exc)}

    if platform not in VALID_PLATFORMS:
        return {"ok": False, "platform": platform, "config": None,
                "outcome": "unknown_platform",
                "detail": f"site platform {platform!r} has no write path"}

    if platform == "shopify":
        config = _resolve_shopify(conn, shopify_domain, api_version=api_version,
                                  site_id=site_id)
        if not _credentials_ok("shopify", config):
            missing = "SHOPIFY_DOMAIN/SHOPIFY_ACCESS_TOKEN"
            if not config.get("shop_domain"):
                missing = "SHOPIFY_DOMAIN (or site_config.shopify_domain)"
            elif not config.get("access_token"):
                missing = ("SHOPIFY_ACCESS_TOKEN (env, or per-site "
                           "site_config.shopify_access_token)")
            return {"ok": False, "platform": "shopify", "config": config,
                    "outcome": "no_credentials",
                    "detail": f"missing {missing}"}
        return {"ok": True, "platform": "shopify", "config": config,
                "detail": None}

    if platform == "netlify":
        return _resolve_and_check_netlify(conn, github_repo, github_branch,
                                          github_root_dir)

    # platform == 'framer'
    config = _resolve_framer(conn, framer_site_ref)
    if not _credentials_ok("framer", config):
        missing = []
        import os
        if not os.environ.get(FRAMER_API_KEY_ENV):
            missing.append(FRAMER_API_KEY_ENV)
        if not config.get("framer_site_ref"):
            missing.append("site_config.framer_site_ref")
        return {"ok": False, "platform": "framer", "config": config,
                "outcome": "no_credentials",
                "detail": f"missing {', '.join(missing)}"}
    return {"ok": True, "platform": "framer", "config": config, "detail": None}


def _resolve_and_check_netlify(conn, github_repo, github_branch,
                               github_root_dir):
    config = _resolve_netlify(conn, github_repo, github_branch,
                              github_root_dir)
    if not _credentials_ok("netlify", config):
        missing = []
        import os
        if not os.environ.get(GITHUB_TOKEN_ENV):
            missing.append(GITHUB_TOKEN_ENV)
        if not config.get("github_repo"):
            missing.append("site_config.github_repo")
        return {"ok": False, "platform": "netlify", "config": config,
                "outcome": "no_credentials",
                "detail": f"missing {', '.join(missing)}"}
    return {"ok": True, "platform": "netlify", "config": config,
            "detail": None}
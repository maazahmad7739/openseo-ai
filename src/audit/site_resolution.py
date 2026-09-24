"""Site/domain resolution for the on-demand audit engine (plan/23 §2.1).

Determines whether an arbitrary URL belongs to a CONNECTED store (a
registered site_config row) or falls into the brand-agnostic
audit_checklist mode.

Resolution order (plan/23 §2.1):
  1. site_config.domain      == registrable_domain(url)  -> connected
     (match_basis 'domain')
  2. site_config.shopify_domain == registrable_domain(url) OR the URL
     host itself is *.myshopify.com                    -> connected
     (match_basis 'shopify_domain' / 'myshopify_host')
  3. no match                                           -> audit_checklist
     (match_basis 'none')

Normalizations:
  * leading www. stripped before comparing
  * port/scheme dropped (comparison is host-only, via canonicalize_url's
    host handling)
  * myshopify.com handles compare case-insensitively
  * a site_config domain stored as FQDN ("example.com") matches a URL on
    any of its subdomains? NO — v1 compares registrable domains exactly:
    "shop.example.com" does NOT match site_config.domain "example.com"
    unless the URL's registrable form IS the registered form (documented
    honest limit; subdomain matching is v2).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connectors.url_normalize import canonicalize_url  # noqa: E402

VALID_MODES = ("audit_checklist", "connected")
VALID_MATCH_BASIS = ("domain", "shopify_domain", "myshopify_host", "none")

MYSHOPIFY_SUFFIX = ".myshopify.com"


def extract_host(url):
    """Hostname (lowercased, www. stripped) or None for non-URLs.

    Tolerates scheme-less input ('example.com/page') by assuming https —
    the audit entry point accepts pasted URLs that way.
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        url = f"https://{url}"
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def extract_domain(url):
    """Registrable domain of a URL (host minus www.; v1 does NOT strip
    public suffixes — 'example.com' and 'example.co.uk' are compared as
    full hosts; documented honest limit).

    Returns None for non-URLs / IP literals / localhost (an IP or
    'localhost' is never a registrable brand domain).
    """
    host = extract_host(url)
    if not host:
        return None
    # IP literals are never brand domains
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost"):
        return None
    return host


def normalize_domain_for_match(domain):
    """site_config side: 'www.example.com' stored by an operator still
    matches a URL on 'example.com'."""
    if not domain:
        return None
    domain = domain.strip().lower()
    if not domain:
        return None
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.rstrip(".") or None


def resolve_site(conn, url):
    """The §2.1 typed resolution result against the real site_config table.

    Returns {"mode", "site_id", "site_name", "match_basis", "domain"}.
    mode 'audit_checklist' iff match_basis 'none'.
    """
    domain = extract_domain(url)
    host = extract_host(url)
    base = {"site_id": None, "site_name": None}

    if domain is None:
        return dict(base, mode="audit_checklist", match_basis="none",
                    domain=None)

    # 1. site_config.domain match
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT site_id, site_name, domain, shopify_domain
            FROM site_config
            ORDER BY created_at ASC
            """
        )
        rows = cur.fetchall()
    for site_id, site_name, cfg_domain, cfg_shopify in rows:
        if normalize_domain_for_match(cfg_domain) == domain:
            return dict(base, site_id=site_id, site_name=site_name,
                        mode="connected", match_basis="domain",
                        domain=domain)
    # 2. shopify_domain match / *.myshopify.com host
    for site_id, site_name, cfg_domain, cfg_shopify in rows:
        if cfg_shopify and normalize_domain_for_match(cfg_shopify) == domain:
            return dict(base, site_id=site_id, site_name=site_name,
                        mode="connected", match_basis="shopify_domain",
                        domain=domain)
    if host and host.endswith(MYSHOPIFY_SUFFIX):
        handle = host[: -len(MYSHOPIFY_SUFFIX)]
        for site_id, site_name, cfg_domain, cfg_shopify in rows:
            cfg = normalize_domain_for_match(cfg_shopify) or ""
            if cfg.endswith(MYSHOPIFY_SUFFIX) and \
                    cfg[: -len(MYSHOPIFY_SUFFIX)] == handle:
                return dict(base, site_id=site_id, site_name=site_name,
                            mode="connected", match_basis="myshopify_host",
                            domain=domain)
        # a bare *.myshopify.com URL with no registered handle: still a
        # Shopify host, but no connected credentials exist -> checklist
        # mode is the only safe route (no store to write to).
        return dict(base, mode="audit_checklist", match_basis="none",
                    domain=domain)

    # 3. no match
    return dict(base, mode="audit_checklist", match_basis="none",
                domain=domain)


def normalized_url_for_storage(raw_url):
    """Canonical URL for the session row (url_normalize's shared
    canonicalizer — same normal form the batch pipeline hashes)."""
    return canonicalize_url(raw_url)
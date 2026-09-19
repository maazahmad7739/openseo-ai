"""Shared URL canonicalization before md5 hashing.

The schema computes `url_hash` / `page_url_hash` as md5() of the exact stored
URL string (00-schema.sql). Cross-source enrichment only joins when the SAME
page is spelled identically across GSC (full https URL), GA4 (host-less page
path), Shopify (constructed /products/{handle}), and the OpenSEO crawl. That
only happens if every ingestion point writes the same canonical form.

canonicalize_url() is that shared normalizer:
  - host lowercased, leading "www." stripped (GSC may report www / non-www)
  - host-less paths (GA4 pagePath) prefixed with https://{domain}
  - trailing slash removed (except root) so /collections/x == /collections/x/
  - fragment dropped; scheme kept https/http as given
  - default ports dropped (https:443 / http:80)
  - query params sorted; known tracking params (utm_*, gclid, fbclid, ...)
    dropped so marketing-tagged URLs collapse onto their canonical page
  - percent-encoding normalized (unreserved chars unescaped)
"""

from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "utm_id", "utm_campaignid",
    "gclid", "gclsrc", "dclid", "fbclid", "msclkid", "igshid",
    "yclid", "_ga", "_gl", "mc_cid", "mc_eid",
}


def canonicalize_url(url, domain=None):
    """Return the canonical form of a page URL, or the input unchanged when
    it is not a URL (e.g. a bare path or an opaque identifier)."""
    if not url:
        return url
    url = url.strip()
    if not url:
        return url
    if url.startswith("/"):
        if not domain:
            return url
        url = f"https://{domain}{url}"
    if "://" not in url:
        return url
    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = _default_port(scheme, parts.port)
    userinfo = ""
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += f":{parts.password}"
        userinfo += "@"
    netloc = f"{userinfo}{host}"
    if port:
        netloc += f":{port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = _canonical_query(parts.query)
    return urlunsplit((scheme, netloc, path, query, ""))


def _canonical_query(query):
    if not query:
        return ""
    kept = []
    for k, v in parse_qsl(query, keep_blank_values=True):
        if k.lower() in TRACKING_PARAMS:
            continue
        kept.append((k, v))
    kept.sort()
    return urlencode(kept)


def _default_port(scheme, port):
    if port is None:
        return None
    if scheme == "http" and port == 80:
        return None
    if scheme == "https" and port == 443:
        return None
    return port
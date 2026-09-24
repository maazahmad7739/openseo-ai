"""Live page fetch + parse for the on-demand audit engine (plan/23 §1 step 2).

Contract (Phase A):
  * GET-only, single fetch, HTTP_TIMEOUT_SECONDS=8, 1 redirect hop max,
    response size cap (MAX_BODY_BYTES = 3MB) — no crawler loops.
  * SSRF guard: literal/loopback/private/link-local hosts and localhost
    variants are rejected BEFORE any connection is attempted; every
    redirect hop is re-checked so a redirect cannot smuggle the fetch into
    a protected address.
  * robots.txt honored for the page fetch (typed `blocked_robots`).
  * Parser never fabricates: a field absent from the HTML stays None /
    empty; heading_outline holds only H1–H3 actually present, in document
    order; script/style/noscript/svg/template content is dropped.
  * render_status is ALWAYS 'not_rendered' in v1 — this is a plain GET,
    no headless browser (plan/23 §1.4); the enum matches
    pages.render_status (plan/00-schema.sql).
  * never-raise at the fetch boundary: every failure is a typed
    PageFetchError so the API layer maps it to the §4.1 contract.

Parsing is stdlib html.parser (+ a regex fallback for parser-hostile HTML)
— no requirements change (plan grounding: fastapi/pydantic/psycopg2/
uvicorn only).
"""

import hashlib
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser

from fixes.generator import _body_to_text

HTTP_TIMEOUT_SECONDS = 8
MAX_BODY_BYTES = 3 * 1024 * 1024
MAX_REDIRECT_HOPS = 1
USER_AGENT = "OpenSEOAuditBot/1.0 (+https://openseo.example/bot)"
# robots.txt rules are matched by the bare product token: the stdlib
# RobotFileParser splits the PASSED agent at "/" but keeps the RULE-side
# token whole, so a "User-agent: OpenSEOAuditBot/1.0" rule can never
# match its own bot — rule authors (and our check) use the bare name.
ROBOTS_AGENT = "OpenSEOAuditBot"

ALLOWED_SCHEMES = ("http", "https")

SKIP_CONTENT = {"script", "style", "noscript", "svg", "template"}


class PageFetchError(Exception):
    """Typed fetch failure; `reason` + `detail` feed the §4.1 contract."""

    def __init__(self, reason, detail=None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


# ------------------------------------------------------------
# SSRF guard
# ------------------------------------------------------------

def _is_ip_disallowed(ip_str):
    """True when the IP is loopback / private / link-local / reserved —
    exactly the ranges an SSRF attempt would target."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def ssrf_check_host(host):
    """Reject localhost variants, protected IP literals, and hosts that
    RESOLVE into protected ranges. Raises PageFetchError('ssrf_blocked').

    Returns the resolved address list; an unresolvable host does NOT fail
    here — the fetch layer reports that failure.
    """
    host = (host or "").strip().rstrip(".")
    if not host:
        raise PageFetchError("invalid_url", "empty host")
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise PageFetchError("ssrf_blocked", f"host {host!r} is not fetchable")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass  # a name, not an IP literal
    else:
        if _is_ip_disallowed(str(ip)):
            raise PageFetchError("ssrf_blocked",
                                 f"IP {host!r} is in a protected range")
        return [str(ip)]

    # Resolve-and-check: DNS landing in a protected range is blocked too.
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return []
    resolved = []
    for info in infos:
        addr = info[4][0]
        if _is_ip_disallowed(addr):
            raise PageFetchError(
                "ssrf_blocked",
                f"{host!r} resolves to protected address {addr!r}")
        if addr not in resolved:
            resolved.append(addr)
    return resolved


def validate_url(raw_url):
    """URL scheme/shape validation for the §4.1 400 contract.

    Returns the urlsplit result; raises PageFetchError('invalid_url') for
    non-HTTP(S) schemes, missing host, or malformed shapes.
    """
    if not raw_url or not isinstance(raw_url, str):
        raise PageFetchError("invalid_url", "empty URL")
    url = raw_url.strip()
    if not url:
        raise PageFetchError("invalid_url", "empty URL")
    if "://" not in url:
        raise PageFetchError("invalid_url", "missing scheme")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise PageFetchError("invalid_url", f"malformed URL: {exc}") from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise PageFetchError("invalid_url", f"scheme {parts.scheme!r} not allowed")
    if not parts.hostname:
        raise PageFetchError("invalid_url", "missing host")
    return parts


# ------------------------------------------------------------
# robots.txt
# ------------------------------------------------------------

def robots_allowed(url, opener=None, timeout=None, user_agent=None):
    """robots.txt check for the page fetch (same-origin /robots.txt).

    robots.txt unreachable = allowed (standard practice — availability
    is never blocked by an unavailable robots file).

    Agent matching: the stdlib RobotFileParser tests `agent in
    rule.agent` (lowercased substring), so the full USER_AGENT with its
    contact-suffix would never match a "User-agent: Name/1.0" rule —
    the bare product token is what rule authors write; check with that.
    """
    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    try:
        if opener is not None:
            with opener.open(robots_url,
                             timeout=timeout or HTTP_TIMEOUT_SECONDS) as resp:
                body = resp.read(MAX_BODY_BYTES).decode("utf-8", errors="replace")
            parser.parse(body.splitlines())
        else:
            parser.set_url(robots_url)
            parser.read()
    except Exception:
        return True
    agent = user_agent or ROBOTS_AGENT
    return parser.can_fetch(agent, url)


# ------------------------------------------------------------
# Fetch
# ------------------------------------------------------------

class _RedirectLimit(Exception):
    pass


class _SingleRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib's default handler allows up to 10 hops; the plan pins 1.
    Every hop target is SSRF-re-checked (unless the TEST-ONLY guard
    escape is set, in which case hop-limit enforcement still applies)."""

    def __init__(self, max_hops=MAX_REDIRECT_HOPS, ssrf_guard=True):
        super().__init__()
        self._max_hops = max_hops
        self._ssrf_guard = ssrf_guard

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        hops = getattr(req, "audit_redirect_hops", 0) + 1
        if hops > self._max_hops:
            raise _RedirectLimit(f"more than {self._max_hops} redirect(s)")
        if self._ssrf_guard:
            try:
                ssrf_check_host(urllib.parse.urlsplit(newurl).hostname)
            except PageFetchError as exc:
                raise _RedirectLimit(str(exc)) from exc
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            new_req.audit_redirect_hops = hops
        return new_req


def _build_opener(ssrf_guard=True):
    opener = urllib.request.build_opener(
        _SingleRedirectHandler(ssrf_guard=ssrf_guard))
    opener.addheaders = [("User-Agent", USER_AGENT),
                         ("Accept", "text/html,application/xhtml+xml")]
    return opener


def fetch_page(raw_url, timeout=None, opener=None, ssrf_guard=True):
    """GET one page → {url, status_code, content_type, body_html}.

    Raises PageFetchError (typed) on every failure path — never a raw
    network exception. `ssrf_guard=False` is a TEST-ONLY escape hatch so
    the fetch logic can be exercised against a loopback server (live
    callers never pass it).
    """
    parts = validate_url(raw_url)
    if ssrf_guard:
        ssrf_check_host(parts.hostname)

    timeout = timeout if timeout is not None else HTTP_TIMEOUT_SECONDS
    opener = opener or _build_opener(ssrf_guard=ssrf_guard)

    try:
        with opener.open(raw_url, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            content_type = resp.headers.get("Content-Type") or ""
            body = resp.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise PageFetchError("page_unreachable", f"HTTP {exc.code}") from exc
    except _RedirectLimit as exc:
        raise PageFetchError("page_unreachable",
                             f"redirect limit exceeded: {exc}") from exc
    except (urllib.error.URLError, OSError, socket.timeout) as exc:
        raise PageFetchError("page_unreachable",
                             f"connection failed: {exc}") from exc

    if len(body) > MAX_BODY_BYTES:
        raise PageFetchError("page_unreachable",
                             f"response exceeds {MAX_BODY_BYTES} bytes")
    return {
        "url": raw_url,
        "status_code": status,
        "content_type": content_type,
        "body_html": body.decode("utf-8", errors="replace"),
    }


# ------------------------------------------------------------
# Parsing (stdlib html.parser + regex fallback)
# ------------------------------------------------------------

class _AuditHTMLParser(HTMLParser):
    """Title / meta description / H1–H3 / <body> extraction.

    Lenient by design: real-world HTML is malformed. Content of
    script/style/noscript/svg/template is dropped; heading text is
    whitespace-collapsed; document order preserved; H1–H3 only.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = None
        self.meta_description = None
        self.headings = []            # [(level, text)] document order
        self.body_html_parts = []
        # state
        self._in_title = False
        self._title_buf = []
        self._title_done = False
        self._in_body = False
        self._heading = None
        self._heading_buf = []
        self._skip_depth = 0

    # -- meta capture (shared by starttag + startendtag) -----------------
    def _capture_meta(self, attrs):
        if self.meta_description is not None:
            return
        lowered = {k.lower(): (v or "") for k, v in attrs or []}
        if lowered.get("name", "").strip().lower() == "description":
            content = lowered.get("content", "").strip()
            if content:
                self.meta_description = content

    # -- start tag -------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP_CONTENT:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if self._in_body and tag != "body":
            self.body_html_parts.append(_reconstruct_open(tag, attrs))
        if tag == "body":
            self._in_body = True
            return
        if tag == "title" and not self._title_done:
            self._in_title = True
            self._title_buf = []
            return
        if tag == "meta":
            self._capture_meta(attrs)
            return
        if tag in ("h1", "h2", "h3") and self._heading is None:
            self._heading = tag
            self._heading_buf = []

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP_CONTENT or self._skip_depth:
            return
        if tag == "meta":
            self._capture_meta(attrs)
            return
        if self._in_body and tag != "body":
            self.body_html_parts.append(
                _reconstruct_open(tag, attrs) + f"</{tag}>")

    # -- end tag ---------------------------------------------------------
    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP_CONTENT and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "body":
            self._in_body = False
            return
        if tag == "title":
            self._in_title = False
            if self.title is None:
                joined = " ".join("".join(self._title_buf).split())
                self.title = joined or None
            self._title_done = True
            return
        if tag in ("h1", "h2", "h3") and self._heading == tag:
            text = " ".join("".join(self._heading_buf).split())
            if text:
                self.headings.append((tag, text))
            self._heading = None
            self._heading_buf = []
        if self._in_body and tag not in SKIP_CONTENT:
            self.body_html_parts.append(f"</{tag}>")

    # -- data ------------------------------------------------------------
    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self._title_buf.append(data)
        if self._heading:
            self._heading_buf.append(data)
        if self._in_body:
            self.body_html_parts.append(data)


def _reconstruct_open(tag, attrs):
    if not attrs:
        return f"<{tag}>"
    rendered = " ".join(k if v in (None, "") else f'{k}="{v}"'
                        for k, v in attrs)
    return f"<{tag} {rendered}>"


# ------------------------------------------------------------
# Fallback regex extractor (for HTML the parser chokes on)
# ------------------------------------------------------------

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_NAME_RE = re.compile(r'name\s*=\s*["\']description["\']', re.I)
_META_CONTENT_RE = re.compile(r'content\s*=\s*["\'](.*?)["\']', re.I | re.S)
_BODY_RE = re.compile(r"<body\b[^>]*>(.*)</body\s*>", re.I | re.S)
_HEADING_RE = re.compile(r"<(h[123])\b[^>]*>(.*?)</\1\s*>", re.I | re.S)
_SKIP_RE = re.compile(r"<(?:script|style|noscript|svg|template)\b.*?"
                      r"</(?:script|style|noscript|svg|template)>", re.I | re.S)


def _fallback_extract(html):
    """Regex extractor for pages the stdlib parser chokes on."""
    html = html or ""
    title = None
    m = _TITLE_RE.search(html)
    if m:
        title = " ".join(m.group(1).split()) or None
    meta = None
    for mm in _META_TAG_RE.finditer(html):
        tag = mm.group(0)
        if _META_NAME_RE.search(tag):
            cm = _META_CONTENT_RE.search(tag)
            if cm:
                meta = " ".join(cm.group(1).split()) or None
            break
    bm = _BODY_RE.search(html)
    body_html = bm.group(1) if bm else html
    body_html = _SKIP_RE.sub(" ", body_html)
    headings = []
    for hm in _HEADING_RE.finditer(body_html):
        text = " ".join(re.sub(r"<[^>]+>", " ", hm.group(2)).split())
        if text:
            headings.append((hm.group(1).lower(), text))
    return title, meta, headings, body_html.strip()


def parse_page_html(html):
    """HTML → audit-page-snapshot fields.

    Returns {title, meta_description, h1, heading_outline, body_html,
             body_text, render_status}. render_status is ALWAYS
    'not_rendered' in v1 (plain GET; no headless browser — plan/23 §1.4).
    """
    html = html or ""
    title, meta, headings, body_html = None, None, [], ""
    parser = _AuditHTMLParser()
    try:
        parser.feed(html)
        parser.close()
        title = parser.title
        meta = parser.meta_description
        headings = parser.headings
        body_html = "".join(parser.body_html_parts).strip()
    except Exception:
        title, meta, headings, body_html = _fallback_extract(html)

    outline = [{"level": level, "text": text} for level, text in headings]
    h1 = next((entry["text"] for entry in outline if entry["level"] == "h1"),
              None)
    body_text = _body_to_text(body_html) if body_html else ""
    return {
        "title": title or None,
        "meta_description": meta or None,
        "h1": h1,
        "heading_outline": outline,
        "body_html": body_html or None,
        "body_text": body_text or None,
        "render_status": "not_rendered",
    }


def content_hash(body_text):
    """sha256 of the normalized body text (cache key + drift detector)."""
    normalized = " ".join((body_text or "").split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def fetch_heading_outline(raw_url, timeout=None, opener=None, max_headings=30):
    """Competitor heading structure for ONE URL (Task 4 seam).

    Reuses the full guard stack (validate → SSRF → robots → GET → parse)
    but discards the body — only the H1–H3 outline is needed, so a
    competitor scan never persists page copy. Never raises: any failure
    (invalid URL, SSRF, robots block, HTTP error, size cap, parse) returns
    a typed {"ok": False, ...} dict.

    max_headings bounds the outline so a runaway page can't blow the
    payload fed to the gap analyzer.
    """
    try:
        snapshot = build_page_snapshot(raw_url, timeout=timeout,
                                       opener=opener)
    except PageFetchError as exc:
        return {"ok": False, "reason": exc.reason, "detail": exc.detail}
    except Exception as exc:  # never-raise at the boundary
        return {"ok": False, "reason": "fetch_failed", "detail": str(exc)}
    outline = (snapshot.get("heading_outline") or [])[:max_headings]
    return {
        "ok": True,
        "url": snapshot.get("url") or raw_url,
        "status_code": snapshot.get("status_code"),
        "title": snapshot.get("title"),
        "h1": snapshot.get("h1"),
        "heading_outline": outline,
    }


# ------------------------------------------------------------
# Orchestrated snapshot build (validate → SSRF → robots → GET → parse)
# ------------------------------------------------------------

def build_page_snapshot(raw_url, timeout=None, opener=None,
                        check_robots=True, ssrf_guard=True):
    """Full Phase A ingest. Returns the snapshot dict (page fields + url +
    status_code + content_hash). Raises PageFetchError (typed).

    `ssrf_guard=False` is a TEST-ONLY escape hatch for loopback fixture
    servers (mirrored into the robots check); live callers never pass it.
    """
    parts = validate_url(raw_url)
    if ssrf_guard:
        ssrf_check_host(parts.hostname)
    if check_robots:
        # The robots check is itself a fetch of {origin}/robots.txt; under
        # the TEST-ONLY loopback escape the guard must not re-block it.
        allowed = robots_allowed(raw_url.strip(), opener=opener)
        if not allowed:
            raise PageFetchError("blocked_robots",
                                 "robots.txt disallows this URL")
    fetched = fetch_page(raw_url.strip(), timeout=timeout, opener=opener,
                         ssrf_guard=ssrf_guard)
    snapshot = parse_page_html(fetched["body_html"])
    snapshot["url"] = fetched["url"]
    snapshot["status_code"] = fetched["status_code"]
    snapshot["content_hash"] = content_hash(snapshot.get("body_text"))
    return snapshot
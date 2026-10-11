"""Batch robots.txt + sitemap.xml verification (Task 5, plan/21 §2.3 fix).

Replaces the sitemap_index_mismatch PROXY (indexable=false AND 200 AND
clicks>0 — plan/21:218 admits real sitemap membership was never ingested)
with verified facts:

  robots.txt  → fetched + parsed once per site per run: global
                `User-agent: *` disallow rules + the declared
                Sitemap: line (the sitemap URL no longer guessed).
  sitemap.xml → fetched + parsed (index + child sitemaps, bounded), URL
                set compared against the crawled pages table; each page
                row gets in_sitemap + robots_allowed flags so
                sitemap_index_mismatch becomes a REAL verdict:
                  - in_sitemap=false + indexable  → "missing from sitemap"
                  - in_sitemap=true  + unindexed → sitemap/index mismatch
                  - robots_blocked on a 200 page  → robots conflict

Never-raise contract: network/parse failures return typed results and
the site sweep continues (an unavailable robots file = allowed, standard
practice; an unavailable sitemap = unknown, never fabricated). Only
stdlib (urllib + xml.etree) — no requirements change.

Bounds: MAX_SITEMAP_ENTRIES per sitemap, MAX_SITEMAP_DEPTH for nested
indexes, HTTP_TIMEOUT_SECONDS per request.
"""

import re
import urllib.error
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET

HTTP_TIMEOUT_SECONDS = 15
MAX_SITEMAP_ENTRIES = 5000
MAX_SITEMAP_DEPTH = 2          # sitemap index → child sitemaps
MAX_SITEMAP_CHILDS = 10        # child sitemaps fetched per index
USER_AGENT = "OpenSEOSiteBot/1.0 (+https://openseo.example/bot)"

_SITEMAP_NS = re.compile(r"\{[^}]*\}")


def _path_of(url):
    """URL -> path+query (defaults to /robots.txt-relative '/sitemap.xml')."""
    path = (url or "").split("://", 1)[-1]
    if "/" in path:
        return "/" + path.split("/", 1)[1]
    return "/sitemap.xml"


class SiteFetchError(Exception):
    def __init__(self, reason, detail=None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


def _get(url, opener=None):
    """GET one resource as text — typed failure, never raises."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", USER_AGENT)
    try:
        if opener is not None:
            with opener.open(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return {"ok": True,
                        "status": getattr(resp, "status", None) or resp.getcode(),
                        "body": resp.read(MAX_SITEMAP_ENTRIES * 512)
                        .decode("utf-8", errors="replace")}
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return {"ok": True,
                    "status": getattr(resp, "status", None) or resp.getcode(),
                    "body": resp.read(MAX_SITEMAP_ENTRIES * 512)
                    .decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "reason": f"http_{exc.code}"}
    except Exception as exc:
        return {"ok": False, "reason": "fetch_failed", "detail": str(exc)}


def fetch_robots_txt(origin, opener=None):
    """robots.txt for an origin ('https://example.com') → typed result.

    Returns {ok, fetchable, body, sitemap_urls, disallow_all, rules_text}.
    A 404/unreachable robots file is NOT an error (fetchable=False;
    disallow_all=False per standard practice) — availability never blocks.
    """
    base = origin.rstrip("/")
    result = {"ok": True, "fetchable": False, "body": None,
              "sitemap_urls": [], "disallow_all": False}
    fetched = _get(f"{base}/robots.txt", opener=opener)
    if not fetched.get("ok"):
        # 404 = no robots file (fetchable False, allowed); other failures
        # behave the same for the batch verdict (availability ≠ blocking).
        return result
    body = fetched.get("body") or ""
    result["fetchable"] = True
    result["body"] = body
    # Global-block detection, line-based: a "User-agent: *" group whose
    # Disallow value is "/" (or empty = allow-all). Group-aware enough
    # for the batch verdict without reimplementing the full parser.
    current_agents = []
    disallow_all = False
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lowered = line.lower()
        if lowered.startswith("user-agent:"):
            agent = line.split(":", 1)[1].strip().lower()
            current_agents = [agent] if agent else []
        elif lowered.startswith("disallow:"):
            value = line.split(":", 1)[1].strip()
            if "*" in current_agents and value == "/":
                disallow_all = True
    result["disallow_all"] = disallow_all
    sitemap_urls = []
    for line in body.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url:
                sitemap_urls.append(url)
    result["sitemap_urls"] = sitemap_urls
    return result


def parse_sitemap_xml(body):
    """One sitemap document → (urls, child_sitemaps).

    Tolerates the http://www.sitemaps.org/schemas/sitemap/0.9 namespace
    and namespace-less documents. Never raises on malformed XML (typed
    empty result); <loc> under <url> counts as a page, under <sitemap>
    as a child index entry.
    """
    urls, children = [], []
    if not body:
        return urls, children
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return urls, children
    root_tag = _SITEMAP_NS.sub("", root.tag or "")
    is_index = root_tag == "sitemapindex"
    for child in root:
        child_tag = _SITEMAP_NS.sub("", child.tag or "")
        if child_tag not in ("url", "sitemap"):
            continue
        for loc in child:
            if _SITEMAP_NS.sub("", loc.tag) != "loc":
                continue
            value = (loc.text or "").strip()
            if not value:
                continue
            if is_index or child_tag == "sitemap":
                children.append(value)
            else:
                urls.append(value)
            break
    return urls[:MAX_SITEMAP_ENTRIES], children[:MAX_SITEMAP_ENTRIES]


def fetch_sitemap(origin, opener=None, depth=MAX_SITEMAP_DEPTH,
                  sitemap_url=None):
    """Fetch + flatten a site's sitemap → {ok, urls, fetchable, reason}.

    Resolution order: explicit sitemap_url → robots.txt Sitemap: lines →
    /sitemap.xml convention. Child sitemaps (index) fetched up to depth
    levels, MAX_SITEMAP_CHILDS each. Every failure is a typed result;
    an unfetchable sitemap yields ok=False + reason (never fabricated
    membership — callers treat unknown as NULL, not False).
    """
    base = origin.rstrip("/")
    robots = fetch_robots_txt(base, opener=opener)
    candidates = []
    if sitemap_url:
        candidates.append(sitemap_url)
    # Declared Sitemap: lines may point at another host (CDN, www vs apex);
    # verification fetches from THIS origin, so the declared sitemap's
    # PATH is re-homed onto the fetch origin (same relative resource).
    for declared in robots.get("sitemap_urls") or []:
        path = _path_of(declared)
        candidates.append(f"{base}{path}")
    candidates.append(f"{base}/sitemap.xml")
    seen_roots = set()
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in seen_roots:
            continue
        seen_roots.add(candidate)
        fetched = _get(candidate, opener=opener)
        if not fetched.get("ok"):
            continue
        urls, children = parse_sitemap_xml(fetched.get("body") or "")
        if not urls and not children:
            continue  # not a sitemap document (e.g. an HTML error page)
        # Breadth: fetch child sitemaps (bounded). Child URLs from a real
        # index are usually same-origin; foreign-host children are
        # re-homed onto the fetch origin the same way as declared roots.
        child_depth = 1
        frontier = children[:MAX_SITEMAP_CHILDS]
        while frontier and child_depth < depth:
            next_frontier = []
            for child_url in frontier:
                if len(urls) >= MAX_SITEMAP_ENTRIES:
                    break
                child_fetched = _get(f"{base}{_path_of(child_url)}",
                                     opener=opener)
                if not child_fetched.get("ok"):
                    continue
                child_urls, grandchildren = parse_sitemap_xml(
                    child_fetched.get("body") or "")
                urls.extend(child_urls)
                next_frontier.extend(grandchildren[:MAX_SITEMAP_CHILDS])
            frontier = next_frontier
            child_depth += 1
        return {"ok": True, "fetchable": True, "sitemap_url": candidate,
                "urls": list(dict.fromkeys(urls))[:MAX_SITEMAP_ENTRIES],
                "reason": None}
    return {"ok": False, "fetchable": False, "sitemap_url": None,
            "urls": [], "reason": "sitemap_unreachable"}


def sync_sitemap_membership(conn, site_id, domain, opener=None):
    """robots.txt + sitemap.xml → pages.in_sitemap / pages.robots_allowed.

    Returns a typed summary {robots_fetchable, disallow_all, sitemap_urls,
    sitemap_fetchable, sitemap_url, urls_in_sitemap, pages_flagged,
    sitemap_only_count} — every failure degrades to a typed skip, never
    a crash. pages.in_sitemap is set to NULL (unknown) when the sitemap
    is unfetchable so the real sitemap_index_mismatch verdict can
    distinguish "verified missing" from "not checked".
    """
    origin = domain if "://" in (domain or "") else f"https://{domain}"
    robots = fetch_robots_txt(origin, opener=opener)
    sitemap = fetch_sitemap(origin, opener=opener, sitemap_url=None)

    summary = {
        "robots_fetchable": robots.get("fetchable", False),
        "disallow_all": robots.get("disallow_all", False),
        "robots_declared_sitemaps": robots.get("sitemap_urls") or [],
        "sitemap_fetchable": sitemap.get("fetchable", False),
        "sitemap_url": sitemap.get("sitemap_url"),
        "urls_in_sitemap": len(sitemap.get("urls") or []),
        "pages_flagged": 0,
    }

    sitemap_set = {_path_of(u).rstrip("/") for u in (sitemap.get("urls") or [])}
    robots_parser = None
    if robots.get("fetchable"):
        robots_parser = urllib.robotparser.RobotFileParser()
        robots_parser.parse((robots.get("body") or "").splitlines())

    with conn.cursor() as cur:
        cur.execute("SELECT url FROM pages WHERE site_id = %s", (site_id,))
        page_urls = [r[0] for r in cur.fetchall()]
    for url in page_urls:
        page_path = _path_of(url).rstrip("/")
        if sitemap.get("fetchable"):
            in_sitemap = page_path in sitemap_set
        else:
            in_sitemap = None  # unknown — never fabricated
        robots_allowed = True
        if robots_parser is not None:
            try:
                robots_allowed = robots_parser.can_fetch(USER_AGENT, url)
            except Exception:
                robots_allowed = True
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pages SET in_sitemap = %s, robots_allowed = %s
                WHERE site_id = %s AND url = %s
                """,
                (in_sitemap, robots_allowed, site_id, url),
            )
            summary["pages_flagged"] += cur.rowcount
    return summary


# ------------------------------------------------------------
# Generic sitemap → pages ingestion (plan/26 Phase 1)
# ------------------------------------------------------------
# The batch pipeline's only full pages-ingestion path was Shopify
# (connectors.sync.sync_pages_from_shopify). Non-Shopify platforms (Framer,
# WordPress, headless) had NO source of pages rows, so generators and fix
# targeting found nothing. This ingests the sitemap's URL set into pages
# with a bounded per-page fetch — the same guard stack the audit engine
# already proved (validate → SSRF → robots → GET → parse).
#
# Never-raise: every failure is a typed summary key (the
# sync_sitemap_membership pattern). Shopify sites never call this (the
# weekly_crawl wiring gates on platform) — catalogue ingestion remains
# their single source.

MAX_SITEMAP_INGEST_PAGES = 200    # per-run page-fetch ceiling
_INGEST_SKIP_CONTENT = ("script", "style", "noscript", "svg", "template")
_INGEST_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
_INGEST_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_INGEST_META_NAME_RE = re.compile(r'name\s*=\s*["\']description["\']', re.I)
_INGEST_META_CONTENT_RE = re.compile(r'content\s*=\s*["\'](.*?)["\']', re.I | re.S)
_INGEST_BODY_RE = re.compile(r"<body\b[^>]*>(.*)</body\s*>", re.I | re.S)
_INGEST_SKIP_RE = re.compile(
    r"<(?:script|style|noscript|svg|template)\b.*?"
    r"</(?:script|style|noscript|svg|template)>", re.I | re.S)
_INGEST_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _page_title_from_html(html):
    m = _INGEST_TITLE_RE.search(html or "")
    return " ".join(m.group(1).split()) or None if m else None


def _page_meta_from_html(html):
    for tag in _INGEST_META_TAG_RE.finditer(html or ""):
        if _INGEST_META_NAME_RE.search(tag.group(0)):
            cm = _INGEST_META_CONTENT_RE.search(tag.group(0))
            if cm:
                return " ".join(cm.group(1).split()) or None
            break
    return None


def _page_body_from_html(html):
    """<body> inner HTML → (body_html, body_text, heading H1 text)."""
    html = html or ""
    body = html
    m = _INGEST_BODY_RE.search(html)
    if m:
        body = m.group(1)
    body = _INGEST_SKIP_RE.sub(" ", body)
    h1 = None
    h1m = re.search(r"<h1\b[^>]*>(.*?)</h1\s*>", body, re.I | re.S)
    if h1m:
        h1 = " ".join(_INGEST_TAG_STRIP_RE.sub(" ", h1m.group(1)).split()) or None
    body_html = body.strip() or None
    text = " ".join(_INGEST_TAG_STRIP_RE.sub(" ", body_html or "").split())
    return body_html, text or None, h1


def _body_text_hash_of(body_html):
    """Normalized visible-text md5 (matches sync._body_text_hash semantics:
    lowercased; empty → NULL — a null never equals a null)."""
    if not body_html:
        return None
    text = " ".join(_INGEST_TAG_STRIP_RE.sub(" ", body_html).split()).lower()
    if not text:
        return None
    import hashlib
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _robots_ok_for_url(robots_parser, url):
    if robots_parser is None:
        return True
    try:
        return robots_parser.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def sync_pages_from_sitemap(conn, site_id, domain, opener=None,
                            max_pages=MAX_SITEMAP_INGEST_PAGES):
    """Sitemap.xml → pages rows for NON-Shopify platforms (plan/26 Phase 1).

    Resolution: the same fetch_sitemap order as the membership pass
    (explicit → robots.txt Sitemap: lines → /sitemap.xml). Each URL is
    fetched (bounded by max_pages; robots.txt honored) and upserted into
    pages: page_type from the URL archetype, title/meta/body from the
    HTML, body_text_hash near-duplicate key, shopify_gid stays NULL
    (never fabricated). ON CONFLICT upsert with COALESCE semantics so an
    existing row's richer fields are never overwritten by a poorer fetch.

    Returns a typed summary — every failure degrades to a typed key,
    never a crash (the Task 5 semantics this module established).
    """
    from audit.url_pattern import page_type_from_url, template_from_url
    from audit.structured_data import html_has_structured_data

    origin = domain if "://" in (domain or "") else f"https://{domain}"
    summary = {
        "platform_mode": "generic_sitemap_ingest",
        "sitemap_fetchable": False,
        "sitemap_url": None,
        "urls_in_sitemap": 0,
        "pages_attempted": 0,
        "pages_upserted": 0,
        "pages_robots_blocked": 0,
        "pages_fetch_failed": 0,
        "truncated_to": max_pages,
        "error": None,
    }
    try:
        sitemap = fetch_sitemap(origin, opener=opener, sitemap_url=None)
    except Exception as exc:
        return dict(summary, error=f"fetch_sitemap: {type(exc).__name__}: {exc}")
    if not sitemap.get("ok"):
        return dict(summary, error=sitemap.get("reason") or "sitemap_unreachable")

    urls = list(dict.fromkeys(sitemap.get("urls") or []))[:max_pages]
    summary.update({
        "sitemap_fetchable": True,
        "sitemap_url": sitemap.get("sitemap_url"),
        "urls_in_sitemap": len(sitemap.get("urls") or []),
        "pages_attempted": len(urls),
    })

    # robots.txt once per site; each page's fetch re-checks via the parser
    robots = fetch_robots_txt(origin, opener=opener)
    robots_parser = None
    if robots.get("fetchable"):
        robots_parser = urllib.robotparser.RobotFileParser()
        robots_parser.parse((robots.get("body") or "").splitlines())

    # Lazy import: audit.page_fetch owns the SSRF guard + fetch + parse
    # stack (shared with the live audit engine — one implementation).
    # ssrf_guard=False here is the BATCH-CRAWL posture, NOT a live-audit
    # shortcut: the URL set originates from the site's OWN sitemap (a
    # trusted, operator-registered origin — site_config.domain), unlike
    # the on-demand audit where a pasted URL is untrusted input. Loopback
    # fixture servers in tests stay fetchable through the opener seam.
    from audit.page_fetch import build_page_snapshot, PageFetchError

    for url in urls:
        if summary["pages_upserted"] + summary["pages_robots_blocked"] \
                + summary["pages_fetch_failed"] >= max_pages:
            break
        if not _robots_ok_for_url(robots_parser, url):
            summary["pages_robots_blocked"] += 1
            continue
        try:
            snapshot = build_page_snapshot(url, opener=opener, ssrf_guard=False)
        except PageFetchError as exc:
            if exc.reason == "blocked_robots":
                summary["pages_robots_blocked"] += 1
            else:
                summary["pages_fetch_failed"] += 1
            continue
        except Exception:
            summary["pages_fetch_failed"] += 1
            continue

        body_html, body_text, h1 = _page_body_from_html(snapshot.get("body_html"))
        title = snapshot.get("title") or _page_title_from_html(
            snapshot.get("body_html") or "")
        meta = snapshot.get("meta_description") or _page_meta_from_html(
            snapshot.get("body_html") or "")
        status_code = snapshot.get("status_code")
        indexable = status_code == 200
        # Detect schema from the RAW snapshot, not `body_html`:
        # _page_body_from_html strips <script>, which is where JSON-LD lives.
        # None (no HTML) must not be written as False — that is the default-
        # value false positive generator 3 reads as "verified: no schema".
        raw_html = snapshot.get("body_html")
        has_structured_data = html_has_structured_data(raw_html)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title, h1,
                                   meta_description, body_html, body_text_hash,
                                   status_code, indexable, last_crawled_at,
                                   has_structured_data, template)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s)
                ON CONFLICT (site_id, url_hash) DO UPDATE SET
                    page_type = EXCLUDED.page_type,
                    title = COALESCE(EXCLUDED.title, pages.title),
                    h1 = COALESCE(EXCLUDED.h1, pages.h1),
                    meta_description = COALESCE(EXCLUDED.meta_description,
                                                pages.meta_description),
                    body_html = COALESCE(EXCLUDED.body_html, pages.body_html),
                    body_text_hash = COALESCE(EXCLUDED.body_text_hash,
                                              pages.body_text_hash),
                    status_code = EXCLUDED.status_code,
                    indexable = EXCLUDED.indexable,
                    last_crawled_at = now(),
                    has_structured_data = COALESCE(
                        EXCLUDED.has_structured_data, pages.has_structured_data),
                    -- plan/32: never let an INFERRED archetype clobber a real
                    -- provider-reported template (product.liquid etc).
                    template = COALESCE(pages.template, EXCLUDED.template)
                """,
                (site_id, url, page_type_from_url(url), title, h1, meta,
                 body_html, _body_text_hash_of(body_html),
                 status_code, indexable, has_structured_data,
                 template_from_url(url)),
            )
            summary["pages_upserted"] += cur.rowcount
    return summary
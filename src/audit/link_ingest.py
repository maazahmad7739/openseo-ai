"""Link-graph ingestion into page_links (plan/31, Phase 5 closure).

page_links (plan/22 §4) existed but was never populated — the engine could
detect orphans/deep pages (plan/03 counts only) but never suggest WHERE a
new internal link should go, because the source->target edge set was
unknown.

Ingestion point: the on-demand audit engine (audit.engine.run_audit). When
a page snapshot for a CONNECTED site is persisted, its parsed <a> records
(audit.page_fetch.parse_page_html -> snapshot["links"]) are resolved against
the site's origin and upserted into page_links:

  * internal only — hrefs resolving to a host on the SAME registrable
    domain (exact-match + www-strip, mirroring site_resolution);
  * relative hrefs (/path) resolve against the page URL;
  * scheme normalization https/http; fragments dropped; query KEPT
    (faceted URLs are real edges);
  * rel attribute (nofollow/sponsored/...) recorded on the edge;
  * first_seen_at = first ingest date, last_seen_at = latest (upsert);
  * anchors without text are still recorded (nav/icon links) — anchor
    text is advisory for the generator, not an edge precondition.

Fail-soft contract: ingestion never blocks an audit. Any DB error degrades
to a typed {"ingested": 0, "error": ...} in the audit result.

Suggestion-only (plan/31 §3): nothing here ever writes a fix row or a
redirect — placement recommendations enter the Action Queue as
status='proposed' and require operator review (two-gate discipline).
"""

import sys
import os
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connectors.url_normalize import canonicalize_url  # noqa: E402


class LinkIngestError(Exception):
    pass


def _host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _strip_www(host):
    return host[4:] if host.startswith("www.") else host


def resolve_internal_links(page_url, links, site_domain=None):
    """Raw <a> records -> internal edge dicts for page_links upsert.

    Returns (edges, skipped_count). An edge is skipped (and counted) when
    its href is external, non-HTTP(S), empty after normalization, or a
    self-loop (page -> itself). Same-page fragment links are self-loops.
    hrefs that are not URLs (mailto:, tel:, javascript:) never resolve.
    site_domain is advisory (the page URL's host is authoritative); when
    given it must match, else nothing resolves (no edges).
    """
    base_host = _strip_www(_host_of(page_url))
    if site_domain:
        domain_host = _strip_www(_host_of(
            site_domain if "://" in site_domain
            else f"https://{site_domain}"))
        if domain_host and domain_host != base_host:
            # page URL and site_config disagree — refuse to guess
            return [], 0
    if not base_host:
        return [], 0
    edges = []
    skipped = 0
    seen = set()
    for link in links or []:
        href = (link or {}).get("href") or ""
        href = href.strip()
        if not href:
            skipped += 1
            continue
        lowered = href.lower()
        if lowered.startswith(("mailto:", "tel:", "javascript:",
                               "data:", "#")):
            skipped += 1
            continue
        lowered = href.lower()
        if lowered.startswith(("mailto:", "tel:", "javascript:",
                               "data:", "#")):
            skipped += 1
            continue
        parts = urlsplit(href)
        if parts.scheme and parts.scheme.lower() not in ("http", "https"):
            skipped += 1
            continue
        if parts.scheme:
            # absolute http(s): internal only when the host matches the
            # page's registrable domain (exact + www-strip)
            target_host = _strip_www((parts.hostname or "").lower())
            if target_host != base_host:
                skipped += 1
                continue
            target = canonicalize_url(href)
        elif href.startswith("//"):
            # protocol-relative: resolve against the page's scheme
            scheme = urlsplit(page_url).scheme or "https"
            absolute = f"{scheme}:{href}"
            if _strip_www(_host_of(absolute)) != base_host:
                skipped += 1
                continue
            target = canonicalize_url(absolute)
        elif href.startswith("/"):
            # root-relative: resolve against the site origin
            target = canonicalize_url(f"https://{base_host}{href}")
        else:
            # bare relative path — resolve against the page URL (urljoin
            # handles '../' and sibling paths correctly)
            from urllib.parse import urljoin
            target = canonicalize_url(urljoin(page_url, href))
            if _strip_www(_host_of(target)) != base_host:
                skipped += 1
                continue
        if not target:
            skipped += 1
            continue
        if target == canonicalize_url(page_url):
            # self-loop (fragment-only or identical page): not an edge
            skipped += 1
            continue
        key = target
        if key in seen:
            continue
        seen.add(key)
        edges.append({
            "target_url": target,
            "anchor_text": ((link or {}).get("text") or None),
            "rel": ((link or {}).get("rel") or None),
        })
    return edges, skipped


def ingest_page_links(conn, site_id, page_url, snapshot):
    """Upsert one page's internal edges into page_links (fail-soft).

    Returns {'ok', 'ingested', 'skipped', 'error'?}. Never raises for DB
    faults: the audit pipeline must not fail because the link graph write
    did (suggestion-only surface; the audit itself already succeeded).
    """
    try:
        edges, skipped = resolve_internal_links(page_url,
                                                snapshot.get("links"))
        if not edges:
            return {"ok": True, "ingested": 0, "skipped": skipped}
        today = __import__("datetime").date.today()
        with conn.cursor() as cur:
            for edge in edges:
                cur.execute(
                    """
                    INSERT INTO page_links
                        (site_id, source_url, target_url, anchor_text,
                         rel, first_seen_at, last_seen_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (site_id, source_url_hash, target_url_hash)
                    DO UPDATE SET
                        anchor_text = COALESCE(EXCLUDED.anchor_text,
                                               page_links.anchor_text),
                        rel = COALESCE(EXCLUDED.rel, page_links.rel),
                        last_seen_at = EXCLUDED.last_seen_at
                    """,
                    (site_id, page_url, edge["target_url"],
                     edge["anchor_text"], edge["rel"], today, today),
                )
        conn.commit()
        return {"ok": True, "ingested": len(edges), "skipped": skipped}
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "ingested": 0, "skipped": 0,
                "error": f"{type(exc).__name__}: {exc}"}
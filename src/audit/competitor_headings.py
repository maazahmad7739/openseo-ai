"""Competitor heading-outline grounding (Task 4, plan/21 §2.2 closure).

Snippet-hook inference (fixes/generator.py content_outline_gaps) infers
expected sections from SERP snippets; this module adds the REAL
competitor heading structures: for each top non-self SERP URL of the
cluster, audit.page_fetch.fetch_heading_outline returns the H1–H3
outline (SSRF-guarded, robots-honored, body discarded).

Deterministic section mapping: heading text → section archetypes reuse
the same vocabulary as the snippet path so both signals merge in the
same output schema ({expected, missing, covered}_sections) — consumers
switch sources without an interface change.

Cost/abuse bounds:
  * top N competitor URLs only (default 3), bounded by COMPETITOR_LIMIT
  * headings capped per URL (fetch_heading_outline max_headings)
  * every fetch failure is a typed skip — one bad competitor never
    aborts the outline build.
"""

import re
from collections import Counter

from audit.page_fetch import fetch_heading_outline

# Top competitor URLs scanned per cluster (payload + wall-clock bound).
COMPETITOR_SCAN_LIMIT = 3

# Heading text tokens → the SAME section archetypes _SECTION_BY_HOOK uses
# (fixes/generator.py). Heading-side cues are broader than snippet hooks:
# real H2/H3s spell the section out ("Pricing", "Customer Reviews").
_HEADING_SECTION_RULES = (
    (re.compile(r"\b(test|tested|hands[- ]on|lab|benchmark)", re.I),
     "Test results / hands-on findings"),
    (re.compile(r"\b(how to|step|guide|walkthrough|tutorial|setup)", re.I),
     "Step-by-step walkthrough"),
    (re.compile(r"\b(vs|versus|compare|comparison|compared)\b", re.I),
     "Head-to-head comparison table"),
    (re.compile(r"\b(top picks|best|shortlist|our picks)\b", re.I),
     "Top-picks shortlist"),
    (re.compile(r"\b(worth it|verdict|buying advice|should you buy)\b", re.I),
     "Buying-advice / verdict section"),
    (re.compile(r"\b(shipping|returns|delivery|free shipping)\b", re.I),
     "Shipping & returns info"),
    (re.compile(r"\b(warranty|support|guarantee|customer service)\b", re.I),
     "Warranty & support info"),
    (re.compile(r"\b(battery|specs?|specifications|hours)\b", re.I),
     "Battery & specs detail"),
    (re.compile(r"\b(pricing|price|cost|plans)\b", re.I),
     "Pricing & value section"),
    (re.compile(r"\b(faq|questions|q&a)\b", re.I),
     "FAQ section"),
)


def heading_to_section(text):
    """One heading string → section archetype (first rule hit wins)."""
    for pattern, section in _HEADING_SECTION_RULES:
        if pattern.search(text or ""):
            return section
    return None


def scan_competitor_headings(competitor_urls, fetcher=None, limit=None):
    """Heading outlines for the top non-self competitor URLs.

    fetcher: TEST seam — a callable(url) -> fetch_heading_outline-shaped
    dict; live callers leave it None (real fetch through page_fetch).
    Returns {"ok": scanned_count, "outlines": [{url, h1, headings}, ...]}.
    """
    fetcher = fetcher or fetch_heading_outline
    limit = limit or COMPETITOR_SCAN_LIMIT
    outlines = []
    seen = set()
    for url in (competitor_urls or [])[:limit]:
        url = (url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        result = fetcher(url)
        if not result.get("ok"):
            continue
        outline = [h for h in (result.get("heading_outline") or [])
                   if (h or {}).get("text")]
        if not outline:
            continue
        outlines.append({
            "url": url,
            "h1": result.get("h1"),
            "headings": outline,
        })
    return {"ok": len(outlines), "outlines": outlines}


def sections_from_headings(outlines):
    """Scanned competitor outlines → (expected_sections, heading_examples).

    A section is EXPECTED when ≥ half of the scanned competitors cover it
    (same frequency semantics as the snippet path). Deterministic.
    """
    n = len(outlines or [])
    if not n:
        return []
    counts = {}
    for entry in outlines:
        sections = {heading_to_section(h.get("text"))
                    for h in entry.get("headings") or []}
        for section in sections:
            if section:
                counts[section] = counts.get(section, 0) + 1
    expected = [s for s, c in counts.items() if c >= max(2, n // 2)]
    # Deterministic order: frequency DESC, then alphabetical.
    return sorted(expected, key=lambda s: (-counts[s], s))


def content_outline_gaps_with_headings(rec, body_text=None, fetcher=None,
                                       limit=None):
    """Heading-grounded replacement for the snippet-inferred outline.

    Builds the expected-section set from REAL competitor heading
    structures when the scan yields any, and merges the snippet-hook
    sections as a fallback layer (a section seen either way is expected).
    Coverage is judged against the page's own body text only —
    competitor wording is never inserted (grounded-only rule).

    Falls back to the pure snippet path (returns its result) when no
    competitor URLs are available or the scan yields nothing — never
    blocks a fix.
    """
    from fixes.generator import content_outline_gaps, _SECTION_CUES

    competitors = rec.get("competitor_context") or {}
    urls = [t.get("url") for t in (competitors.get("titles") or []) if t.get("url")]
    scan = scan_competitor_headings(urls, fetcher=fetcher, limit=limit)

    base = content_outline_gaps(rec, body_text=body_text)
    if not scan["outlines"]:
        base["competitor_headings_scanned"] = 0
        base["grounding"] = "snippet_hooks"
        return base

    expected = list(base.get("expected_sections") or [])
    for section in sections_from_headings(scan["outlines"]):
        if section not in expected:
            expected.append(section)

    body = (body_text if body_text is not None
            else rec.get("body_text") or "").lower()
    covered, missing = [], []
    for section in expected:
        cues = _SECTION_CUES.get(section, ())
        if any(cue in body for cue in cues):
            covered.append(section)
        else:
            missing.append(section)

    return {
        "expected_sections": expected,
        "missing_sections": missing,
        "covered_sections": covered,
        "competitor_headings_scanned": scan["ok"],
        "grounding": "competitor_headings+snippet_hooks",
    }
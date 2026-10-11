"""Structured-data presence detection over raw HTML.

`pages.has_structured_data` is `BOOLEAN DEFAULT false` (plan/00-schema.sql).
Every ingest path that fetched a page but never wrote that column therefore
stored a *default*, not an observation — and generator 3 (plan/03,
`structured_data_failure`) reads the default as "verified: no schema", so
uncrawled/untagged pages were reported as missing schema.

This module closes that gap: the ingest paths that already hold the fetched
HTML call it so the column records what was actually seen.

Three syntaxes are recognised, because a template that emits microdata is not
missing schema (plan/16-skill-technical-fix.md §structured_data_failure):

  1. JSON-LD   <script type="application/ld+json">
  2. Microdata <div itemscope itemtype="...schema.org/...">
  3. RDFa      vocab / typeof / property attributes on schema.org terms

Returns None when there is no HTML to inspect (genuinely unknown — callers
must leave the column untouched rather than record a false), True when any
syntax is present, False when the HTML was inspected and held none.

Stdlib only (`re`): site_fetch.py is deliberately dependency-free.
"""

import re

# <script ... type="application/ld+json" ...> — attribute order/quoting varies,
# so match the tag then look for the type token inside it.
_SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>", re.I)
_LD_JSON_TYPE_RE = re.compile(
    r"""type\s*=\s*["']?\s*application/ld\+json""", re.I)

# Microdata: an itemtype pointing at schema.org is the reliable marker.
_ITEMSCOPE_RE = re.compile(r"\bitemscope\b", re.I)
_ITEMTYPE_RE = re.compile(
    r"""\bitemtype\s*=\s*["'][^"']*schema\.org""", re.I)

# RDFa: schema.org terms used as vocabulary/type/property values.
_RDFA_RE = re.compile(
    r"""\b(?:vocab|typeof|property|prefix)\s*=\s*["'][^"']*schema\.org""",
    re.I)

# Cheap pre-filter: schema.org appears in almost every real page that has
# structured data in any of the three syntaxes, so a single substring test
# skips the three regex passes on the common negative path.
_SCHEMA_HINT = "schema.org"


def html_has_structured_data(html):
    """True/False if `html` was inspected, None if there was nothing to inspect.

    Callers MUST distinguish None from False: None means "we did not look",
    which is not evidence of absence. Writing False for None reintroduces the
    exact default-value false positive this module exists to remove.
    """
    if not html or not html.strip():
        return None
    if _LD_JSON_TYPE_RE.search("".join(_SCRIPT_TAG_RE.findall(html))):
        return True
    if _ITEMTYPE_RE.search(html) or (
            _ITEMSCOPE_RE.search(html) and _SCHEMA_HINT in html):
        return True
    if _RDFA_RE.search(html):
        return True
    return False


def structured_data_evidence(syntax):
    """Human-readable provenance for the evidence payload / operator log."""
    return {
        "json_ld": "JSON-LD (application/ld+json)",
        "microdata": "microdata (itemscope/itemtype schema.org)",
        "rdfa": "RDFa (vocab/typeof/property schema.org)",
    }.get(syntax, syntax)


def detect_structured_data_syntax(html):
    """Which syntax carried the schema, for operator-facing evidence.

    Returns None when nothing was found or nothing could be inspected.
    """
    if not html or not html.strip():
        return None
    if _LD_JSON_TYPE_RE.search("".join(_SCRIPT_TAG_RE.findall(html))):
        return "json_ld"
    if _ITEMTYPE_RE.search(html) or (
            _ITEMSCOPE_RE.search(html) and _SCHEMA_HINT in html):
        return "microdata"
    if _RDFA_RE.search(html):
        return "rdfa"
    return None

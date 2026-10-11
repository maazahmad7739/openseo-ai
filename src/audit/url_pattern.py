"""Shared URL-pattern → page_type inference (plan/23 §3.2 + plan/26 Phase 1).

The single URL-archetype table both the audit context adapter and the
generic sitemap ingestion read. Lived in audit/context_adapter.py until
Phase 1 needed it for batch ingestion — moved here so there is no second
copied table (same no-fork rule as the generator cue sets).
"""

URL_PATTERN_PAGE_TYPES = (
    ("/products/", "product"),
    ("/collections/", "collection"),
    ("/blog/", "blog"),
    ("/guides/", "blog"),
    ("/pages/", "page"),
    ("/articles/", "blog"),
)


def page_type_from_url(url):
    """Deterministic page_type from the URL archetype. Unknown -> 'unknown'."""
    path = _path_of(url)
    for pattern, page_type in URL_PATTERN_PAGE_TYPES:
        if pattern in path:
            return page_type
    return "unknown"


# ─────────────────────────────────────────────────────────────
# Template identity (plan/32)
# ─────────────────────────────────────────────────────────────
# `pages.template` was only ever written by the DataForSEO crawl provider
# (connectors/sync.py). Every locally-fetched page — the generic sitemap
# ingest and the on-demand audit — left it NULL, so generator 3 could not
# group findings by template and emitted one recommendation PER PAGE for a
# single template-wide defect (18 near-identical rows on kineticgrowth.co.in
# for one JSON-LD gap).
#
# When the provider supplies a real template name ("product.liquid") that
# value always wins. This is the fallback for everything else, and it is
# deliberately namespaced `urlarchetype:<x>` so an inferred grouping can
# never be mistaken for a server-reported one:
#
#   * precise, real templates group exactly;
#   * inferred ones group by URL archetype, which is the finest identity
#     available without server-side template names. On a brochure site where
#     every page is `<slug>.html` at the root, that is one "page" bucket —
#     which is the correct grouping for aggregation, since those pages do
#     share a template.
#
# It is a heuristic, not ground truth. Anything that must know the real
# template needs the crawl provider.

_INFERRED_TEMPLATE_PREFIX = "urlarchetype:"


def _path_of(url):
    """URL -> path component only (host and query/fragment stripped).

    The host must go: without stripping it, the site root
    `https://x.com/` yields leaf 'x.com', which looks like a file-shaped
    leaf and gets bucketed as a flat page instead of the homepage.
    """
    rest = (url or "").split("://", 1)[-1]
    slash = rest.find("/")
    path = rest[slash:] if slash != -1 else ""
    return path.split("?", 1)[0].split("#", 1)[0].lower()


def template_from_url(url):
    """Inferred template identity for a URL, or None when unknowable.

    Derived from the URL archetype, NOT from the page's HTML: a real Liquid
    template name is only available from the crawl provider, and inferring
    one from markup would be a guess dressed as data.
    """
    path = _path_of(url)
    if not path or path.strip("/") == "":
        # Site root — the homepage template.
        return f"{_INFERRED_TEMPLATE_PREFIX}home"
    page_type = page_type_from_url(url)
    if page_type != "unknown":
        return f"{_INFERRED_TEMPLATE_PREFIX}{page_type}"
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    if not leaf:
        return f"{_INFERRED_TEMPLATE_PREFIX}home"
    # A file-shaped leaf (`why-us.html`) is a flat content page; anything
    # else at an unrecognised path is its own archetype, bucketed as
    # 'other' rather than pretending to be more specific than we are.
    if "." in leaf:
        return f"{_INFERRED_TEMPLATE_PREFIX}flatpage"
    return f"{_INFERRED_TEMPLATE_PREFIX}other"


def is_inferred_template(template):
    """True when `template` came from template_from_url, not a provider."""
    return bool(template) and str(template).startswith(_INFERRED_TEMPLATE_PREFIX)

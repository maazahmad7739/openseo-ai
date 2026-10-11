"""Tenant-scoped grounding corpus for off-page community replies (plan/33).

This module answers one question and refuses to answer any other: WHAT MAY
THE DRAFTER SAY ABOUT THIS CLIENT'S PRODUCTS?

The answer is a frozen, hashed corpus built from exactly one source - the
tenant's own `pages.body_html` (plan/22:123, the only durable per-tenant
product copy in the schema), ranked by the existing generated relevance
column on `page_query_match_scores`.

No competitor text, no SERP body, no third-party catalogue, and no other
tenant's data may enter this corpus. The tenant boundary is enforced
structurally by the composite FK on `offpage_drafts`
`(site_id, recommendation_id) -> recommendations (site_id, recommendation_id)`
(plan/33), so a draft cannot carry a site_id that disagrees with the
recommendation it claims to answer. The loader takes `site_id` from that
recommendation and never accepts a caller-supplied tenant, which means
there is no argument through which a caller can ask for another tenant's
catalogue.

Why a hash: publishing is MANUAL and may happen days after generation.
`grounding_corpus_hash` is md5 of the concatenated corpus, so at audit time
we can prove precisely which of the client's pages the model was able to
see when it wrote the draft. The alternative - trusting that a row's
`validation` blob was written honestly - is not an audit trail.
"""

import hashlib
import html
import re

# ------------------------------------------------------------
# Corpus bounds. These are a prompt-budget decision, not a quality knob:
# a larger corpus does not produce a more useful reply, it produces a
# slower one that dilutes the tenant's own facts with marginal pages.
# ------------------------------------------------------------
CORPUS_MAX_PAGES = 12
CORPUS_MAX_CHARS = 6000

# A page whose body is shorter than this contributes no usable facts. In
# practice the short tail is navigation, size charts and shipping copy,
# which would enter the fact base as noise and make the grounding
# tripwire satisfiable by boilerplate.
CORPUS_MIN_BODY_CHARS = 120

# ------------------------------------------------------------
# HTML -> text normalisation
# ------------------------------------------------------------
# NOTE: `fixes.generator._body_to_text` strips tags but does NOT unescape
# entities. That is survivable for a 60-char meta description, where a
# mismatched entity is cosmetic. It is not survivable here, because this
# text is the SUBSTRING MATCH DOMAIN for `verify_grounded_claims`: a draft
# claiming "caf&eacute;" against a page saying "café" fails the grounding
# check on a true statement, and the failure is indistinguishable from a
# hallucination. `connectors.sync._body_text_hash` already unescapes; this
# matches that behaviour deliberately.
_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)\b.*?</\1>",
                           re.I | re.S)
_BLOCK_END = re.compile(r"</(p|div|li|tr|h[1-6]|br)\s*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def body_to_text(body_html):
    """Visible text of an ingested body, entity-decoded and normalised.

    Block-level close tags become sentence boundaries so that the
    sentence-level grounding tripwire in the drafter splits on real
    sentence ends rather than mid-clause.
    """
    if not body_html:
        return ""
    text = _SCRIPT_STYLE.sub(" ", body_html)
    text = _BLOCK_END.sub(". ", text)
    text = _ANY_TAG.sub(" ", text)
    text = html.unescape(text)
    text = _WS.sub(" ", text).strip()
    return text


def corpus_hash(corpus_text):
    """Stable md5 of the normalised corpus. Same pages in the same order
    always produce the same hash, so a redraft can be compared against
    the previous version to detect that the underlying catalogue moved."""
    return hashlib.md5((corpus_text or "").encode("utf-8")).hexdigest()


def load_brand_corpus(conn, rec, limit=CORPUS_MAX_PAGES):
    """Build the tenant fact base for one off-page recommendation.

    Returns a dict:
        text      normalised, lowercased corpus (the fact base)
        sources   [{page_id, url, title, chars}] for provenance/UI
        page_ids  the UUIDs that fed `grounding_page_ids`
        hash      md5 of `text`
        chars     len(text)
        empty     True when the tenant has no usable catalogue text

    Ranking is `page_query_match_scores.aggregate_score`, the same
    generated column the on-page pipeline already uses:
        0.30*intent_page_type + 0.30*catalogue_match
      + 0.25*gsc_evidence  + 0.15*semantic_content
    so the pages surfaced here are the ones most likely to be the product
    the thread is actually about, rather than merely the tenant's
    best-selling anything.

    `site_id` is read from `rec` and never taken as a parameter. The
    query also filters `body_html IS NOT NULL AND btrim(body_html) <> ''`
    because `connectors/sync.py` writes an INSERT fallback whose `title`
    column is bound to the URL, not a real title - so `pages.title` is not
    trustworthy on its own and a page with no body contributes no facts
    rather than a URL masquerading as one.
    """
    site_id = rec["site_id"]
    cluster_id = rec.get("cluster_id")

    if not cluster_id:
        # No cluster means no relevance signal, and relevance is what keeps
        # the corpus about THIS thread. Returning empty makes the caller
        # fail closed at the grounding gate rather than dump the whole
        # catalogue at the model.
        return _empty_corpus()

    parts = []
    sources = []
    page_ids = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.page_id, p.url, p.title, p.body_html
            FROM page_query_match_scores s
            JOIN pages p
              ON p.page_id = s.page_id
             AND p.site_id = s.site_id
            WHERE s.site_id = %(site_id)s
              AND s.cluster_id = %(cluster_id)s
              AND p.page_type IN ('product', 'collection')
              AND p.body_html IS NOT NULL
              AND btrim(p.body_html) <> ''
            ORDER BY s.aggregate_score DESC NULLS LAST, p.url
            LIMIT %(limit)s
            """,
            {"site_id": site_id, "cluster_id": cluster_id, "limit": limit},
        )
        for page_id, url, title, body_html in cur.fetchall():
            text = body_to_text(body_html)
            if len(text) < CORPUS_MIN_BODY_CHARS:
                # Too thin to be facts. Dropping it here means the
                # grounding tripwire cannot be satisfied by a page whose
                # only content is a size chart.
                continue
            if title:
                parts.append(title)
            parts.append(text)
            page_ids.append(page_id)
            sources.append({"page_id": str(page_id), "url": url,
                            "title": title, "chars": len(text)})

    corpus = _WS.sub(" ", " ".join(parts)).strip().lower()
    if len(corpus) > CORPUS_MAX_CHARS:
        # Truncate on a SOURCE boundary, not mid-sentence: a corpus cut
        # mid-sentence produces a fact base whose last entry is a
        # fragment, which then reads as an ungrounded claim.
        corpus = _truncate_on_boundary(corpus)

    if not corpus.strip():
        return _empty_corpus()
    return {
        "text": corpus,
        "sources": sources,
        "page_ids": page_ids,
        "hash": corpus_hash(corpus),
        "chars": len(corpus),
        "empty": False,
    }


def _truncate_on_boundary(corpus, limit=CORPUS_MAX_CHARS):
    head = corpus[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut > limit // 2:
        return head[:cut + 1].strip()
    return head.strip()


def _empty_corpus():
    return {"text": "", "sources": [], "page_ids": [], "hash": None,
            "chars": 0, "empty": True}


def corpus_is_usable(corpus):
    """Grounding gate: a corpus too small to confirm anything must refuse.

    Distinct from `empty` - a tenant can have a catalogue and still have
    too little of it to ground a product claim. Returning False here makes
    the drafter emit an advice-only draft (or drop) instead of
    hallucinating, which is the whole point of the tenant boundary.
    """
    return (not corpus.get("empty")) and corpus.get("chars", 0) >= 200

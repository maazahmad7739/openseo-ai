"""Deterministic keyword-cluster inference for the audit engine
(plan/23 §1 step 4, §6 Phase B item 1).

No LLM. The candidate waterfall (in order, stopping at the first source
that yields any candidate):

  1. title ∩ H1 ∩ body tokens  — the strongest grounded signal
  2. title ∩ H1                — the page's own naming agreement
  3. H1 ∩ body-frequency terms — body-led fallback
  4. title tokens (top phrase) — last resort
  5. domain handle             — absolute fallback (never empty output)

Candidates are ≤3 (MAX_CANDIDATES), ranked by a deterministic score:
(phrase length desc, intersection-source priority, body-frequency
support). The PRIMARY is candidates[0]; when the top two tie
(`ambiguous()`), the engine MAY spend one keyword_volume call to
disambiguate — otherwise candidates[0] wins.

Intent inference REUSES the cue sets from src/fixes/generator.py
(INTENT_TRANSACTIONAL_CUES / _COMMERCIAL_ / _INFORMATIONAL_) — no
duplicate tables (plan/23 §6 Phase B). Navigational is not inferred in
v1 (nothing in a page's own copy grounds it); 'unknown' is the honest
default and the validator treats it as pass-through, same as batch.
"""

import re
from collections import Counter

from fixes.generator import (INTENT_COMMERCIAL_CUES,
                             INTENT_INFORMATIONAL_CUES,
                             INTENT_TRANSACTIONAL_CUES)

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do",
    "for", "from", "has", "have", "how", "in", "is", "it", "its", "of",
    "on", "or", "our", "that", "the", "their", "them", "these", "this",
    "to", "was", "we", "were", "what", "when", "where", "which", "who",
    "will", "with", "you", "your", "now", "new", "all", "more", "also",
    "about", "into", "out", "not", "no", "so", "than", "then", "there",
    "they", "been", "being", "over", "just", "only", "get", "here", "one",
    "two", "use", "using",
}

MAX_CANDIDATES = 3
MIN_TOKEN_LEN = 3
BODY_FREQ_LIMIT = 40

_WORD_RE = re.compile(r"[a-z0-9']{3,}")
_URL_SLUG_RE = re.compile(r"[-_+]+")


def _tokens(text):
    """Lowercased word tokens: stopwords dropped, length ≥ 3."""
    return [t for t in _WORD_RE.findall((text or "").lower())
            if t not in STOPWORDS]


def _token_set(text):
    return set(_tokens(text))


def body_term_counts(body_text, limit=None):
    """Token -> count, descending; alphabetical tie-break so equal-count
    orderings never flip between runs (determinism invariant)."""
    counts = Counter(_tokens(body_text))
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if limit:
        items = items[:limit]
    return dict(items)


def _phrases(text, max_size=3):
    """Contiguous token phrases (longest-first windows) + singles, e.g.
    'Aurora Desk Lamp — Warm LED' -> ['aurora desk lamp', 'aurora desk',
    'aurora', ...]. Documented order: size 3, then 2, then 1."""
    toks = _tokens(text)
    phrases = []
    for size in range(max_size, 0, -1):
        for i in range(len(toks) - size + 1):
            phrase = " ".join(toks[i:i + size])
            if phrase not in phrases:
                phrases.append(phrase)
    return phrases


# ------------------------------------------------------------
# Intent (cue sets reused from fixes.generator — no duplicates)
# ------------------------------------------------------------

def infer_intent(title, h1=None, body_text=None):
    """Deterministic intent: transactional / commercial / informational /
    unknown. Only CONTRADICTION-free mapping, mirroring the validator's
    semantics in fixes.generator.intent_match_check."""
    blob = " ".join(filter(None, [title, body_text or "", h1 or ""])).lower()
    if not blob.strip():
        return "unknown"
    has_txn = any(c in blob for c in INTENT_TRANSACTIONAL_CUES)
    has_comm = any(c in blob for c in INTENT_COMMERCIAL_CUES)
    has_info = any(c in blob for c in INTENT_INFORMATIONAL_CUES)
    if has_txn:
        return "transactional"
    if has_comm:
        return "commercial"
    if has_info:
        return "informational"
    return "unknown"


# ------------------------------------------------------------
# Candidates (waterfall)
# ------------------------------------------------------------

def infer_candidates(title, h1=None, body_text=None):
    """≤3 ranked candidates from the waterfall. Returns a list of
    {"keyword", "source", "score"} — deterministic: the same page copy
    always yields the same list in the same order."""
    title = (title or "").strip()
    h1 = (h1 or "").strip()
    if not title and not h1:
        return []

    title_phrases = _phrases(title)
    h1_phrases = _phrases(h1)
    body_set = set(body_term_counts(body_text, BODY_FREQ_LIMIT))

    def _intersection_len(phrases_a, phrases_b):
        common = set(phrases_a) & set(phrases_b)
        return max((len(p.split()) for p in common), default=0), \
            next(iter(sorted(common)), None)

    candidates = []

    def _add(phrase, source):
        if not phrase or any(c["keyword"] == phrase for c in candidates):
            return
        candidates.append({
            "keyword": phrase,
            "source": source,
            "score": (len(phrase.split()),
                      -len(candidates),
                      sum(1 for t in phrase.split() if t in body_set)),
        })

    # 1. title ∩ H1 ∩ body: the longest shared phrase whose tokens the
    #    body also supports.
    shared = set(title_phrases) & set(h1_phrases)
    if shared:
        body_backed = [p for p in sorted(shared, key=lambda p: (-len(p.split()), p))
                       if all(t in body_set for t in p.split())]
        if body_backed:
            _add(body_backed[0], "title_h1_body")

    # 2. title ∩ H1 (no body requirement)
    if not candidates and shared:
        _add(sorted(shared, key=lambda p: (-len(p.split()), p))[0],
             "title_h1")

    # 3. H1 ∩ body-frequency terms
    if not candidates and h1_phrases:
        body_backed = [p for p in h1_phrases
                       if all(t in body_set for t in p.split())]
        if body_backed:
            _add(body_backed[0], "h1_body")

    # 4. title top phrase
    if not candidates and title_phrases:
        _add(title_phrases[0], "title")

    return candidates[:MAX_CANDIDATES]


def _domain_handle(url):
    """'lume-store.com' -> 'lume store' (absolute fallback candidate)."""
    host = (url or "").split("://", 1)[-1].split("/", 1)[0]
    host = host.split(":", 1)[0].lower()
    if host.startswith("www."):
        host = host[4:]
    handle = _URL_SLUG_RE.sub(" ", host.split(".")[0]).strip()
    return handle or None


def ambiguous(candidates):
    """Tie between the top two candidates (equal score tuples) — the
    engine's cue to optionally spend ONE keyword_volume call
    (plan/23 §5.3)."""
    if len(candidates) < 2:
        return False
    return candidates[0]["score"] == candidates[1]["score"]


def infer(url, title=None, h1=None, body_text=None):
    """The §1-step-4 entry: candidates + primary + intent in one call.

    Returns {"candidates": [...≤3...], "primary_keyword": str|None,
             "intent": str, "ambiguous": bool}.
    The output is never empty of a primary when a URL is given (domain
    handle is the absolute fallback).
    """
    cands = infer_candidates(title, h1, body_text)
    primary = cands[0]["keyword"] if cands else None
    if not primary and url:
        handle = _domain_handle(url)
        if handle:
            primary = handle
            cands = [{"keyword": handle, "source": "domain_handle",
                      "score": (len(handle.split()), 0, 0)}]
    return {
        "candidates": cands,
        "primary_keyword": primary,
        "intent": infer_intent(title, h1, body_text),
        "ambiguous": ambiguous(cands),
    }
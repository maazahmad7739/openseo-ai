"""Fix generator hook (plan/21 §2.1): approved recommendation -> concrete fix.

Deterministic decision logic picks WHAT (target, field, write target);
v1 drafts the VALUE deterministically (LLM drafting lands later —
generation_source records which path produced the row); a deterministic
validator gates the draft before any row is written. Generation happens
at recommendation approval; the operator then reviews the DIFF as the
second gate.
"""

import json
import re
from datetime import datetime, timedelta, timezone

from fixes.policy import PolicyBlocked, check_conflict

# Title-quality gates (plan/21 §2.1 checks 3 + validator denylist).
TITLE_MIN_CHARS = 20
TITLE_MAX_CHARS = 60
BANNED_TITLE_PATTERNS = ("|", "»", "«", "★", "→", "free shipping", "best price")

# plan/21 §2.1 check 6 — protect winners: position ≤2 + rising CTR -> never
# rewrite. Protection only engages when the GSC sample is meaningful, so
# sparse data can never fabricate a "winner".
PROTECT_POSITION_MAX = 2.0
PROTECT_MIN_IMPRESSIONS = 100

# plan/21 §2.1 check 2 — intent match: deterministic framing cues. A title
# fails intent match only on CONTRADICTION (informational intent + hard
# transactional push, or buying intent with purely informational framing);
# a neutral title is never a mismatch.
INTENT_TRANSACTIONAL_CUES = ("buy", "shop", "order", "price", "sale",
                             "discount", "cart", "checkout", "for sale")
INTENT_COMMERCIAL_CUES = ("best", "top", "review", "reviews", "vs",
                          "compare", "comparison", "cheap", "affordable",
                          "deal", "deals")
INTENT_INFORMATIONAL_CUES = ("guide", "how to", "what is", "learn", "tips",
                             "tutorial", "ideas", "meaning", "examples")

# Emoji blocks (misc symbols/pictographs + variation selector): titles are
# plain text; any glyph from these ranges is a denylist hit.
_EMOJI_RANGES = ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x1F1E6, 0x1F1FF))


class FixGenerationError(Exception):
    pass


def _body_to_text(body_html):
    """Strip HTML to visible text for the meta draft lead (grounded only:
    an ingested body is described by itself, never fabricated)."""
    if not body_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", body_html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class FixNotSupported(FixGenerationError):
    """Recommendation is real but has no v1 fix path (plan_only surface)."""


# ------------------------------------------------------------
# LLM candidate drafting (plan/21 §2.1 "agent drafts ≤3 candidates"):
# constrained LLM -> deterministic fact-check -> deterministic validator.
# Every stage fails SOFT: any LLM/parse/credential error degrades to the
# deterministic baseline drafter (a fix is never lost to provider outage,
# and a bad LLM draft is never written).
# ------------------------------------------------------------

LLM_CANDIDATE_COUNT = 3        # hard cap on candidates requested per field


class LlmDraftError(Exception):
    """Typed LLM drafting failure (config, network, schema) — caller falls
    back to the deterministic drafter."""


def _llm_client():
    """Resolve the configured provider; raises LlmDraftError (soft) on any
    construction problem (missing credentials, import failure)."""
    try:
        from agents.ollama_client import OllamaClient
        return OllamaClient()
    except Exception as exc:  # OllamaConfigError + anything unexpected
        raise LlmDraftError(f"llm client unavailable: {exc}") from None


def build_draft_prompt(rec):
    """Grounding payload for the LLM drafter: verified page facts, cluster
    intent, competitor framing cues. Facts come ONLY from the page's own
    data — competitor text is labeled a style reference, never as facts."""
    body_text = (rec.get("body_text") or rec.get("body_text_excerpt") or "")[:600]
    competitors = rec.get("competitor_context") or {}
    return {
        "page": {
            "title": rec.get("page_title"),
            "body_text_excerpt": body_text,
            "page_type": rec.get("page_type"),
        },
        "cluster": {
            "primary_keyword": rec.get("primary_keyword"),
            "intent": rec.get("intent"),
        },
        "competitor_serp_style_reference": {
            "titles": [t.get("title") for t in (competitors.get("titles") or [])[:5]],
            "snippets": [s.get("snippet") for s in (competitors.get("snippets") or [])[:5]],
            "note": ("Structural/framing reference ONLY. Never copy wording "
                     "or claim any fact that is not in page facts."),
        },
        "constraints": {
            "title_chars": f"{TITLE_MIN_CHARS}-{TITLE_MAX_CHARS}",
            "meta_chars": f"{META_MIN_CHARS}-{META_MAX_CHARS}",
            "primary_keyword_must_start_in_first_half_of_title": True,
            "meta_keyword_required": True,
            "no_banned_patterns": list(BANNED_TITLE_PATTERNS) + list(BANNED_META_PATTERNS),
            "no_emoji_no_allcaps_runs_no_spam_punctuation": True,
            "no_double_quotes_in_meta": True,
            "brand_name_at_most_once": True,
            "invent_no_facts": ("Every specific claim (materials, warranty, "
                                "shipping, certifications, specs, numbers) "
                                "must appear in the page facts."),
        },
        "output_schema": {
            "titles": f"up to {LLM_CANDIDATE_COUNT} strings, "
                      f"{TITLE_MIN_CHARS}-{TITLE_MAX_CHARS} chars each",
            "metas": f"up to {LLM_CANDIDATE_COUNT} strings, "
                     f"{META_MIN_CHARS}-{META_MAX_CHARS} chars each",
        },
    }


DRAFT_SYSTEM_PROMPT = """You are an SEO copy drafter. You receive verified page
facts, the target keyword cluster, and competitor SERP titles/snippets as a
STYLE reference only.

Return ONLY JSON: {"titles": [...], "metas": [...]} — up to 3 candidates each.

Hard rules:
- Use ONLY facts present in page.title or page.body_text_excerpt. Never claim
  specs, materials, certifications, shipping, warranties, or numbers that are
  not in the page facts.
- Borrow competitor FRAMING (e.g. leading with 'Best', a year, a count) but
  never copy competitor wording.
- Respect the character bounds exactly. Include the primary keyword (start it
  within the first half of titles).
- No emoji, no ALL-CAPS shouting, no spam punctuation, no double quotes in
  metas, no banned patterns, brand name at most once.
- If you cannot produce a candidate that obeys all rules, return fewer
  candidates — never invent facts to fill one."""


def llm_draft_candidates(rec, client=None):
    """Ask the LLM for title + meta candidates. Returns
    (titles: list[str], metas: list[str]) — possibly empty. Raises
    LlmDraftError on config/network/schema failure (caller falls back)."""
    client = client or _llm_client()
    payload = build_draft_prompt(rec)
    try:
        answer = client.complete_json(DRAFT_SYSTEM_PROMPT, payload, retries=1)
    except Exception as exc:
        raise LlmDraftError(f"llm call failed: {exc}") from None
    if not isinstance(answer, dict):
        raise LlmDraftError("llm answer is not a JSON object")
    titles = answer.get("titles") or []
    metas = answer.get("metas") or []
    if not isinstance(titles, list) or not isinstance(metas, list):
        raise LlmDraftError("llm answer schema invalid (titles/metas not lists)")
    clean_titles = [t.strip() for t in titles[:LLM_CANDIDATE_COUNT]
                    if isinstance(t, str) and t.strip()]
    clean_metas = [m.strip() for m in metas[:LLM_CANDIDATE_COUNT]
                   if isinstance(m, str) and m.strip()]
    return clean_titles, clean_metas


# ------------------------------------------------------------
# Deterministic fact-checking guard (verify_grounded_claims)
# ------------------------------------------------------------

# Claim-cue vocabulary: sensitive/specific commercial claim types an LLM
# might hallucinate. Each cue must be substantiated by the page's own facts.
CLAIM_CUES = (
    # materials & product attributes
    "organic", "leather", "cotton", "vegan", "recycled", "sustainable",
    "waterproof", "water-resistant", "wireless", "bluetooth",
    "noise cancelling", "handmade", "handcrafted", "premium", "luxury",
    # certifications & standards
    "certified", "fda", "gmp", "iso", "fair trade",
    # commercial promises
    "free shipping", "free delivery", "free returns", "money-back",
    "lifetime warranty", "warranty", "guarantee", "30-day", "60-day",
    "same-day", "next-day", "cash on delivery", "installments",
    # fact-asserting superlatives
    "award-winning", "best-selling", "number one", "#1", "clinically proven",
    "doctor recommended", "eco-friendly", "non-toxic", "bpa free",
)

# Numeric claims ("40-hour", "45 dB", "22 pairs", "50% off") must exist in
# the page facts verbatim (whitespace-normalized).
_CLAIM_NUMBER = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*[- ]?\s*(?:hours?|hrs?|days?|db|watts?|w\b|mah|"
    r"lbs?|kgs?|oz|ml|g\b|inch(?:es)?|mm|cm|m\b|pairs?|years?|percent|%|off)\b",
    re.I)


def _claim_tokens(text):
    """Extract claim cues + numeric claims present in a candidate."""
    lowered = (text or "").lower()
    cues = [cue for cue in CLAIM_CUES if cue in lowered]
    numbers = [re.sub(r"\s+", " ", m.group(0).strip().lower())
               for m in _CLAIM_NUMBER.finditer(text or "")]
    return cues, numbers


def _fact_base(rec):
    """The ONLY text a candidate's claims may draw from: page title + the
    page's own body text (grounded facts), lowercased, whitespace-normalized."""
    parts = [(rec.get("page_title") or ""),
             (rec.get("body_text") or rec.get("body_text_excerpt") or "")]
    return re.sub(r"\s+", " ", " ".join(parts)).lower()


def verify_grounded_claims(candidate, rec):
    """Fact-check one LLM candidate against the page's own verified facts.

    Returns (ok: bool, problems: list[str]). Every claim cue or numeric
    claim in the candidate must appear in the page's title/body text;
    otherwise the candidate is REJECTED (hallucination guard). Purely
    deterministic; never touches the network.
    """
    problems = []
    if not (candidate or "").strip():
        return False, ["empty candidate"]
    fact_base = _fact_base(rec)
    cues, numbers = _claim_tokens(candidate)
    for cue in cues:
        if cue in fact_base:
            continue
        # Inflected fact forms: a "30-day" claim is grounded by "30 days"
        # in the body (and vice versa); check the number + unit word.
        inflected = _claim_number_inflections(cue)
        if inflected and any(inf in fact_base for inf in inflected):
            continue
        problems.append(f"ungrounded claim: {cue!r} not in page facts")
    for number in numbers:
        if number in fact_base:
            continue
        # Numeric claims match flexibly: "30-day" <-> "30 days" <-> "30 day".
        variants = {number,
                    number.replace("-", " "),
                    re.sub(r"(\d+)\s*day\b", r"\1 days", number),
                    re.sub(r"(\d+)\s*days?\b", r"\1-day", number),
                    re.sub(r"(\d+)\s*hours?\b", r"\1-hour", number),
                    re.sub(r"(\d+)\s*-?\s*hours?\b", r"\1 hours", number)}
        if not any(v in fact_base for v in variants):
            problems.append(f"ungrounded numeric claim: {number!r} not in page facts")
    return (not problems), problems


def _claim_number_inflections(cue):
    """For a hyphenated number-cue ('30-day'), generate the body-side
    inflections that would ground it."""
    m = re.match(r"^(\d+)\s*-\s*(\w+)$", cue)
    if not m:
        return []
    num, unit = m.groups()
    return [f"{num} {unit}", f"{num} {unit}s", f"{num}-{unit}s"]


# ------------------------------------------------------------
# LLM -> fact-check -> validator -> fallback pipeline
# ------------------------------------------------------------

def draft_candidates_with_fallback(rec, client=None, fact_check=True):
    """Full drafting pipeline (plan/21 §2.1 value generation, now LLM-first).

    1. LLM drafts up to 3 candidates per field (grounded prompt).
    2. verify_grounded_claims rejects hallucinated candidates.
    3. Survivors run the existing deterministic validators.
    4. First fully-passing candidate wins per field.
    5. No LLM / all candidates fail -> deterministic baseline drafts
       (draft_title / draft_meta_description) with NO error — the pipeline
       always produces a best-effort draft or None.

    Returns dict:
        {"title": str|None, "meta": str|None,
         "title_source": "llm"|"deterministic",
         "meta_source": "llm"|"deterministic",
         "rejected": [{"field", "candidate", "problems"}]}
    """
    out = {"title": None, "meta": None,
           "title_source": "deterministic", "meta_source": "deterministic",
           "rejected": []}
    try:
        llm_titles, llm_metas = llm_draft_candidates(rec, client=client)
    except LlmDraftError:
        llm_titles, llm_metas = [], []

    intent = rec.get("intent")
    site_name = rec.get("site_name")
    keyword = rec.get("primary_keyword")

    # --- titles ---
    for candidate in llm_titles:
        if fact_check:
            ok, claims = verify_grounded_claims(candidate, rec)
            if not ok:
                out["rejected"].append({"field": "title", "candidate": candidate,
                                        "problems": claims})
                continue
        problems = validate_title_draft(candidate, keyword,
                                        product_title=rec.get("page_title"),
                                        intent=intent, site_name=site_name)
        if problems:
            out["rejected"].append({"field": "title", "candidate": candidate,
                                    "problems": problems})
            continue
        out["title"] = candidate
        out["title_source"] = "llm"
        break

    # --- metas ---
    for candidate in llm_metas:
        if fact_check:
            ok, claims = verify_grounded_claims(candidate, rec)
            if not ok:
                out["rejected"].append({"field": "meta", "candidate": candidate,
                                        "problems": claims})
                continue
        problems = validate_meta_draft(candidate, keyword,
                                       intent=intent, site_name=site_name)
        if problems:
            out["rejected"].append({"field": "meta", "candidate": candidate,
                                    "problems": problems})
            continue
        out["meta"] = candidate
        out["meta_source"] = "llm"
        break

    # --- deterministic fallback (never throws) ---
    if out["title"] is None:
        out["title"] = draft_title(rec)
        out["title_source"] = "deterministic"
    if out["meta"] is None:
        out["meta"] = draft_meta_description(rec)
        out["meta_source"] = "deterministic"
    return out


# ------------------------------------------------------------
# SERP competitor grounding (plan/21 §2.1/§2.2 — gap closure)
# ------------------------------------------------------------

COMPETITOR_POSITION_MAX = 10   # top-1..10 organic results feed the patterns
COMPETITOR_LIMIT = 10          # cap rows read per cluster (payload bound)


def load_competitor_context(conn, site_id, cluster_id, target_url,
                            reference_date=None):
    """Top-1..10 NON-self organic results for the target's cluster, latest
    snapshot. Returns a dict of deterministic pattern extracts:

        titles:       list[dict] {title, position, pattern}
        snippets:     list[dict] {snippet, position, hooks}
        url_patterns: list[str] archetype segments (deduped, ordered)
        heading_hints: list[str] structural tokens seen across titles
                       (How to / Best / vs / Review / Year markers)

    Empty dict when no SERP rows exist (grounding degrades gracefully to
    the prior keyword-only behavior — never blocks a fix).
    """
    if not cluster_id:
        return {}
    end = reference_date or datetime.now(timezone.utc).date()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT result_title, result_snippet, result_url, url_pattern, position
            FROM openseo_serp_snapshots
            WHERE site_id = %s AND cluster_id = %s
              AND is_self = false
              AND position <= %s
              AND snapshot_date >= %s::date - INTERVAL '35 days'
            ORDER BY snapshot_date DESC, position ASC
            LIMIT %s
            """,
            (site_id, cluster_id, COMPETITOR_POSITION_MAX, end, COMPETITOR_LIMIT),
        )
        rows = cur.fetchall()
    if not rows:
        return {}

    titles = []
    snippets = []
    patterns = []
    seen_urls = set()
    for title, snippet, url, pattern, position in rows:
        if url in seen_urls:
            continue  # one row per competitor URL (latest snapshot wins)
        seen_urls.add(url)
        if title:
            titles.append({"title": title.strip(), "position": position,
                           "pattern": _title_framing(title, position)})
        if snippet:
            snippets.append({"snippet": snippet.strip(), "position": position,
                             "hooks": _snippet_hooks(snippet)})
        if pattern:
            patterns.append(pattern)
    return {"titles": titles, "snippets": snippets,
            "url_patterns": list(dict.fromkeys(patterns))}


# Framing tokens (deterministic; extracted, never copied). Ordered by
# specificity: a "vs / compare" title frames differently from a guide.
_FRAMING_YEAR = re.compile(r"\b(20\d{2})\b")
_FRAMING_COUNT = re.compile(r"\b(\d{1,3})\s+(best|top|things|tips|ways|pairs|reasons)\b", re.I)
_FRAMING_HOOKS = (
    ("guide", "guide"),
    ("vs", "vs"),
    ("compare", "compare"),
    ("review", "review"),
    ("best", "best"),
    ("how to", "how-to"),
    ("worth it", "worth-it"),
)


def _title_framing(title, position):
    """Structural framing of one competitor title (deterministic tokens)."""
    t = (title or "").lower()
    framing = []
    for token, name in _FRAMING_HOOKS:
        if token in t:
            framing.append(name)
    m = _FRAMING_YEAR.search(t)
    if m:
        framing.append(f"year:{m.group(1)}")
    m = _FRAMING_COUNT.search(t)
    if m:
        framing.append(f"count:{m.group(1)}")
    if "|" in title or "—" in title or " - " in t:
        framing.append("split-separator")
    return framing


_SNIPPET_VALUE_CUES = (
    "free", "tested", "compare", "returns", "warranty", "battery", "hours",
    "dB", "waterproof", "ship", "guide", "checklist", "verified")


def _snippet_hooks(snippet):
    """Value-prop / hook tokens present in one competitor snippet."""
    s = (snippet or "").lower()
    return [cue for cue in _SNIPPET_VALUE_CUES if cue.lower() in s]


def _competitor_prefix(rec):
    """Pick the strongest grounded framing token to lead the draft with.

    Deterministic: highest-frequency token across top-3 competitor titles
    (ties -> lower position wins -> alphabetical). Returns a SHORT lead
    fragment ('Best ', '2026 ', 'Tested: ') or None when no clear pattern.
    Never copies competitor wording verbatim — only the structural cue.
    """
    competitors = rec.get("competitor_context") or {}
    titles = competitors.get("titles") or []
    if not titles:
        return None
    from collections import Counter
    token_scores = Counter()
    for entry in titles[:3]:
        for token in entry.get("pattern") or []:
            if token in ("split-separator",):
                continue  # punctuation, not a framing hook
            token_scores[token] += 1
    if not token_scores:
        return None
    best, _count = token_scores.most_common(1)[0]
    if best.startswith("year:"):
        return best.split(":", 1)[1] + " "
    if best == "best":
        return "Best "
    if best == "count":
        m = _FRAMING_COUNT.search(titles[0]["title"].lower())
        if m:
            return f"Top {m.group(1)} "
        return "Top "
    if best in ("guide", "how-to"):
        return None  # guide framing is meta/snippet territory, not a title lead
    if best == "review":
        return "Reviewed: "
    return None


def _competitor_snippet_leads(rec, max_leads=3):
    """Distinct hook cues across top competitor snippets, frequency-ordered.
    Used to ORDER the meta draft's value props (positioning insight), while
    the facts themselves still come only from the page's own body copy."""
    competitors = rec.get("competitor_context") or {}
    from collections import Counter
    counts = Counter()
    for entry in (competitors.get("snippets") or [])[:5]:
        for hook in entry.get("hooks") or []:
            counts[hook] += 1
    return [hook for hook, _n in counts.most_common(max_leads)]


# Content structural tokens (plan/21 §2.2 row "content structure"): section
# archetypes the top-ranking pages cover, inferred from title framing + URL
# archetypes. Deterministic; feeds the content-outline hint in evidence and
# the thin-content guard's "missing core angles" signal.
_SECTION_BY_HOOK = {
    "review": "Test results / hands-on findings",
    "guide": "Step-by-step walkthrough",
    "how-to": "Step-by-step walkthrough",
    "vs": "Head-to-head comparison table",
    "compare": "Head-to-head comparison table",
    "best": "Top-picks shortlist",
    "worth-it": "Buying-advice / verdict section",
    "free": "Shipping & returns info",
    "warranty": "Warranty & support info",
    "battery": "Battery & specs detail",
    "tested": "Test results / hands-on findings",
    "returns": "Shipping & returns info",
}

# Coverage cues per section: a section counts as COVERED when the page's own
# body mentions any of these (deterministic; competitor text never counted).
_SECTION_CUES = {
    "Test results / hands-on findings": ("test", "tested", "hands-on", "lab"),
    "Step-by-step walkthrough": ("step", "guide", "how to", "walkthrough"),
    "Head-to-head comparison table": ("compare", "comparison", "versus", " vs "),
    "Top-picks shortlist": ("top pick", "best", "shortlist"),
    "Buying-advice / verdict section": ("worth it", "verdict", "advice", "buying"),
    "Shipping & returns info": ("shipping", "returns", "delivery"),
    "Warranty & support info": ("warranty", "support", "guarantee"),
    "Battery & specs detail": ("battery", "spec", "hours"),
}


def content_outline_gaps(rec, body_text=None):
    """Structural outline guidance for content improvements (plan/21 §2.2):
    competitor SERP patterns -> expected section angles; the page's own
    body text is checked for coverage. Returns:

        expected_sections: ordered list of section angles top competitors
                           consistently cover (frequency >= 2)
        missing_sections:  expected angles with NO cue coverage in the page
                           body (the thin-content / missing-angle signal)
        covered_sections:  expected angles the page already addresses

    Grounded only: a "section" is claimed covered when the page's OWN body
    text contains the cue word — competitor text is never inserted.
    """
    competitors = rec.get("competitor_context") or {}
    from collections import Counter
    hook_counts = Counter()
    for entry in (competitors.get("snippets") or [])[:5]:
        for hook in entry.get("hooks") or []:
            hook_counts[hook] += 1
    n_snippets = len(competitors.get("snippets") or [])
    # A section is "expected" when >= half of the observed competitors cover it.
    expected = []
    for hook, count in hook_counts.most_common():
        if n_snippets and count >= max(2, n_snippets // 2):
            section = _SECTION_BY_HOOK.get(hook)
            if section and section not in expected:
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
    return {"expected_sections": expected,
            "missing_sections": missing,
            "covered_sections": covered}


# ------------------------------------------------------------
# Decision logic inputs
# ------------------------------------------------------------

def _outline_gaps(rec, body_text=None):
    """Heading-grounded outline gaps with graceful degradation (Task 4).

    Prefers real competitor heading structures (audit.competitor_headings,
    bounded to the top few SERP URLs); falls back to the pure
    snippet-hook inference when no scan result is available (offline,
    no competitor URLs, or all fetches failed). Never raises, never
    blocks a fix on grounding unavailability.
    """
    try:
        from audit.competitor_headings import content_outline_gaps_with_headings
        return content_outline_gaps_with_headings(rec, body_text=body_text)
    except Exception:
        return content_outline_gaps(rec, body_text=body_text)


def load_decision_inputs(conn, recommendation_id):
    """Everything the deterministic decision logic reads (data -> home)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT site_id, generator, action_type, target_url, proposed_url, "
            "cluster_id, status FROM recommendations WHERE recommendation_id = %s",
            (recommendation_id,),
        )
        row = cur.fetchone()
    if not row:
        raise FixGenerationError(f"recommendation {recommendation_id} not found")
    rec = {
        "recommendation_id": recommendation_id,
        "site_id": str(row[0]),
        "generator": row[1],
        "action_type": row[2],
        "target_url": row[3],
        "proposed_url": row[4],
        "cluster_id": str(row[5]) if row[5] else None,
        "status": row[6],
    }
    if rec["action_type"] not in ("improve_page", "consolidate", "technical_fix"):
        raise FixNotSupported(
            f"fix generation covers improve_page/consolidate/technical_fix "
            f"(got {rec['action_type']!r})")
    if not rec["target_url"]:
        raise FixNotSupported(f"{rec['action_type']} row has no target_url")
    if rec["status"] not in ("approved", "proposed"):
        raise FixGenerationError(
            f"fix generation requires an approved recommendation "
            f"(current status: {rec['status']!r})")

    # technical_fix (Phase 3): the issue_type rides in primary_keyword
    # (plan/03 reuse); the page row supplies the write-target GID. The
    # status_failure (404) branch needs NO page row (the URL is dead) and
    # different evidence fields — both load evidence + domain, then the
    # issue_type routes to the right generator.
    if rec["action_type"] == "technical_fix":
        rec["primary_keyword"] = None
        rec["intent"] = None
        rec["competitor_context"] = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT r.evidence_json, "
                "p.url, p.title, p.shopify_gid, p.page_type, p.indexable, "
                "p.status_code, s.shopify_domain, s.domain "
                "FROM recommendations r "
                "LEFT JOIN pages p ON p.site_id = r.site_id AND p.url = r.target_url "
                "LEFT JOIN site_config s ON s.site_id = r.site_id "
                "WHERE r.recommendation_id = %s",
                (recommendation_id,),
            )
            trow = cur.fetchone()
        if not trow:
            raise FixGenerationError(
                f"recommendation {recommendation_id} vanished mid-load")
        try:
            # psycopg2 auto-converts jsonb to dict; json.loads only for str.
            rec["evidence"] = (trow[0] if isinstance(trow[0], dict)
                               else json.loads(trow[0]) if trow[0] else {})
        except (TypeError, ValueError):
            rec["evidence"] = {}
        rec["shop_domain"] = trow[7]
        rec["domain"] = trow[8]
        if not trow[1]:
            # No page row: only the status_failure branch is legal here —
            # the dead URL may be absent from pages (404, already removed).
            return rec
        rec["page_title"] = trow[2]
        rec["shopify_gid"] = trow[3]
        rec["page_type"] = trow[4]
        rec["page_indexable"] = trow[5]
        rec["page_status_code"] = trow[6]
        return rec

    # Redirect targets (consolidate): the SOURCE page is the redirect
    # subject, the survivor (target_url) is the destination. No page row is
    # required for the source — it may be a 404 that was already removed.
    if rec["action_type"] == "consolidate":
        if not rec.get("proposed_url"):
            raise FixNotSupported("consolidate row has no proposed_url "
                                  "(redirect source)")
        with conn.cursor() as cur:
            cur.execute("SELECT domain FROM site_config WHERE site_id = %s",
                        (rec["site_id"],))
            drow = cur.fetchone()
            cur.execute(
                "SELECT evidence_json FROM recommendations "
                "WHERE recommendation_id = %s",
                (recommendation_id,),
            )
            erow = cur.fetchone()
        try:
            # psycopg2 auto-converts jsonb to dict; json.loads only for str.
            rec["evidence"] = (erow[0] if isinstance(erow[0], dict)
                               else json.loads(erow[0]) if erow[0] else {})
        except (TypeError, ValueError):
            rec["evidence"] = {}
        rec["source_url"] = rec["proposed_url"]
        rec["survivor_url"] = rec["target_url"]
        rec["domain"] = (drow[0] if drow else None)
        rec["primary_keyword"] = rec["intent"] = None
        rec["competitor_context"] = {}
        return rec

    with conn.cursor() as cur:
        cur.execute(
            "SELECT url, title, shopify_gid, meta_description, page_type, body_html "
            "FROM pages "
            "WHERE site_id = %s AND url = %s",
            (rec["site_id"], rec["target_url"]),
        )
        page = cur.fetchone()
        if not page:
            raise FixGenerationError(
                f"target_url not in pages table: {rec['target_url']}")
        rec["page_title"] = page[1]
        rec["shopify_gid"] = page[2]
        rec["page_meta_description"] = page[3]
        rec["page_type"] = page[4]
        rec["body_html"] = page[5]
        rec["body_text"] = _body_to_text(page[5])
        if rec["cluster_id"]:
            cur.execute(
                "SELECT primary_keyword, intent FROM keyword_clusters "
                "WHERE cluster_id = %s",
                (rec["cluster_id"],),
            )
            krow = cur.fetchone()
            rec["primary_keyword"] = krow[0] if krow else None
            rec["intent"] = krow[1] if krow else None
        else:
            rec["primary_keyword"] = None
            rec["intent"] = None

    # SERP competitor grounding (plan/21 §2.1/§2.2 gap closure): structural
    # framing + value-prop cues from the top-1..10 non-self organic results
    # for this cluster. Purely additive — empty dict degrades every consumer
    # to the prior keyword-only behavior.
    rec["competitor_context"] = load_competitor_context(
        conn, rec["site_id"], rec["cluster_id"], rec["target_url"])
    return rec


# ------------------------------------------------------------
# Title-quality checks (deterministic gate — data -> home)
# ------------------------------------------------------------

def title_quality_checks(current_title, primary_keyword):
    checks = {
        "length_ok": bool(current_title) and TITLE_MIN_CHARS <= len(current_title) <= TITLE_MAX_CHARS,
        "has_primary_kw": (bool(current_title) and bool(primary_keyword)
                           and primary_keyword.lower() in current_title.lower()),
        "is_blank": not bool(current_title),
    }
    return checks


# ------------------------------------------------------------
# plan/21 §2.1 checks 2, 4, 5, 6 — grounded, deterministic
# ------------------------------------------------------------

def protect_winner_check(conn, site_id, target_url, reference_date=None):
    """plan/21 §2.1 check 6: position ≤2 + rising CTR -> never rewrite.

    Rising = the 7-day average CTR is strictly above the prior-7-day
    average, over a meaningful sample (>= PROTECT_MIN_IMPRESSIONS
    impressions in the recent window). No GSC rows for the target -> not a
    winner (nothing to protect). Returns True when the page is protected.
    """
    end = reference_date or datetime.now(timezone.utc).date()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                AVG(sp.position) FILTER (
                    WHERE sp.date >= %s::date - INTERVAL '7 days'),
                AVG(sp.ctr) FILTER (
                    WHERE sp.date >= %s::date - INTERVAL '7 days'),
                SUM(sp.impressions) FILTER (
                    WHERE sp.date >= %s::date - INTERVAL '7 days'),
                AVG(sp.ctr) FILTER (
                    WHERE sp.date BETWEEN %s::date - INTERVAL '14 days'
                                       AND %s::date - INTERVAL '8 days')
            FROM search_performance sp
            WHERE sp.site_id = %s AND sp.page_url = %s
              AND sp.date >= %s::date - INTERVAL '14 days'
            """,
            (end, end, end, end, end, site_id, target_url, end),
        )
        pos_recent, ctr_recent, imps_recent, ctr_prior = cur.fetchone()
    if pos_recent is None or ctr_recent is None:
        return False
    if float(pos_recent) > PROTECT_POSITION_MAX:
        return False
    if int(imps_recent or 0) < PROTECT_MIN_IMPRESSIONS:
        return False
    if ctr_prior is None:
        return False
    return float(ctr_recent) > float(ctr_prior)


def intent_match_check(intent, title):
    """plan/21 §2.1 check 2: intent vs title framing (deterministic cues).

    Only a CONTRADICTION fails: e.g. informational intent + hard
    transactional push ('Buy now'), or transactional/commercial intent with
    an informational-only framing. Neutral titles pass; intent 'unknown'
    always passes (no grounded verdict possible).
    """
    if not intent or intent == "unknown":
        return True
    lowered = (title or "").lower()
    if not lowered.strip():
        return True
    has_txn = any(c in lowered for c in INTENT_TRANSACTIONAL_CUES)
    has_comm = any(c in lowered for c in INTENT_COMMERCIAL_CUES)
    has_info = any(c in lowered for c in INTENT_INFORMATIONAL_CUES)
    if intent == "informational":
        # Buying push on informational intent is a contradiction.
        return not has_txn
    if intent in ("transactional", "commercial"):
        # Purely informational framing with no buy/commercial cue: the
        # title does not serve a buying intent.
        return has_txn or has_comm or not has_info
    return True


def duplicate_title_check(conn, site_id, draft, exclude_url):
    """plan/21 §2.1 check 4: the draft must not duplicate another page's
    title site-wide (pages.title self-join, case/whitespace-insensitive).
    Returns True when a duplicate EXISTS (draft rejected)."""
    normalized = re.sub(r"\s+", " ", (draft or "").strip()).lower()
    if not normalized:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM pages
            WHERE site_id = %s
              AND url <> %s
              AND lower(regexp_replace(COALESCE(title, ''), '\\s+', ' ', 'g'))
                  = %s
            """,
            (site_id, exclude_url, normalized),
        )
        return cur.fetchone()[0] > 0


def brand_suffix_check(draft, site_name):
    """plan/21 §2.1 check 5: brand suffix must appear at most once and be
    a SUFFIX, never a mid-title duplication ('Brand X — Brand Y').
    Returns True when the draft is OK, False when it violates brand rules."""
    brand = (site_name or "").strip().lower()
    if not brand:
        return True
    lowered = (draft or "").lower()
    return lowered.count(brand) <= 1


def _caps_runs(text):
    """ALL-CAPS runs of 3+ words (numbers/symbols don't count as words)."""
    words = re.findall(r"[A-Za-z']+", text or "")
    return sum(1 for w in words if len(w) >= 3 and w.isupper())


def _has_emoji(text):
    for ch in (text or ""):
        code = ord(ch)
        for lo, hi in _EMOJI_RANGES:
            if lo <= code <= hi:
                return True
    return False


def _spam_stacks(text):
    """Spam stacks: repeated '!!!', '***', price-tags, or 2+ same char runs."""
    if re.search(r"(\W)\1{2,}", text or ""):
        return True
    return bool(re.search(r"[!$]{4,}", text or ""))


def validator_extra_checks(draft, primary_keyword):
    """plan/21 §2.1 value-constraint additions (denylist IS in v1 validator):
    ALL-CAPS runs, emoji, spam stacks, keyword-in-first-half."""
    problems = []
    if _caps_runs(draft) >= 3:
        problems.append("ALL-CAPS run (3+ shouted words)")
    if _has_emoji(draft):
        problems.append("emoji in title")
    if _spam_stacks(draft):
        problems.append("spam punctuation stack")
    if primary_keyword:
        # Keyword must START within the first half (its end may spill over
        # — a long keyword can never fully fit in half of a short title).
        half = max(1, len(draft) // 2)
        start = draft.lower().find(primary_keyword.lower())
        if start == -1 or start >= half:
            problems.append("primary keyword not in first half")
    return problems


def validate_title_draft(draft, primary_keyword, product_title=None,
                         intent=None, site_name=None):
    """Deterministic validator gate (plan/21 §2.1 hard constraints)."""
    problems = []
    if not isinstance(draft, str) or not draft.strip():
        problems.append("draft is empty")
        return problems
    stripped = draft.strip()
    if len(stripped) < TITLE_MIN_CHARS:
        problems.append(f"too short ({len(stripped)} < {TITLE_MIN_CHARS})")
    if len(stripped) > TITLE_MAX_CHARS:
        problems.append(f"too long ({len(stripped)} > {TITLE_MAX_CHARS})")
    if primary_keyword and primary_keyword.lower() not in stripped.lower():
        problems.append("primary keyword missing")
    lowered = stripped.lower()
    for pattern in BANNED_TITLE_PATTERNS:
        if pattern.lower() in lowered:
            problems.append(f"banned pattern: {pattern!r}")
    if stripped != stripped.strip() or "  " in stripped:
        problems.append("whitespace artifacts")
    # Shopify stores seo.title == product.title as NULL ('inherit from the
    # product title') — a no-op whose read-back reads null. Phase 1.5 live
    # finding; such a draft can never pass the read-back verify.
    product = (product_title or "").strip()
    if product and stripped == product:
        problems.append("draft equals the product title — Shopify stores that "
                        "as null (no-op write)")
    # plan/21 §2.1 additions: caps/emoji/spam + keyword-in-first-half.
    problems.extend(validator_extra_checks(stripped, primary_keyword))
    # plan/21 §2.1 check 2: intent contradiction.
    if not intent_match_check(intent, stripped):
        problems.append(f"intent mismatch ({intent} intent contradicted by "
                        "title framing)")
    # plan/21 §2.1 check 5: brand suffix boilerplate.
    if not brand_suffix_check(stripped, site_name):
        problems.append("brand name duplicated in title")
    return problems


def draft_title(rec):
    """Deterministic v0 draft — SERP-grounded (plan/21 §2.1 gap closure).

    Reads the structural framing of the top-ranking competitor titles
    (load_competitor_context) and mirrors the DOMINANT pattern's shape:
    a 'Best' market leads with 'Best ', a year-marker title leads with the
    year, a tested/review title leads with 'Reviewed: ', a counted-list
    title leads with 'Top N '. Only the STRUCTURE is borrowed — every word
    stays from the page's own title and the cluster's keyword. When no
    clear competitor pattern exists, falls back to the naive
    keyword-forward concatenation (prior behavior). The validator still
    gates the result; a failing draft means NO fix row.
    """
    page_title = (rec.get("page_title") or "").strip()
    keyword = (rec.get("primary_keyword") or "").strip()
    if not page_title:
        return None
    candidate = page_title
    needs_fronting = (keyword
                      and (keyword.lower() not in page_title.lower()
                           or keyword.lower() not in page_title.lower()[:max(1, len(page_title) // 2)]))
    if needs_fronting:
        prefix = _competitor_prefix(rec)
        if prefix:
            candidate = f"{prefix}{keyword.title() if keyword.islower() else keyword}: {page_title}"
        else:
            candidate = f"{keyword.title()}: {page_title}" if keyword.islower() else f"{keyword} — {page_title}"
        candidate = candidate.strip()
    if len(candidate) > TITLE_MAX_CHARS:
        candidate = candidate[:TITLE_MAX_CHARS - 1].rstrip() + "…"
    if len(candidate) < TITLE_MIN_CHARS:
        return None
    return candidate


# ------------------------------------------------------------
# Phase 2 (plan/21 §2.2): meta-description checks + drafting
# ------------------------------------------------------------

# SERP snippet bounds (Google truncates ~155-160 chars; floor avoids
# one-liner snippets). Same philosophy as the title bounds: mechanical,
# deterministic, plan/21 §2.2 "Meta missing/dup/truncated".
META_MIN_CHARS = 70
META_MAX_CHARS = 155

BANNED_META_PATTERNS = ("free shipping", "best price", "click here",
                        "buy now", "limited time")


def meta_quality_checks(current_meta, primary_keyword):
    """plan/21 §2.2 row 1: meta missing / duplicated / truncated.

    Returns the typed check dict; a fix is generated only when at least one
    check FAILS (same only-fix-what's-broken rule as the title gate).
    """
    meta = (current_meta or "").strip()
    checks = {
        "is_missing": not bool(meta),
        "length_ok": bool(meta) and META_MIN_CHARS <= len(meta) <= META_MAX_CHARS,
        "has_primary_kw": (bool(meta) and bool(primary_keyword)
                           and primary_keyword.lower() in meta.lower()),
    }
    checks["quality_failed"] = (checks["is_missing"]
                                or not checks["length_ok"]
                                or not checks["has_primary_kw"])
    return checks


def duplicate_meta_check(conn, site_id, draft, exclude_url):
    """plan/21 §2.2: the draft must not duplicate another page's live meta
    description site-wide (pages.meta_description self-join)."""
    normalized = re.sub(r"\s+", " ", (draft or "").strip()).lower()
    if not normalized:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM pages
            WHERE site_id = %s
              AND url <> %s
              AND lower(regexp_replace(COALESCE(meta_description, ''), '\\s+', ' ', 'g'))
                  = %s
            """,
            (site_id, exclude_url, normalized),
        )
        return cur.fetchone()[0] > 0


def _meta_denylint(draft):
    problems = []
    lowered = (draft or "").lower()
    for pattern in BANNED_META_PATTERNS:
        if pattern in lowered:
            problems.append(f"banned meta pattern: {pattern!r}")
    if _has_emoji(draft or ""):
        problems.append("emoji in meta description")
    if re.search(r"(\W)\1{2,}", draft or ""):
        problems.append("spam punctuation stack")
    # Quotes break Google's snippet rendering; strip rather than reject at
    # draft level is NOT done — the validator rejects (deterministic).
    if '"' in (draft or ""):
        problems.append("double quote in meta description")
    return problems


def draft_meta_description(rec):
    """Deterministic v0 meta draft — SERP-grounded (plan/21 §2.2 gap closure).

    FACTS stay strictly page-owned (page title + the page's own body copy +
    product type — never competitor claims). What the SERP grounds is the
    ORDERING and framing: when top competitor snippets consistently lead
    with certain value-prop hooks (tested > free > warranty...), the draft
    surfaces the page's own matching fact in that order, so the snippet
    competes on the angles that already win clicks. The validator still
    gates the result; a failing draft means NO fix row.
    """
    page_title = (rec.get("page_title") or "").strip()
    keyword = (rec.get("primary_keyword") or "").strip()
    page_type = (rec.get("page_type") or "").strip()
    body_text = (rec.get("body_text") or "").strip()
    if not page_title:
        return None

    # Grounded lead: first sentence of the page's own body copy.
    lead = ""
    if body_text:
        for sep in (". ", "! ", "? "):
            idx = body_text.find(sep)
            if idx != -1 and idx >= 20:
                lead = body_text[:idx + 1].strip()
                break
        if not lead and len(body_text) >= 40:
            lead = body_text[:120].strip()

    # Hook-ordered facts: the page's own body sentences that match the
    # dominant competitor hook cues surface first (grounded positioning —
    # the FACT stays ours, the ORDER is competitor-informed).
    supporting = []
    if body_text:
        competitor_leads = _competitor_snippet_leads(rec)
        for sentence in [s.strip() for s in body_text.split(". ") if s.strip()]:
            sentence_full = sentence if sentence.endswith(".") else sentence + "."
            for hook in competitor_leads:
                if hook in sentence_full.lower():
                    supporting.append(sentence_full)
                    break
            if len(supporting) >= 2:
                break

    parts = [page_title]
    if lead and lead.lower() != page_title.lower():
        parts.append(lead)
    for extra in supporting:
        if extra.lower() != lead.lower() and len(" — ".join(parts + [extra])) <= META_MAX_CHARS + 20:
            parts.append(extra)
            break
    # Floor guarantee: a short lead alone can't make META_MIN_CHARS — the
    # page-type line completes the draft (competitor-hook-ordered).
    if len(" — ".join(parts)) < META_MIN_CHARS:
        competitor_leads = _competitor_snippet_leads(rec)
        if page_type == "product":
            tail = ("Fast, free delivery and easy returns."
                    if "free" in competitor_leads
                    else "Shop now with fast delivery and easy returns.")
        elif page_type == "collection":
            tail = ("Compare models side by side and find your fit."
                    if "compare" in competitor_leads
                    else "Browse the full range in one place.")
        else:
            tail = "Learn what makes it worth it."
        parts.append(tail)

    draft = " — ".join(parts)
    if keyword and keyword.lower() not in draft.lower():
        draft = f"{keyword.title()}: {draft}" if keyword.islower() else f"{keyword} — {draft}"

    if len(draft) > META_MAX_CHARS:
        draft = draft[:META_MAX_CHARS - 1].rstrip() + "…"
    if len(draft) < META_MIN_CHARS:
        return None
    return draft


def validate_meta_draft(draft, primary_keyword, intent=None, site_name=None):
    """Deterministic meta validator (plan/21 §2.2)."""
    problems = []
    if not isinstance(draft, str) or not draft.strip():
        problems.append("draft is empty")
        return problems
    stripped = draft.strip()
    if len(stripped) < META_MIN_CHARS:
        problems.append(f"too short ({len(stripped)} < {META_MIN_CHARS})")
    if len(stripped) > META_MAX_CHARS:
        problems.append(f"too long ({len(stripped)} > {META_MAX_CHARS})")
    if primary_keyword and primary_keyword.lower() not in stripped.lower():
        problems.append("primary keyword missing")
    problems.extend(_meta_denylint(stripped))
    if _caps_runs(stripped) >= 3:
        problems.append("ALL-CAPS run (3+ shouted words)")
    # plan/21 §2.1 check 2 semantics apply to the snippet too.
    if not intent_match_check(intent, stripped):
        problems.append(f"intent mismatch ({intent} intent contradicted by "
                        "meta framing)")
    if not brand_suffix_check(stripped, site_name):
        problems.append("brand name duplicated in meta description")
    return problems


def generate_meta_fix_for_recommendation(conn, recommendation_id,
                                         generation_source="deterministic",
                                         live_meta_description=None):
    """Phase 2 hook: approved improve_page recommendation -> seo.description fix.

    Same contract as generate_fix_for_recommendation (idempotent, conflict
    guard via uq_fixes_active_per_target, policy NOT enforced here):
      1. protect winners + consolidate suppression (shared §2.1 guards)
      2. meta quality checks on the LIVE value (fresh read when supplied,
         else pages.meta_description) — all-pass -> no fix
      3. deterministic draft -> validator gate -> duplicate-meta guard
      4. INSERT generated_fixes (sub_type='seo.description', risk 'medium')
    """
    rec = load_decision_inputs(conn, recommendation_id)

    # Shared suppression gates (identical rationale to the title path).
    if protect_winner_check(conn, rec["site_id"], rec["target_url"]):
        raise FixNotSupported(
            "protect-winner rule: position ≤2 with rising CTR — "
            "meta rewrite suppressed (never rewrite what works)")
    if rec.get("cluster_id"):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT recommendation_id FROM recommendations
                WHERE site_id = %s AND cluster_id = %s
                  AND action_type = 'consolidate'
                  AND status IN ('raw', 'proposed', 'approved', 'in_progress')
                  AND recommendation_id <> %s
                LIMIT 1
                """,
                (rec["site_id"], rec["cluster_id"], rec["recommendation_id"]),
            )
            active_consolidate = cur.fetchone()
        if active_consolidate:
            raise FixNotSupported(
                "consolidate suppression: an active consolidate "
                "recommendation exists on this cluster — meta fix "
                f"suppressed (consolidating rec {active_consolidate[0]})")

    # Site-level + body inputs for checks/drafting.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT site_name FROM site_config WHERE site_id = %s",
            (rec["site_id"],),
        )
        srow = cur.fetchone()
    site_name = srow[0] if srow else None
    intent = rec.get("intent")
    page_type = rec.get("page_type")
    body_text = rec.get("body_text")

    gate_meta = (live_meta_description if live_meta_description is not None
                 else rec["page_meta_description"])
    checks = meta_quality_checks(gate_meta, rec["primary_keyword"])
    if not checks["quality_failed"]:
        raise FixNotSupported(
            "meta description passes all quality checks — no fix warranted "
            f"(checks={checks})")

    # LLM-first drafting with fact-check + validator gates and a
    # deterministic fallback (soft-fail pipeline, never throws).
    rec["site_name"] = site_name
    drafted = draft_candidates_with_fallback(rec)
    new_meta = drafted["meta"]
    meta_source = drafted["meta_source"]
    if new_meta is None:
        raise FixNotSupported(
            "meta draft failed validation constraints — no fix row")
    problems = validate_meta_draft(new_meta, rec["primary_keyword"],
                                   intent=intent, site_name=site_name)
    if problems:
        raise FixNotSupported(f"meta draft rejected by validator: {problems}")

    # plan/21 §2.2: no duplicate metas site-wide.
    if duplicate_meta_check(conn, rec["site_id"], new_meta,
                            exclude_url=rec["target_url"]):
        raise FixNotSupported(
            "duplicate meta description: another page on this site already "
            "uses the proposed description — draft rejected (plan/21 §2.2)")

    gid = rec.get("shopify_gid")
    if not gid:
        raise FixNotSupported(
            "target page has no shopify_gid — meta fix cannot target "
            "Shopify GraphQL")

    conflict = check_conflict(conn, rec["site_id"], rec["target_url"],
                              "seo.description")
    if conflict:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT recommendation_id, fix_id, status, diff_json "
                "FROM generated_fixes WHERE fix_id = %s", (conflict,))
            row = cur.fetchone()
        if row and str(row[0]) == str(rec["recommendation_id"]):
            diff = row[3] if not isinstance(row[3], str) else json.loads(row[3])
            return {"fix_id": str(row[1]), "status": row[2], "created": False,
                    "diff": diff}
        raise PolicyBlocked("active_fix_conflict", {"conflicting_fix_id": conflict})

    payload = build_meta_payload(gid, new_meta)
    diff = [{"field": "seo.description", "old_value": gate_meta,
             "new_value": new_meta}]
    # SERP grounding evidence rides on the payload for console/audit:
    # framing pattern used + content outline gaps (§2.2 content row) +
    # drafting source (llm vs deterministic) for the audit trail.
    grounding = {"competitor_titles_sampled": len((rec.get("competitor_context")
                                                   or {}).get("titles") or []),
                 "url_patterns": (rec.get("competitor_context") or {}).get("url_patterns") or [],
                 "draft_source": meta_source,
                 "llm_candidates_rejected": [r for r in drafted["rejected"]
                                             if r["field"] == "meta"]}
    outline = _outline_gaps(rec)
    if outline["missing_sections"]:
        grounding["content_outline_missing"] = outline["missing_sections"]
    grounding["content_outline_expected"] = outline["expected_sections"]
    grounding["content_outline_grounding"] = outline.get("grounding")
    grounding["competitor_headings_scanned"] = outline.get("competitor_headings_scanned", 0)
    payload["grounding"] = grounding
    # 'agent' = LLM-drafted value; 'deterministic' = baseline drafter.
    generation_source = "agent" if meta_source == "llm" else generation_source
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO generated_fixes
                    (recommendation_id, site_id, action_type, sub_type, target_url,
                     target_entity_ref, payload_json, diff_json, generation_source,
                     status, risk_tier)
                VALUES (%s, %s, %s, 'seo.description', %s, %s, %s::jsonb, %s::jsonb,
                        %s, 'generated', 'medium')
                ON CONFLICT (site_id, target_url, COALESCE(sub_type, ''))
                    WHERE status IN ('generated','approved','queued') DO NOTHING
                RETURNING fix_id, status
                """,
                (rec["recommendation_id"], rec["site_id"], rec["action_type"],
                 rec["target_url"], gid, json.dumps(payload), json.dumps(diff),
                 generation_source),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row:
        return {"fix_id": str(row[0]), "status": row[1], "created": True,
                "diff": diff, "payload": payload}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fix_id, status FROM generated_fixes "
            "WHERE recommendation_id = %s AND sub_type = 'seo.description' "
            "AND status IN ('generated','approved','queued') "
            "ORDER BY created_at DESC LIMIT 1",
            (rec["recommendation_id"],),
        )
        existing = cur.fetchone()
    if not existing:
        raise FixGenerationError("insert conflicted but no active meta fix found")
    return {"fix_id": str(existing[0]), "status": existing[1], "created": False,
            "diff": diff, "payload": payload}


# ------------------------------------------------------------
# Payload + diff builders (GraphQL-only, registry-verified targets)
# ------------------------------------------------------------

PRODUCT_UPDATE_DOC_URL = (
    "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/productUpdate"
)


def build_payload(gid, new_title):
    return {
        "mutation": "productUpdate",
        "variables": {"product": {"id": gid, "seo": {"title": new_title}}},
        "scope_required": "write_products",
        "doc_url": PRODUCT_UPDATE_DOC_URL,
    }


PRODUCT_UPDATE_META_DOC_URL = (
    "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/productUpdate"
)


def build_meta_payload(gid, new_meta):
    """Phase 2 (plan/21 §4 verified): productUpdate(seo: {description})."""
    return {
        "mutation": "productUpdate",
        "variables": {"product": {"id": gid, "seo": {"description": new_meta}}},
        "scope_required": "write_products",
        "doc_url": PRODUCT_UPDATE_META_DOC_URL,
    }


def build_diff(old_value, new_value):
    return [{"field": "seo.title", "old_value": old_value, "new_value": new_value}]


# ------------------------------------------------------------
# Redirect fix generation (consolidate → 301 weaker → survivor; plan/21 §2.5)
# ------------------------------------------------------------

URL_REDIRECT_DOC_URL = (
    "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/urlRedirectCreate"
)


def _redirect_path_part(url, domain=None):
    """Absolute URL (or bare path) -> the path part Shopify redirects key on.

    Shopify UrlRedirectInput.path/target accept absolute paths; a bare
    handle or a full URL both normalize to the leading-slash path. None
    when nothing extractable (never guessed).
    """
    raw = (url or "").strip()
    if not raw:
        return None
    parsed = urlparse_url(raw if "://" in raw else f"https://{domain or 'x.local'}{raw}")
    path = parsed.path or ""
    if not path or path == "/":
        return None
    path = path.rstrip("/")
    return path or None


def urlparse_url(url):
    from urllib.parse import urlparse
    return urlparse(url)


def build_redirect_payload(source_path, survivor_path):
    """urlRedirectCreate payload (plan/21 §2.5 + §4 VERIFIED surface)."""
    return {
        "mutation": "urlRedirectCreate",
        "variables": {"urlRedirect": {"path": source_path,
                                      "target": survivor_path}},
        "scope_required": "write_online_store_navigation",
        "doc_url": URL_REDIRECT_DOC_URL,
    }


# ------------------------------------------------------------
# Consolidate survivor verification (Phase 4, plan/21 §2.5 + plan/17):
# the generator SQL picks the loser by impressions for ONE cluster. The
# fix-generation gate re-verifies the pick against the FULL evidence stack —
# a page that only loses this cluster may be a primary earner elsewhere,
# where a 301 would trade one conflict for several traffic losses.
# ------------------------------------------------------------

# The redirect source may keep at most this share of its OWN total GSC
# impressions (28d) on the cluster being consolidated. Below the threshold
# it is still a primary earner elsewhere -> differentiation, not a 301.
CONSOLIDATE_CLUSTER_SHARE_MIN = 0.5
# Absolute floor: below this total footprint the share ratio is noise.
CONSOLIDATE_MIN_FOOTPRINT_IMPRESSIONS = 100


def _cluster_share_stats(conn, site_id, source_url, cluster_id,
                         reference_date=None):
    """28d GSC footprint of the redirect source, split by cluster membership.

    Returns (cluster_impressions, other_impressions) where cluster rows are
    the ones whose query belongs to the recommendation's cluster (via
    cluster_queries) and "other" is everything else. No rows -> (0, 0).
    """
    end = reference_date or datetime.now(timezone.utc).date()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(sp.impressions) FILTER (
                    WHERE cq.cluster_id = %s::uuid), 0),
                COALESCE(SUM(sp.impressions) FILTER (
                    WHERE cq.cluster_id IS DISTINCT FROM %s::uuid), 0)
            FROM search_performance sp
            LEFT JOIN cluster_queries cq ON sp.query = cq.query
            WHERE sp.site_id = %s AND sp.page_url = %s
              AND sp.date >= %s::date - INTERVAL '28 days'
            """,
            (cluster_id, cluster_id, site_id, source_url, end),
        )
        cluster_imps, other_imps = cur.fetchone()
    return int(cluster_imps or 0), int(other_imps or 0)


def verify_consolidation_direction(conn, rec, reference_date=None):
    """plan/17 Step 2 gate 1: the loser must be weaker ACROSS clusters.

    The candidate's weaker_url (proposed_url / redirect source) lost THIS
    cluster's query split. Before redirecting it away, check its full 28d
    GSC footprint: if the majority of its impressions still come from OTHER
    clusters, it is a primary earner elsewhere and the 301 would trade one
    conflict for real traffic losses -> refuse (differentiation is the
    right fix, not consolidation).

    Returns a grounding dict on pass; raises FixNotSupported on fail.
    """
    cluster_id = rec.get("cluster_id")
    if not cluster_id:
        # No cluster membership to reason across: single-query candidate.
        # The generator already filtered alternation; allow (data -> home:
        # never invent a footprint that isn't measurable).
        return {"cross_cluster_check": "skipped_no_cluster"}

    cluster_imps, other_imps = _cluster_share_stats(
        conn, rec["site_id"], rec["source_url"], cluster_id, reference_date)
    total = cluster_imps + other_imps
    share = (cluster_imps / total) if total else 1.0
    grounding = {
        "cross_cluster_check": "passed",
        "cluster_impressions_28d": cluster_imps,
        "other_cluster_impressions_28d": other_imps,
        "cluster_share": round(share, 3),
    }
    if total >= CONSOLIDATE_MIN_FOOTPRINT_IMPRESSIONS \
            and share < CONSOLIDATE_CLUSTER_SHARE_MIN:
        raise FixNotSupported(
            "split-intent guard: the redirect source earns "
            f"{round((1 - share) * 100)}% of its impressions OUTSIDE this "
            "cluster (primary earner elsewhere) — redirecting trades one "
            "conflict for traffic losses; differentiate instead "
            "(plan/17 Step 2: loser must be weaker across ALL clusters)")
    return grounding


def verify_survivor_strength(conn, rec):
    """plan/17 Step 2 gates 2 + 3: relative strength, survivor sanity, tiebreakers.

    Deterministic checks, all data -> home:
      * relative-strength guard (wrong-direction): the rec's candidate
        evidence (impressions_distribution, the generator's per-URL split
        for this cluster) must show the SOURCE weaker than the survivor —
        a winner→loser redirect is always a mis-wired candidate, however
        it was seeded. Skipped when the distribution is missing (older rows)
        — never fabricated;
      * survivor must be indexable (verified EARLY so the failure names the
        survivor, not the adapter);
      * wrong-direction asset guard: the cluster's product coverage (from
        catalogue_coverage) must not be orphaned by redirecting the source;
      * deterministic tiebreakers recorded for the audit trail:
        pages.indexable, internal_links_in, coverage owner + count.

    Returns a grounding dict; raises FixNotSupported on any refusal.
    """
    evidence = rec.get("evidence")
    if not isinstance(evidence, dict):
        try:
            evidence = json.loads(evidence) if isinstance(evidence, str) else {}
        except (TypeError, ValueError):
            evidence = {}

    # Relative-strength guard: source must be the weaker page on THIS cluster.
    distribution = evidence.get("impressions_distribution")
    if isinstance(distribution, dict) and rec["source_url"] in distribution \
            and rec["survivor_url"] in distribution:
        try:
            src_imps = float(distribution[rec["source_url"]] or 0)
            surv_imps = float(distribution[rec["survivor_url"]] or 0)
        except (TypeError, ValueError):
            src_imps = surv_imps = None
        if src_imps is not None and src_imps > surv_imps:
            raise FixNotSupported(
                "wrong-direction guard: the redirect source OUT-EARNS the "
                f"survivor on this cluster ({src_imps} vs {surv_imps} "
                "impressions) — redirecting the winner into the loser "
                "destroys the stronger page's equity; the candidate "
                "direction is mis-wired (plan/17: target_url must be the "
                "stronger page)")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT url, indexable, internal_links_in FROM pages "
            "WHERE site_id = %s AND url IN (%s, %s)",
            (rec["site_id"], rec["source_url"], rec["survivor_url"]),
        )
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    src = rows.get(rec["source_url"]) or (None, None)
    surv = rows.get(rec["survivor_url"])
    if surv is None:
        raise FixNotSupported(
            f"survivor page not in pages table: {rec['survivor_url']} — "
            "refusing to redirect to an unknown destination")
    if surv[0] is False:
        raise FixNotSupported(
            f"survivor page is not indexable ({rec['survivor_url']}) — "
            "redirect would trade one conflict for a dead destination "
            "(plan/17: consolidating INTO a broken page is nonsense)")

    grounding = {
        "source": {"indexable": src[0], "internal_links_in": src[1]},
        "survivor": {"indexable": surv[0], "internal_links_in": surv[1]},
    }
    coverage_owner = coverage_count = None
    if rec.get("cluster_id"):
        # catalogue_coverage is per-cluster (UNIQUE(site_id, cluster_id));
        # existing_collection_url names the URL that carries the cluster's
        # product coverage.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT existing_collection_url, matching_product_count "
                "FROM catalogue_coverage "
                "WHERE site_id = %s AND cluster_id = %s",
                (rec["site_id"], rec["cluster_id"]),
            )
            cov_row = cur.fetchone()
        if cov_row and cov_row[0]:
            coverage_owner, coverage_count = cov_row[0], cov_row[1]
        grounding["source"]["matching_product_count"] = (
            coverage_count if coverage_owner == rec["source_url"] else None)
        grounding["survivor"]["matching_product_count"] = (
            coverage_count if coverage_owner == rec["survivor_url"] else None)
        grounding["coverage_owner_url"] = coverage_owner
        grounding["cluster_matching_product_count"] = coverage_count

    # Wrong-direction guard (plan/17 Step 2 gate 3): the cluster's product
    # coverage must survive the consolidation. When the coverage owner is
    # the redirect SOURCE (and the survivor is a different URL), redirecting
    # the source orphans the catalogue — the direction is wrong, or the
    # coverage must migrate to the survivor first (coverage row repointed).
    # Not enforceable when coverage is unknown (no row) — never fabricated.
    if coverage_owner == rec["source_url"] and coverage_count:
        raise FixNotSupported(
            "wrong-direction guard: the redirect source carries the cluster's "
            f"product coverage ({coverage_count} matching products via "
            "catalogue_coverage) — redirecting would orphan the catalogue; "
            "merge/migrate coverage to the survivor first or reverse the "
            "direction (plan/17 Step 2: consolidation must not destroy the "
            "weaker page's assets)")
    return grounding


def generate_redirect_fix_for_recommendation(conn, rec, generation_source="deterministic"):
    """Approved consolidate recommendation -> sub_type='redirect' fix row.

    Contract mirrors the title/meta paths: idempotent regeneration on the
    same recommendation, 409 on a conflicting active fix, no policy
    enforcement here (generation runs while execution stays disabled).
    target_entity_ref stores the SURVIVOR page's GID when known (audit
    context only — the created redirect id comes from the write envelope).

    plan/21 §2.5 gates enforced here (deterministic):
      * Phase 4 sharpened-direction gates (plan/17 Step 2): split-intent
        guard (loser must be weaker across ALL clusters), survivor sanity
        (indexable; never consolidate into a broken page) and the
        wrong-direction guard (source must not carry the cluster's only
        product coverage while the survivor carries none);
      * path == target -> no-op loop, rejected
      * survivor must exist in pages and be indexable (never redirect to a
        dead destination)

    The status_failure route (rec.route='status_failure') skips the
    split-intent guard: the source is already DEAD (404), there is no
    healthy earner to protect — the residual-traffic gate ran upstream.
    """
    source_url = rec["source_url"]
    survivor_url = rec["survivor_url"]
    domain = rec.get("domain")

    source_path = _redirect_path_part(source_url, domain)
    survivor_path = _redirect_path_part(survivor_url, domain)
    if not source_path or not survivor_path:
        raise FixNotSupported(
            "cannot derive redirect path parts "
            f"(source={source_path!r}, survivor={survivor_path!r}) — no fix row")
    if source_path == survivor_path:
        raise FixNotSupported("redirect source == survivor (no-op loop)")

    sharpened_grounding = {}
    if rec.get("route") != "status_failure":
        sharpened_grounding = verify_consolidation_direction(conn, rec)
        sharpened_grounding.update(verify_survivor_strength(conn, rec))
    else:
        # status_failure: only the survivor-sanity half applies (no healthy
        # source to protect).
        sharpened_grounding = verify_survivor_strength(conn, rec)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT url, indexable, shopify_gid FROM pages "
            "WHERE site_id = %s AND url = %s",
            (rec["site_id"], survivor_url),
        )
        survivor = cur.fetchone()
    if not survivor:
        raise FixNotSupported(
            f"survivor page not in pages table: {survivor_url} — refusing to "
            "redirect to an unknown destination")
    if survivor[1] is False:
        raise FixNotSupported(
            f"survivor page is not indexable ({survivor_url}) — redirect "
            "would trade one conflict for a dead destination")

    conflict = check_conflict(conn, rec["site_id"], source_url, "redirect")
    if conflict:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT recommendation_id, fix_id, status, diff_json "
                "FROM generated_fixes WHERE fix_id = %s", (conflict,))
            row = cur.fetchone()
        if row and str(row[0]) == str(rec["recommendation_id"]):
            diff = row[3] if not isinstance(row[3], str) else json.loads(row[3])
            return {"fix_id": str(row[1]), "status": row[2], "created": False,
                    "diff": diff}
        raise PolicyBlocked("active_fix_conflict", {"conflicting_fix_id": conflict})

    payload = build_redirect_payload(source_path, survivor_path)
    # Routing context rides on the payload for console/audit: status_failure
    # rows carry the dead-URL evidence, consolidate rows the cannibalization
    # context + the Phase-4 sharpened-direction evidence (cross-cluster
    # footprint + tiebreakers). Empty dict degrades cleanly for consolidates
    # generated before this gate existed.
    payload["grounding"] = {
        "route": rec.get("route") or "consolidate",
        "source_url": source_url,
        "survivor_url": survivor_url,
        **({"status_code": rec["evidence"].get("status_code"),
            "gsc_clicks_28d": rec["evidence"].get("gsc_clicks_28d"),
            "issue_type": "status_failure"}
           if rec.get("route") == "status_failure" else {}),
        **({"sharpened_direction": sharpened_grounding}
           if sharpened_grounding else {}),
    }
    diff = [{"field": "redirect", "old_value": None,
             "new_value": f"{source_path} -> {survivor_path}"}]
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO generated_fixes
                    (recommendation_id, site_id, action_type, sub_type, target_url,
                     target_entity_ref, payload_json, diff_json, generation_source,
                     status, risk_tier)
                VALUES (%s, %s, %s, 'redirect', %s, %s, %s::jsonb, %s::jsonb,
                        %s, 'generated', 'high')
                ON CONFLICT (site_id, target_url, COALESCE(sub_type, ''))
                    WHERE status IN ('generated','approved','queued') DO NOTHING
                RETURNING fix_id, status
                """,
                (rec["recommendation_id"], rec["site_id"], rec["action_type"],
                 source_url, survivor[2] or survivor_url, json.dumps(payload),
                 json.dumps(diff), generation_source),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row:
        return {"fix_id": str(row[0]), "status": row[1], "created": True,
                "diff": diff, "payload": payload}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fix_id, status FROM generated_fixes "
            "WHERE recommendation_id = %s AND sub_type = 'redirect' "
            "AND status IN ('generated','approved','queued') "
            "ORDER BY created_at DESC LIMIT 1",
            (rec["recommendation_id"],),
        )
        existing = cur.fetchone()
    if not existing:
        raise FixGenerationError("insert conflicted but no active row found")
    return {"fix_id": str(existing[0]), "status": existing[1], "created": False,
            "diff": diff, "payload": payload}


# ------------------------------------------------------------
# The hook: recommendation -> generated_fixes row (status='generated')
# ------------------------------------------------------------

def generate_fix_for_recommendation(conn, recommendation_id,
                                    generation_source="deterministic",
                                    live_seo_title=None):
    """Generate a concrete fix row for an approved improve_page recommendation.

    Returns (fix_row_dict, created: bool). No policy/cap enforcement here:
    generation is allowed while execution stays disabled (fix_policy 0 =
    'generation allowed, execution disabled'). Duplicate protection comes
    from uq_fixes_active_per_target (one active fix per site+url+field).

    live_seo_title: the CURRENT value on the store for the field being
    rewritten, when the caller has a fresh read (API/executor callers do).
    The quality gate judges the subject it rewrites: seo.title when known,
    else pages.title (truncation on the live field is exactly the failure
    this fix exists for). The executor's stale-diff guard still protects
    against drift between generation and pickup.

    The recommendation itself is NOT transitioned here — gate 1 approval is
    an operator/API decision; this hook only writes the generated_fixes row.

    consolidate rows route to the redirect path (301 weaker -> survivor).
    """
    rec = load_decision_inputs(conn, recommendation_id)
    if rec["action_type"] == "consolidate":
        return generate_redirect_fix_for_recommendation(conn, rec,
                                                        generation_source=generation_source)
    if rec.get("action_type") == "technical_fix":
        evidence = rec.get("evidence") or {}
        issue_type = evidence.get("issue_type")
        if issue_type == "status_failure":
            return generate_status_failure_redirect_fix(
                conn, rec, generation_source=generation_source)
        return generate_publish_fix_for_recommendation(conn, rec,
                                                       generation_source=generation_source)

    # plan/21 §2.1 check 6 — protect winners: position ≤2 + rising CTR
    # means the page is already winning; a rewrite risks the best asset.
    if protect_winner_check(conn, rec["site_id"], rec["target_url"]):
        raise FixNotSupported(
            "protect-winner rule: position ≤2 with rising CTR — "
            "title rewrite suppressed (never rewrite what works)")

    # plan/21 §2.1 check 7 — consolidate suppression: an active
    # consolidate recommendation on this cluster means the target page
    # may soon be redirected away; a title fix would be wasted/conflicting.
    if rec.get("cluster_id"):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT recommendation_id FROM recommendations
                WHERE site_id = %s AND cluster_id = %s
                  AND action_type = 'consolidate'
                  AND status IN ('raw', 'proposed', 'approved', 'in_progress')
                  AND recommendation_id <> %s
                LIMIT 1
                """,
                (rec["site_id"], rec["cluster_id"], rec["recommendation_id"]),
            )
            active_consolidate = cur.fetchone()
        if active_consolidate:
            raise FixNotSupported(
                "consolidate suppression: an active consolidate "
                "recommendation exists on this cluster — title fix "
                f"suppressed (consolidating rec {active_consolidate[0]})")

    # Site-level inputs for the extended validator checks.
    with conn.cursor() as cur:
        cur.execute("SELECT site_name FROM site_config WHERE site_id = %s",
                    (rec["site_id"],))
        srow = cur.fetchone()
    rec["site_name"] = srow[0] if srow else None
    site_name = rec["site_name"]
    intent = rec.get("intent")

    # v1 = seo.title only (pilot; safe default per plan/21 §7.4).
    gate_title = live_seo_title if live_seo_title is not None else rec["page_title"]
    checks = title_quality_checks(gate_title, rec["primary_keyword"])
    if checks["is_blank"] or not checks["length_ok"] or not checks["has_primary_kw"]:
        # A fix is only worth generating when a quality check actually failed.
        pass
    else:
        raise FixNotSupported(
            "title passes all quality checks — no fix warranted "
            f"(checks={checks})")

    # LLM-first drafting with fact-check + validator gates and a
    # deterministic fallback (soft-fail pipeline, never throws).
    rec["site_name"] = site_name
    drafted = draft_candidates_with_fallback(rec)
    new_title = drafted["title"]
    title_source = drafted["title_source"]
    if new_title is None:
        raise FixNotSupported("draft failed validation constraints — no fix row")
    problems = validate_title_draft(new_title, rec["primary_keyword"],
                                    product_title=rec["page_title"],
                                    intent=intent, site_name=site_name)
    if problems:
        raise FixNotSupported(f"draft rejected by validator: {problems}")

    # plan/21 §2.1 check 4 — duplicate-title guard: the new title must not
    # equal another page's title on the same site (pages.title self-join).
    if duplicate_title_check(conn, rec["site_id"], new_title,
                             exclude_url=rec["target_url"]):
        raise FixNotSupported(
            "duplicate title: another page on this site already uses the "
            "proposed title — draft rejected (plan/21 §2.1 check 4)")

    # Executor requires a GID; collections GIDs land with the §1.4 patch.
    gid = rec.get("shopify_gid")
    if not gid:
        raise FixNotSupported(
            "target page has no shopify_gid (collection GID ingestion is a "
            "§1.4 prerequisite) — fix cannot target Shopify GraphQL")

    conflict = check_conflict(conn, rec["site_id"], rec["target_url"], "seo.title")
    if conflict:
        # Same-recommendation conflict = idempotent regeneration (return the
        # existing active row); a DIFFERENT recommendation's active fix is a
        # real 409 conflict.
        with conn.cursor() as cur:
            cur.execute("SELECT recommendation_id FROM generated_fixes WHERE fix_id = %s",
                        (conflict,))
            row = cur.fetchone()
        if row and str(row[0]) == str(rec["recommendation_id"]):
            with conn.cursor() as cur:
                cur.execute("SELECT fix_id, status, diff_json FROM generated_fixes "
                            "WHERE fix_id = %s", (conflict,))
                existing = cur.fetchone()
            diff = existing[2] if not isinstance(existing[2], str) else json.loads(existing[2])
            return {"fix_id": str(existing[0]), "status": existing[1], "created": False,
                    "diff": diff}
        raise PolicyBlocked("active_fix_conflict", {"conflicting_fix_id": conflict})

    payload = build_payload(gid, new_title)
    # SERP grounding evidence rides on the payload (console/audit): the
    # framing pattern applied + competitor sample size. Content outline
    # gaps feed §2.2's content row from the title path too. The drafting
    # source (llm vs deterministic) is recorded for the audit trail.
    ctx = rec.get("competitor_context") or {}
    prefix = _competitor_prefix(rec)
    payload["grounding"] = {
        "framing_pattern": prefix.strip() if prefix else "keyword-forward",
        "competitor_titles_sampled": len(ctx.get("titles") or []),
        "url_patterns": ctx.get("url_patterns") or [],
        "content_outline": _outline_gaps(rec),
        "draft_source": drafted["title_source"],
        "llm_candidates_rejected": [r for r in drafted["rejected"]
                                    if r["field"] == "title"],
    }
    # generation_source: 'agent' = LLM-drafted value, 'deterministic' =
    # baseline drafter (column CHECK constraint allows exactly these two).
    generation_source = "agent" if title_source == "llm" else generation_source
    # old_value = the value the gate judged (live seo.title when a fresh
    # read supplied it) — the executor's stale-diff guard compares the live
    # snapshot against exactly this value.
    diff = build_diff(gate_title, new_title)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO generated_fixes
                    (recommendation_id, site_id, action_type, sub_type, target_url,
                     target_entity_ref, payload_json, diff_json, generation_source,
                     status, risk_tier)
                VALUES (%s, %s, %s, 'seo.title', %s, %s, %s::jsonb, %s::jsonb,
                        %s, 'generated', 'low')
                ON CONFLICT (site_id, target_url, COALESCE(sub_type, ''))
                    WHERE status IN ('generated','approved','queued') DO NOTHING
                RETURNING fix_id, status
                """,
                (rec["recommendation_id"], rec["site_id"], rec["action_type"],
                 rec["target_url"], gid, json.dumps(payload), json.dumps(diff),
                 generation_source),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row:
        return {"fix_id": str(row[0]), "status": row[1], "created": True,
                "diff": diff, "payload": payload}
    # Index conflict: fetch the existing active row (idempotent generation).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fix_id, status FROM generated_fixes "
            "WHERE recommendation_id = %s AND sub_type = 'seo.title' "
            "AND status IN ('generated','approved','queued') "
            "ORDER BY created_at DESC LIMIT 1",
            (rec["recommendation_id"],),
        )
        existing = cur.fetchone()
    if not existing:
        raise FixGenerationError("insert conflicted but no active row found")
    return {"fix_id": str(existing[0]), "status": existing[1], "created": False,
            "diff": diff, "payload": payload}
# ------------------------------------------------------------
# Publish-state fix generation (Phase 3, plan/21 §2.3 #1/#3):
# sitemap_index_mismatch / not_indexable where the blocking mechanism is
# the Shopify publish state — the only OTHER auto-executable surface.
# Root-cause chain comes from plan/03's mechanism_hypothesis (canonical →
# robots → status → publish_state); only publish_state routes to a write.
# ------------------------------------------------------------

PUBLISH_PRODUCT_STATUS_DOC_URL = (
    "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/productUpdate"
)
PUBLISHABLE_PUBLISH_DOC_URL = (
    "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/publishablePublish"
)

# issue_types whose evidence can carry mechanism_hypothesis (plan/03).
PUBLISH_STATE_ISSUE_TYPES = ("sitemap_index_mismatch", "not_indexable")


def build_publish_product_payload(gid, status="ACTIVE"):
    """productUpdate(status) payload (VERIFIED 2026-01 ProductUpdateInput.status)."""
    return {
        "mutation": "productUpdate",
        "variables": {"product": {"id": gid, "status": status}},
        "scope_required": "write_products",
        "doc_url": PUBLISH_PRODUCT_STATUS_DOC_URL,
    }


def build_collection_publish_payload(gid, publication_id):
    """publishablePublish payload (VERIFIED 2026-01; write_publications)."""
    return {
        "mutation": "publishablePublish",
        "variables": {"id": gid, "publicationId": publication_id},
        "scope_required": "write_publications",
        "doc_url": PUBLISHABLE_PUBLISH_DOC_URL,
    }


def generate_publish_fix_for_recommendation(conn, rec,
                                            generation_source="deterministic"):
    """Approved technical_fix recommendation -> publish-state fix row.

    Routing rules (plan/21 §2.3, deterministic):
      * issue_type must be sitemap_index_mismatch or not_indexable;
      * evidence.mechanism_hypothesis must be 'publish_state' (canonical /
        robots / status blocks are theme- or crawl-controlled -> plan_only);
      * product pages -> productUpdate(status: ACTIVE) (write_products);
      * collection pages -> publishablePublish (write_publications) with the
        publication id resolved + cached via resolve_publication_id — a None
        publication id refuses (typed), never a guessed channel;
      * GID required (the executor writes GraphQL).

    Contract mirrors the title/meta/redirect paths: idempotent regeneration,
    409 on a conflicting active fix, no policy enforcement here.
    """
    from connectors.shopify import resolve_publication_id, ShopifyGraphQLClient
    import os

    evidence = rec.get("evidence")
    if not isinstance(evidence, dict):
        try:
            evidence = json.loads(evidence) if isinstance(evidence, str) else {}
        except (TypeError, ValueError):
            evidence = {}
    issue_type = evidence.get("issue_type") or rec.get("primary_keyword")
    if issue_type not in PUBLISH_STATE_ISSUE_TYPES:
        raise FixNotSupported(
            f"publish-state fix applies to {PUBLISH_STATE_ISSUE_TYPES} only "
            f"(got {issue_type!r})")

    hypothesis = evidence.get("mechanism_hypothesis")
    if hypothesis and hypothesis != "publish_state":
        raise FixNotSupported(
            f"mechanism_hypothesis={hypothesis!r} is not publish-controlled — "
            "no auto write (plan_only surface)")

    gid = rec.get("shopify_gid")
    if not gid:
        raise FixNotSupported(
            "target page has no shopify_gid — fix cannot target Shopify GraphQL")

    page_type = rec.get("page_type")
    if page_type == "product":
        sub_type = "product_publish_product"
        payload = build_publish_product_payload(gid, "ACTIVE")
        diff = [{"field": "product.status",
                 "old_value": evidence.get("current_status") or "DRAFT",
                 "new_value": "ACTIVE"}]
    elif page_type == "collection":
        sub_type = "collection_publish"
        shop_domain = rec.get("shop_domain")
        token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
        publication_id = None
        if shop_domain and token:
            client = ShopifyGraphQLClient(shop_domain=shop_domain,
                                          access_token=token)
            publication_id = resolve_publication_id(conn, client)
        if not publication_id:
            raise FixNotSupported(
                "no publication id resolved (system_config cache empty and "
                "no store credentials for the publications query) — refusing "
                "to guess a channel; enable after the scope probe caches one")
        payload = build_collection_publish_payload(gid, publication_id)
        diff = [{"field": "publishable.published_on_publication",
                 "old_value": False, "new_value": True}]
    else:
        raise FixNotSupported(
            f"publish-state write covers products/collections only "
            f"(page_type={page_type!r})")

    conflict = check_conflict(conn, rec["site_id"], rec["target_url"], sub_type)
    if conflict:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT recommendation_id, fix_id, status, diff_json "
                "FROM generated_fixes WHERE fix_id = %s", (conflict,))
            row = cur.fetchone()
        if row and str(row[0]) == str(rec["recommendation_id"]):
            diff = row[3] if not isinstance(row[3], str) else json.loads(row[3])
            return {"fix_id": str(row[1]), "status": row[2], "created": False,
                    "diff": diff}
        raise PolicyBlocked("active_fix_conflict",
                            {"conflicting_fix_id": conflict})

    payload["grounding"] = {
        "issue_type": issue_type,
        "mechanism_hypothesis": hypothesis or "publish_state",
        "indexable": rec.get("page_indexable"),
        "status_code": rec.get("page_status_code"),
    }
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO generated_fixes
                    (recommendation_id, site_id, action_type, sub_type, target_url,
                     target_entity_ref, payload_json, diff_json, generation_source,
                     status, risk_tier)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s, 'generated', 'medium')
                ON CONFLICT (site_id, target_url, COALESCE(sub_type, ''))
                    WHERE status IN ('generated','approved','queued') DO NOTHING
                RETURNING fix_id, status
                """,
                (rec["recommendation_id"], rec["site_id"], rec["action_type"],
                 sub_type, rec["target_url"], gid, json.dumps(payload),
                 json.dumps(diff), generation_source),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row:
        return {"fix_id": str(row[0]), "status": row[1], "created": True,
                "diff": diff, "payload": payload}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fix_id, status FROM generated_fixes "
            "WHERE recommendation_id = %s AND sub_type = %s "
            "AND status IN ('generated','approved','queued') "
            "ORDER BY created_at DESC LIMIT 1",
            (rec["recommendation_id"], sub_type),
        )
        existing = cur.fetchone()
    if not existing:
        raise FixGenerationError("insert conflicted but no active row found")
    return {"fix_id": str(existing[0]), "status": existing[1], "created": False,
            "diff": diff, "payload": payload}


# ------------------------------------------------------------
# status_failure redirect generation (Phase 3 closure, plan/21 §2.3 #4):
# a 404 product/collection URL with residual GSC traffic -> 301 to the
# nearest equivalent. Write target = the EXISTING redirect adapter
# (urlRedirectCreate; rollback urlRedirectDelete of the created GID).
# ------------------------------------------------------------

# plan/16 residual-traffic gate: a dead URL with no residual visibility is
# hygiene, not a redirect (Google has already forgotten it).
STATUS_FAILURE_MIN_CLICKS_28D = 10
STATUS_FAILURE_MIN_IMPRESSIONS_28D = 100


def _status_failure_survivor(conn, rec):
    """Resolve the redirect destination, deterministic (plan/21 §2.3 #4).

    Order:
      1. The dead page's OWN product_type collection, if one exists in pages
         and is indexable (`/collections/<product-type-slug>`).
      2. catalogue_coverage.existing_collection_url for the cluster the
         recommendation carries (when present), if indexable in pages.
      3. Refuse (FixNotSupported) — never a guessed destination.

    Returns the SURVIVOR URL (absolute) that all redirect gates run against.
    """
    evidence = rec.get("evidence") or {}
    page_type = evidence.get("page_type")
    product_type = (evidence.get("product_type") or "").strip()

    if product_type and page_type in ("product", None):
        slug = re.sub(r"[^a-z0-9]+", "-", product_type.lower()).strip("-")
        candidate = (f"https://{rec['domain']}/collections/{slug}"
                     if rec.get("domain") and slug else None)
        if candidate:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT url, indexable FROM pages "
                    "WHERE site_id = %s AND (url = %s OR url = %s) "
                    "ORDER BY indexable DESC, url LIMIT 1",
                    (rec["site_id"], candidate,
                     candidate.rstrip("/") + "/"),
                )
                row = cur.fetchone()
            if row and row[1] is not False:
                return row[0]

    if rec.get("cluster_id"):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cc.existing_collection_url FROM catalogue_coverage cc "
                "WHERE cc.site_id = %s AND cc.cluster_id = %s "
                "AND cc.existing_collection_url IS NOT NULL",
                (rec["site_id"], rec["cluster_id"]),
            )
            cc_row = cur.fetchone()
        if cc_row and cc_row[0]:
            candidate = cc_row[0]
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT url, indexable FROM pages "
                    "WHERE site_id = %s AND url = %s",
                    (rec["site_id"], candidate),
                )
                row = cur.fetchone()
            if row and row[1] is not False:
                return row[0]
    return None


def generate_status_failure_redirect_fix(conn, rec,
                                         generation_source="deterministic"):
    """Approved status_failure recommendation -> sub_type='redirect' fix row.

    Routing (plan/21 §2.3 #4 + plan/16 status_failure, deterministic):
      * evidence.status_code must be a dead-status (404/410) — 5xx/403 are
        real defects with a DIFFERENT fix, not a 301;
      * residual-traffic gate: gsc_clicks_28d >= 10 (or impressions >= 100
        with some clicks) — no residual visibility = plan_only hygiene;
      * destination: the dead page's product_type collection, else
        catalogue_coverage.existing_collection_url, else refuse;
      * survivor must be indexable (never redirect to a dead destination);
      * source path == survivor path -> no-op loop, refused;
      * sub_type='redirect' (risk 'high') — SAME adapter, conflict guard,
        and rollback contract as the consolidate path: pre-state snapshot at
        pickup, revert deletes the created redirect via snapshot
        redirect.created_id.
    """
    evidence = rec.get("evidence")
    if not isinstance(evidence, dict):
        try:
            evidence = json.loads(evidence) if isinstance(evidence, str) else {}
        except (TypeError, ValueError):
            evidence = {}

    issue_type = evidence.get("issue_type") or rec.get("primary_keyword")
    status_code = evidence.get("status_code") or rec.get("page_status_code")
    if status_code not in (404, 410):
        raise FixNotSupported(
            f"status_failure redirect applies to 404/410 only (got "
            f"HTTP {status_code!r}) — 5xx/403 need a different fix")

    clicks = int(evidence.get("gsc_clicks_28d") or 0)
    impressions = int(evidence.get("gsc_impressions_28d") or 0)
    if not (clicks >= STATUS_FAILURE_MIN_CLICKS_28D
            or (clicks > 0 and impressions >= STATUS_FAILURE_MIN_IMPRESSIONS_28D)):
        raise FixNotSupported(
            f"no residual traffic on the dead URL (clicks_28d={clicks}, "
            f"impressions_28d={impressions}) — plan_only hygiene, no 301 "
            "(plan/16: Google has already forgotten it)")

    source_url = rec["target_url"]
    survivor_url = _status_failure_survivor(conn, rec)
    if not survivor_url:
        raise FixNotSupported(
            "no indexable equivalent destination for the 404 "
            "(no product_type collection in pages, no "
            "catalogue_coverage.existing_collection_url) — refusing to "
            "redirect to a guessed target")

    # Reuse the shared redirect machinery: same payload builder, conflict
    # guard shape, and (at execution) the same adapter + rollback contract.
    redirect_rec = dict(rec)
    redirect_rec["route"] = "status_failure"
    redirect_rec["source_url"] = source_url
    redirect_rec["survivor_url"] = survivor_url
    redirect_rec["domain"] = rec.get("domain")
    # check_conflict keys on the SOURCE url (the redirect subject), matching
    # the consolidate path exactly.
    return generate_redirect_fix_for_recommendation(
        conn, redirect_rec, generation_source=generation_source)

"""Segmented off-page drafter with hard eligibility gates (plan/33).

v1 drafts two SEPARATE segments and assembles them only after both pass:

    advice_md   genuinely useful, peer-level answer to the thread
    mention_md  at most a natural, grounded mention of the client

The split exists because of a false-positive in the house grounding
check. `fixes.generator.verify_grounded_claims` validates the WHOLE
candidate against the client's page facts, which is correct for a
60-character meta description where every word is about the product, and
WRONG for a community reply that is 80% general advice. A paragraph
explaining how to evaluate any warranty contains the cue "warranty"
(`CLAIM_CUES`), and would be rejected as an ungrounded claim about the
client even though it asserts nothing about the client. So:

    mention_md -> verify_grounded_claims(...) against the TENANT CORPUS
                  (every claim here IS about the client; full guard)
    advice_md  -> the inverse check (see `advice_problems`): generic
                  category vocabulary is allowed, a commercial claim
                  about the client is not

The gates are evaluated in order and each one writes an `offpage_events`
row on BOTH outcomes. A log that records only refusals is not an audit
trail - it cannot prove a gate was ever evaluated and passed, and that is
the property that makes the system defensible to an operator reviewing a
published reply.

Three gates refuse EVERYTHING, including an advice-only draft, because
their absence makes any AI-authored reply inappropriate or impossible:
`ai_content_allowed`, `context_sufficient`, `grounding_corpus`.

The remaining gates gate the MENTION only. When one of them refuses, the
draft degrades to advice-only rather than failing outright. That is the
literal reading of "if any answer is uncertain, don't generate the
promotional version" - the useful answer is still worth having, and an
advice-only reply is exactly the behaviour Reddit's own organic guidance
asks for.

Nothing in this module publishes. Publication is manual and lives in the
queue UI; `offpage_drafts` has no row the automated executor can claim.
"""

import json
import re

from fixes.generator import (
    BANNED_META_PATTERNS,
    LlmDraftError,
    _content_tokens,
    _has_emoji,
    _llm_client,
    verify_grounded_claims,
)

# ------------------------------------------------------------
# Thresholds
# ------------------------------------------------------------
# MIN-CONTEXT CHARS: a DataForSEO SERP description for a Reddit thread is
# frequently a truncated thread title rather than the question asked.
# Below this we cannot tell what is being asked, and a draft written
# without knowing the question is worse than no draft - it is a generic
# reply that says nothing, which is the behaviour platform defences
# cluster on. The gate fails closed and drops the opportunity.
MIN_CONTEXT_CHARS = 180

# Set by _call_model when the provider call itself raised. Kept module-level
# so the None-return contract (provider failure is not a content problem)
# stays intact while the reason stays visible in the gate log.
_LAST_PROVIDER_ERROR = None

# Single-reply helpfulness floor. KEPT deliberately even though the
# account-level 80/20 ratio was retired: this is gate 5 ("is the mention
# genuinely necessary?") expressed as a measurement. It is a property of
# ONE reply and is not satisfiable by producing more content, so unlike
# the retired cross-post ratio it cannot be optimised against.
MIN_HELPFULNESS_RATIO = 0.80

MIN_ADVICE_WORDS = 40
MAX_ADVICE_WORDS = 220
MAX_MENTION_WORDS = 60

# plan/35: post_kind='article' (Medium long-form) uses its own word window.
# 650-1500 content words: deep enough to be an authority piece, tight enough
# that a single model call reliably lands inside it - the OLD 800-2000 window
# turned every draft into a coin flip (undershoot < 800 refusals and runaway
# > 2000 refusals on the same instruction). Bounds stay the fail-closed
# long-form contract: outside the window the article is refused, never
# silently trimmed.
LONG_FORM_MIN_WORDS = 650
LONG_FORM_MAX_WORDS = 1500

# X (plan/35): conservative 280-char ceiling default, matching the free-tier
# public post limit. Deliberately NOT derived from any paid-tier published
# limit (plan/33's "never derive a brake from a platform limit" rule).
X_CHAR_LIMIT_DEFAULT = 280

# X account must be at least this old before promotional copy is allowed.
# Age is OPERATOR-ASSERTED unless last_verified_at is set (plan/33 §3).
X_MIN_ACCOUNT_AGE_DAYS = 90

# Medium long-form: at most one contextual backlink, and it must resolve to
# a page in grounding_page_ids. A link to anything else is a claim the
# corpus did not authorise.
LONG_FORM_MAX_BACKLINKS = 1

# Reused from the collection validator's inverse-overlap tripwire
# (`COLLECTION_DESC_OVERLAP_MIN`). Long-form prose never satisfies
# substring matching, so for a reply the sentence-overlap measure is the
# PRIMARY grounding signal and the claim-cue check is secondary.
MIN_SENTENCE_OVERLAP = 0.35

# plan/35 long-form grounding: applied at PARAGRAPH-BLOCK level, not
# sentence level. A per-sentence floor on a 1,000-word article punished
# necessary transitional sentences ("The result is the same either way.")
# - a false positive class the live tenant corpus kept tripping. Measured
# over a whole paragraph block, the corpus vocabulary amortises the
# connective tissue, so genuinely grounded prose passes while a block of
# invented specifics still fails. Same floor, coarser unit.
MIN_BLOCK_OVERLAP = 0.35

# Call-to-action and promotion-by-stealth vocabulary. Checked against
# `advice_md` because that segment is supposed to be pure answer; a CTA
# there is a mention that escaped into the advice.
_CTA_CUES = (
    "check out", "try it", "try ours", "our store", "our site", "our shop",
    "sign up", "subscribe", "discount code", "promo code", "use code",
    "dm me", "link in bio", "we make", "we sell", "our product",
)

# Superlatives that are only defensible inside a verified fact base.
_PROMO_SUPERLATIVES = (
    "best", "leading", "top-rated", "top rated", "#1", "number one",
    "unmatched", "unbeatable", "world-class", "world class",
)

_BLOCK_END = re.compile(r"</(p|div|li|tr|h[1-6]|br)\s*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+|\bwww\.\S+", re.I)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


class OffpageDraftBlocked(Exception):
    """Refused before any draft could be written. Recorded as an
    `offpage_events` row against the thread/community, never silently."""


# ------------------------------------------------------------
# Off-page drafting system prompt
# ------------------------------------------------------------
OFFPAGE_SYSTEM_PROMPT = """You write replies for a client's own account in \
online communities. You are NOT a marketer writing ad copy, and your output \
is reviewed by a human before anything is posted.

You receive:
  - thread: the title and the SERP snippet of a real community thread
  - sources: VERIFIED text from the client's own product pages

Return ONLY a JSON object with exactly two string fields:

  "advice_md"  A genuinely useful, peer-level answer to the thread's actual \
question. Write the way an experienced person in the category would answer \
in a forum: concrete, specific, no preamble, no headings, no "Great \
question!". Generic category knowledge is welcome here - you may discuss \
what to look for, common trade-offs, and how to evaluate an option, using \
ordinary vocabulary that anyone in the category would use. You must NOT \
mention the client's brand, link to it, or make any claim about a specific \
product. NEVER include any URL or link in advice_md - links belong only \
in mention_md, and only when permitted.

  "mention_md"  AT MOST two sentences connecting the client's product to \
the question, or an empty string if the connection would be forced. Every \
factual claim here must be supported by the supplied sources. No \
superlatives, no calls to action, no urgency. If the sources do not \
support a natural mention, return an empty string - an empty mention is a \
correct and expected answer.

The advice must stand on its own. A reader who ignores the brand entirely \
should still find the reply worth their time."""


OFFPAGE_ARTICLE_ADDENDUM = """

KIND: This job is a LONG-FORM ARTICLE, not a comment.

"advice_md" MUST be a complete article of about 1400 words, counting \
every word the way a text editor does (including "the", "of", "a"). \
Hard range: 1200-1700 editor words; shorter or longer is discarded. \
Structure the size deliberately: about 45 short paragraphs of 2-3 \
sentences each, and STOP WRITING once you reach the target size - do \
not repeat earlier points, do not add a closing summary, do not pad. \
NO headings, NO preamble, no ALL-CAPS words, normal sentence case. \
"advice_md" must contain NO links, NO brand name, NO call to action, \
and no superlatives - it is the brand-free authority piece, and plain \
category vocabulary is correct there. Tool names, platform names and \
acronyms that the sources never mention (GA4, ROAS, Meta, Klaviyo, \
etc.) are treated as ungrounded vocabulary: build every sentence from \
ordinary ecommerce vocabulary plus the sources' own words instead.

"mention_md" MUST hold exactly one sentence that connects the client's \
product to the article's topic and ends with a Markdown link in the \
form [name](URL) - for example [product pages](https://www.gethelium.\
co/ad-stack) - where the URL is copied verbatim from the sources list. \
Never invent a URL, never link a page the sources do not list, and \
never put a link in advice_md.

Each PARAGRAPH-BLOCK (a pair of adjacent paragraphs) of advice_md is \
checked mechanically: at least 35% of the block's content words (words \
of 3+ letters, ignoring the/a/of/in, etc.) must ALSO appear somewhere \
in the source text. The payload's `vocabulary_to_reuse` list is that \
vocabulary, measured from the sources - build EVERY paragraph from \
those words. Short transitional sentences do NOT fail by themselves: \
the grounded vocabulary of their surrounding paragraphs carries the \
block. What still fails: a block built from invented specifics. THE \
WAY TO PASS: write each paragraph around the reuse vocabulary. The \
sources also say "adaptive storefront", "shopper intent", "paths to \
purchase", "real time personalisation", "conversion", "sessions" - \
REUSE those exact words while you develop the advice. Example of a \
paragraph that passes: "Adaptive storefront real time personalisation \
gives each shopper clearer paths to purchase. Conversion without \
scaling spend compounds because visitor context reaches the shopper at \
the moment that matters." Example of a paragraph that FAILS: "Email \
marketers should segment their lists quarterly because list hygiene \
drives deliverability metrics upward across every measurable sender \
reputation dimension."

Write NATURAL, fluent prose: vary sentence openers and lengths (8-25 \
words), never repeat a sentence pattern or an opening phrase, and drop \
a reuse word that cannot fit a sentence naturally - only the block's \
overall share of grounded vocabulary matters.
"""

# ------------------------------------------------------------
# Small deterministic helpers
# ------------------------------------------------------------
def strip_html(text):
    text = _BLOCK_END.sub(" ", text or "")
    text = _ANY_TAG.sub(" ", text)
    return _WS.sub(" ", text).strip()


def word_count(text):
    return len(_content_tokens(text))


def sentences(text):
    return [s.strip() for s in _SENTENCE_SPLIT.split(strip_html(text))
            if s.strip()]


def helpfulness_ratio(advice_md, mention_md):
    """Share of the reply that is answer rather than promotion.

    Measured on CONTENT WORDS, not characters, so that a long link in the
    mention cannot be used to dilute the ratio. Denominator is floored at
    the advice length so an empty mention yields 1.0 rather than a
    division error.
    """
    advice_words = word_count(advice_md)
    mention_words = word_count(mention_md)
    total = advice_words + mention_words
    if total == 0:
        return 0.0
    return round(advice_words / total, 3)


def promo_density(mention_md):
    """Promotional vocabulary per word in the mention segment, 0..1."""
    words = word_count(mention_md)
    if words == 0:
        return 0.0
    lowered = mention_md.lower()
    hits = sum(1 for cue in _CTA_CUES + _PROMO_SUPERLATIVES if cue in lowered)
    return round(min(hits / words * 10, 1.0), 3)


def link_count(text):
    return len(_URL.findall(text or ""))


# ------------------------------------------------------------
# Context sufficiency (gate 0 - refuses EVERYTHING)
# ------------------------------------------------------------
def context_problems(thread):
    """Can this thread be answered at all from a SERP snippet?

    v1 is SERP-only by decision (plan/33). This gate is the accepted cost
    of that decision made explicit and measured: when the snippet cannot
    support a real answer, the opportunity is DROPPED rather than drafted
    blind. `context_source='operator_import'` bypasses the length floor
    only - every other gate still applies, because human-supplied text is
    no more permission to promote.
    """
    title = (thread.get("title") or "").strip()
    snippet = (thread.get("snippet") or "").strip()
    source = thread.get("context_source") or "serp"
    combined = strip_html(f"{title} {snippet}")
    chars = len(combined)

    if not title and not snippet:
        return ["no thread context at all"]
    if source == "serp" and chars < MIN_CONTEXT_CHARS:
        return [f"SERP context too thin to answer "
                f"({chars} chars < {MIN_CONTEXT_CHARS}); drop the "
                f"opportunity or have an operator paste the thread text"]
    words = word_count(combined)
    if words < 25:
        return [f"context too thin to answer ({words} content words)"]
    return []


# ------------------------------------------------------------
# Eligibility gates (promotion only; advice degrades instead of failing)
# ------------------------------------------------------------
def community_promotion_problems(community):
    """Gate 1: does this community allow promotion at all?

    Three separate facts, each nullable on purpose. NULL is never read as
    permissive: the community rules were not resolved, and promoting into
    an unread-ruleset community is the failure mode that gets an account
    banned.
    """
    problems = []
    if not community.get("enabled"):
        problems.append("community disabled (kill-switch active)")
    if community.get("ai_content_allowed") is not True:
        # Refuses ALL drafting, not just promotion - this gate is checked
        # separately in `hard_gate_problems`.
        pass
    if community.get("link_policy") in (None, "unknown"):
        problems.append("link policy unresolved ('unknown')")
    if community.get("link_policy") == "forbidden":
        problems.append("community forbids promotional links")
    if community.get("allows_self_promo") is not True:
        problems.append("self-promotion permission not confirmed (NULL = "
                        "rules never read)")
    return problems


def ai_content_problems(community):
    """Gate 0: may we author AI content in this community at all?

    Some communities prohibit substantial AI-generated content outright;
    others permit it. NULL means the rules were never read, and reading
    it as permissive is the expensive direction to be wrong in, because
    the consequence is a community-wide ban rather than one removed post.
    """
    if community.get("enabled") is not True:
        return ["community disabled (kill-switch active)"]
    if community.get("ai_content_allowed") is not True:
        return ["community rules on AI-generated content unresolved or "
                "prohibited (ai_content_allowed is not TRUE)"]
    return []


def thread_promotion_problems(thread):
    """Gate 2: does THIS THREAD permit promotion?

    A separate fact from the community's. A subreddit may allow
    self-promotion only in designated weekly threads, or require
    moderator approval for a particular question. Community permission
    cannot answer this, and conflating them produces exactly the silent
    automod removal this system exists to prevent.
    """
    if thread.get("promotion_allowed") is True:
        return []
    if thread.get("promotion_allowed") is None:
        return ["thread-level promotion permission not determined "
                "(NULL = nobody checked this thread's rules)"]
    return ["this thread does not permit promotion"]


def account_problems(account, community_id):
    """Gates 3 and 4: is this account appropriate here, and under its cap?

    Account age and karma are OPERATOR-ASSERTED, not verified - v1 uses no
    platform auth for reads, and `last_verified_at IS NULL` is the
    structural signal that nobody confirmed them. Standing in a specific
    community is asserted separately in `offpage_account_communities`.
    """
    problems = []
    if account.get("status") != "active":
        problems.append(f"account status is {account.get('status')!r}, "
                        f"not 'active'")
    if not (account.get("brand_name") or "").strip():
        problems.append("no brand_name configured; refusing to invent one")
    if account.get("last_verified_at") is None:
        # Not fatal: an aged account we have not platform-verified is
        # still a real account. Recorded so the UI can render the
        # standing as asserted rather than confirmed.
        pass
    if not account.get("standing"):
        problems.append("account standing in this community not assessed "
                        "(offpage_account_communities)")
    elif account.get("standing") in ("none",):
        problems.append("account has no standing in this community")
    return problems


def cap_problems(cap_usage):
    """Gate 5: promotional ceiling for this account, rolling 30 days.

    A CEILING, not the retired ratio floor. A floor is a target: the only
    way to satisfy a helpful:promotional ratio while wanting more
    promotional output is to manufacture filler, which is itself spam. A
    ceiling cannot be satisfied by producing more content, so it cannot
    be optimised against.
    """
    if not cap_usage:
        return ["promotional cap usage unknown; refusing to assume "
                "headroom"]
    if cap_usage.get("cap_reached"):
        return [f"promotional cap reached "
                f"({cap_usage['promotional_30d']}/"
                f"{cap_usage['promotional_cap_30d']} in 30 days)"]
    return []


# ------------------------------------------------------------
# plan/35 gate adjustments (documented in plan/35-offpage-multi-platform-specs.md)
# ------------------------------------------------------------

def channel_enabled_problems(ctx):
    """Gate (HARD): a plan/35 channel is refuse-by-default until an operator
    opts the tenant in via offpage_platform_settings.enabled.

    reddit/quora/medium keep the plan/33 behaviour (no settings row = as
    before). x and shopify_community HAVE no prior behaviour: without an
    enabled settings row there is nothing to draft for, so everything is
    refused - the fix_policy fail-closed posture applied to a channel.
    `medium` is not on this list because its reply path predates plan/35;
    the ARTICLE path on medium is additionally gated by post_kind below.
    """
    if ctx.get("platform") not in ("x", "shopify_community"):
        return []
    settings = ctx.get("settings") or {}
    if not settings.get("enabled"):
        return ["offpage_platform_settings row missing or disabled for "
                f"platform {ctx.get('platform')} (fail-closed; operator "
                "opt-in required)"]
    return []


def x_account_standing_problems(account):
    """Gate (SOFT): an X account too young to have real standing must not
    carry a brand mention.

    Age is OPERATOR-ASSERTED unless `last_verified_at` is set (plan/33 §3),
    so NULL here is a refusal, not an unknown: we have no basis to believe
    an account is old enough, and posting promotion from a fresh account is
    the single strongest account-fingerprint signal on X.
    """
    problems = []
    created = account.get("account_created_on")
    if created is None:
        problems.append("X account age not asserted (account_created_on "
                        "NULL); cannot confirm the account is "
                        f"{X_MIN_ACCOUNT_AGE_DAYS}+ days old")
        return problems
    try:
        import datetime as _dt
        age_days = (_dt.date.today() - created).days
    except TypeError:
        problems.append("account_created_on is not a date")
        return problems
    if age_days < X_MIN_ACCOUNT_AGE_DAYS:
        problems.append(f"X account only {age_days}d old (< "
                        f"{X_MIN_ACCOUNT_AGE_DAYS}d required for promotion)")
    return problems


def shopify_community_fit_problems(ctx):
    """Gate (SOFT): does the CLIENT'S Shopify app genuinely answer this
    merchant question?

    The `functional-ness` of a Shopify Community post is fact, not spin:
    a merchant asks how to do X in their store, the client's app automates
    X. Without a configured app identity (targeting.shopify.fit_keywords)
    or a thread that matches it, a promotional mention is promotional
    noise in a merchant forum - exactly the overt-spam behaviour the
    channel exists to avoid. The advice segment is still published
    (helpful content is how standing is built); the mention is withheld.
    """
    if ctx.get("platform") != "shopify_community":
        return []
    problems = []
    if ctx.get("site_platform") != "shopify":
        problems.append("client site platform is not 'shopify'; there is "
                        "no app whose fit to assert")
        return problems
    targeting = (ctx.get("settings") or {}).get("targeting") or {}
    shopify_targets = targeting.get("shopify") or {}
    fit_keywords = shopify_targets.get("fit_keywords") or []
    if not fit_keywords:
        problems.append("no targeting.shopify.fit_keywords configured in "
                        "offpage_platform_settings; app->merchant fit "
                        "cannot be asserted")
        return problems
    haystack = strip_html(f"{ctx.get('title') or ''} "
                          f"{ctx.get('snippet') or ''}").lower()
    if not any(str(k).lower() in haystack for k in fit_keywords):
        problems.append("thread topic does not match any configured "
                        "shopify fit_keywords; refusing a promotional "
                        "mention into an unproven merchant conversation")
    return problems


def platform_promo_cap_problems(conn, ctx):
    """Gate (SOFT): the platform-level promotional ceiling (a third brake
    on top of the account cap and the community cap, plan/35 §3).

    Counted from offpage_publications - never a denormalised counter - so
    it cannot drift out of agreement with the event trail (plan/33 rule).
    NULL/absent ceiling = no platform-level brake configured.
    """
    settings = ctx.get("settings") or {}
    cap = settings.get("promotional_posts_per_30d")
    if not cap:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM offpage_publications p
            JOIN offpage_communities c
              ON c.site_id = p.site_id
             AND c.community_id = p.community_id
            WHERE p.site_id = %s
              AND c.platform = %s
              AND p.post_type = 'promotional'
              AND p.status IN ('live', 'removed')
              AND p.submitted_at > now() - INTERVAL '30 days'
            """,
            (ctx["site_id"], ctx["platform"]),
        )
        used = cur.fetchone()[0]
    if used >= cap:
        return [f"platform promotional ceiling reached "
                f"({used}/{cap} in 30 days)"]
    return []


def char_ceiling(ctx):
    """Effective character ceiling for this draft (plan/35 §3).

    X is character-constrained in a way Reddit/Quora are not; the ceiling
    comes from offpage_platform_settings (reply for replies, post for
    articles). NULL = no ceiling (Reddit/Quora/Medium).
    """
    settings = ctx.get("settings") or {}
    if ctx.get("post_kind") == "article":
        return settings.get("post_char_limit")
    return settings.get("reply_char_limit")


def _canon_url(u):
    """www/scheme/markdown-normalised URL for corpus matching.

    The tenant's corpus pages legitimately exist under both the apex and
    the www host; the model copying either variant of the SAME page must
    not be refused a matching backlink. The model also writes mentions as
    markdown `[text](url)` or reddit-style `[url].` — the `_URL` regex
    attaches those trailing `)` / `]` / punctuation to the match, so the
    canon step trims them. Normalising scheme + `www.` + trailing
    punctuation makes the match host-canonical while still requiring an
    EXACT path match - a link to another site's page or an invented path
    never passes.
    """
    out = (u or "").strip().lower()
    out = re.sub(r"^https?://", "", out)
    out = re.sub(r"^www\.", "", out)
    return out.rstrip(r"/.,()\[\]}>'\"")


def longform_problems(ctx, advice_text, mention_text, corpus):
    """Gate (HARD): plan/35 Medium long-form contract.

    An 'article' is a different artifact from a reply: it is the client's
    own authority surface, so its standard is stricter, not looser:
      - length within [LONG_FORM_MIN_WORDS, LONG_FORM_MAX_WORDS];
      - the advice must be corpus-grounded at PARAGRAPH-BLOCK level: every
        thematic block (paragraph, or a 4-sentence group when the response
        has no paragraphs) above the overlap floor. A per-sentence floor on
        a 1,000-word body punished necessary transitional sentences, a
        false-positive class the live tenant corpus kept tripping; the
        block unit amortises connective tissue while still refusing any
        block that leans on invented specifics;
      - exactly one contextual backlink, and it must resolve to a page in
        this draft's grounding_page_ids. A backlink to anything else is a
        claim the tenant corpus never authorised.

    Refusing (rather than demoting to advice-only) is correct here: an
    article with no backlink is not long-form distribution, it is a blog
    the operator did not ask for.
    """
    problems = []
    words = word_count(advice_text)
    if words < LONG_FORM_MIN_WORDS:
        problems.append(f"long-form too short ({words} words < "
                        f"{LONG_FORM_MIN_WORDS})")
    if words > LONG_FORM_MAX_WORDS:
        problems.append(f"long-form too long ({words} words > "
                        f"{LONG_FORM_MAX_WORDS})")
    overlap = paragraph_overlap_problems(advice_text, corpus["text"])
    if overlap:
        problems.append("long-form advice not corpus-grounded: "
                        + "; ".join(overlap))
    links = _URL.findall(mention_text or "")
    if not links:
        problems.append("long-form article carries no contextual backlink "
                        "(mention_md empty or link-free)")
    elif len(links) > LONG_FORM_MAX_BACKLINKS:
        problems.append(f"long-form article has {len(links)} backlinks "
                        f"(max {LONG_FORM_MAX_BACKLINKS})")
    else:
        allowed = {_canon_url(str(s.get("url") or ""))
                   for s in corpus.get("sources", [])}
        in_corpus = [u for u in links if _canon_url(u) in allowed]
        if not in_corpus:
            problems.append("contextual backlink does not resolve to a "
                            "grounding_page_ids source URL")
    return problems


# ------------------------------------------------------------
# Content gates
# ------------------------------------------------------------
def advice_problems(advice_md, brand_name, post_kind="reply"):
    """Gate 6: is the answer independently useful, and free of the brand?

    The INVERSE of `verify_grounded_claims`. That function asks "is every
    claim here supported by the client's pages?"; this asks "is this
    segment free of claims ABOUT the client?". Generic category
    vocabulary is explicitly allowed - that is what makes the segment
    advice rather than promotion, and requiring it to be corpus-backed
    would be the false positive that motivated the segmented design.

    `post_kind` picks the word window: a reply's 40..220 words is right
    for a forum comment and wrong for a Medium article of 650..1500
    content words (plan/35), and vice versa. The article bounds are the
    fail-closed long-form contract - below the floor the piece is a stub,
    above the ceiling it is a brain-dump, and either one is refused
    rather than shipped.
    """
    problems = []
    text = strip_html(advice_md)
    if not text:
        return ["empty advice segment"]
    words = word_count(text)
    min_words = (LONG_FORM_MIN_WORDS if post_kind == "article"
                 else MIN_ADVICE_WORDS)
    max_words = (LONG_FORM_MAX_WORDS if post_kind == "article"
                 else MAX_ADVICE_WORDS)
    if words < min_words:
        problems.append(f"advice too short ({words} words < "
                        f"{min_words})")
    if words > max_words:
        problems.append(f"advice too long ({words} words > "
                        f"{max_words})")
    lowered = text.lower()
    if brand_name and brand_name.strip().lower() in lowered:
        # A brand name in the advice segment is a mention that escaped
        # into the advice, which is precisely the promotion-by-stealth the
        # split exists to make visible.
        problems.append(f"advice segment names the client brand "
                        f"({brand_name!r}); that belongs in mention_md")
    if link_count(text):
        problems.append("advice segment contains a link")
    for cue in _CTA_CUES:
        # Word-boundary match: a bare substring match refused a legitimate
        # advice segment for containing "our shoppers" (the cue "our shop"
        # is its prefix). "shoppers" is vocabulary, not a call to action.
        if re.search(r"\b%s\b" % re.escape(cue), lowered):
            problems.append(f"call-to-action in advice segment: {cue!r}")
    for pattern in BANNED_META_PATTERNS:
        if pattern in lowered:
            problems.append(f"banned promotional phrase: {pattern!r}")
    if _has_emoji(text):
        problems.append("emoji in reply")
    if _shout_run(text) >= 3:
        problems.append("ALL-CAPS run (3+ shouted words)")
    if re.search(r"(\W)\1{2,}", text):
        problems.append("spam punctuation stack")
    return problems


def _shout_run(text):
    """Longest run of CONSECUTIVE all-caps words (3+ chars each).

    `_caps_runs` counts total caps words anywhere in the text, which
    refuses legitimate analytics prose: ROAS ... GA4 ... SEO are acronyms
    scattered across a paragraph, not shouting. Shouting is adjacency -
    "BUY NOW FREE SHIPPING" - so count only consecutive caps words.
    """
    run = longest = 0
    for w in re.findall(r"[A-Za-z']+", text or ""):
        if len(w) >= 3 and w.isupper():
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest


def mention_problems(mention_md, corpus, community):
    """Gates 7 and 8: is the mention necessary, and is it grounded?

    `verify_grounded_claims` is applied to the mention segment ALONE,
    against the tenant corpus. Because every claim in this segment is
    about the client, the house grounding check is exactly right here -
    it is the whole-string check that was wrong for the reply as a whole.
    """
    problems = []
    text = strip_html(mention_md)
    if not text:
        return problems          # empty mention is a correct answer
    if word_count(text) > MAX_MENTION_WORDS:
        problems.append(f"mention too long ({word_count(text)} words > "
                        f"{MAX_MENTION_WORDS})")
    # Grounding. `_fact_base` reads rec["body_text"], so the corpus is
    # supplied under that key rather than reimplementing the fact base.
    rec_for_facts = {"page_title": "", "body_text": corpus.get("text", "")}
    ok, grounding_problems = verify_grounded_claims(text, rec_for_facts)
    if not ok:
        problems.extend(grounding_problems)
    lowered = text.lower()
    for cue in _PROMO_SUPERLATIVES:
        if cue in lowered:
            problems.append(f"unverified superlative: {cue!r}")
    for cue in ("urgent", "hurry", "last chance", "today only"):
        if cue in lowered:
            problems.append(f"urgency language: {cue!r}")
    links = link_count(text)
    policy = community.get("link_policy")
    if links and policy in ("forbidden", "unknown", "ask_first"):
        problems.append(f"link present but link_policy={policy!r}")
    return problems


def sentence_overlap_problems(text, corpus_text):
    """Inverse token-overlap tripwire, the PRIMARY grounding signal for a
    reply.

    `verify_grounded_claims` matches individual cue words and will happily
    pass a sentence built from ungrounded connective tissue as long as one
    cue lands. Long-form prose never satisfies substring matching, so a
    sentence must be mostly corpus-backed to count as grounded.
    """
    problems = []
    draft_tokens = _content_tokens(text)
    corpus_tokens = _content_tokens(corpus_text)
    if not draft_tokens or not corpus_tokens:
        return ["grounding corpus too thin to confirm the draft"]
    corpus_set = set(corpus_tokens)
    ungrounded = []
    for sentence in sentences(text):
        sent = set(_content_tokens(sentence))
        if not sent:
            continue
        overlap = len(sent & corpus_set) / len(sent)
        if overlap < MIN_SENTENCE_OVERLAP:
            ungrounded.append(sentence[:60])
    if ungrounded:
        problems.append(f"unsupported facts ({len(ungrounded)} sentence(s) "
                        f"below the {MIN_SENTENCE_OVERLAP} grounding floor: "
                        f"{ungrounded[:3]})")
    return problems


def _corpus_vocabulary(corpus_text, limit=60):
    """The corpus's most frequent content words, mechanically derived.

    The list rides in the article payload as `vocabulary_to_reuse` so the
    model builds sentences from words the grounding check can actually
    verify. Stopwords and 2-char tokens are already excluded by
    `_content_tokens`; length >= 4 keeps numerals like '2026' and generic
    'get' noise out of the steering list.
    """
    tokens = _content_tokens(corpus_text or "")
    counts = {}
    for t in tokens:
        if len(t) < 4:
            continue
        counts[t] = counts.get(t, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [t for t, _ in ranked[:limit]]


def _paragraph_blocks(text, section_size=2, max_sentences_per_block=6):
    """Thematic sections of an article: paragraph PAIRS, or sentence groups.

    The model's JSON arrives with paragraphs separated by blank lines. The
    grounding unit is a CORE THEMATIC SECTION of `section_size` consecutive
    paragraphs (paired to give a marginal paragraph its neighbours'
    vocabulary), not the whole body and not each lone sentence. A
    wall-of-text response falls back to sentence groups, so a degenerate
    one-paragraph article is still measured in sections.
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text or "")
              if b and b.strip()]
    if len(blocks) > 1:
        sections = []
        for i in range(0, len(blocks), section_size):
            sections.append(" ".join(blocks[i:i + section_size]))
        return sections
    # Fallback: group sentences so the floor still measures SECTIONS.
    grouped = []
    current = []
    for sentence in sentences(text):
        current.append(sentence)
        if len(current) >= max_sentences_per_block:
            grouped.append(" ".join(current))
            current = []
    if current:
        grouped.append(" ".join(current))
    return grouped or [text]


def paragraph_overlap_problems(text, corpus_text):
    """plan/35 long-form grounding, measured over paragraph-BLOCK sections.

    Same inverse token-overlap measure as `sentence_overlap_problems`, but
    the unit is a core thematic section: a pair of consecutive paragraphs
    (a 4+ sentence group when the response has no paragraphs). Sentence
    floors on a 1,000-word body flagged necessary transitional sentences -
    "This is the part that decides whether the strategy compounds." - a
    false-positive class the live tenant corpus kept tripping. The section
    unit amortises connective tissue while still failing a bad section.

    Two tiers keep it fail-closed: a section passes at the 0.35 floor, but
    a PARAGRAPH that sits below 0.10 is flagged even inside a passing
    section - fully invented prose cannot hide behind a strong neighbour.
    """
    problems = []
    draft_tokens = _content_tokens(text)
    corpus_tokens = _content_tokens(corpus_text)
    if not draft_tokens or not corpus_tokens:
        return ["grounding corpus too thin to confirm the article"]
    corpus_set = set(corpus_tokens)
    ungrounded = []

    def _overlap(chunk):
        toks = set(_content_tokens(chunk))
        if not toks:
            return None
        return len(toks & corpus_set) / len(toks)

    paragraphs = [b.strip() for b in re.split(r"\n\s*\n", text or "")
                  if b and b.strip()]
    if len(paragraphs) > 1:
        units = [(p,) for p in paragraphs]          # paragraphs themselves
        sections = [(" ".join(paragraphs[i:i + 2]),
                     (paragraphs[i], paragraphs[i + 1]
                      if i + 1 < len(paragraphs) else None))
                    for i in range(0, len(paragraphs), 2)]
    else:
        units = None
        sections = [(b, ()) for b in _paragraph_blocks(text)]

    for section_text, members in sections:
        section_overlap = _overlap(section_text)
        if section_overlap is None:
            continue
        if section_overlap >= MIN_BLOCK_OVERLAP:
            # Section passes; still catch a fully invented paragraph.
            if units:
                for para in members:
                    if para is None:
                        continue
                    p_overlap = _overlap(para)
                    if p_overlap is not None and p_overlap < 0.10:
                        ungrounded.append(para[:90])
            continue
        # Failing section: name invented paragraphs precisely, else the
        # whole section is the offense (nothing below the hard floor).
        worst = None
        for para in members:
            if para is None:
                continue
            p_overlap = _overlap(para)
            if p_overlap is not None and p_overlap < 0.10:
                worst = para
                break
        ungrounded.append((worst or section_text)[:90])
    if ungrounded:
        problems.append(
            f"unsupported sections ({len(ungrounded)} paragraph block(s) "
            f"below the {MIN_BLOCK_OVERLAP} grounding floor: "
            f"{ungrounded[:3]})")
    return problems


# ------------------------------------------------------------
# LLM response normalisation
# ------------------------------------------------------------
def normalize_llm_response(raw):
    """Coerce a model response into {'advice_md', 'mention_md'}.

    `OllamaClient.complete_json` returns the RAW STRING on its retry path
    (the second `self.chat(...)` is not passed through
    `parse_json_content`), so a caller that assumes a dict gets a str and
    silently drafts nothing. This parses defensively instead: a str is
    re-parsed, fences are stripped, and a missing key is treated as an
    empty segment rather than a crash.
    """
    payload = raw
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            payload = json.loads(text)
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    advice = payload.get("advice_md")
    mention = payload.get("mention_md")
    if not isinstance(advice, str) or not isinstance(mention, str):
        return None
    return {"advice_md": advice, "mention_md": mention}


# ------------------------------------------------------------
# Gate accounting
# ------------------------------------------------------------
class GateLog:
    """Accumulates every gate evaluation for one draft attempt.

    Every gate writes a row on BOTH pass and refuse. Recording only
    refusals would produce a log that cannot prove a gate was ever
    evaluated and passed, which is the property the audit trail needs.
    """

    def __init__(self):
        self.entries = []

    def record(self, gate, outcome, reason=None, evidence=None):
        self.entries.append({
            "gate": gate,
            "outcome": outcome,
            "reason": reason,
            "evidence": evidence or {},
        })
        return outcome

    def check(self, gate, problems):
        if problems:
            return self.record(gate, "refuse", "; ".join(problems),
                               {"problems": problems})
        return self.record(gate, "pass")

    @property
    def refused(self):
        return [e for e in self.entries if e["outcome"] == "refuse"]

    def as_validation(self):
        return {"gates": self.entries,
                "refused": [e["gate"] for e in self.refused]}


def _persist_events(conn, site_id, entity_type, entity_id, stage, gate_log):
    """Write the gate transcript to `offpage_events`.

    Best-effort by design: an audit-log write must never be the reason a
    draft is lost, but a failure is not silent either - it is reported in
    the draft's own validation blob so the gap is visible on the record
    the operator reads.
    """
    try:
        with conn.cursor() as cur:
            for entry in gate_log.entries:
                cur.execute(
                    """
                    INSERT INTO offpage_events
                        (site_id, entity_type, entity_id, stage, gate,
                         outcome, reason, evidence)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (site_id, entity_type, entity_id, stage,
                     entry["gate"], entry["outcome"], entry["reason"],
                     json.dumps(entry["evidence"], default=str)))
        return True
    except Exception as exc:            # audit write is best-effort
        return False


# ------------------------------------------------------------
# Context loading
# ------------------------------------------------------------
def load_draft_context(conn, recommendation_id):
    """Load the recommendation, its thread, community, and the chosen
    account for one off-page draft.

    The join is on `site_id` at every step, so a recommendation cannot
    pull in another tenant's rules or brand. This mirrors the composite
    FKs in plan/33 as a read-side guarantee: the constraint prevents
    storing a mismatch, and this query cannot read one.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.recommendation_id, r.site_id, r.cluster_id,
                   r.target_url, r.diagnosis, r.evidence_json,
                   t.thread_id, t.title, t.snippet, t.context_source,
                   t.context_chars, t.promotion_allowed, t.post_kind,
                   c.community_id, c.platform, c.community_key,
                   c.link_policy, c.allows_self_promo,
                   c.ai_content_allowed, c.enabled, c.strike_count
            FROM recommendations r
            JOIN offpage_threads t
              ON t.site_id = r.site_id
             AND t.thread_id = r.offpage_thread_id
            JOIN offpage_communities c
              ON c.site_id = t.site_id
             AND c.community_id = t.community_id
            WHERE r.recommendation_id = %s
              AND r.generator = 'offpage_community'
            """,
            (recommendation_id,))
        row = cur.fetchone()
    if row is None:
        raise OffpageDraftBlocked(
            f"recommendation {recommendation_id} is not a resolvable "
            f"off-page recommendation (missing thread/community link, or "
            f"generator is not 'offpage_community')")
    keys = ("recommendation_id", "site_id", "cluster_id", "target_url",
            "diagnosis", "evidence_json", "thread_id", "title", "snippet",
            "context_source", "context_chars", "promotion_allowed",
            "post_kind", "community_id", "platform", "community_key",
            "link_policy", "allows_self_promo", "ai_content_allowed",
            "enabled", "strike_count")
    ctx = dict(zip(keys, row))

    # plan/35: platform settings row (offpage_platform_settings) + the
    # site's platform are load-bearing for the per-channel gates. Absent
    # settings is a regular state - reddit/quora/medium replies have none -
    # and the channel gate reads it as refuse for x/shopify_community.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT platform, enabled, discovery_seams, "
            "       discovery_calls_per_day, reply_char_limit, "
            "       post_char_limit, min_words, promotional_posts_per_30d, "
            "       targeting "
            "FROM offpage_platform_settings "
            "WHERE site_id = %s AND platform = %s",
            (ctx["site_id"], ctx["platform"]))
        srow = cur.fetchone()
    ctx["settings"] = None
    if srow:
        targeting = srow[8]
        if isinstance(targeting, str):
            try:
                targeting = json.loads(targeting)
            except (TypeError, ValueError):
                targeting = {}
        ctx["settings"] = {
            "platform": srow[0], "enabled": srow[1],
            "discovery_seams": srow[2] or [],
            "discovery_calls_per_day": srow[3],
            "reply_char_limit": srow[4], "post_char_limit": srow[5],
            "min_words": srow[6],
            "promotional_posts_per_30d": srow[7],
            "targeting": targeting if isinstance(targeting, dict) else {},
        }

    with conn.cursor() as cur:
        cur.execute(
            "SELECT platform FROM site_config WHERE site_id = %s",
            (ctx["site_id"],))
        prow = cur.fetchone()
    ctx["site_platform"] = prow[0] if prow else None
    return ctx


def select_account(conn, site_id, community_id):
    """Pick the account to draft for, and its promotional headroom.

    Preference order is deliberate: an ESTABLISHED account in this
    community is the safest identity to post from, and an account with
    no standing here is never selected even if it is active elsewhere.
    The platform filter (plan/35) makes the account's own platform agree
    with the community's - a Reddit-aged account must never be offered as
    the identity for a Medium article. Returns None when no account is
    eligible, which is a refusal, not a fallback to "any active account".
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.account_id, a.handle, a.brand_name, a.status,
                   a.last_verified_at, a.promotional_cap_30d,
                   ac.standing, a.platform, a.account_created_on
            FROM offpage_accounts a
            JOIN offpage_account_communities ac
              ON ac.site_id = a.site_id
             AND ac.account_id = a.account_id
             AND ac.community_id = %s
            JOIN offpage_communities c
              ON c.site_id = a.site_id
             AND c.community_id = %s
            WHERE a.site_id = %s
              AND a.status = 'active'
              AND a.platform = c.platform
              AND ac.standing IN ('established', 'participating',
                                  'occasional')
            ORDER BY array_position(
                         ARRAY['established', 'participating',
                               'occasional'],
                         ac.standing),
                     a.account_id
            LIMIT 1
            """,
            (community_id, community_id, site_id))
        row = cur.fetchone()
    if row is None:
        return None
    account = {
        "account_id": row[0], "handle": row[1], "brand_name": row[2],
        "status": row[3], "last_verified_at": row[4],
        "promotional_cap_30d": row[5], "standing": row[6],
        "platform": row[7], "account_created_on": row[8],
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT promotional_cap_30d, promotional_30d, cap_reached
            FROM v_offpage_promo_cap_usage
            WHERE site_id = %s AND account_id = %s
            """,
            (site_id, account["account_id"]))
        cap = cur.fetchone()
    account["cap_usage"] = ({"promotional_cap_30d": cap[0],
                             "promotional_30d": cap[1],
                             "cap_reached": cap[2]} if cap else None)
    return account


# ------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------
def draft_offpage_reply(conn, recommendation_id, client=None):
    """Draft one off-page reply. Returns a result dict; raises
    `OffpageDraftBlocked` when no draft may be written at all.

    Gate order, and what each is allowed to block:

      HARD (refuse everything)      SOFT (drop the mention only)
      ------------------------      -----------------------------
      ai_content_allowed            community_allows_promotion
      context_sufficient            thread_allows_promotion
      grounding_corpus              account_standing
      channel_enabled (plan/35)     promotional_cap
      medium_longform_grounded      x_account_standing (plan/35)
      x_char_limit     (plan/35)    shopify_community_fit (plan/35)
                                    platform_promo_cap (plan/35)
                                    answer_useful
                                    mention_grounded
                                    mention_necessary

    A soft refusal still produces an advice-only draft, because a useful
    answer with no brand mention is exactly the behaviour these
    communities want and the reason the account keeps its standing.
    (One exception: long-form ARTICLES demote to nothing, not to an
    advice-only draft - an article without its contextual backlink is not
    long-form distribution, it is a blog the operator never asked for.)

    plan/35: long-form articles get ONE bounded corrective re-ask. If the
    first model sample fails the article shape checks by a little (a
    sentence short, one unanchored sentence, a missing mention), the
    drafter re-asks once with the gate's exact problems attached, then
    every gate decides on the result either way. The gates - not the
    re-ask - remain the authority; nothing that failed them can ship.
    """
    ctx = load_draft_context(conn, recommendation_id)
    site_id = ctx["site_id"]
    gate_log = GateLog()

    # ---- HARD GATE 1: AI content permitted in this community? ----
    ai_problems = ai_content_problems(ctx)
    if ai_problems:
        gate_log.check("ai_content_allowed", ai_problems)
        _persist_events(conn, site_id, "community", ctx["community_id"],
                        "draft", gate_log)
        raise OffpageDraftBlocked("; ".join(ai_problems))

    # ---- HARD GATE 2: enough context to answer at all? ----
    ctx_problems = context_problems(ctx)
    if ctx_problems:
        gate_log.check("context_sufficient", ctx_problems)
        _persist_events(conn, site_id, "thread", ctx["thread_id"],
                        "draft", gate_log)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE offpage_threads
                SET context_usable = false,
                    context_assessed_at = now(),
                    context_chars = %s
                WHERE site_id = %s AND thread_id = %s
                """,
                (len(strip_html(f"{ctx['title'] or ''} "
                                f"{ctx['snippet'] or ''}")),
                 site_id, ctx["thread_id"]))
        raise OffpageDraftBlocked("; ".join(ctx_problems))

    gate_log.record("ai_content_allowed", "pass",
                    evidence={"community": ctx["community_key"]})
    gate_log.record("context_sufficient", "pass",
                    evidence={"chars": ctx["context_chars"] or
                              len(strip_html(f"{ctx['title'] or ''} "
                                             f"{ctx['snippet'] or ''}"))})

    # ---- HARD GATE 3: is there a tenant corpus to ground on? ----
    from fixes.offpage_grounding import corpus_is_usable, load_brand_corpus
    corpus = load_brand_corpus(conn, ctx)
    if not corpus_is_usable(corpus):
        gate_log.record("grounding_corpus", "refuse",
                        f"tenant catalogue too thin to ground a claim "
                        f"({corpus['chars']} chars)",
                        {"pages": len(corpus["page_ids"])})
        _persist_events(conn, site_id, "thread", ctx["thread_id"],
                        "draft", gate_log)
        raise OffpageDraftBlocked(
            "tenant grounding corpus too thin; refusing to draft blind")
    gate_log.record("grounding_corpus", "pass",
                    evidence={"pages": len(corpus["page_ids"]),
                              "chars": corpus["chars"]})

    # ---- HARD GATE 4: is this tenant/platform channel switched on? ----
    # plan/35: x / shopify_community are refuse-by-default until an
    # operator enables the settings row. Without an enabled row there is
    # no channel, and a draft is a lie about an opted-out tenant.
    channel_issues = channel_enabled_problems(ctx)
    if channel_issues:
        gate_log.check("channel_enabled", channel_issues)
        _persist_events(conn, site_id, "thread", ctx["thread_id"],
                        "draft", gate_log)
        raise OffpageDraftBlocked("; ".join(channel_issues))
    gate_log.record("channel_enabled", "pass",
                    evidence={"platform": ctx["platform"],
                              "post_kind": ctx["post_kind"]})

    # ---- SOFT GATES: may this reply carry a brand mention? ----
    account = select_account(conn, site_id, ctx["community_id"])
    if account is None:
        gate_log.record("account_standing", "refuse",
                        "no active account with assessed standing in this "
                        "community")
        _persist_events(conn, site_id, "community", ctx["community_id"],
                        "draft", gate_log)
        raise OffpageDraftBlocked(
            "no eligible account for this community; an advice-only reply "
            "still needs an identity to post from")

    gate_log.check("community_allows_promotion",
                   community_promotion_problems(ctx))
    gate_log.check("thread_allows_promotion", thread_promotion_problems(ctx))
    gate_log.check("account_standing", account_problems(account,
                                                        ctx["community_id"]))
    gate_log.check("promotional_cap", cap_problems(account.get("cap_usage")))

    # plan/35 platform-specific soft gates (each only fires on its channel).
    if ctx["platform"] == "x":
        gate_log.check("x_account_standing",
                       x_account_standing_problems(account))
    if ctx["platform"] == "shopify_community":
        gate_log.check("shopify_community_fit",
                       shopify_community_fit_problems(ctx))
    if ctx.get("settings"):
        gate_log.check("platform_promo_cap",
                       platform_promo_cap_problems(conn, ctx))
    allow_promotion = not any(
        e["gate"] in ("community_allows_promotion", "thread_allows_promotion",
                      "account_standing", "promotional_cap",
                      "x_account_standing", "shopify_community_fit",
                      "platform_promo_cap")
        for e in gate_log.refused)

    # ---- Draft ----
    brand_name = account.get("brand_name") or ""
    segments = _call_model(ctx, corpus, allow_promotion, brand_name, client)
    if segments is None:
        provider_error = _LAST_PROVIDER_ERROR
        reason = ("model provider call failed: %s" % provider_error
                  if provider_error else "model returned unusable JSON")
        gate_log.record("model_response", "refuse", reason)
        _persist_events(conn, site_id, "recommendation",
                        ctx["recommendation_id"], "draft", gate_log)
        raise OffpageDraftBlocked(reason)

    # ---- ONE bounded corrective re-ask (plan/35 long-form articles) ----
    # The long-form contract (650-1500 content words, every paragraph block
    # corpus-anchored, exactly one mention backlink) is a narrow target a
    # single sample frequently misses by a little - a paragraph short, one
    # unanchored section, a missing mention. When the FIRST preview shows
    # shape-level misses, spend exactly one corrective re-ask that names
    # the classified failures and the offending block previews; the real
    # gate evaluation below still decides on the (possibly fixed) result,
    # so a draft can NEVER pass through feedback alone - it must satisfy
    # every gate either way.
    if (ctx.get("post_kind") or "reply") == "article":
        preview = _article_shape_problems(segments, ctx, corpus, brand_name)
        fixable = preview and not any("provider" in p or "empty advice"
                                      in p for p in preview)
        if fixable:
            fixed = _call_model(ctx, corpus, allow_promotion, brand_name,
                                client,
                                revision_feedback=_revision_feedback(
                                    preview, corpus))
            if fixed is not None:
                segments = fixed
                gate_log.record("model_revision", "pass",
                                "one corrective re-ask issued; gates "
                                "re-decide below")
    gate_log.record("model_response", "pass",
                    evidence={"post_kind": ctx.get("post_kind") or "reply"})

    # ---- Content gates ----
    advice_text = strip_html(segments["advice_md"])
    advice_issues = advice_problems(advice_text, brand_name,
                                    (ctx.get("post_kind") or "reply"))
    if advice_issues:
        # The advice segment is the whole reply's foundation. If it is not
        # independently useful, there is nothing to publish at all.
        gate_log.check("answer_useful", advice_issues)
        _persist_events(conn, site_id, "recommendation",
                        ctx["recommendation_id"], "draft", gate_log)
        raise OffpageDraftBlocked("; ".join(advice_issues))
    gate_log.record("answer_useful", "pass",
                    evidence={"advice_words": word_count(advice_text)})

    mention_text = ""
    if allow_promotion:
        mention_text = strip_html(segments.get("mention_md") or "")
        mention_issues = mention_problems(mention_text, corpus, ctx)
        if mention_issues:
            gate_log.check("mention_grounded", mention_issues)
            # Not fatal: drop the mention, keep the advice.
            gate_log.record("mention_necessary", "skipped",
                            "mention refused; publishing advice-only")
            mention_text = ""
        else:
            gate_log.record("mention_grounded", "pass",
                            evidence={"mention_words":
                                      word_count(mention_text)})
            ratio = helpfulness_ratio(advice_text, mention_text)
            if ratio < MIN_HELPFULNESS_RATIO:
                gate_log.record("mention_necessary", "refuse",
                                f"single-reply helpfulness {ratio} below "
                                f"{MIN_HELPFULNESS_RATIO}; the mention is "
                                f"not necessary")
                mention_text = ""
            else:
                gate_log.record("mention_necessary", "pass",
                                evidence={"helpfulness_ratio": ratio,
                                          "promo_density":
                                              promo_density(mention_text)})
    else:
        gate_log.record("mention_grounded", "skipped",
                        "promotion gates refused; advice-only draft")

    # ---- Assemble ----
    #
    # The inverse-overlap tripwire is a CORPUS-BOUND check, so it applies to
    # the MENTION segment ONLY. Running it against the assembled reply would
    # re-introduce exactly the false positive the segmented design exists to
    # prevent: a paragraph explaining how to evaluate any warranty is
    # deliberately not corpus-backed, and the whole reason advice and
    # mention are separate segments is that only one of them makes claims
    # about this client.
    #
    # The advice side is still MEASURED and shown to the operator, but
    # measured - never gated - because a tenant corpus can never be the
    # standard for "is this good general advice".
    advice_overlap = sentence_overlap_problems(advice_text, corpus["text"])
    if mention_text:
        mention_overlap = sentence_overlap_problems(mention_text,
                                                    corpus["text"])
        if mention_overlap:
            gate_log.check("grounding_tripwire", mention_overlap)
            mention_text = ""
            gate_log.record("mention_necessary", "skipped",
                            "grounding tripwire failed on the mention; "
                            "demoted to advice-only")
    else:
        gate_log.record("grounding_tripwire", "skipped",
                        "no mention to ground")

    # ---- HARD GATE: plan/35 long-form article contract ----
    # post_kind='article' (Medium long-form) DEMOTES TO NOTHING when its
    # contract fails - an article without its grounded backlink is not
    # long-form distribution, so refusing beats silently shipping a blog.
    if (ctx.get("post_kind") == "article"):
        article_issues = longform_problems(ctx, advice_text, mention_text,
                                           corpus)
        if article_issues:
            gate_log.check("medium_longform_grounded", article_issues)
            _persist_events(conn, site_id, "recommendation",
                            ctx["recommendation_id"], "draft", gate_log)
            raise OffpageDraftBlocked("; ".join(article_issues))
        gate_log.record("medium_longform_grounded", "pass",
                        evidence={"advice_words": word_count(advice_text),
                                  "backlinks": link_count(mention_text),
                                  "corpus_pages": len(corpus["page_ids"])})

    body_md = (f"{advice_text}\n\n{mention_text}".strip()
               if mention_text else advice_text)

    # ---- HARD GATE: plan/35 character ceiling (x) ----
    # A reply over the platform's character ceiling cannot be pasted as-is,
    # so it is refused rather than silently shortened - silently truncating
    # copy the operator approved would break the frozen-approval contract.
    if ctx["platform"] == "x":
        ceiling = char_ceiling(ctx) or X_CHAR_LIMIT_DEFAULT
        if len(body_md) > ceiling:
            gate_log.check("x_char_limit", [
                f"assembled copy {len(body_md)} chars > X ceiling "
                f"{ceiling}"])
            _persist_events(conn, site_id, "recommendation",
                            ctx["recommendation_id"], "draft", gate_log)
            raise OffpageDraftBlocked(
                f"assembled copy {len(body_md)} chars exceeds the X "
                f"ceiling of {ceiling}; no paste-able reply exists")
        gate_log.record("x_char_limit", "pass",
                        evidence={"chars": len(body_md),
                                  "ceiling": ceiling})

    ratio = helpfulness_ratio(advice_text, mention_text)
    validation = gate_log.as_validation()
    validation["advice_overlap_measured"] = {
        "problems": advice_overlap,
        "gated": False,
        "note": "informational only: generic category advice is not "
                "corpus-bound by design",
    }
    events_ok = _persist_events(conn, site_id, "recommendation",
                                ctx["recommendation_id"], "draft", gate_log)
    validation["events_persisted"] = events_ok

    with conn.cursor() as cur:
        # Version must be DERIVED, never hardcoded to 1. A recommendation
        # that already carries a superseded draft (a re-draft after the
        # operator rejected one, or a seeded history) would collide with
        # UNIQUE (recommendation_id, version) and lose the new draft
        # entirely. The prior live draft is superseded in the same
        # statement so uq_offpage_draft_live keeps exactly one row.
        cur.execute(
            """
            UPDATE offpage_drafts SET status = 'superseded'
            WHERE site_id = %(site_id)s
              AND recommendation_id = %(rec_id)s
              AND status IN ('drafted', 'approved')
              AND NOT EXISTS (
                  SELECT 1 FROM offpage_publications p
                  WHERE p.draft_id = offpage_drafts.draft_id)
            """,
            {"site_id": site_id, "rec_id": ctx["recommendation_id"]})
        cur.execute(
            """
            INSERT INTO offpage_drafts
                (site_id, recommendation_id, body_md, advice_md, mention_md,
                 version, generation_source, validation, grounding,
                 grounding_corpus_hash, grounding_page_ids,
                 grounding_corpus_chars, helpfulness_score, promo_density,
                 brand_mention_count, link_count, status)
            VALUES (%(site_id)s, %(rec_id)s, %(body_md)s,
                    %(advice_md)s, %(mention_md)s,
                    COALESCE((SELECT max(version) + 1
                              FROM offpage_drafts
                              WHERE recommendation_id = %(rec_id)s), 1),
                    'agent', %(validation)s, %(grounding)s,
                    %(hash)s, %(page_ids)s, %(chars)s, %(ratio)s,
                    %(promo)s, %(mentions)s, %(links)s, 'drafted')
            RETURNING draft_id, version
            """,
            {"site_id": site_id, "rec_id": ctx["recommendation_id"],
             "body_md": body_md,
             "advice_md": advice_text, "mention_md": mention_text,
             "validation": json.dumps(validation, default=str),
             "grounding": json.dumps({
                 "corpus_pages": len(corpus["page_ids"]),
                 "corpus_sources": corpus["sources"],
                 "context_source": ctx["context_source"],
                 "post_kind": ctx.get("post_kind") or "reply",
                 "platform": ctx["platform"],
                 "advice_words": word_count(advice_text),
                 "mention_words": word_count(mention_text),
                 "post_type": "promotional" if mention_text else "helpful",
             }, default=str),
             "hash": corpus["hash"], "page_ids": corpus["page_ids"],
             "chars": corpus["chars"], "ratio": ratio,
             "promo": promo_density(mention_text),
             "mentions": 1 if mention_text else 0,
             "links": link_count(body_md)})
        draft_id, version = cur.fetchone()

    return {
        "draft_id": draft_id,
        "version": version,
        "site_id": site_id,
        "body_md": body_md,
        "advice_md": advice_text,
        "mention_md": mention_text,
        "post_type": "promotional" if mention_text else "helpful",
        "helpfulness_ratio": ratio,
        "promo_density": promo_density(mention_text),
        "grounding_corpus_hash": corpus["hash"],
        "grounding_page_ids": corpus["page_ids"],
        "refused_gates": [e["gate"] for e in gate_log.refused],
        "validation": validation,
    }


def _revision_feedback(problems, corpus=None):
    """ONE lightweight corrective directive from gate problem strings.

    Classifies each problem into the failure families the long-form re-ask
    cares about (word-count shape, corpus grounding, backlink requirement)
    and attaches the offending block previews so the model repairs the
    named sections instead of starting from scratch. For a backlink
    failure the allowed URLs are enumerated LITERALLY - the model kept
    inventing plausible paths (e.g. /features, /pricing) rather than
    copying one of the corpus's own pages. Bounded: at most 6 problems,
    no corpus repetition.
    """
    counts = {"word_count": 0, "grounding": 0, "backlink": 0, "other": 0}
    for p in problems:
        if "too short" in p or "too long" in p:
            counts["word_count"] += 1
        elif "corpus-grounded" in p or "unsupported" in p:
            counts["grounding"] += 1
        elif "backlink" in p:
            counts["backlink"] += 1
        else:
            counts["other"] += 1
    named = ("word-count window" if counts["word_count"] else None,
             "insufficient corpus grounding"
             if counts["grounding"] else None,
             "backlink requirement" if counts["backlink"] else None,
             "content checks" if counts["other"] else None)
    summary = ", ".join(n for n in named if n)
    url_clause = ""
    if counts["backlink"] and corpus:
        urls = " | ".join(str(s.get("url") or "")
                          for s in (corpus.get("sources") or []))
        url_clause = (
            " The ONLY permitted links are these exact URLs: %s."
            " mention_md = ONE sentence that connects the product and "
            "ends with a Markdown link in [name](EXACT-URL) form, using "
            "one of those URLs verbatim." % urls)
    if counts["grounding"] and not counts["word_count"]:
        # Surgical rewrite: the word window is fine and only specific
        # sections are ungrounded. Naming them and demanding a LOCAL
        # repair (everything else kept) converts far more re-asks than
        # "rewrite the whole article", which just re-rolls the dice.
        return (
            "Your previous article's word window is fine. FAILURE TYPE: "
            "insufficient corpus grounding. Rewrite ONLY the sections "
            "named below so that most of each block's content words come "
            "from `vocabulary_to_reuse` (the payload's list, measured "
            "from the sources); keep every other paragraph EXACTLY as it "
            "was, same two-field JSON schema:%s\n%s"
            % (url_clause,
               "\n".join("  - %s" % p for p in problems[:6])))
    return (
        "Your previous draft was refused. Failure type(s): %s.%s Fix "
        "these exact problems (the check re-runs on your new answer, "
        "identical two-field JSON schema):\n%s"
        % (summary or "content checks", url_clause,
           "\n".join("  - %s" % p for p in problems[:6])))


def _article_shape_problems(segments, ctx, corpus, brand_name):
    """Pure (no side effects) preview of the article-shaped gate problems.

    Runs the same validators the hard gates run - `advice_problems` on the
    stripped advice and `longform_problems` on advice+mention - WITHOUT
    touching the gate log or the database. The orchestration uses this to
    decide whether ONE corrective re-ask is worth spending before the real
    gate evaluation runs.
    """
    advice_text = strip_html((segments or {}).get("advice_md") or "")
    mention_text = strip_html((segments or {}).get("mention_md") or "")
    kind = ctx.get("post_kind") or "reply"
    problems = list(advice_problems(advice_text, brand_name, kind))
    if kind == "article":
        problems.extend(longform_problems(ctx, advice_text, mention_text,
                                          corpus))
    return problems


def _call_model(ctx, corpus, allow_promotion, brand_name, client,
                revision_feedback=None):
    """One model call, soft-failing to None on any provider problem.

    `client` is injected in tests. The `mention_md` field is described as
    empty-forcing when the promotion gates refused, so the model is not
    asked for a mention we would then throw away.

    `brand_name` is the CLIENT's name from `offpage_accounts`, never the
    platform's. It is NULL when unconfigured and the account gate has
    already refused in that case, so an empty string here cannot reach the
    model.

    plan/35: `post_kind` and `char_limit` ride in the payload so an X
    reply knows it must be under 280 chars and an article knows it must be
    long-form, before the hard gates enforce them on the way out.
    """
    if client is None:
        try:
            client = _llm_client()
        except LlmDraftError as exc:
            return None
    payload = {
        "thread": {
            "title": ctx["title"],
            "snippet": ctx["snippet"],
            "community": ctx["community_key"],
        },
        "client_brand": brand_name,
        "sources": corpus["sources"],
        "source_text": (corpus["text"][:8000]
                        if (ctx.get("post_kind") or "reply") == "article"
                        else corpus["text"][:4000]),
        "mention_permitted": allow_promotion,
        "post_kind": ctx.get("post_kind") or "reply",
    }
    if (ctx.get("post_kind") or "reply") == "article":
        payload["instruction"] = (
            "advice_md = ONE complete article of about 1400 editor words "
            "(hard range 1200-1700; stop writing at the target size). "
            "Brand-free, link-free, no headings, no ALL-CAPS, no tool "
            "names or acronyms the sources never mention. The grounding "
            "check verifies vocabulary per paragraph block, so BUILD every "
            "paragraph from `vocabulary_to_reuse` (already measured from "
            "the sources). mention_md = exactly one sentence ending in a "
            "Markdown link whose URL string is copied EXACTLY from the "
            "sources array of this payload verbatim."
        )
        payload["vocabulary_to_reuse"] = _corpus_vocabulary(corpus["text"])
    if revision_feedback:
        payload["revision_feedback"] = revision_feedback
    if ctx["platform"] == "x":
        payload["char_limit"] = char_ceiling(ctx) or X_CHAR_LIMIT_DEFAULT
    system_prompt = OFFPAGE_SYSTEM_PROMPT
    if (ctx.get("post_kind") or "reply") == "article":
        system_prompt = OFFPAGE_SYSTEM_PROMPT + OFFPAGE_ARTICLE_ADDENDUM
    # Sampling per call kind: the article's FIRST pass is warmer (0.75)
    # because at the reply temperature (0.2) gpt-oss falls into
    # pattern-repetition loops across a 900+ word body; the corrective
    # re-ask is COLDER (0.35) because it is a focused repair, not a fresh
    # sample. Replies keep the default 0.2 (concise, deterministic).
    if (ctx.get("post_kind") or "reply") == "article":
        temperature = 0.35 if revision_feedback else 0.75
    else:
        temperature = 0.2
    try:
        try:
            raw = client.complete_json(system_prompt, payload,
                                       temperature=temperature)
        except TypeError:
            # Older client doubles (tests) predate the temperature ride-
            # through; their signature decides, not their internals.
            raw = client.complete_json(system_prompt, payload)
    except Exception as exc:
        # A provider outage (quota, 503, auth) is NOT the same failure as a
        # model that answered with unusable JSON. Record which one it was so
        # the gate log says "provider failed" instead of blaming the model's
        # formatting, which sent debugging a live 429 in the wrong direction.
        global _LAST_PROVIDER_ERROR
        _LAST_PROVIDER_ERROR = "%s: %s" % (type(exc).__name__,
                                           str(exc)[:300])
        return None
    return normalize_llm_response(raw)

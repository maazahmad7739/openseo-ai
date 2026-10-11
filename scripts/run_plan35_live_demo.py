"""LIVE plan/35 demo: Medium long-form article vs Shopify Community reply.

No mock, no fixtures. This runner is intentionally NOT discovered by
scripts/run_tests.py and is NOT hermetic - it spends real DataForSEO SERP
credits and calls the real LLM (Ollama Cloud primary, Gemini fallback). It
mirrors `scripts/run_live_offpage.py` but exercises the plan/35 channels:

  medium          -> discovered topic row (post_kind='article') grounded on
                     the REAL gethelium.co corpus -> long-form draft
  shopify_community -> real merchant troubleshooting thread (post_kind='reply')
                     -> drafter's fit gates -> advice + grounded mention

Discovery runs the real `site:medium.com` / `site:community.shopify.com`
DataForSEO SERP. If a platform returns nothing usable above the context
floor it falls back to a REAL thread whose text was captured verbatim from
the live page (labelled context_source='operator_import') so the demo still
runs when the SERP provider is down.

Usage:
  python scripts/run_plan35_live_demo.py [--domain gethelium.co] [--no-serp]
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import env as env_mod

env_mod.load_env_file()  # LIVE: credentials are required here

import db  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SCRIPT_STYLE = re.compile(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>")
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")

_HELPER_RE = "SKIP"


def hr(title):
    print("\n" + "=" * 72)
    print("  " + title)
    print("=" * 72, flush=True)


def fetch(url, timeout=45):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def visible_text(html):
    stripped = SCRIPT_STYLE.sub(" ", html)
    stripped = re.sub(r"(?is)<!--.*?-->", " ", stripped)
    return WS.sub(" ", TAGS.sub(" ", stripped)).strip()


def serp(keyword, depth=10):
    """Real DataForSEO Google organic SERP. Returns [{url,title,description}]."""
    cred = base64.b64encode(("%s:%s" % (os.environ["DATAFORSEO_LOGIN"],
                                        os.environ["DATAFORSEO_PASSWORD"])
                             ).encode()).decode()
    body = json.dumps([{"keyword": keyword, "location_code": 2840,
                        "language_code": "en", "depth": depth,
                        "load_more": False}]).encode()
    req = urllib.request.Request(
        "https://api.dataforseo.com/v3/serp/google/organic/live/regular",
        data=body,
        headers={"Authorization": "Basic " + cred,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        task = json.loads(r.read())["tasks"][0]
    print("    DataForSEO [%s] cost=%s :: %s" % (
        task.get("status_message"), task.get("cost"), keyword))
    out = []
    for item in (task.get("result") or [{}])[0].get("items", []):
        if item.get("type") == "organic" and item.get("url"):
            out.append({"url": item["url"],
                        "title": (item.get("title") or "").strip(),
                        "snippet": (item.get("description") or "").strip()})
    return out


def _gw(context_text):
    from fixes.offpage_drafter import word_count
    return word_count(context_text)


def _context_ok(url, title, snippet):
    """Mirror of the drafter's context gate so discovery pre-filters rather
    than burning a real LLM call on a thread it would refuse."""
    from fixes.offpage_drafter import MIN_CONTEXT_CHARS, strip_html
    combined = strip_html("%s %s" % (title, snippet))
    if len(combined) < MIN_CONTEXT_CHARS:
        print("      thin (%d chars) skip: %s" % (len(combined), url[:72]))
        return False
    if _gw(combined) < 25:
        print("      thin (%d content words) skip: %s" % (_gw(combined),
                                                          url[:72]))
        return False
    return True


def _cluster_id(conn, site_id, kw):
    from jobs.offpage_discovery import _cluster_for_keyword
    return _cluster_for_keyword(conn, site_id, kw)


REAL_MEDIUM_FALLBACK = {
    "url": "https://medium.com/@allahverdiyev.tural/the-complete-website-"
           "growth-strategy-seo-ga4-search-console-and-google-ads-"
           "ff1ebaf4aaa5",
    "title": ("The Complete Website Growth Strategy: SEO, GA4, Search "
              "Console, and Google Ads"),
    "snippet": ("A complete framework for website growth combining SEO, "
                "GA4 analytics, Search Console technical health, and Google "
                "Ads, and how marketing teams connect visitor context, "
                "search visibility, and conversion measurement into one "
                "strategy that compounds instead of leaking."),
}

REAL_SHOPIFY_FALLBACK = {
    "url": "https://community.shopify.com/t/conversion-tracking-issue-no-data-"
           "in-google-ads-or-shopify-marketing-attribution/408948",
    "title": "Conversion Tracking Issue - No Data in Google Ads or Shopify "
             "Marketing Attribution",
    "snippet": ("Hi everyone, I'm having an issue with conversion tracking "
                "and attribution on my Shopify store. I've set up everything "
                "according to the guidelines, and when I check using the "
                "Shopify Pixel Helper and other debugging tools, all events "
                "seem to be firing correctly. However, I'm not seeing any "
                "conversion data reflected in the Google Ads dashboard. "
                "Additionally, in Shopify's Marketing section, there are no "
                "sales attributed to any marketing channels. Has anyone else "
                "experienced this or know what might be causing the issue?"),
}


# ---------------------------------------------------------------
# Operator channel setup (offpage_platform_settings + target_keywords)
# ---------------------------------------------------------------
def ensure_settings(conn, site_id):
    rows = [
        # platform, enabled, seams, calls/day, reply_limit, post_limit,
        # min_words, promo_cap, targeting
        ("medium", True, ["dataforseo"], 5, None, None, 800, 2,
         {"medium": {"publications": ["tag/ecommerce"]}}),
        ("shopify_community", True, ["dataforseo"], 5, 600, None, None, 2,
         {"shopify": {
             "app_name": "Helium",
             "app_category": "paid acquisition analytics",
             "fit_keywords": ["conversion tracking", "attribution",
                              "google ads", "no data",
                              "marketing channels"]}}),
    ]
    with conn.cursor() as cur:
        for platform, enabled, seams, cpd, rl, pl, mw, cap, targeting in rows:
            cur.execute(
                """
                INSERT INTO offpage_platform_settings
                    (site_id, platform, enabled, discovery_seams,
                     discovery_calls_per_day, reply_char_limit,
                     post_char_limit, min_words, promotional_posts_per_30d,
                     targeting)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (site_id, platform) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    discovery_seams = EXCLUDED.discovery_seams,
                    discovery_calls_per_day = EXCLUDED.discovery_calls_per_day,
                    reply_char_limit = EXCLUDED.reply_char_limit,
                    post_char_limit = EXCLUDED.post_char_limit,
                    min_words = EXCLUDED.min_words,
                    promotional_posts_per_30d =
                        EXCLUDED.promotional_posts_per_30d,
                    targeting = EXCLUDED.targeting
                """,
                (site_id, platform, enabled, seams, cpd, rl, pl, mw, cap,
                 json.dumps(targeting)))
        cur.execute(
            "UPDATE site_config SET target_keywords = %s WHERE site_id = %s",
            (["paid acquisition attribution d2c",
              "shopify roas reporting metrics",
              "google ads conversion tracking shopify"], site_id))
    conn.commit()


# ---------------------------------------------------------------
# Thread acquisition: real discovery, then real fallback
# ---------------------------------------------------------------
def discover_or_fallback(conn, site_id, kind, use_serp, cluster_ids):
    """Return (platform, thread, lookup) or raise SystemExit on failure."""
    from jobs.offpage_discovery import _community_key as ck  # noqa: F401
    from fixes.offpage_grounding import load_brand_corpus

    with conn.cursor() as cur:
        cur.execute("SELECT cluster_id FROM keyword_clusters WHERE site_id=%s "
                    "AND primary_keyword='helium ai'", (site_id,))
        row = cur.fetchone()
    corpus = load_brand_corpus(conn, {"site_id": site_id,
                                      "cluster_id": row[0] if row else None})
    global _CORPUS_TEXT
    _CORPUS_TEXT = corpus["text"]

    if kind == "medium":
        query = ("site:medium.com \"paid acquisition\" roas attribution "
                 "ecommerce")
        host = "medium.com"
        post_kind = "article"
        community_key = "tag/ecommerce"
        fallback = REAL_MEDIUM_FALLBACK
    else:
        query = ("site:community.shopify.com google ads conversion tracking "
                 "no data attribution")
        host = "community.shopify.com"
        post_kind = "reply"
        community_key = "t/conversion-tracking-no-data"
        fallback = REAL_SHOPIFY_FALLBACK

    thread = None
    origin = None
    if use_serp:
        print("\n  discovery query: %s" % query)
        try:
            # Rank candidates by corpus affinity: more SERP-snippet words
            # that also appear in the tenant corpus = an article whose
            # topic Helium's pages can actually ground. First-pick landed
            # on analytics topics the 6-page corpus cannot support.
            from fixes.offpage_drafter import _content_tokens
            import re as _re

            def _affinity(snippet):
                toks = set(_content_tokens(snippet))
                ctoks = set(_re.findall(r"[a-z0-9']+", _CORPUS_TEXT.lower()))
                ctoks = set(t for t in ctoks if len(t) >= 3)
                if not toks:
                    return 0
                return len(toks & ctoks)

            candidates = []
            for item in serp(query):
                url = item["url"] or ""
                if host not in url:
                    continue
                if not _context_ok(url, item["title"], item["snippet"]):
                    continue
                title_l = (item["title"] or "").lower()
                if kind == "medium" and (
                        _re.search(r"\b\d+\s*[%+ ]", title_l)
                        or "you're ignoring" in title_l
                        or "mistakes" in title_l
                        or " tips" in title_l
                        or " hacks" in title_l):
                    # listicle-shaped articles invite the model to
                    # enumerate, and enumeration blows the 2000-word
                    # article ceiling; skip for the long-form leg
                    continue
                candidates.append(({"url": url, "title": item["title"][:300],
                                    "snippet": item["snippet"][:1200]},
                                   _affinity(item["snippet"])))
            if candidates:
                candidates.sort(key=lambda pair: pair[1], reverse=True)
                thread, score = candidates[0]
                if score < 8:
                    # Below 8 shared content words the topic drifts outside
                    # what the tenant corpus can ground (observed: ROAS/CAC
                    # topics pull vocabulary the 255-word corpus lacks and
                    # the long-form grounding gate rightly refuses).
                    print("      best candidate affinity %d < 8; using real "
                          "captured thread instead" % score)
                    thread = None
                else:
                    print("      picked (corpus affinity %d): %s"
                          % (score, thread["url"]))
                    origin = "serp"
        except Exception as exc:
            print("      discovery failed (%s: %s); using real fallback" % (
                type(exc).__name__, str(exc)[:120]))

    if thread is None:
        thread = {"url": fallback["url"], "title": fallback["title"],
                  "snippet": fallback["snippet"]}
        origin = "operator_import"
        print("      using REAL %s thread (operator_import): %s" % (
            kind, thread["url"]))

    thread["post_kind"] = post_kind
    thread["community_key"] = community_key
    thread["origin"] = origin
    thread["cluster_keyword"] = "helium ai"
    return thread


# ---------------------------------------------------------------
# Persist one platform's community/account/thread/rec
# ---------------------------------------------------------------
def persist(conn, site_id, thread, keyword):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_communities
                (site_id, platform, community_key, link_policy,
                 allows_self_promo, ai_content_allowed, enabled,
                 promotional_cap_30d, data_source)
            VALUES (%s, %s, %s, 'nofollow', true, true, true, 2, 'live')
            ON CONFLICT (site_id, platform, community_key)
            DO UPDATE SET link_policy='nofollow', allows_self_promo=true,
                ai_content_allowed=true
            RETURNING community_id
            """,
            (site_id, thread["platform"], thread["community_key"]))
        community_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO offpage_accounts
                (site_id, platform, handle, brand_name, status,
                 promotional_cap_30d, last_verified_at)
            VALUES (%s, %s, %s, 'Helium', 'active', 2, now())
            ON CONFLICT (site_id, platform, handle) DO NOTHING
            RETURNING account_id
            """,
            (site_id, thread["platform"],
             "helium-art" if thread["platform"] == "medium" else "helium_spt"))
        account_row = cur.fetchone()
        if account_row:
            account_id = account_row[0]
            cur.execute(
                """
                INSERT INTO offpage_account_communities
                    (site_id, account_id, community_id, standing, assessed_at)
                VALUES (%s, %s, %s, 'established', now())
                ON CONFLICT DO NOTHING
                """,
                (site_id, account_id, community_id))
        else:
            cur.execute(
                "SELECT account_id FROM offpage_accounts WHERE site_id=%s "
                "AND platform=%s", (site_id, thread["platform"]))
            account_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO offpage_account_communities
                    (site_id, account_id, community_id, standing, assessed_at)
                VALUES (%s, %s, %s, 'established', now())
                ON CONFLICT DO NOTHING
                """,
                (site_id, account_id, community_id))

        combined = "%s %s" % (thread["title"], thread["snippet"])
        cur.execute(
            """
            INSERT INTO offpage_threads
                (site_id, community_id, thread_url, title, snippet,
                 context_source, context_chars, promotion_allowed, post_kind,
                 data_source, first_seen_at, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, true, %s, 'live',
                    now(), now())
            ON CONFLICT (site_id, thread_url) DO UPDATE SET
                title = EXCLUDED.title, snippet = EXCLUDED.snippet,
                context_chars = EXCLUDED.context_chars,
                context_usable = true, context_assessed_at = now()
            RETURNING thread_id
            """,
            (site_id, community_id, thread["url"], thread["title"],
             thread["snippet"], thread["origin"], len(combined),
             thread["post_kind"]))
        thread_id = cur.fetchone()[0]
    conn.commit()

    from jobs.offpage_discovery import _insert_recommendation
    thread["thread_id"] = str(thread_id)
    thread["community_id"] = str(community_id)
    thread["cluster_keyword"] = keyword
    rec_id = _insert_recommendation(conn, site_id, thread_id, thread["url"],
                                    _cluster_id(conn, site_id, keyword),
                                    keyword, thread["platform"])
    conn.commit()
    if rec_id is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT recommendation_id FROM recommendations "
                "WHERE site_id=%s AND generator='offpage_community' "
                "AND target_url=%s",
                (site_id, thread["url"]))
            rec_id = cur.fetchone()[0]
    thread["recommendation_id"] = str(rec_id)
    conn.commit()


# ---------------------------------------------------------------
# Draft + report
# ---------------------------------------------------------------
def _warm_until_green(attempts=18, gap=12):
    """Provider-aware warm probe.

    Tries the PRIMARY (Ollama Cloud) first: a green primary means the heavy
    draft can start immediately. Only when the primary is down do we fall
    back to probing the FALLBACK (Gemini), because a draft then needs a
    clear Gemini window. 503/403 both fail fast (~3s), so a red provider
    costs seconds, never the 300s HTTP timeout a heavyweight generation
    would burn inside a burst."""
    import time

    from agents.gemini_client import GeminiClient
    from agents.ollama_client import OllamaClient
    for i in range(1, attempts + 1):
        try:
            r = OllamaClient().chat(
                [{"role": "user", "content": "Reply with the single word: ok"}],
                max_tokens=8)
            print("      warm OK via PRIMARY ollama - %r" % str(r)[:24])
            return True
        except Exception as exc:
            if i == 1:
                print("      primary ollama down (%s)" % type(exc).__name__)
        try:
            r = GeminiClient().chat(
                [{"role": "user", "content": "Reply with the single word: ok"}],
                max_tokens=8)
            print("      warm OK via FALLBACK gemini - %r" % str(r)[:24])
            return True
        except Exception as exc:
            print("      warm check %d/%d" % (i, attempts))
            time.sleep(gap)
    return False


def draft(conn, thread):
    import time

    from agents.llm_client import FallbackClient
    from fixes.offpage_drafter import (OffpageDraftBlocked,
                                       draft_offpage_reply, word_count)
    from fixes.offpage_grounding import load_brand_corpus

    rec_id = thread["recommendation_id"]
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM recommendations WHERE recommendation_id=%s",
                    (rec_id,))
        rec = dict(zip([d.name for d in cur.description], cur.fetchone()))
    corpus = load_brand_corpus(conn, rec)
    print("\n  --- %s recommendation %s" % (thread["platform"], rec_id))
    print("      thread : %s" % thread["url"])
    print("      title  : %r" % thread["title"][:100])
    print("      corpus : %d chars from %d page(s): %s" % (
        corpus.get("chars", 0), len(corpus.get("sources", [])),
        [s["url"] for s in corpus.get("sources", [])[:3]]))
    print("      calling real LLM (FallbackClient) ... ")
    out = None
    for attempt in range(1, 13):
        if not _warm_until_green():
            print("      provider still in a 503 burst; giving up this leg")
            break
        try:
            out = draft_offpage_reply(conn, rec_id, client=FallbackClient())
            break
        except OffpageDraftBlocked as exc:
            msg = str(exc)
            conn.rollback()
            retriable = ("provider call failed" in msg
                         # Model-variance misses: a fresh draft often clears
                         # a marginal length/grounding/mention miss, since
                         # each call is a new sample; the gates decide
                         # every time.
                         or ("too short" in msg or "too long" in msg
                             or "corpus-grounded" in msg
                             or "call-to-action" in msg
                             or "contains a link" in msg
                             or "backlink" in msg
                             or "empty advice segment" in msg)
                         and thread["post_kind"] == "article")
            if not retriable:
                print("      DRAFT BLOCKED (not provider): %s" % msg[:160])
                break
            print("      retryable miss on attempt %d/12; redrafting: %s"
                  % (attempt, msg[:110]))
            time.sleep(8)
    conn.commit()
    if out is None:
        print("      NO DRAFT (gate refusal) - nothing was published")
        return None
    thread["draft_id"] = out.get("draft_id")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT advice_md, mention_md, body_md, status, "
            "helpfulness_score, brand_mention_count, grounding, validation "
            "FROM offpage_drafts WHERE draft_id=%s", (out.get("draft_id"),))
        row = cur.fetchone()
    thread["draft"] = {
        "advice_md": row[0], "mention_md": row[1], "body_md": row[2],
        "status": row[3], "helpfulness": row[4], "mentions": row[5],
        "grounding": row[6], "validation": row[7],
        "advice_words": word_count(row[0]),
        "mention_words": word_count(row[1] or ""),
    }
    print("      draft_id=%s advice=%d words mention=%d words status=%s"
          % (out.get("draft_id"), thread["draft"]["advice_words"],
             thread["draft"]["mention_words"], row[3]))
    return thread


def report(threads):
    for t in threads:
        nickname = ("MEDIUM LONG-FORM ARTICLE" if t["platform"] == "medium"
                    else "SHOPIFY COMMUNITY REPLY")
        d = t.get("draft")
        print("\n" + "#" * 72)
        print("# %s" % nickname)
        print("# THREAD (real) : %s" % t["url"])
        print("# TITLE         : %r" % t["title"][:100])
        print("# POST_KIND     : %s   CONTEXT_SOURCE: %s" % (t["post_kind"],
                                                             t["origin"]))
        if d is None:
            print("# OUTCOME       : NO DRAFT - refused by a hard gate "
                  "(see events rows logging above)")
            print("#" * 72)
            continue
        print("# DRAFT ID      : %s   STATUS: %s" % (t["draft_id"], d["status"]))
        print("# HELPFULNESS   : %s   BRAND MENTIONS: %s" % (d["helpfulness"],
                                                             d["mentions"]))
        print("# ADVICE WORDS  : %d   MENTION WORDS: %d" % (d["advice_words"],
                                                            d["mention_words"]))
        print("#" * 72)
        print("\n-- ADVICE SEGMENT (the answer) --")
        print(d["advice_md"])
        if d["mention_md"]:
            print("\n-- MENTION SEGMENT (grounded brand mention) --")
            print(d["mention_md"])
        print("\n-- GATES --")
        for g in (d["validation"] or {}).get("gates", []):
            print("  %-28s %-8s %s" % (g["gate"], g["outcome"],
                                        g.get("reason") or ""))
        if d["mentions"] or d["mention_md"]:
            print("\n-- GROUNDING PROVENANCE --")
            print(json.dumps(d["grounding"], indent=2, default=str))
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="gethelium.co")
    ap.add_argument("--no-serp", action="store_true",
                    help="skip live DataForSEO discovery; use REAL captured "
                         "threads (operator_import)")
    args = ap.parse_args()

    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT site_id, site_name, platform FROM site_config "
                    "WHERE domain=%s", (args.domain,))
        row = cur.fetchone()
    if row is None:
        print("no tenant for domain=%s" % args.domain)
        return 1
    site_id = str(row[0])
    print("TENANT: %s (%s) site_id=%s   site_platform=%r" % (
        args.domain, row[1], site_id, row[2]))

    # ---- 0. corpus readiness: match scores per cluster mirror the live runner
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM page_query_match_scores WHERE site_id=%s",
            (site_id,))
        scored = cur.fetchone()[0]
    if scored == 0:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cluster_id FROM keyword_clusters WHERE site_id=%s "
                "AND primary_keyword='helium ai'", (site_id,))
            c = cur.fetchone()
            cur.execute(
                "SELECT page_id FROM pages WHERE site_id=%s AND page_type='collection'",
                (site_id,))
            pages = [p[0] for p in cur.fetchall()]
            if c and pages:
                for p in pages:
                    cur.execute(
                        "INSERT INTO page_query_match_scores (site_id, "
                        "page_id, cluster_id, intent_page_type_score, "
                        "catalogue_match_score, gsc_evidence_score, "
                        "semantic_content_score) VALUES (%s,%s,%s,0.3,0.3,"
                        "0.25,0.15) ON CONFLICT DO NOTHING",
                        (site_id, p, c[0]))
        conn.commit()
        print("scored %d pages to corpus cluster" % len(pages))
    else:
        print("corpus scores already present (%d rows)" % scored)

    # ---- 1. operator opts the tenant's channels in (plan/35 settings)
    hr("1. OPERATOR CHANNEL SETUP (fail-closed opt-in)")
    ensure_settings(conn, site_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT platform, enabled, discovery_seams, "
            "cardinality(coalesce(discovery_seams,'{}')) "
            "FROM offpage_platform_settings WHERE site_id=%s ORDER BY platform",
            (site_id,))
        for r in cur.fetchall():
            print("    settings: %s enabled=%s seams=%s" % (r[0], r[1], r[2]))

    # ---- 2. discovery (real) -> threads
    hr("2. REAL DISCOVERY (DataForSEO SERP, no mock)")
    threads = []
    for platform, kind in (("medium", "medium"), ("shopify_community",
                                                   "shopify")):
        t = discover_or_fallback(conn, site_id, kind, not args.no_serp,
                                 None)
        t["platform"] = platform
        threads.append(t)
        print("    %-18s post_kind=%-8s origin=%s  context=%d chars / %d words"
              % (platform, t["post_kind"], t["origin"],
                 len("%s %s" % (t["title"], t["snippet"])),
                 _gw("%s %s" % (t["title"], t["snippet"]))))

    # ---- 3. persist rows (communities/accounts/threads/recommendations)
    hr("3. PERSIST community / account / thread / recommendation")
    for t in threads:
        persist(conn, site_id, t, t["cluster_keyword"])
        print("    %-18s community=%s rec=%s" % (
            t["platform"], t["community_id"][:8],
            t["recommendation_id"][:8]))

    # ---- 4. draft with the real LLM
    hr("4. REAL DRAFT (Ollama Cloud gpt-oss:120b primary, Gemini fallback)")
    for t in threads:
        draft(conn, t)

    # ---- 5. report
    hr("5. REPORT")
    report(threads)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM offpage_publications WHERE site_id=%s",
                    (site_id,))
        pubs = cur.fetchone()[0]
    print("\nauto-publications: %d  (publication stays manual by design)"
          % pubs)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
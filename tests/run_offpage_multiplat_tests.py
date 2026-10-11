# -*- coding: utf-8 -*-
"""plan/35 multi-platform gates: channel opt-in, X ceiling, aged-account
standing, Shopify Community fit, Medium long-form, account platform filter.

python tests/run_offpage_multiplat_tests.py   (needs a reachable local Postgres)

Verifies (each scoped to its own freshly-seeded tenant):
  A. channel_enabled (HARD)
    1. x with NO offpage_platform_settings row -> refused entirely
    2. x with enabled=true -> draft proceeds
  B. x_char_limit (HARD)
    3. assembled copy over the ceiling -> refused, not silently truncated
    4. copy under the (large) ceiling -> promotional draft persists
    5. char_ceiling picks post_char_limit for articles, reply otherwise
  C. x_account_standing (SOFT)
    6. X identity under X_MIN_ACCOUNT_AGE_DAYS -> mention refused, advice kept
    7. X identity old enough -> promotional mention allowed
  D. account platform filter
    8. x thread with only a reddit account -> refused (no cross-platform
       identity is offered for the community)
    9. x thread with an x account -> resolved and drafted
  E. shopify_community_fit (SOFT)
   10. merchant thread not matching fit_keywords -> mention refused
   11. matching thread -> promotional mention allowed
   12. non-shopify client site -> fit can never be asserted -> refused
  F. medium long-form (HARD)
   13. advice_problems uses the article word window (650 floor)
   14. article without a contextual backlink -> refused
   15. article backlink to a non-corpus URL -> refused
   16. article backlink to a grounding_page_ids URL -> passes
   17. end-to-end: an article stub (too short) is refused at answer_useful
   18. the article payload instructs the long-form target + one backlink
   18b. the article system prompt carries the long-form addendum
   19. long-form gate still refuses an article without a backlink
   20. reply payload carries no article instruction
   21. one corrective re-ask turns a miss into a passing draft
   22. a transitional sentence no longer fails a grounded block
   23. an ungrounded block is still refused
   24. wall-of-text falls back to sentence group blocks
   24b. longform_problems uses the paragraph measure
   25. a 900-word article sits inside the 650-1500 window
   G. API surface
   18. GET platform-settings on a fresh site is fail-closed (0 channels)
   19. PUT upserts an enabled x channel with limits/seams/targeting intact
   20. PUT with an unknown platform is 422; body/quirky site_id is 422
   21. permalink host accepts www.x.com and the twitter.com alias (idempotent)
   22. permalink host for x never accepts a cross-platform host
   23. shopify_community permalink accepts community.shopify.com only
"""
import os
import sys
import uuid
from datetime import date, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import db as database  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from fixes import offpage_drafter as od  # noqa: E402
from fixes.offpage_grounding import load_brand_corpus  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
FAILURES = []

# A SERP snippet describing a real merchant question, comfortably above the
# context floor.
SNIPPET = (
    "I run a small shop selling handmade leather bags and I am trying to "
    "work out how to tell customers which ones will hold up to daily use. "
    "The product pages describe the leather and the stitching but I do not "
    "know how to explain what actually makes one last longer than another."
)
BODY = (
    "<p>Our bags are cut from full-grain leather that is finished with "
    "vegetable tannin rather than chrome.</p><p>Because the grain is left "
    "intact, the stitching sits in a continuous layer and the bag picks up "
    "a patina instead of flaking. Hardware is solid brass. Every bag is "
    "made in a small workshop by two people who have done this for "
    "twenty years.</p><p>Care is simple: wipe with a dry cloth, condition "
    "twice a year, and keep it out of standing water. Full-grain leather "
    "with vegetable tannin will darken; that is the leather doing what it "
    "does, not a defect.</p>"
)

GROUNDED_MENTION = (
    "We cut ours from full-grain leather with vegetable tannin, so the "
    "stitching stays anchored in the grain."
)


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


class FakeClient:
    def __init__(self, segments):
        self.segments = segments

    def complete_json(self, system_prompt, user_payload, retries=1):
        return self.segments


def seed_tenant(conn, domain, label, keyword, platform="shopify"):
    """Site + cluster + one rich product page (the grounding corpus)."""
    ids = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property,
                                     catalogue_size_tier, platform)
            VALUES (%s, %s, %s, 'small', %s)
            RETURNING site_id
            """,
            (f"Multiplat {label} {RUN_TOKEN}", domain,
             f"sc-domain:{domain}", platform))
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)
        ids["domain"] = domain
        ids["page_url"] = f"https://{domain}/products/bag"

        cur.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title, body_html,
                               indexable, status_code, crawl_depth)
            VALUES (%s, %s, 'product', 'signature product', %s,
                    true, 200, 3)
            RETURNING page_id
            """,
            (site_id, ids["page_url"], BODY))
        ids["page_id"] = str(cur.fetchone()[0])

        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword, keywords,
                                          intent)
            VALUES (%s, %s, %s, 'informational')
            RETURNING cluster_id
            """,
            (site_id, keyword, [keyword]))
        ids["cluster_id"] = str(cur.fetchone()[0])

        cur.execute(
            """
            INSERT INTO page_query_match_scores
                (site_id, page_id, cluster_id, intent_page_type_score,
                 catalogue_match_score, gsc_evidence_score,
                 semantic_content_score)
            VALUES (%s, %s, %s, 0.9, 0.9, 0.5, 0.5)
            """,
            (site_id, ids["page_id"], ids["cluster_id"]))
    return ids


def seed_world(conn, tenant, label, platform, community_key, thread_url,
               snippet=SNIPPET, post_kind="reply", settings_args=None,
               account_created_on=None, standing="established",
               site_platform=None, create_account=True):
    """Community + thread + account + raw recommendation for a platform."""
    out = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_communities
                (site_id, platform, community_key, link_policy,
                 allows_self_promo, ai_content_allowed, enabled, data_source)
            VALUES (%s, %s, %s, 'nofollow', true, true, true, 'import')
            RETURNING community_id
            """,
            (tenant["site_id"], platform,
             f"{community_key}{label}{RUN_TOKEN}"))
        community_id = cur.fetchone()[0]
        out["community_id"] = str(community_id)

        cur.execute(
            """
            INSERT INTO offpage_threads
                (site_id, community_id, thread_url, title, snippet,
                 context_source, context_chars, promotion_allowed,
                 post_kind, data_source)
            VALUES (%s, %s, %s, %s, %s, 'operator_import', %s, true, %s,
                    'import')
            RETURNING thread_id
            """,
            (tenant["site_id"], community_id, thread_url,
             "How do I tell quality leather goods apart?", snippet,
             len(snippet), post_kind))
        thread_id = cur.fetchone()[0]
        out["thread_id"] = str(thread_id)

        if create_account:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO offpage_accounts
                        (site_id, platform, handle, brand_name, status,
                         promotional_cap_30d, account_created_on)
                    VALUES (%s, %s, %s, 'Aurora Audio', 'active', 3, %s)
                    RETURNING account_id
                    """,
                    (tenant["site_id"], platform, f"u/{label}{RUN_TOKEN}",
                     account_created_on))
                account_id = cur.fetchone()[0]
                out["account_id"] = str(account_id)

                cur.execute(
                    """
                    INSERT INTO offpage_account_communities
                        (site_id, account_id, community_id, standing,
                         assessed_at)
                    VALUES (%s, %s, %s, %s, now())
                    """,
                    (tenant["site_id"], account_id, community_id, standing))

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recommendations
                    (site_id, generator, action_type, status, diagnosis,
                     target_url, cluster_id, offpage_thread_id)
                VALUES (%s, 'offpage_community', 'offpage_reply', 'raw', %s,
                        %s, %s, %s)
                RETURNING recommendation_id
                """,
                (tenant["site_id"], "Off-page opportunity", thread_url,
                 tenant["cluster_id"], thread_id))
            out["recommendation_id"] = str(cur.fetchone()[0])

    if settings_args is not None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO offpage_platform_settings
                    (site_id, platform, enabled, discovery_seams,
                     discovery_calls_per_day, reply_char_limit,
                     post_char_limit, min_words, promotional_posts_per_30d,
                     targeting)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (tenant["site_id"], platform, settings_args.get("enabled", True),
                 settings_args.get("seams", []),
                 settings_args.get("calls_per_day", 5),
                 settings_args.get("reply_char_limit"),
                 settings_args.get("post_char_limit"),
                 settings_args.get("min_words"),
                 settings_args.get("promo_cap"),
                 _json(settings_args.get("targeting", {}))))
    if site_platform is not None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE site_config SET platform = %s WHERE site_id = %s",
                (site_platform, tenant["site_id"]))
    return out


# Long merchant questions (above the context floor). The E-scenarios
# intentionally do NOT match the configured fit_keywords ('inventory')
# except E11, whose thread genuinely is about inventory sync.
SHOPIFY_Q1 = (
    "I run a Shopify store and I am trying to work out how to explain to "
    "customers which of my products will hold up to daily use, and what "
    "actually makes one product last longer than another, because the "
    "product pages do not say."
)
SHOPIFY_Q2 = (
    "I run a Shopify store and I am trying to work out how to keep my "
    "inventory in sync with my warehouse so my product pages never show a "
    "sold-out state while we still have stock on hand."
)


def _json(obj):
    import json
    return json.dumps(obj)


def _advice(words=180):
    """A corpus-grounded paragraph of length >= `words` CONTENT words.

    word_count() drops stopwords, so raw token count is not the measure;
    this builds with od.word_count so the long-form floor is genuinely met.
    Every sentence is verbatim corp-umished vocabulary (the whole-part
    overlap gate measures ~1.0 overlap against the tenant corpus).
    """
    sentence = ("Because the grain is left intact, the stitching sits in a "
                "continuous layer and the bag picks up a patina instead of "
                "flaking.")
    out = []
    while od.word_count(" ".join(out)) < words:
        out.append(sentence)
    return " ".join(out)


def _blocked(fn):
    try:
        fn()
        return None
    except od.OffpageDraftBlocked as exc:
        return str(exc)


def run(conn):
    # ---------------- A. channel_enabled ----------------
    t_a = seed_tenant(conn, f"mp-a-{RUN_TOKEN}.example.com", "A", "leather care")
    w_a = seed_world(conn, t_a, "a", "x",
                     "x.com/vendor", f"https://x.com/a/status/11",
                     settings_args=None)
    msg = _blocked(lambda: od.draft_offpage_reply(
        conn, w_a["recommendation_id"], client=FakeClient(
            {"advice_md": _advice(), "mention_md": GROUNDED_MENTION})))
    check("A1 x without settings row is refused by channel_enabled",
          msg and "offpage_platform_settings row missing or disabled" in msg,
          str(msg))

    t_b = seed_tenant(conn, f"mp-b-{RUN_TOKEN}.example.com", "B", "leather care")
    w_b = seed_world(conn, t_b, "b", "x",
                     "x.com/vendor", f"https://x.com/b/status/22",
                     settings_args={"enabled": True, "reply_char_limit": 5000},
                     account_created_on=date.today() - timedelta(days=400))
    r_b = od.draft_offpage_reply(conn, w_b["recommendation_id"],
                                 client=FakeClient(
                                     {"advice_md": _advice(),
                                      "mention_md": GROUNDED_MENTION}))
    check("A2 x with enabled=true drafts a promotional reply",
          r_b["post_type"] == "promotional", str(r_b.get("refused_gates")))

    # ---------------- B. x_char_limit ----------------
    t_c = seed_tenant(conn, f"mp-c-{RUN_TOKEN}.example.com", "C", "leather care")
    w_c = seed_world(conn, t_c, "c", "x",
                     "x.com/vendor", f"https://x.com/c/status/33",
                     settings_args={"enabled": True, "reply_char_limit": 30},
                     account_created_on=date.today() - timedelta(days=400))
    msg = _blocked(lambda: od.draft_offpage_reply(
        conn, w_c["recommendation_id"], client=FakeClient(
            {"advice_md": _advice(), "mention_md": GROUNDED_MENTION})))
    check("B3 copy over the X ceiling is refused, not truncated",
          msg and "X ceiling" in msg, str(msg))

    t_d = seed_tenant(conn, f"mp-d-{RUN_TOKEN}.example.com", "D", "leather care")
    w_d = seed_world(conn, t_d, "d", "x",
                     "x.com/vendor", f"https://x.com/d/status/44",
                     settings_args={"enabled": True, "reply_char_limit": 5000},
                     account_created_on=date.today() - timedelta(days=400))
    r_d = od.draft_offpage_reply(conn, w_d["recommendation_id"],
                                 client=FakeClient(
                                     {"advice_md": _advice(),
                                      "mention_md": GROUNDED_MENTION}))
    check("B4 copy under a large ceiling persists a draft",
          r_d["draft_id"] is not None and r_d["body_md"] != "")

    check("B5 char_ceiling picks post_char_limit for articles",
          od.char_ceiling({"post_kind": "article",
                           "settings": {"post_char_limit": 7000,
                                        "reply_char_limit": 280}}) == 7000)
    check("B5b char_ceiling uses reply_char_limit for replies",
          od.char_ceiling({"post_kind": "reply",
                           "settings": {"post_char_limit": 7000,
                                        "reply_char_limit": 280}}) == 280)

    # ---------------- C. x_account_standing ----------------
    t_e = seed_tenant(conn, f"mp-e-{RUN_TOKEN}.example.com", "E", "leather care")
    w_e = seed_world(conn, t_e, "e", "x",
                     "x.com/vendor", f"https://x.com/e/status/55",
                     settings_args={"enabled": True, "reply_char_limit": 5000},
                     account_created_on=date.today() - timedelta(days=10),
                     standing="established")
    r_e = od.draft_offpage_reply(conn, w_e["recommendation_id"],
                                 client=FakeClient(
                                     {"advice_md": _advice(),
                                      "mention_md": GROUNDED_MENTION}))
    check("C6 young X identity: mention refused, advice kept",
          r_e["post_type"] == "helpful"
          and "x_account_standing" in r_e["refused_gates"]
          and r_e["body_md"] != "", str(r_e["refused_gates"]))

    t_f = seed_tenant(conn, f"mp-f-{RUN_TOKEN}.example.com", "F", "leather care")
    w_f = seed_world(conn, t_f, "f", "x",
                     "x.com/vendor", f"https://x.com/f/status/66",
                     settings_args={"enabled": True, "reply_char_limit": 5000},
                     account_created_on=date.today() - timedelta(days=400))
    r_f = od.draft_offpage_reply(conn, w_f["recommendation_id"],
                                 client=FakeClient(
                                     {"advice_md": _advice(),
                                      "mention_md": GROUNDED_MENTION}))
    check("C7 aged X identity allows the promotional mention",
          r_f["post_type"] == "promotional", str(r_f["refused_gates"]))

    # ---------------- D. account platform filter ----------------
    t_g = seed_tenant(conn, f"mp-g-{RUN_TOKEN}.example.com", "G", "leather care")
    # reddit account with standing in the reddit community only.
    w_g_reddit = seed_world(conn, t_g, "g1", "reddit",
                            "r/ecommerce",
                            f"https://reddit.com/r/ecommerce/g1/comments/t",
                            snippet="Question about listing leather bags?",
                            settings_args=None)
    # an x thread whose community has NO x account.
    w_g_x = seed_world(conn, t_g, "g2", "x",
                       "x.com/vendor", f"https://x.com/g/status/77",
                       settings_args={"enabled": True,
                                      "reply_char_limit": 5000},
                       account_created_on=date.today() - timedelta(days=400),
                       create_account=False)
    msg = _blocked(lambda: od.draft_offpage_reply(
        conn, w_g_x["recommendation_id"], client=FakeClient(
            {"advice_md": _advice(), "mention_md": GROUNDED_MENTION})))
    check("D8 x thread with only a reddit account is refused",
          msg and "no eligible account" in msg, str(msg))

    # now give the x community its own aged active account.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_accounts
                (site_id, platform, handle, brand_name, status,
                 promotional_cap_30d, account_created_on)
            VALUES (%s, 'x', 'u/gx2', 'Aurora Audio', 'active', 3, %s)
            RETURNING account_id
            """,
            (t_g["site_id"], date.today() - timedelta(days=400)))
        acc_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO offpage_account_communities
                (site_id, account_id, community_id, standing, assessed_at)
            SELECT site_id, %s, community_id, 'established', now()
            FROM offpage_communities WHERE site_id = %s AND platform = 'x'
            """,
            (acc_id, t_g["site_id"]))
    r_g = od.draft_offpage_reply(conn, w_g_x["recommendation_id"],
                                 client=FakeClient(
                                     {"advice_md": _advice(),
                                      "mention_md": GROUNDED_MENTION}))
    check("D9 x community with an x account drafts",
          r_g["post_type"] == "promotional", str(r_g["refused_gates"]))

    # ---------------- E. shopify_community_fit ----------------
    t_h = seed_tenant(conn, f"mp-h-{RUN_TOKEN}.example.com", "H", "email marketing")
    w_h = seed_world(
        conn, t_h, "h", "shopify_community",
        "community.shopify.com/c/Apps-and-Customizations",
        "https://community.shopify.com/c/Apps-and-Customizations/m-p/1001",
        snippet=SHOPIFY_Q1,
        settings_args={"enabled": True,
                       "targeting": {"shopify": {"fit_keywords": ["inventory"]}}})
    r_h = od.draft_offpage_reply(conn, w_h["recommendation_id"],
                                 client=FakeClient(
                                     {"advice_md": _advice(),
                                      "mention_md": GROUNDED_MENTION}))
    check("E10 merchant thread not matching fit_keywords refuses the mention",
          r_h["post_type"] == "helpful"
          and "shopify_community_fit" in r_h["refused_gates"],
          str(r_h["refused_gates"]))

    t_i = seed_tenant(conn, f"mp-i-{RUN_TOKEN}.example.com", "I", "inventory sync")
    w_i = seed_world(
        conn, t_i, "i", "shopify_community",
        "community.shopify.com/c/Inventory",
        "https://community.shopify.com/c/Inventory/m-p/2002",
        snippet=SHOPIFY_Q2,
        settings_args={"enabled": True,
                       "targeting": {"shopify": {"fit_keywords": ["inventory"]}}})
    r_i = od.draft_offpage_reply(conn, w_i["recommendation_id"],
                                 client=FakeClient(
                                     {"advice_md": _advice(),
                                      "mention_md": GROUNDED_MENTION}))
    check("E11 matching merchant thread allows the mention",
          r_i["post_type"] == "promotional", str(r_i["refused_gates"]))

    t_j = seed_tenant(conn, f"mp-j-{RUN_TOKEN}.example.com", "J", "email marketing",
                      platform="framer")
    w_j = seed_world(
        conn, t_j, "j", "shopify_community",
        "community.shopify.com/c/Apps-and-Customizations",
        "https://community.shopify.com/c/Apps-and-Customizations/m-p/3003",
        snippet=SHOPIFY_Q1,
        settings_args={"enabled": True,
                       "targeting": {"shopify": {"fit_keywords": ["inventory"]}}})
    r_j = od.draft_offpage_reply(conn, w_j["recommendation_id"],
                                 client=FakeClient(
                                     {"advice_md": _advice(),
                                      "mention_md": GROUNDED_MENTION}))
    check("E12 non-shopify client site refuses the fit (cannot assert)",
          r_j["post_type"] == "helpful"
          and "shopify_community_fit" in r_j["refused_gates"],
          str(r_j["refused_gates"]))

    # ---------------- F. medium long-form ----------------
    t_k = seed_tenant(conn, f"mp-k-{RUN_TOKEN}.example.com", "K", "leather care")
    w_k = seed_world(conn, t_k, "k", "medium",
                     "medium.com/@aurora",
                     "https://medium.com/@aurora/how-to-tell-quality-leather",
                     snippet=SNIPPET,
                     post_kind="article",
                     settings_args=None,
                     account_created_on=date.today() - timedelta(days=400))
    corpus = load_brand_corpus(conn, {
        "site_id": t_k["site_id"], "cluster_id": t_k["cluster_id"]})
    article_advice = _advice(words=900)

    check("F13 advice_problems uses the article word window",
          any("650" in p and "too short" in p
              for p in od.advice_problems("Too short.", "Aurora Audio",
                                          post_kind="article")),
          str(od.advice_problems("Too short.", "Aurora Audio",
                                 post_kind="article")))

    p_no_link = od.longform_problems({}, article_advice, "", corpus)
    check("F14 article without a backlink is refused",
          any("no contextual backlink" in p for p in p_no_link),
          str(p_no_link))
    p_bad_link = od.longform_problems(
        {}, article_advice, "Read the full guide at https://unrelated.example/stats",
        corpus)
    check("F15 article backlink to a non-corpus URL is refused",
          any("does not resolve" in p for p in p_bad_link),
          str(p_bad_link))
    p_good_link = od.longform_problems(
        {}, article_advice, f"Read the full guide at {t_k['page_url']}",
        corpus)
    check("F16 article backlink to a grounding page passes",
          not p_good_link, str(p_good_link))

    msg = _blocked(lambda: od.draft_offpage_reply(
        conn, w_k["recommendation_id"], client=FakeClient(
            {"advice_md": "Stub story.", "mention_md": "x"})))
    check("F17 article stub is refused end-to-end (too short)",
          msg and "too short" in msg, str(msg))

    class _CapturingClient(FakeClient):
        def __init__(self, segments):
            super().__init__(segments)
            self.passed_payload = None
            self.passed_system = None

        def complete_json(self, system_prompt, user_payload, retries=1):
            self.passed_system = system_prompt
            self.passed_payload = user_payload
            return self.segments

    cap_art = _CapturingClient({"advice_md": article_advice, "mention_md": ""})
    blocked_ = _blocked(lambda: od.draft_offpage_reply(
        conn, w_k["recommendation_id"], client=cap_art))
    check("F18 article payload instructs the long-form target + one backlink",
          isinstance(cap_art.passed_payload, dict)
          and "1200-1700" in cap_art.passed_payload.get("instruction", "")
          and "copied EXACTLY from the sources array"
          in cap_art.passed_payload.get("instruction", ""),
          str(cap_art.passed_payload))
    check("F18b article system prompt carries the long-form addendum",
          "KIND: This job is a LONG-FORM ARTICLE"
          in (cap_art.passed_system or "")
          and "1200-1700" in (cap_art.passed_system or "")
          and "Each PARAGRAPH" in (cap_art.passed_system or ""),
          str(cap_art.passed_system)[:120])
    check("F19 long-form gate still refuses an article without a backlink",
          blocked_ and "no contextual backlink" in blocked_, str(blocked_))

    cap_rep = _CapturingClient(
        {"advice_md": _advice(), "mention_md": GROUNDED_MENTION})
    _call_ctx = {"title": "reply", "snippet": SNIPPET,
                 "community_key": "medium.com/@aurora", "platform": "medium",
                 "post_kind": "reply"}
    od._call_model(_call_ctx, corpus, True, "Aurora Audio", cap_rep)
    check("F20 reply payload carries no article instruction",
          "instruction" not in (cap_rep.passed_payload or {})
          and cap_rep.passed_payload.get("post_kind") == "reply",
          str(cap_rep.passed_payload))

    class _ScriptedClient:
        """First call misses, the corrective re-ask (with revision
        feedback) returns a compliant article."""

        def __init__(self):
            self.calls = []

        def complete_json(self, system_prompt, user_payload, retries=1):
            self.calls.append(dict(user_payload))
            if user_payload.get("revision_feedback"):
                return {
                    "advice_md": article_advice,
                    "mention_md": (
                        "We cut ours from full-grain leather with "
                        "vegetable tannin, so the stitching stays anchored "
                        "in the grain; the full guide is at "
                        f"{t_k['page_url']}"),
                }
            return {"advice_md": "Short first attempt.", "mention_md": ""}

    scripted = _ScriptedClient()
    r_fix = od.draft_offpage_reply(conn, w_k["recommendation_id"],
                                   client=scripted)
    check("F21 one corrective re-ask turns a miss into a passing draft",
          len(scripted.calls) == 2
          and r_fix["post_type"] == "promotional"
          and "medium_longform_grounded" not in r_fix["refused_gates"]
          and any(c.get("revision_feedback") for c in scripted.calls)
          and "failure type" in next(
              c["revision_feedback"]
              for c in scripted.calls if c.get("revision_feedback")).lower(),
          str(len(scripted.calls)) + " " + str(r_fix["refused_gates"]))

    # paragraph-level grounding (plan/35 block measure)
    grounded_body = "\n\n".join([
        _advice(words=60) + " That is the decision that compounds.",
        _advice(words=60),
    ])
    check("F22 a transitional sentence no longer fails a grounded block",
          not od.paragraph_overlap_problems(grounded_body, corpus["text"]),
          str(od.paragraph_overlap_problems(grounded_body,
                                            corpus["text"])))
    invented_block = ("\n\n".join([
        _advice(words=60),
        "Email list hygiene quarterly audits improve sender reputation "
        "scores across every measurable deliverability dimension and "
        "subject-line benchmark in the industry."]))
    check("F23 an ungrounded block is still refused",
          od.paragraph_overlap_problems(invented_block, corpus["text"]),
          str(od.paragraph_overlap_problems(invented_block,
                                            corpus["text"])))
    wall = " ".join([_advice(words=40), "That is the trade-off.",
                     _advice(words=40)])
    wall_blocks = od._paragraph_blocks(wall)
    check("F24 wall-of-text falls back to sentence group blocks",
          len(wall_blocks) >= 2
          and not od.paragraph_overlap_problems(wall, corpus["text"]),
          str(len(wall_blocks)))
    article_body = "\n\n".join([
        _advice(words=90) + " That is the trade-off, and it compounds."
        for _ in range(8)])
    check("F24b longform_problems uses the paragraph measure",
          not od.longform_problems({}, article_body,
                                   f"Full guide at {t_k['page_url']}",
                                   corpus),
          str(od.longform_problems({}, article_body,
                                   f"Full guide at {t_k['page_url']}",
                                   corpus)))
    check("F25 900-word article sits inside the 650-1500 window",
          not od.longform_problems({}, article_advice,
                                   f"Full guide at {t_k['page_url']}",
                                   corpus),
          str(od.longform_problems({}, article_advice,
                                   f"Full guide at {t_k['page_url']}",
                                   corpus)))

    # ---------------- G. API surface (plan/35) ----------------
    import api.main as api_main  # noqa: F401
    client = TestClient(api_main.app)

    t_m = seed_tenant(conn, f"mp-m-{RUN_TOKEN}.example.com", "M", "leather care")
    r = client.get(f"/api/offpage/platform-settings?site_id={t_m['site_id']}")
    g = r.json()
    check("G1 GET platform-settings on a fresh site is fail-closed",
          r.status_code == 200 and g["active_channels"] == 0
          and g["items"] == [], r.text[:200])

    body = {"site_id": t_m["site_id"], "platform": "x", "enabled": True,
            "discovery_seams": ["serp"], "discovery_calls_per_day": 40,
            "reply_char_limit": 280, "post_char_limit": None,
            "min_words": 30, "promotional_posts_per_30d": 5,
            "targeting": {"shopify": {"fit_keywords": ["inventory"]}}}
    r = client.put(f"/api/offpage/platform-settings?site_id={t_m['site_id']}",
                   json=body)
    g = r.json()
    check("G2 PUT upserts an enabled x channel",
          r.status_code == 200 and g["active_channels"] == 1, r.text[:200])
    x_item = next((i for i in g["items"] if i["platform"] == "x"), None)
    check("G2b settings round-trip (limits, seams, calls, targeting)",
          x_item is not None and x_item["enabled"] is True
          and x_item["reply_char_limit"] == 280
          and x_item["min_words"] == 30
          and x_item["discovery_seams"] == ["serp"]
          and x_item["discovery_calls_per_day"] == 40
          and x_item["targeting"] ==
          {"shopify": {"fit_keywords": ["inventory"]}}, str(g))

    r = client.put(f"/api/offpage/platform-settings?site_id={t_m['site_id']}",
                   json={"site_id": t_m["site_id"], "platform": "myspace"})
    check("G3 PUT with an unknown platform is 422",
          r.status_code == 422, r.text[:120])
    r = client.put(f"/api/offpage/platform-settings?site_id={t_m['site_id']}",
                   json={"site_id": str(uuid.uuid4()), "platform": "x"})
    check("G4 PUT with a mismatched body site_id is 422",
          r.status_code == 422, r.text[:120])

    r = client.post(f"/api/offpage/drafts/{r_b['draft_id']}/permalink"
                    f"?site_id={t_b['site_id']}",
                    json={"permalink": "https://www.x.com/vendor/status/22",
                          "operator": "tester"})
    check("G5 x permalink accepts www.x.com", r.status_code == 200,
          r.text[:160])
    r = client.post(f"/api/offpage/drafts/{r_b['draft_id']}/permalink"
                    f"?site_id={t_b['site_id']}",
                    json={"permalink": "https://twitter.com/vendor/status/22"})
    check("G5b x permalink accepts the twitter.com alias (idempotent re-submit)",
          r.status_code == 200 and r.json()["status"] == "pending_verify",
          r.text[:160])
    r = client.post(f"/api/offpage/drafts/{r_b['draft_id']}/permalink"
                    f"?site_id={t_b['site_id']}",
                    json={"permalink": "https://reddit.com/r/x/comments/22"})
    check("G5c cross-platform host is 422", r.status_code == 422, r.text[:160])

    r = client.post(f"/api/offpage/drafts/{r_i['draft_id']}/permalink"
                    f"?site_id={t_i['site_id']}",
                    json={"permalink":
                           "https://community.shopify.com/c/Inventory/m-p/2002",
                           "operator": "tester"})
    check("G6 shopify_community accepts community.shopify.com",
          r.status_code == 200, r.text[:160])
    r = client.post(f"/api/offpage/drafts/{r_i['draft_id']}/permalink"
                    f"?site_id={t_i['site_id']}",
                    json={"permalink": "https://x.com/what/status/9"})
    check("G6b shopify_community permalink does not accept x.com",
          r.status_code == 422, r.text[:160])


def main():
    conn = database.get_connection()
    conn.autocommit = True
    try:
        run(conn)
    finally:
        conn.close()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all plan/35 multi-platform gate tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
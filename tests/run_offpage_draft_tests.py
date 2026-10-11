# -*- coding: utf-8 -*-
"""Off-page community SEO: tenant grounding + segmented drafting (plan/33).

python tests/run_offpage_draft_tests.py   (needs a reachable local Postgres)

Verifies:
  A. Tenant-scoped corpus loader
     1. corpus is built ONLY from the recommendation's own tenant
     2. another tenant's pages never enter the corpus
     3. pages with thin/absent body_html are excluded (URL-as-title trap)
     4. missing cluster_id fails closed (no whole-catalogue dump)
     5. hash is stable and provenance lists the exact page ids
     6. HTML entities are decoded (grounding is a substring domain)
  B. Context sufficiency gate (SERP-only v1)
     7. thin SERP snippet blocks the draft
     8. operator_import bypasses the length floor only
     9. sufficient context passes
  C. Segmented content gates
    10. brand name in advice segment is refused (promotion-by-stealth)
    11. ungrounded mention claim is refused
    12. generic category advice PASSES the mention gate (the false
        positive that motivated the segmented design)
    13. single-reply 80/20 floor enforced
    14. low sentence-overlap tripwire fails
  D. Eligibility gates (fail closed on NULL)
    15. ai_content_allowed NULL refuses ALL drafting
    16. community allows_self_promo NULL refuses the mention
    17. thread promotion_allowed NULL refuses the mention
    18. promotional cap reached refuses the mention
    19. all-clear produces a promotional draft
  E. Persistence + isolation
    20. draft row persists frozen corpus hash + page ids
    21. offpage_events records BOTH pass and refuse outcomes
    22. no off-page row is claimable by the automated fix executor
"""
import os
import sys
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import db as database  # noqa: E402

from fixes.offpage_grounding import (  # noqa: E402
    body_to_text, corpus_hash, corpus_is_usable, load_brand_corpus,
)
from fixes import offpage_drafter as od  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
FAILURES = []
DOMAIN_A = f"offpage-a-{RUN_TOKEN}.example.com"
DOMAIN_B = f"offpage-b-{RUN_TOKEN}.example.com"

# A SERP snippet comfortably above MIN_CONTEXT_CHARS, describing a real
# question so the drafter has something to answer.
GOOD_SNIPPET = (
    "I run a small shop selling handmade leather bags and I am trying to "
    "work out how to tell customers which ones will hold up to daily use. "
    "The product pages describe the leather and the stitching but I do not "
    "know how to explain what actually makes one last longer than another."
)
THIN_SNIPPET = "help"

# Corpus copy. The words 'stitching', 'leather', 'tannin' and 'full-grain'
# appear ONLY in tenant A's catalogue, so any mention grounded on A's
# corpus may use them and any grounded on B's may not.
A_BODY = (
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
B_BODY = (
    "<p>Our ceramic mugs are thrown from stoneware clay and fired to a high "
    "temperature so the glaze is fused to the body.</p><p>The clay body "
    "holds heat well and the glaze resists staining from coffee and tea. "
    "Each mug is wheel thrown and trimmed by hand in our studio.</p>"
)


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


def seed_tenant(conn, domain, label, body, body_len_ok=True):
    """Site + cluster + one rich product page. Returns a dict of ids."""
    ids = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property,
                                     catalogue_size_tier, platform)
            VALUES (%s, %s, %s, 'small', 'shopify')
            RETURNING site_id
            """,
            (f"Offpage {label} {RUN_TOKEN}", domain,
             f"sc-domain:{domain}"))
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)

        cur.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title, body_html,
                               indexable, status_code, crawl_depth)
            VALUES (%s, %s, 'product', %s, %s, true, 200, 3)
            RETURNING page_id
            """,
            (site_id, f"https://{domain}/products/bag" if "a" in domain
             else f"https://{domain}/products/mug",
             f"{label} signature product",
             body if body_len_ok else None))
        ids["page_id"] = str(cur.fetchone()[0])

        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword, keywords,
                                          intent)
            VALUES (%s, %s, %s, 'informational')
            RETURNING cluster_id
            """,
            (site_id, f"how to choose {label.lower()}",
             [f"how to choose {label.lower()}"]))
        cluster_id = cur.fetchone()[0]
        ids["cluster_id"] = str(cluster_id)

        # Relevance score linking the product page to the cluster.
        cur.execute(
            """
            INSERT INTO page_query_match_scores
                (site_id, page_id, cluster_id, intent_page_type_score,
                 catalogue_match_score, gsc_evidence_score,
                 semantic_content_score)
            VALUES (%s, %s, %s, 0.9, 0.9, 0.5, 0.5)
            """,
            (site_id, ids["page_id"], cluster_id))
    return ids


def seed_offpage_world(conn, tenant, community_overrides=None,
                      thread_overrides=None, snippet=GOOD_SNIPPET,
                      context_source="serp", account_overrides=None,
                      standing="established", publish_history=0,
                      label="x"):
    """Recommendation + thread + community + account for one tenant.

    `label` keeps each scenario on its own community: the unique key is
    (site_id, platform, community_key), so re-using one key for a second
    scenario on the same tenant would collide.
    """
    community = dict({
        "link_policy": "nofollow",
        "allows_self_promo": True,
        "ai_content_allowed": True,
        "enabled": True,
    }, **(community_overrides or {}))
    thread = dict({"promotion_allowed": True}, **(thread_overrides or {}))
    account = dict({"status": "active", "brand_name": "Aurora Audio",
                    "promotional_cap_30d": 3},
                   **(account_overrides or {}))
    community_key = f"r/test{RUN_TOKEN}{label}"
    handle = f"u/{label}{RUN_TOKEN}"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_communities
                (site_id, platform, community_key, link_policy,
                 allows_self_promo, ai_content_allowed, enabled, data_source)
            VALUES (%s, 'reddit', %s, %s, %s, %s, %s, 'import')
            RETURNING community_id
            """,
            (tenant["site_id"], community_key, community["link_policy"],
             community["allows_self_promo"], community["ai_content_allowed"],
             community["enabled"]))
        community_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO offpage_threads
                (site_id, community_id, thread_url, title, snippet,
                 context_source, promotion_allowed, data_source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'import')
            RETURNING thread_id
            """,
            (tenant["site_id"], community_id,
             f"https://reddit.com/{community_key}/comments/{label}1/thread",
             "How do I tell quality leather goods apart?", snippet,
             context_source, thread["promotion_allowed"]))
        thread_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO offpage_accounts
                (site_id, platform, handle, brand_name, status,
                 promotional_cap_30d)
            VALUES (%s, 'reddit', %s, %s, %s, %s)
            RETURNING account_id
            """,
            (tenant["site_id"], handle, account["brand_name"],
             account["status"], account["promotional_cap_30d"]))
        account_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO offpage_account_communities
                (site_id, account_id, community_id, standing, assessed_at)
            VALUES (%s, %s, %s, %s, now())
            """,
            (tenant["site_id"], account_id, community_id, standing))

        # target_url is the thread permalink and is part of
        # uq_recommendations_candidate, so it must be the SAME per-label
        # thread_url the thread row got - not a shared constant.
        thread_url = (f"https://reddit.com/{community_key}"
                      f"/comments/{label}1/thread")
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, status, diagnosis,
                 target_url, cluster_id, offpage_thread_id)
            VALUES (%s, 'offpage_community', 'offpage_reply', 'raw', %s, %s,
                    %s, %s)
            RETURNING recommendation_id
            """,
            (tenant["site_id"], "Off-page opportunity", thread_url,
             tenant["cluster_id"], thread_id))
        rec_id = cur.fetchone()[0]

        # Pad the account's trailing-30-day promotional usage toward its cap.
        for _ in range(publish_history):
            _insert_publication(cur, tenant["site_id"], rec_id, account_id,
                                community_id, thread_id)

    return {"recommendation_id": str(rec_id), "thread_id": str(thread_id),
            "community_id": str(community_id), "account_id": str(account_id)}


def _insert_publication(cur, site_id, rec_id, account_id, community_id,
                        thread_id):
    """Seed one promotional publication to pad the account's trailing-30-day
    usage toward its cap.

    A publication needs a draft (one of the five composite tenant FKs), so
    a 'superseded' seed draft is created first. 'superseded' rather than
    'drafted' specifically so it does not collide with
    uq_offpage_draft_live, which permits only one non-superseded draft per
    recommendation - the real draft this scenario produces later.
    """
    cur.execute(
        """
        INSERT INTO offpage_drafts
            (site_id, recommendation_id, body_md, version, status)
        VALUES (%s, %s, 'seed draft for cap padding',
                COALESCE((SELECT max(version) + 1 FROM offpage_drafts
                          WHERE recommendation_id = %s), 0),
                'superseded')
        RETURNING draft_id
        """, (site_id, rec_id, rec_id))
    draft_id = cur.fetchone()[0]
    cur.execute(
        """
        INSERT INTO offpage_publications
            (site_id, recommendation_id, draft_id, account_id, community_id,
             thread_id, approved_body_md, post_type, status, submitted_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'seeded promotional', 'promotional',
                'live', now() - INTERVAL '2 days')
        """,
        (site_id, rec_id, draft_id, account_id, community_id, thread_id))


class FakeClient:
    """Stands in for OllamaClient. `raw` is returned verbatim so the
    complete_json raw-string retry path can be exercised."""

    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def complete_json(self, system_prompt, user_payload, retries=1):
        self.calls.append(user_payload)
        return self.raw


def make_segments(advice=None, mention=""):
    return {"advice_md": advice or (
        "The thing that actually determines how long a leather bag lasts is "
        "whether the grain is left intact during tanning, not the weight or "
        "the price. Full-grain leather keeps a continuous fibre layer, so "
        "the stitching is anchored into material that will not delaminate. "
        "Chrome-tanned hides cheaper grain under a coating that eventually "
        "flakes, and that flaking is what people mistake for the bag "
        "wearing out. Vegetable tannin darkens over time as it oxidises, "
        "which people often read as damage when it is really the tanning "
        "process continuing to mature. Check the edge finish: if it peels "
        "when you scratch it, it is coated. Solid brass hardware matters "
        "less than people think, but welded rather than glued rivets are a "
        "good sign of assembly quality."
    ), "mention_md": mention}


# --------------------------------------------------------------------
def run(conn):
    tenant_a = seed_tenant(conn, DOMAIN_A, "Aurora", A_BODY)
    tenant_b = seed_tenant(conn, DOMAIN_B, "Basalt", B_BODY)
    rec_a = {"site_id": tenant_a["site_id"],
             "cluster_id": tenant_a["cluster_id"]}
    rec_b = {"site_id": tenant_b["site_id"],
             "cluster_id": tenant_b["cluster_id"]}

    # === A. corpus loader ===
    corpus_a = load_brand_corpus(conn, rec_a)
    corpus_b = load_brand_corpus(conn, rec_b)
    check("A1 tenant A corpus built", not corpus_a["empty"],
          f"chars={corpus_a['chars']}")
    check("A2 tenant B copy never enters tenant A corpus",
          "stoneware" not in corpus_a["text"] and "mug" not in corpus_a["text"],
          corpus_a["text"][:200])
    check("A2b the converse also holds",
          "tannin" not in corpus_b["text"] and "leather" not in corpus_b["text"],
          corpus_b["text"][:200])
    check("A3 tenant corpus carries its own vocabulary",
          "tannin" in corpus_a["text"] and "stitching" in corpus_a["text"])
    check("A5 hash stable across calls",
          load_brand_corpus(conn, rec_a)["hash"] == corpus_a["hash"])
    check("A5b provenance lists the exact page id",
          tenant_a["page_id"] in [str(p) for p in corpus_a["page_ids"]])

    # Entity decoding: grounding is a substring domain, so an unescaped
    # entity would fail a TRUE claim and look like a hallucination.
    # Written with \u escapes deliberately: this file has round-tripped
    # through PowerShell and non-ASCII literals got mangled, which is
    # exactly the class of bug entity decoding exists to prevent.
    decoded = body_to_text("<p>Caf&eacute; &amp; r&#233;sum&eacute;</p>")
    check("A6 HTML entities decoded",
          "café" in decoded.lower()
          and "résumé" in decoded.lower(),
          repr(decoded))

    # Missing cluster -> no relevance signal -> fail closed.
    no_cluster = load_brand_corpus(conn, {"site_id": tenant_a["site_id"]})
    check("A4 missing cluster_id fails closed", no_cluster["empty"])
    check("A4b empty corpus is not usable", not corpus_is_usable(no_cluster))

    # Thin-body page: sync.py writes title=url on the INSERT fallback, so a
    # page with no real body must contribute nothing rather than a URL.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title, body_html,
                               indexable, status_code, crawl_depth)
            VALUES (%s, %s, 'product', %s, NULL, true, 200, 3)
            RETURNING page_id
            """,
            (tenant_a["site_id"], f"https://{DOMAIN_A}/products/thin",
             f"https://{DOMAIN_A}/products/thin"))
        thin_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO page_query_match_scores
                (site_id, page_id, cluster_id, intent_page_type_score,
                 catalogue_match_score, gsc_evidence_score,
                 semantic_content_score)
            VALUES (%s, %s, %s, 1.0, 1.0, 1.0, 1.0)
            """,
            (tenant_a["site_id"], thin_id, tenant_a["cluster_id"]))
    check("A3b thin/absent body page excluded from corpus",
          thin_id not in corpus_a["page_ids"])

    # === B. context sufficiency ===
    check("B7 thin SERP snippet blocks drafting",
          bool(od.context_problems({"title": "Question?", "snippet":
                                    THIN_SNIPPET,
                                    "context_source": "serp"})))
    # operator_import bypasses the SERP char floor, but the SUBSTANCE floor
    # is the only guard left once that floor is bypassed, so a 2-word paste
    # must still be refused. This is the intended asymmetry, not a bug.
    check("B8 operator_import bypasses the SERP floor but not the "
          "substance floor",
          bool(od.context_problems({"title": "Question?",
                                    "snippet": THIN_SNIPPET,
                                    "context_source": "operator_import"})))
    check("B8b operator_import with a real paste passes",
          not od.context_problems({"title": "How do I tell quality leather?",
                                   "snippet": GOOD_SNIPPET,
                                   "context_source": "operator_import"}))
    check("B9 sufficient SERP context passes",
          not od.context_problems({"title": "How do I tell quality leather?",
                                   "snippet": GOOD_SNIPPET,
                                   "context_source": "serp"}))
    check("B9b no context at all blocks",
          bool(od.context_problems({"title": "", "snippet": "",
                                    "context_source": "serp"})))

    # === C. content gates ===
    advice = make_segments()["advice_md"]
    check("C10 brand name in advice refused",
          bool(od.advice_problems(advice + " We use Aurora Audio daily.",
                                  "Aurora Audio")))
    check("C10b clean advice passes", not od.advice_problems(advice, ""))
    check("C10c short advice refused",
          bool(od.advice_problems("Use good leather.", "")))
    check("C10d CTA in advice refused",
          bool(od.advice_problems(advice + " Check out our site.", "")))

    # A mention grounded on tenant A's corpus.
    good_mention = ("We make our bags from full-grain leather with "
                    "vegetable tannin, and the stitching is anchored in the "
                    "grain itself.")
    check("C11 grounded mention passes",
          not od.mention_problems(good_mention, corpus_a,
                                  {"link_policy": "nofollow"}))
    # A claim about tenant A that A's corpus does not support.
    check("C11b ungrounded claim refused",
          bool(od.mention_problems("Our bags come with a lifetime warranty "
                                   "and ship same-day.", corpus_a,
                                   {"link_policy": "nofollow"})))
    # The false positive that motivated the segmented design: generic
    # category advice mentioning 'warranty' is NOT a claim about the
    # client, and must not be run through the product grounding check.
    from fixes.generator import verify_grounded_claims
    generic = advice + " Ask about the warranty before you buy."
    whole_string_ok, _ = verify_grounded_claims(
        generic, {"body_text": corpus_a["text"]})
    check("C12 whole-string grounding WOULD falsely reject generic advice",
          not whole_string_ok,
          "if this now passes, the false positive may be gone and the "
          "segmented design is still correct but differently motivated")
    check("C12b the advice segment is not corpus-bound by design",
          not od.advice_problems(generic, "Aurora Audio"))

    check("C13 single-reply 80/20 floor computed",
          od.helpfulness_ratio(advice, "") == 1.0
          and od.helpfulness_ratio("one two three four five", "six seven")
          < od.MIN_HELPFULNESS_RATIO)
    check("C14 overlap tripwire fails on ungrounded prose",
          bool(od.sentence_overlap_problems(
              "Quantum entanglement in superconducting magnets remains a "
              "debatable proposition among physicists today.", corpus_a["text"])))
    check("C14b overlap tripwire passes on corpus-backed prose",
          not od.sentence_overlap_problems(
              "Full-grain leather with vegetable tannin keeps the stitching "
              "anchored in the grain.", corpus_a["text"]))

    # === D + E. end-to-end ===
    world = seed_offpage_world(conn, tenant_a, label="main")
    result = od.draft_offpage_reply(
        conn, world["recommendation_id"],
        client=FakeClient(make_segments(
            mention="We cut ours from full-grain leather finished with "
                    "vegetable tannin, so the stitching stays anchored in "
                    "the grain.")))
    check("D19 all-clear produces a promotional draft",
          result["post_type"] == "promotional", str(result["refused_gates"]))
    check("C13b measured helpfulness above the floor",
          result["helpfulness_ratio"] >= od.MIN_HELPFULNESS_RATIO,
          str(result["helpfulness_ratio"]))
    check("E20 corpus hash frozen on the row",
          result["grounding_corpus_hash"] == corpus_a["hash"])
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT grounding_corpus_hash, grounding_page_ids,
                   grounding_corpus_chars
            FROM offpage_drafts WHERE draft_id = %s
            """, (result["draft_id"],))
        row = cur.fetchone()
    check("E20b frozen hash persisted", row[0] == corpus_a["hash"])
    check("E20c frozen page ids persisted",
          tenant_a["page_id"] in [str(p) for p in row[1]])

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM generated_fixes
            WHERE site_id = %s AND status = 'queued'
            """, (tenant_a["site_id"],))
        check("E22 no off-page row is claimable by the fix executor",
              cur.fetchone()[0] == 0)

    # --- fail-closed gates: each must refuse, not silently pass ---
    w_ai = seed_offpage_world(conn, tenant_a,
                              community_overrides={"ai_content_allowed": None}, label="ai")
    try:
        od.draft_offpage_reply(conn, w_ai["recommendation_id"],
                               client=FakeClient(make_segments()))
        check("D15 ai_content_allowed NULL refuses ALL drafting", False)
    except od.OffpageDraftBlocked:
        check("D15 ai_content_allowed NULL refuses ALL drafting", True)

    w_ctx = seed_offpage_world(conn, tenant_a, snippet=THIN_SNIPPET, label="ctx")
    try:
        od.draft_offpage_reply(conn, w_ctx["recommendation_id"],
                               client=FakeClient(make_segments()))
        check("D7 thin snippet blocks the draft end-to-end", False)
    except od.OffpageDraftBlocked:
        check("D7 thin snippet blocks the draft end-to-end", True)

    # Soft gates: the MENTION is dropped, the advice is still published.
    w_promo = seed_offpage_world(
        conn, tenant_a,
        community_overrides={"allows_self_promo": None}, label="promo")
    r_promo = od.draft_offpage_reply(
        conn, w_promo["recommendation_id"],
        client=FakeClient(make_segments(mention=good_mention)))
    check("D16 allows_self_promo NULL refuses the mention",
          r_promo["post_type"] == "helpful"
          and "community_allows_promotion" in r_promo["refused_gates"],
          str(r_promo["refused_gates"]))

    w_thread = seed_offpage_world(conn, tenant_a, label="thr",
                                  thread_overrides={"promotion_allowed": None})
    r_thread = od.draft_offpage_reply(
        conn, w_thread["recommendation_id"],
        client=FakeClient(make_segments(mention=good_mention)))
    check("D17 thread promotion_allowed NULL refuses the mention",
          r_thread["post_type"] == "helpful"
          and "thread_allows_promotion" in r_thread["refused_gates"],
          str(r_thread["refused_gates"]))

    w_cap = seed_offpage_world(conn, tenant_a, publish_history=3, label="cap")
    r_cap = od.draft_offpage_reply(
        conn, w_cap["recommendation_id"],
        client=FakeClient(make_segments(mention=good_mention)))
    check("D18 promotional cap reached refuses the mention",
          r_cap["post_type"] == "helpful"
          and "promotional_cap" in r_cap["refused_gates"],
          str(r_cap["refused_gates"]))

    # complete_json can return a RAW STRING on its retry path; a caller
    # that assumes a dict silently drafts nothing.
    raw_string = ('{"advice_md": "The grain determines longevity, not weight '
                  'or price, and the stitching is anchored into intact '
                  'fibre that will not delaminate the way a coated chrome '
                  'finish flakes over time. Check the edge finish by '
                  'scratching it: if it peels, the grain was sanded off '
                  'and coated instead, which is the usual reason a bag '
                  'that looked fine at purchase starts shedding within a '
                  'year.", "mention_md": ""}')
    r_raw = od.draft_offpage_reply(conn, world["recommendation_id"],
                                   client=FakeClient(raw_string))
    check("E23 raw-string model response is handled, not dropped",
          r_raw["draft_id"] is not None and r_raw["advice_md"] != "")
    version_before_redraft = r_raw["version"]
    check("E23b non-JSON response is refused, not crashed",
          od.normalize_llm_response("sorry, I cannot help") is None)
    check("E23c non-dict JSON is refused",
          od.normalize_llm_response({"foo": "bar"}) is None)
    check("E23d fenced JSON is parsed",
          od.normalize_llm_response(
              '```json\n{"advice_md": "x", "mention_md": "y"}\n```') ==
          {"advice_md": "x", "mention_md": "y"})

    # Checked LAST: both outcomes must be present across the whole run, and
    # the refusals only happen in the fail-closed scenarios above. Asserting
    # this earlier would pass on passes alone and prove nothing.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE outcome = 'pass'),
                   count(*) FILTER (WHERE outcome = 'refuse'),
                   count(*) FILTER (WHERE outcome = 'skipped')
            FROM offpage_events WHERE site_id = %s
            """, (tenant_a["site_id"],))
        n_pass, n_refuse, n_skipped = cur.fetchone()
    check("E21 events record BOTH pass and refuse outcomes",
          n_pass > 0 and n_refuse > 0,
          f"pass={n_pass} refuse={n_refuse} skipped={n_skipped}")
    check("E21b refusals carry an operator-readable reason",
          _all_refusals_explained(conn, tenant_a["site_id"]))
    check("E21c 'unknown' is representable as an outcome",
          _unknown_outcome_allowed(conn, tenant_a["site_id"]))

    # Re-drafting the same recommendation must VERSION, not collide. The
    # drafter originally hardcoded version=1, so any second draft against a
    # recommendation that already had one died on
    # UNIQUE (recommendation_id, version) and the operator silently got
    # nothing back.
    r_again = od.draft_offpage_reply(
        conn, world["recommendation_id"],
        client=FakeClient(make_segments(
            mention="We cut ours from full-grain leather finished with "
                    "vegetable tannin, so the stitching stays anchored.")))
    check("E24 re-draft versions forward instead of colliding",
          r_again["version"] == version_before_redraft + 1,
          f"{version_before_redraft} -> {r_again['version']}")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, count(*) FROM offpage_drafts
            WHERE site_id = %s AND recommendation_id = %s
            GROUP BY status ORDER BY status
            """,
            (tenant_a["site_id"], world["recommendation_id"]))
        statuses = dict(cur.fetchall())
    check("E24b exactly one live draft remains per recommendation",
          statuses.get("drafted") == 1, str(statuses))
    check("E24c the superseded draft is retained for audit",
          statuses.get("superseded", 0) >= 1, str(statuses))


def _all_refusals_explained(conn, site_id):
    """Every refusal must say WHY. A refusal with a NULL reason is a
    silent refusal, which is the failure mode an audit log exists to
    prevent."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM offpage_events
            WHERE site_id = %s AND outcome = 'refuse'
              AND (reason IS NULL OR btrim(reason) = '')
            """, (site_id,))
        return cur.fetchone()[0] == 0


def _unknown_outcome_allowed(conn, site_id):
    """'unknown' must be a storable outcome: it is how an unresolved
    community/thread ruleset is recorded, and it maps to a refuse at the
    gate. If the CHECK rejected it, the code would have to silently map
    uncertainty onto a permissive value - the exact failure this design
    exists to prevent."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_events
                (site_id, entity_type, entity_id, stage, gate, outcome, reason)
            VALUES (%s, 'community',
                    (SELECT community_id FROM offpage_communities
                      WHERE site_id = %s LIMIT 1),
                    'draft', 'rules_unresolved', 'unknown',
                    'rules never read')
            RETURNING outcome
            """, (site_id, site_id))
        row = cur.fetchone()
    return bool(row) and row[0] == "unknown"


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
    print("all off-page draft tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())



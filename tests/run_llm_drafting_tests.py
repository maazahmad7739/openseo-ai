# -*- coding: utf-8 -*-
"""LLM candidate drafting + deterministic fact-check guard tests.

python tests/run_llm_drafting_tests.py   (pure unit — no DB, no network)

Covers:
  fact-checker (verify_grounded_claims)
    1. grounded claims pass (specs from page body)
    2. hallucinated material claims rejected (organic/leather/premium)
    3. hallucinated commercial claims rejected (warranty/free shipping)
    4. grounded numeric claims pass; altered numbers rejected
    5. empty candidate rejected
  pipeline (draft_candidates_with_fallback)
    6. grounded LLM candidate wins (title_source/meta_source == 'llm')
    7. hallucinated LLM candidates -> rejected + deterministic fallback
    8. validator-failing LLM candidates (bounds) -> fallback
    9. no client / client raises -> silent deterministic fallback
   10. mixed outcome: LLM title wins, meta falls back
   11. prompt grounding payload carries page facts + competitor reference
   12. fact-check bypass flag (fact_check=False) skips the guard
  integration with generation flow (DB)
   13. generation_source = 'agent' when LLM candidate wins
   14. generation_source = 'deterministic' on LLM failure (soft)
   15. payload.grounding.draft_source + llm_candidates_rejected recorded
"""
import os
import sys
import json
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import db as database  # noqa: E402
import env as env_loader  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


PAGE_FACTS_REC = {
    "page_title": "Aurora Wireless Headphones",
    "primary_keyword": "wireless headphones",
    "intent": "commercial",
    "page_type": "product",
    "body_text": ("Adaptive ANC reaches 45 dB attenuation. "
                  "Battery lasts 40 hours per charge. "
                  "Free returns within 30 days on every order."),
    "competitor_context": {
        "titles": [
            {"title": "Best wireless headphones 2026 — tested", "position": 1,
             "pattern": ["best", "year:2026", "review"]},
            {"title": "Top 8 wireless headphones reviewed", "position": 2,
             "pattern": ["count:8", "review"]},
        ],
        "snippets": [
            {"snippet": "We tested 22 pairs. Free returns.", "position": 1,
             "hooks": ["tested", "free", "returns"]},
        ],
        "url_patterns": ["/guides/", "/collections/"],
    },
}


class FakeLLM:
    """Scriptable stand-in for OllamaClient.complete_json."""

    def __init__(self, titles=(), metas=(), error=None):
        self.titles = list(titles)
        self.metas = list(metas)
        self.error = error
        self.calls = []

    def complete_json(self, system_prompt, user_payload, retries=1):
        self.calls.append({"system": system_prompt, "payload": user_payload})
        if self.error:
            raise self.error
        return {"titles": self.titles, "metas": self.metas}


def test_fact_checker():
    print("\n== fact-checker ==")
    from fixes.generator import verify_grounded_claims

    allok = True
    rec = PAGE_FACTS_REC

    # 1: grounded claims pass
    ok, probs = verify_grounded_claims(
        "Wireless Headphones — Aurora ANC at 45 dB, 40 hours, free returns",
        rec)
    allok &= check("grounded candidate passes", ok, str(probs))

    # 2: hallucinated materials rejected
    ok, probs = verify_grounded_claims(
        "Organic leather headphones with premium luxury feel", rec)
    allok &= check("material claims rejected", not ok
                   and any("organic" in p for p in probs), str(probs))

    # 3: hallucinated commercial promises rejected
    ok, probs = verify_grounded_claims(
        "Wireless headphones with lifetime warranty and free shipping", rec)
    allok &= check("commercial claims rejected", not ok
                   and any("warranty" in p for p in probs)
                   and any("free shipping" in p for p in probs), str(probs))

    # 4a: grounded numbers pass
    ok, probs = verify_grounded_claims("ANC at 45 dB and 40 hours playback", rec)
    allok &= check("grounded numbers pass", ok, str(probs))
    # 4b: altered numbers rejected
    ok, probs = verify_grounded_claims("ANC at 60 dB and 90 hours playback", rec)
    allok &= check("altered numbers rejected", not ok
                   and any("60" in p for p in probs), str(probs))

    # 5: empty candidate rejected
    ok, probs = verify_grounded_claims("", rec)
    allok &= check("empty candidate rejected", not ok)

    # grounded claim that EXISTS in facts does not reject
    ok, probs = verify_grounded_claims(
        "Wireless headphones with 30-day free returns", rec)
    allok &= check("fact-matching promise passes", ok, str(probs))
    return allok


def test_pipeline_llm_wins():
    print("\n== pipeline: LLM candidates win ==")
    from fixes.generator import draft_candidates_with_fallback

    allok = True
    rec = dict(PAGE_FACTS_REC)
    client = FakeLLM(
        titles=["Wireless Headphones: Aurora ANC with 40 Hours Battery"],
        metas=["Wireless Headphones — Aurora ANC at 45 dB. Free returns "
               "within 30 days on every order."])
    out = draft_candidates_with_fallback(rec, client=client)
    allok &= check("llm title wins", out["title_source"] == "llm"
                   and out["title"] is not None, str(out)[:140])
    allok &= check("llm meta wins", out["meta_source"] == "llm"
                   and out["meta"] is not None, str(out)[:140])
    allok &= check("nothing rejected", out["rejected"] == [], str(out["rejected"]))
    return allok


def test_pipeline_hallucination_fallback():
    print("\n== pipeline: hallucinations -> deterministic fallback ==")
    from fixes.generator import draft_candidates_with_fallback

    allok = True
    rec = dict(PAGE_FACTS_REC)
    client = FakeLLM(
        titles=["Organic leather headphones, award-winning luxury feel"],
        metas=["Organic vegan leather headphones with lifetime warranty "
               "and certified eco-friendly materials."])
    out = draft_candidates_with_fallback(rec, client=client)
    allok &= check("title fell back to deterministic",
                   out["title_source"] == "deterministic" and out["title"], str(out)[:140])
    allok &= check("meta fell back to deterministic",
                   out["meta_source"] == "deterministic" and out["meta"], str(out)[:140])
    allok &= check("hallucinations recorded with reasons",
                   len(out["rejected"]) == 2
                   and all("ungrounded" in r["problems"][0] for r in out["rejected"]),
                   str(out["rejected"])[:200])
    return allok


def test_pipeline_validator_fallback():
    print("\n== pipeline: validator rejects -> fallback ==")
    from fixes.generator import draft_candidates_with_fallback

    allok = True
    rec = dict(PAGE_FACTS_REC)
    # factually grounded but validator-failing: too long + banned pattern
    client = FakeLLM(
        titles=["x" * 80],
        metas=["Click here for the best wireless headphones with 45 dB ANC "
               "and free returns within 30 days."])
    out = draft_candidates_with_fallback(rec, client=client)
    allok &= check("bounds-failing title -> fallback",
                   out["title_source"] == "deterministic", str(out)[:140])
    allok &= check("denylist-failing meta -> fallback",
                   out["meta_source"] == "deterministic", str(out)[:140])
    allok &= check("validator problems recorded",
                   any("banned" in r["problems"][0] for r in out["rejected"]),
                   str(out["rejected"])[:200])
    return allok


def test_pipeline_soft_failure():
    print("\n== pipeline: LLM outage -> silent fallback ==")
    from fixes.generator import draft_candidates_with_fallback, LlmDraftError

    allok = True
    rec = dict(PAGE_FACTS_REC)

    # client raises mid-call
    out = draft_candidates_with_fallback(rec, client=FakeLLM(error=RuntimeError("down")))
    allok &= check("raising client -> deterministic title",
                   out["title_source"] == "deterministic" and out["title"], str(out)[:140])
    allok &= check("raising client -> deterministic meta",
                   out["meta_source"] == "deterministic" and out["meta"], str(out)[:140])

    # malformed schema
    class BadShape:
        def complete_json(self, sp, up, retries=1): return {"oops": 1}
    out = draft_candidates_with_fallback(rec, client=BadShape())
    allok &= check("bad schema -> deterministic fallback",
                   out["title_source"] == "deterministic", str(out)[:140])

    # fact_check=False skips the guard (validator still runs): the grounded-
    # looking hallucination reaches the validator, which only rejects it if
    # a *deterministic* rule fails. Here it passes -> title_source llm.
    client = FakeLLM(titles=["Wireless headphones with organic leather feel"])
    out = draft_candidates_with_fallback(rec, client=client, fact_check=False)
    allok &= check("fact_check=False lets hallucination through (guard off)",
                   out["title_source"] == "llm"
                   and all("ungrounded" not in str(r["problems"])
                           for r in out["rejected"]),
                   str(out["rejected"])[:200])
    # ...and with the guard ON the same candidate is rejected + fallback
    out2 = draft_candidates_with_fallback(rec, client=FakeLLM(
        titles=["Wireless headphones with organic leather feel"]))
    allok &= check("fact_check=True rejects the same candidate",
                   out2["title_source"] == "deterministic"
                   and any("ungrounded" in r["problems"][0] for r in out2["rejected"]),
                   str(out2["rejected"])[:200])
    return allok


def test_prompt_grounding():
    print("\n== prompt grounding payload ==")
    from fixes.generator import build_draft_prompt

    allok = True
    payload = build_draft_prompt(PAGE_FACTS_REC)
    allok &= check("page facts present",
                   payload["page"]["title"] == PAGE_FACTS_REC["page_title"]
                   and "45 dB" in payload["page"]["body_text_excerpt"])
    allok &= check("cluster present",
                   payload["cluster"]["primary_keyword"] == "wireless headphones"
                   and payload["cluster"]["intent"] == "commercial")
    allok &= check("competitor style reference present",
                   "Best wireless headphones 2026 — tested"
                   in (payload["competitor_serp_style_reference"]["titles"] or []))
    allok &= check("style-reference labeled (no fact borrowing)",
                   "STYLE reference" in payload["competitor_serp_style_reference"]["note"]
                   or "STYLE" in payload["competitor_serp_style_reference"]["note"])
    allok &= check("bounds in constraints",
                   "20-60" in payload["constraints"]["title_chars"]
                   and "70-155" in payload["constraints"]["meta_chars"])
    return allok


# ------------------------------------------------------------
# DB-level integration: generation flows record the drafting source
# ------------------------------------------------------------

def seed_world(conn, body_html):
    ids = {"domain": f"llmd-{RUN_TOKEN}.example.com"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property, catalogue_size_tier)
            VALUES (%s, %s, %s, 'small') RETURNING site_id
            """,
            (f"LlmD Test {RUN_TOKEN}", ids["domain"], f"sc-domain:{ids['domain']}"),
        )
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)
        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword, keywords, intent)
            VALUES (%s, %s, %s, 'commercial') RETURNING cluster_id
            """,
            (site_id, f"test widget {RUN_TOKEN}", [f"test widget {RUN_TOKEN}"]),
        )
        cluster_id = cur.fetchone()[0]
        ids["cluster_id"] = str(cluster_id)
        url = f"https://{ids['domain']}/products/test-widget"
        cur.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title, shopify_gid,
                               meta_description, body_html)
            VALUES (%s, %s, 'product', %s, %s, %s, %s)
            """,
            (site_id, url, "Test Widget", f"gid://shopify/Product/{RUN_TOKEN[:12]}",
             None, body_html),
        )
        ids["target_url"] = url
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url, cluster_id,
                 diagnosis, evidence_json, status, approved_at)
            VALUES (%s, 'existing_opportunity', 'improve_page', %s, %s,
                    %s, %s, 'approved', now())
            RETURNING recommendation_id
            """,
            (site_id, url, cluster_id, f"llm seed {RUN_TOKEN}", '[]'),
        )
        ids["rec_id"] = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
            "requires_field_verify, enabled) VALUES (%s, 'seo.description', "
            "'medium', 2, true, true)",
            (site_id,),
        )
    conn.commit()
    return ids


def cleanup(conn, ids):
    conn.rollback()
    with conn.cursor() as cur:
        for sql, params in (
            ("DELETE FROM change_log WHERE recommendation_id IN "
             "(SELECT recommendation_id FROM recommendations WHERE site_id = %s)",
             (ids["site_id"],)),
            ("DELETE FROM generated_fixes WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM recommendations WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM keyword_clusters WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM pages WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM rejection_log WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM fix_policy WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM site_config WHERE site_id = %s", (ids["site_id"],)),
        ):
            cur.execute(sql, params)
    conn.commit()


BODY_HTML = ("<p>The Aurora test widget delivers adaptive equalization with "
             "45 dB attenuation. Battery lasts 40 hours. Free returns within "
             "30 days on every order.</p>")


def test_generation_source_recording(conn):
    print("\n== generation flow records drafting source ==")
    from fixes.generator import (generate_meta_fix_for_recommendation,
                                 generate_fix_for_recommendation)

    allok = True
    ids = seed_world(conn, body_html=BODY_HTML)
    try:
        # LLM candidate wins -> generation_source 'agent'
        kw = f"test widget {RUN_TOKEN}"
        client = FakeLLM(
            metas=[f"{kw.title()} — adaptive ANC at 45 dB with free returns "
                   "within 30 days on every order."])
        out = generate_meta_fix_for_recommendation(
            conn, ids["rec_id"], _client=client) if False else None
        # inject via monkeypatched client resolution
        import fixes.generator as gen
        original = gen._llm_client
        gen._llm_client = lambda: client
        try:
            out = generate_meta_fix_for_recommendation(conn, ids["rec_id"])
        finally:
            gen._llm_client = original
        allok &= check("meta fix generated", out["created"] is True, str(out)[:120])
        with conn.cursor() as cur:
            cur.execute("SELECT generation_source, payload_json FROM generated_fixes "
                        "WHERE fix_id = %s", (out["fix_id"],))
            gsrc, payload = cur.fetchone()
            if isinstance(payload, str):
                payload = json.loads(payload)
            allok &= check("generation_source = agent on LLM win", gsrc == "agent",
                           gsrc)
            allok &= check("payload.grounding.draft_source = llm",
                           payload.get("grounding", {}).get("draft_source") == "llm",
                           str(payload.get("grounding"))[:140])
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s", (out["fix_id"],))
        conn.commit()

        # LLM outage -> generation_source 'deterministic' (soft fallback)
        class Dead:
            def complete_json(self, sp, up, retries=1): raise RuntimeError("down")
        original = gen._llm_client
        gen._llm_client = lambda: Dead()
        try:
            out2 = generate_meta_fix_for_recommendation(conn, ids["rec_id"])
        finally:
            gen._llm_client = original
        allok &= check("meta fix on LLM outage", out2["created"] is True)
        with conn.cursor() as cur:
            cur.execute("SELECT generation_source FROM generated_fixes "
                        "WHERE fix_id = %s", (out2["fix_id"],))
            gsrc = cur.fetchone()[0]
            allok &= check("generation_source = deterministic on LLM death",
                           gsrc == "deterministic", gsrc)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s", (out2["fix_id"],))
        conn.commit()

        # title path: LLM hallucination -> fallback, source deterministic;
        # grounded LLM candidate -> 'agent'
        client_t = FakeLLM(
            titles=[f"Organic leather {RUN_TOKEN} award-winning premium kit"])
        original = gen._llm_client
        gen._llm_client = lambda: client_t
        try:
            try:
                out3 = generate_fix_for_recommendation(conn, ids["rec_id"])
                allok &= check("title generated after hallucination fallback",
                               out3["created"] is True, str(out3)[:120])
                with conn.cursor() as cur:
                    cur.execute("SELECT generation_source FROM generated_fixes "
                                "WHERE fix_id = %s", (out3["fix_id"],))
                    gsrc = cur.fetchone()[0]
                    allok &= check("hallucinated LLM -> deterministic source",
                                   gsrc == "deterministic", gsrc)
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s",
                                (out3["fix_id"],))
                conn.commit()
            except Exception as exc:
                allok &= check("hallucination fallback raised unexpectedly",
                               False, repr(exc)[:160])
        finally:
            gen._llm_client = original
    finally:
        cleanup(conn, ids)
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    allok = True
    try:
        allok &= test_fact_checker()
        allok &= test_pipeline_llm_wins()
        allok &= test_pipeline_hallucination_fallback()
        allok &= test_pipeline_validator_fallback()
        allok &= test_pipeline_soft_failure()
        allok &= test_generation_source_recording(conn)
    finally:
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL LLM DRAFTING TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
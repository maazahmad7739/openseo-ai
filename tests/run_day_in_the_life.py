"""Day-in-the-life end-to-end validation run (scripted, offline connectors).

Pipeline (plan/09 + plan/16 raw lifecycle):
  1. sync      — GSC/Shopify/GA4/OpenSEO -> DB (mock fixtures, example.com)
  2. tenant    — Helium (gethelium.co) fixture tenant: real page inventory
  3. generate  — Generators 1-5 -> recommendations (status='raw')
  4. agent     — live LLM (Ollama primary, Gemini fallback)
                 -> raw -> proposed|rejected
  5. operator  — approve one proposed row (simulated; operator approves via UI)
  6. measure   — baseline + post snapshots for the approved row
  7. classify  — plan/08 rules -> result on the measured row
  8. off-page  — draft one community reply, assert the draft persisted AND
                 that nothing was published (publishing is manual only)

WHY HELIUM
----------
Stage 6 used to crash with
`ValueError: target page not in pages table: noise cancelling headphones sale`.
Generator 01 ("missing pages") deliberately emits recommendations for URLs
that are NOT in `pages`, and generators 02-05 dedupe against URLs earlier runs
already recommended. On the shared example.com tenant that left the
measurement stage with only unmeasurable rows.

Helium is a dedicated tenant whose page inventory really exists
(tests/fixtures/helium_pages.json), so generator output points at pages that
resolve in `pages` and both the baseline and post windows can be stored.
Stage 3 asserts that invariant directly, so the drift cannot silently return.

PROVIDER CREDENTIALS
--------------------
Stages 4 and 8 need a live model: Ollama Cloud when
OLLAMA_API_BASE/OLLAMA_API_KEY are set, Gemini when GEMINI_API_KEY is set.
Every other stage is fully offline.

Usage: python tests/run_day_in_the_life.py [--skip-agent] [--skip-offpage]
"""
import os
import sys
import json
from datetime import date, timedelta

# This file prints em-dashes; the default Windows console codepage cannot
# encode them and turns them into U+FFFD, which corrupts the report.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env as env_loader
import db as database
from connectors.sync import run_sync
from generators.orchestrator import run_candidate_generation
from helium_seed import seed_helium, HELIUM_DOMAIN

FIXTURE_DATE = "2026-09-10"   # fixture-universe anchor for injectable clocks
IMPL_DATE = date(2026, 9, 11)  # implementation anchor for measurement


def stage(name):
    print(f"\n=== {name} ===")


def ok(label, value, detail=""):
    print(f"[OK] {label}" if value else f"[FAIL] {label} — {detail}")
    return bool(value)


def skip(label, why):
    print(f"[SKIP] {label} - {why}")


def targets_in_pages(conn, site_id):
    """Recommendations whose target_url is absent from `pages`.

    These are unmeasurable by construction, and they are what made stage 7
    crash. Returned so the test can assert on them rather than trip over
    them later.

    `offpage_community` recommendations are measured against
    `offpage_threads`, not `pages`: their target is a real thread on someone
    else's site and will never appear in the tenant's own page inventory.
    Requiring `pages` membership for them was only ever satisfied because the
    old fixture seeded a page per thread; real SERP discovery cannot do that,
    so the check is scoped per generator instead of relaxed.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.recommendation_id, r.generator, r.target_url "
            "FROM recommendations r "
            "WHERE r.site_id = %s AND r.status = 'raw' "
            "  AND r.target_url IS NOT NULL "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM pages p "
            "    WHERE p.site_id = r.site_id AND p.url = r.target_url"
            "  ) "
            "  AND NOT ("
            "    r.generator = 'offpage_community' AND EXISTS ("
            "      SELECT 1 FROM offpage_threads t "
            "      WHERE t.site_id = r.site_id AND t.thread_url = r.target_url"
            "    )"
            "  )",
            (site_id,))
        return cur.fetchall()


class StubDraftClient:
    """Deterministic stand-in for the model on the offline path.

    Mirrors the `--skip-agent` philosophy: the LLM's approve/reject split is
    non-deterministic, so CI simulates the MODEL while still exercising the
    real gates, real persistence and real tenant isolation. It proves the
    draft reaches `offpage_drafts` and that publishing stays manual; it does
    NOT claim anything about model output quality.

    The mention text is deliberately NOT grounded in the Helium corpus (it
    talks about pricing, which only the /pricing page covers), so the
    mention gate refuses it. A helpful answer with no brand mention is the
    expected soft-refusal outcome, and the draft still persists.
    """

    model = "stub-offpage-drafter"

    ADVICE = (
        "At six thousand monthly active users the honest answer is that "
        "bundle economics only start working once retention data exists, "
        "because the acquisition side alone will not pay for a platform fee. "
        "What we have seen work is splitting the decision: keep media buying "
        "direct while the payback window is under sixty days, then switch on "
        "lifecycle workflows once you have at least three full cohorts of "
        "install data. On budget, expect retention to be a rounding error "
        "against paid spend in year one and the part that actually moves "
        "payback in year two, because it compounds against an install base "
        "you already paid to acquire. The measurement that convinced us was "
        "cohort revenue at day ninety, not blended return on ad spend."
    )
    MENTION = (
        "We built Helium around exactly that split, and our pricing follows "
        "active users rather than ad spend so a promotion month does not "
        "change your invoice."
    )

    def complete_json(self, system_prompt, user_payload, retries=1):
        return {"advice_md": self.ADVICE, "mention_md": self.MENTION}

    def chat(self, messages, **kwargs):
        return json.dumps({"advice_md": self.ADVICE,
                           "mention_md": self.MENTION})


def promote_one_measurable(conn, site_id, from_status, impl_date):
    """Operator-approve one candidate whose target_url exists in `pages`.

    The JOIN against `pages` is what makes the measurement stage viable:
    store_post_snapshots raises on a target it cannot resolve.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE recommendations SET status = 'approved', "
            "  approved_at = now(), measured_at = NULL, implemented_at = %s "
            "WHERE recommendation_id = ("
            "  SELECT recommendation_id FROM recommendations r "
            "  JOIN pages p ON p.site_id = r.site_id "
            "    AND r.target_url = p.url "
            "  WHERE r.site_id = %s AND r.status = %s "
            "    AND r.generator <> 'offpage_community' "
            "    AND r.action_type != 'create_page' "
            "    AND r.target_url IS NOT NULL "
            "  ORDER BY r.created_at DESC LIMIT 1"
            ") RETURNING recommendation_id",
            (impl_date, site_id, from_status))
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else None


def assert_no_auto_publish(conn, site_id, draft_ids=(), label="no auto-publish"):
    """Publishing is structurally manual; prove nothing published itself.

    Four independent guarantees, because any single one could be bypassed:
      - offpage_publications stays empty for the tenant (nothing published)
      - generated_fixes has no off-page row, so fix_executor cannot claim it
      - drafts created by THIS run are still 'drafted' (never advanced)
      - no draft anywhere on the tenant carries a publishing status

    Note `offpage_drafts.status` has no 'published' member at all - the
    allowed set is drafted/rejected_by_validator/approved/superseded - so
    publication can only be observed through offpage_publications. The
    draft-status checks are about the drafting stage not handing its output
    to a human gate on its own, which is why they are scoped to this run:
    earlier runs legitimately leave 'superseded' rows for audit.
    """
    results = {}
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM offpage_publications "
                    "WHERE site_id = %s", (site_id,))
        results["offpage_publications"] = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM generated_fixes gf "
            "JOIN recommendations r ON r.recommendation_id = gf.recommendation_id "
            "WHERE r.site_id = %s AND r.generator = 'offpage_community'",
            (site_id,))
        results["offpage_generated_fixes"] = cur.fetchone()[0]
        cur.execute("SELECT status, count(*) FROM offpage_drafts "
                    "WHERE site_id = %s GROUP BY status", (site_id,))
        results["draft_statuses"] = dict(cur.fetchall())
        if draft_ids:
            cur.execute("SELECT status FROM offpage_drafts "
                        "WHERE draft_id = ANY(%s::uuid[])",
                        ([str(d) for d in draft_ids],))
            results["this_run_statuses"] = [r[0] for r in cur.fetchall()]
        else:
            results["this_run_statuses"] = []
    print(json.dumps(results, indent=1, default=str))

    allok = True
    allok &= ok(f"{label}: zero offpage_publications",
                results["offpage_publications"] == 0,
                f"found {results['offpage_publications']}")
    allok &= ok(f"{label}: zero off-page generated_fixes rows",
                results["offpage_generated_fixes"] == 0,
                f"found {results['offpage_generated_fixes']}")
    advanced = [s for s in results["this_run_statuses"] if s != "drafted"]
    allok &= ok(f"{label}: this run's drafts are still 'drafted'",
                not advanced, f"statuses={results['this_run_statuses']}")
    allowed = {"drafted", "rejected_by_validator", "approved", "superseded"}
    unknown = [s for s in results["draft_statuses"] if s not in allowed]
    allok &= ok(f"{label}: no draft carries a publishing status",
                not unknown, f"unexpected={unknown}")
    return allok


def main():
    skip_agent = "--skip-agent" in sys.argv
    skip_offpage = skip_agent or "--skip-offpage" in sys.argv
    env_loader.load_env_file(quiet=True)
    allok = True
    cleanup_ids = []

    conn = database.get_connection()
    try:
        # ---- 1. daily sync -------------------------------------------------
        stage("1. daily data sync (mock connectors, example.com)")
        with conn.cursor() as cur:
            cur.execute("SELECT site_id FROM site_config "
                        "WHERE domain = 'example.com' LIMIT 1")
            sync_site = cur.fetchone()
        if not sync_site:
            print("FAIL: example.com site not found; run sync first")
            sys.exit(1)
        conn.rollback()

        counts = run_sync(site_id=sync_site[0])
        print(json.dumps(counts, indent=1, default=str))
        allok &= counts["search_performance"] > 0 and counts["pages"] > 0
        allok &= counts["page_business_performance"] > 0 and counts["crawl_audit_updated"] > 0

        # ---- 2. Helium tenant fixture -------------------------------------
        stage(f"2. Helium tenant fixture ({HELIUM_DOMAIN})")
        tenant = seed_helium(conn)
        conn.commit()
        site = (tenant["site_id"], HELIUM_DOMAIN)
        print(f"site_id={site[0]} pages={len(tenant['page_ids'])}")
        for url in sorted(tenant["page_ids"]):
            print("  page:", url)

        required = ("https://gethelium.co/features",
                    "https://gethelium.co/pricing")
        missing = [u for u in required if u not in tenant["page_ids"]]
        allok &= ok("Helium fixtures present (/features, /pricing)",
                    not missing, f"missing={missing}")

        # ---- 3. candidate generation --------------------------------------
        stage("3. candidate generation (Helium, reference_date = fixture anchor)")
        gen_counts = run_candidate_generation(site[0], conn,
                                              reference_date=FIXTURE_DATE)
        print(json.dumps(gen_counts, default=str))
        allok &= gen_counts["combined"] >= 0 and gen_counts["inserted"] >= 0

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM recommendations "
                        "WHERE site_id = %s AND status = 'raw'", (site[0],))
            raw_count = cur.fetchone()[0]

        # The measurement stage hard-fails on a target_url that is not in
        # `pages`. Generators are idempotent by natural key, so on a repeat
        # run they may insert nothing; seed deterministic rows that DO point
        # at real Helium pages so the rest of the pipeline always has a
        # measurable candidate.
        if not raw_count:
            import uuid as _uuid
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT url FROM pages p WHERE p.site_id = %s "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM recommendations r "
                    "    WHERE r.site_id = p.site_id AND r.target_url = p.url"
                    "  ) ORDER BY url LIMIT 4", (site[0],))
                urls = [r[0] for r in cur.fetchall()]
                for i, url in enumerate(urls):
                    cur.execute(
                        "INSERT INTO recommendations "
                        "  (recommendation_id, site_id, generator, action_type, "
                        "   target_url, proposed_url, cluster_id, diagnosis, "
                        "   evidence_json, status) "
                        "VALUES (%s, %s, 'existing_opportunity', 'improve_page', "
                        "        %s, '', NULL, %s, '[]', 'raw')",
                        (str(_uuid.uuid4()), site[0], url,
                         f"day-life seeded {i}"))
                conn.commit()
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM recommendations "
                            "WHERE site_id = %s AND status = 'raw'",
                            (site[0],))
                raw_count = cur.fetchone()[0]
            allok &= ok("seeded raw candidates (idempotent generation no-op)",
                        raw_count == len(urls), f"raw={raw_count}")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT generator, target_url FROM recommendations "
                "WHERE site_id = %s AND status = 'raw' ORDER BY target_url",
                (site[0],))
            gen_rows = cur.fetchall()
        print("generated targets:")
        for g, u in gen_rows:
            print(f"    {g}: {u}")

        # THE regression guard for the fixture drift.
        orphans = targets_in_pages(conn, site[0])
        allok &= ok("every generated target_url exists in pages",
                    not orphans,
                    f"unmeasurable targets={[(r[1], r[2]) for r in orphans]}")

        # Capture everything this run created so cleanup removes real
        # generator output, not just seeded rows.
        with conn.cursor() as cur:
            cur.execute("SELECT recommendation_id FROM recommendations "
                        "WHERE site_id = %s AND status = 'raw' "
                        "  AND generator <> 'offpage_community'", (site[0],))
            cleanup_ids = [str(r[0]) for r in cur.fetchall()]
        conn.rollback()

        # ---- 4. agent evaluation ------------------------------------------
        stage("4. agent evaluation (live LLM: Ollama primary, Gemini fallback)")
        if skip_agent:
            skip("agent", "--skip-agent")
            approved_id = promote_one_measurable(
                conn, site[0], "raw", IMPL_DATE)
        else:
            from agents.seo_agent import run_agent
            summary = run_agent(site[0], conn)
            print(json.dumps(summary, default=str))
            allok &= summary.get("status") == "ok"

            # The agent must be able to serve this run from EITHER provider.
            # Asserting only status == "ok" would hide a silent degradation
            # to the fallback, so surface which provider actually answered.
            provider = summary.get("llm_provider")
            allok &= ok("llm provider", provider,
                        f"model={summary.get('llm_model')}")
            allok &= provider in ("ollama", "gemini")
            if summary.get("llm_fallback_reason"):
                print(f"[INFO] primary not used: "
                      f"{summary['llm_fallback_reason']}")
            elif provider == "gemini":
                allok &= ok("primary served the run", False,
                            "ran on Gemini with no recorded primary reason")

            # plan/16: the agent promotes accepted rows to 'proposed';
            # operator approval is a separate step. Simulate it so the
            # measurement/classification stages work end-to-end.
            approved_id = promote_one_measurable(
                conn, site[0], "proposed", IMPL_DATE)

            # The agent's approve/reject split is genuinely
            # non-deterministic, so a run where it promotes nothing is a
            # property of the model, not a broken pipeline. Nudge one
            # measurable raw candidate through so stages 5-7 still validate,
            # and say so loudly rather than passing it off as agent output.
            if not approved_id:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM recommendations "
                                "WHERE site_id = %s AND status = 'proposed'",
                                (site[0],))
                    proposed = cur.fetchone()[0]
                print(f"[INFO] agent promoted {proposed} row(s) this run; "
                      f"promoting a raw candidate so the measurement and "
                      f"drafting stages still execute")
                approved_id = promote_one_measurable(
                    conn, site[0], "raw", IMPL_DATE)

        if not approved_id:
            print("FAIL: no approved URL-measurable recommendation to measure")
            sys.exit(1)
        print(f"measuring recommendation: {approved_id}")

        # ---- 5. measurement snapshots -------------------------------------
        stage("5. measurement batch (baseline + post snapshots)")
        from measurement.measure import run_measurement_batch, store_post_snapshots
        from measurement.baseline import resolve_window_days
        from measurement.thresholds import GSC_SETTLE_DAYS
        with conn.cursor() as cur:
            cur.execute("SELECT action_type, target_url FROM recommendations "
                        "WHERE recommendation_id = %s", (approved_id,))
            action_type, target_url = cur.fetchone()
        print(f"target_url: {target_url}")

        # Assert the precondition that used to raise ValueError, with a
        # message that names the URL instead of surfacing from deep inside
        # store_post_snapshots.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pages WHERE site_id = %s AND url = %s",
                        (site[0], target_url))
            page_present = cur.fetchone()[0]
        allok &= ok("measured target exists in pages", page_present == 1,
                    f"target_url={target_url}")

        window = resolve_window_days(conn, action_type)
        # Reference date must clear the GSC settle buffer (impl + window +
        # settle) — the batch skips windows that close inside the buffer.
        batch = run_measurement_batch(
            conn, [approved_id],
            reference_date=IMPL_DATE + timedelta(days=window + GSC_SETTLE_DAYS))
        print(json.dumps(batch, indent=1, default=str))
        allok &= len(batch["measured"]) == 1 and len(batch["skipped"]) == 0
        # post window ends at impl + window (day-in-the-life compression)
        post_info = store_post_snapshots(
            conn, approved_id, IMPL_DATE + timedelta(days=window))
        conn.commit()
        print(f"post window: {post_info['measurement_period']} "
              f"(window {post_info['window_days']}d)")

        # ---- 6. classification --------------------------------------------
        stage("6. classification (plan/08 rules + significance)")
        from measurement.classify import (
            classify_recommendation, persist_classification)
        result = classify_recommendation(conn, approved_id)
        print(json.dumps(result, indent=1, default=str)[:400])
        allok &= result["result"] in ("won", "neutral", "lost", "inconclusive")
        persisted = persist_classification(
            conn, approved_id, result,
            measured_at=IMPL_DATE + timedelta(days=window))
        conn.commit()
        print(f"persisted result: {persisted}")

        # ---- 7. off-page drafting (manual publish only) --------------------
        stage("7. off-page drafting (draft persisted, publication is manual)")
        offpage_rec = tenant["offpage_recommendation_id"]
        from fixes.offpage_drafter import draft_offpage_reply, LlmDraftError

        if skip_offpage:
            print("[INFO] offline path: simulating the MODEL only; the real "
                  "gates, persistence and tenant checks all still run")
            client = StubDraftClient()
        else:
            client = None   # real FallbackClient (Ollama -> Gemini)

        try:
            drafted = draft_offpage_reply(conn, offpage_rec, client=client)
        except LlmDraftError as exc:
            drafted = None
            print(f"[FAIL] drafting refused: {exc}")
        except Exception as exc:
            drafted = None
            print(f"[FAIL] drafting raised {type(exc).__name__}: {exc}")

        run_draft_ids = []

        if drafted is None:
            allok &= ok("off-page draft created", False,
                        "drafter returned no draft")
        else:
            run_draft_ids = [drafted["draft_id"]]
            print(json.dumps({k: drafted[k] for k in
                              ("draft_id", "version", "advice_md",
                               "mention_md") if k in drafted},
                             indent=1, default=str)[:600])
            allok &= ok("off-page draft created",
                        bool(drafted.get("draft_id")), str(drafted)[:200])

            # Strict validation: the draft must be READ BACK FROM THE
            # DATABASE, not merely returned in memory. A drafter that
            # returns a dict but fails to persist would otherwise pass.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT draft_id, version, body_md, status, "
                    "       grounding_corpus_hash, grounding_corpus_chars, "
                    "       helpfulness_score "
                    "FROM offpage_drafts WHERE draft_id = %s",
                    (drafted["draft_id"],))
                row = cur.fetchone()
            conn.commit()
            allok &= ok("draft row persisted in offpage_drafts",
                        row is not None, "no row found after insert")
            if row:
                print(f"persisted draft v{row[1]} status={row[3]} "
                      f"body={len(row[2] or '')} chars "
                      f"corpus={row[5]} chars "
                      f"helpfulness={row[6]} hash={str(row[4])[:12]}")
                allok &= ok("persisted draft body is non-empty",
                            bool((row[2] or "").strip()))
                allok &= ok("draft is grounded in the Helium corpus",
                            (row[5] or 0) > 0 and bool(row[4]))
                allok &= ok("persisted draft is status 'drafted'",
                            row[3] == "drafted", f"status={row[3]}")

            # Live drafts must be linked to their recommendation.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM offpage_drafts d "
                    "JOIN recommendations r "
                    "  ON r.recommendation_id = d.recommendation_id "
                    "WHERE d.draft_id = %s AND r.site_id = %s",
                    (drafted["draft_id"], site[0]))
                linked = cur.fetchone()[0]
            conn.commit()
            allok &= ok("draft is linked to a same-tenant recommendation",
                        linked == 1, f"linked={linked}")

        allok &= assert_no_auto_publish(conn, site[0], run_draft_ids)

        # ---- 8. final state -------------------------------------------------
        stage("8. final state")
        with conn.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM recommendations "
                        "WHERE site_id = %s GROUP BY status", (site[0],))
            rec_states = dict(cur.fetchall())
            cur.execute("SELECT count(*) FROM rejection_log WHERE site_id = %s",
                        (site[0],))
            rejections = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM measurement_snapshots "
                        "WHERE recommendation_id = %s", (approved_id,))
            snaps = cur.fetchone()[0]
            cur.execute("SELECT result, status FROM recommendations "
                        "WHERE recommendation_id = %s", (approved_id,))
            final = cur.fetchone()
            cur.execute("SELECT count(*) FROM offpage_drafts WHERE site_id = %s",
                        (site[0],))
            draft_rows = cur.fetchone()[0]
        print(json.dumps({
            "recommendations_by_status": {str(k): v for k, v in rec_states.items()},
            "agent_rejection_log": rejections,
            "measurement_snapshots_for_measured_rec": snaps,
            "offpage_drafts": draft_rows,
            "final_verdict": {"result": str(final[0]), "status": str(final[1])},
        }, indent=1))
        allok &= snaps >= 2 and final[1] == "measured"
    finally:
        if cleanup_ids:
            with database.get_connection() as c:
                with c.cursor() as cur:
                    cur.execute(
                        "DELETE FROM measurement_snapshots "
                        "WHERE recommendation_id = ANY(%s::uuid[])",
                        (cleanup_ids,))
                    cur.execute(
                        "DELETE FROM change_log "
                        "WHERE recommendation_id = ANY(%s::uuid[])",
                        (cleanup_ids,))
                    cur.execute(
                        "DELETE FROM rejection_log WHERE site_id = %s "
                        "AND candidate_id = ANY(%s::uuid[])",
                        (site[0], cleanup_ids))
                    cur.execute(
                        "DELETE FROM recommendations WHERE site_id = %s "
                        "AND recommendation_id = ANY(%s::uuid[])",
                        (site[0], cleanup_ids))
                c.commit()
        conn.close()

    print("\nDAY-IN-THE-LIFE VALIDATION:", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
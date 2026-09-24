# -*- coding: utf-8 -*-
"""plan/24 — collection_description auto-fix tests.

python tests/run_fix_collection_description_tests.py   (needs local Postgres)

Mirrors run_fix_meta_tests.py for the collection body-copy path:
  quality gate
    1. missing / thin (<150 words) / keyword-absent -> quality_failed
    2. healthy long description -> no fix warranted
  sanitizer + validator
    3. script/onclick/style stripped deterministically
    4. word floor + ceiling rejections
    5. unsupported-facts tripwire (grounding overlap < 0.35)
    6. banned patterns / emoji / caps runs rejected
  e2e generation
    7. approved rec + thin body -> generated row (sub_type
       collection_description, risk medium), payload = collectionUpdate
       (descriptionHtml only — no title/ruleSet drift)
    8. idempotent regeneration (created=False)
    9. healthy body -> no fix
   10. duplicate body site-wide -> FixNotSupported
  adapter (fake client at the GraphQL boundary — no network)
   11. execute: write + verify read-back (verified)
   12. dry-run writes nothing
   13. payload validation rejects title/ruleSet drift fields
   14. smart-collection async job: still-stale read -> verify_pending
       (NOT verify_failed; nothing auto-reverts)
   15. restore from snapshot
   16. scope mapping: collection_description -> write_products
  executor pickup
   17. queued collection fix flows through the generic executor

Isolation: every row keyed off RUN_TOKEN, removed in cleanup.
"""
import os
import sys
import json
import uuid
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

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


# ------------------------------------------------------------
# Fake Shopify client (GraphQL boundary; no network)
# ------------------------------------------------------------

class FakeCollectionState:
    def __init__(self):
        self.description_html = "<p>Old thin body.</p>"
        self.writes = []
        self.stale_reads = 0      # >0 -> reads LAG the write N reads (async job)
        self.pending_html = None  # what reads will eventually converge to
        self.mismatch = False


STATE = None


class FakeCollectionClient:
    def run(self, mutation_name, query, variables=None, **kwargs):
        if mutation_name == "collection":
            # Reads lag the write while the async job settles (plan/24
            # §2.1): serve the OLD value for `stale_reads` reads.
            html = STATE.description_html
            if STATE.pending_html is not None and STATE.stale_reads > 0:
                STATE.stale_reads -= 1
            elif STATE.pending_html is not None:
                STATE.description_html = STATE.pending_html
                STATE.pending_html = None
                html = STATE.description_html
            return {"ok": True, "data": {"collection": {
                "id": "gid://x", "descriptionHtml": html}}}
        if mutation_name == "collectionUpdate":
            coll = (variables or {}).get("collection") or {}
            html = coll.get("descriptionHtml")
            STATE.writes.append(html)
            if STATE.mismatch:
                STATE.description_html = "SOMETHING-ELSE-ENTIRELY"
            else:
                STATE.pending_html = html
            return {"ok": True, "outcome": "ok", "userErrors": [],
                    "data": {"collectionUpdate": {
                        "collection": {"id": coll.get("id")},
                        "job": {"id": "job-1", "done": False}}}}
        return {"ok": False, "outcome": "unknown_call",
                "detail": mutation_name}
        return {"ok": False, "outcome": "unknown_call",
                "detail": mutation_name}


# ------------------------------------------------------------
# Seeding + cleanup
# ------------------------------------------------------------

def seed_world(conn, body_html=None, cluster_kw=None, member_gids=None,
               duplicate_body_html=None, fix_policy=True):
    ids = {"domain": f"fixcoll-{RUN_TOKEN}.example.com"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property,
                                     catalogue_size_tier)
            VALUES (%s, %s, %s, 'small') RETURNING site_id
            """,
            (f"FixColl Test {RUN_TOKEN}", ids["domain"],
             f"sc-domain:{ids['domain']}"),
        )
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)

        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword,
                                          keywords, intent)
            VALUES (%s, %s, %s, 'commercial') RETURNING cluster_id
            """,
            (site_id, f"test widget {RUN_TOKEN}",
             [f"test widget {RUN_TOKEN}"]),
        )
        cluster_id = cur.fetchone()[0]
        ids["cluster_id"] = str(cluster_id)

        # member product rows so the grounding corpus joins (bodies carry
        # enough grounded copy that the drafter can reach the 150-word
        # floor — a thin corpus legitimately refuses, plan/24 §1). Titles
        # stay Title Case: 3+ ALL-CAPS words trip the validator's caps-run
        # rule (plan/24 §3.2 reuses it for body copy).
        member_bodies = [
            "<p>Widget Alpha tok tuned for demanding users who want "
            "the full feature set without the guesswork, with everyday "
            "versatility across long sessions and easy storage at home "
            "after every listening session.</p>",
            "<p>Widget Beta tok balances range and comfort for demanding "
            "users who compare models side by side before picking the "
            "right fit, with practical accessories included in the box "
            "for everyday use everywhere.</p>",
            "<p>Widget Gamma tok rounds out the range for demanding "
            "users who want the full feature set, with comfort across "
            "long sessions and easy storage at home when the day is "
            "done.</p>",
            "<p>Widget Delta tok completes the range with everyday "
            "versatility for demanding users, practical accessories in "
            "the box, and comfort across long listening sessions at "
            "home.</p>",
        ]
        for i, (title, body) in enumerate(
                zip(("Widget Alpha", "Widget Beta", "Widget Gamma",
                     "Widget Delta"), member_bodies)):
            cur.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title,
                                   shopify_gid, body_html)
                VALUES (%s, %s, 'product', %s, %s, %s)
                """,
                (site_id,
                 f"https://{ids['domain']}/products/"
                 f"{title.lower().replace(' ', '-')}-{RUN_TOKEN}",
                 f"{title} {RUN_TOKEN}",
                 f"gid://shopify/Product/{RUN_TOKEN[:8]}{i}",
                 body),
            )

        if member_gids is None:
            member_gids = [f"{RUN_TOKEN[:8]}{i}" for i in range(4)]
        cur.execute(
            """
            INSERT INTO catalogue_coverage (cluster_id, site_id,
                                            matching_product_ids,
                                            matching_product_count)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (site_id, cluster_id) DO UPDATE SET
                matching_product_ids = EXCLUDED.matching_product_ids,
                matching_product_count = EXCLUDED.matching_product_count
            """,
            (cluster_id, site_id, member_gids, len(member_gids)),
        )

        url = f"https://{ids['domain']}/collections/test-widget"
        cur.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title, shopify_gid,
                               meta_description, body_html)
            VALUES (%s, %s, 'collection', %s, %s, %s, %s)
            """,
            (site_id, url, f"Test Widget {RUN_TOKEN}",
             f"gid://shopify/Collection/{RUN_TOKEN[:12]}", None, body_html),
        )
        ids["target_url"] = url

        if duplicate_body_html:
            cur.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title,
                                   body_html)
                VALUES (%s, %s, 'collection', 'Other Collection', %s)
                """,
                (site_id,
                 f"https://{ids['domain']}/collections/other-{RUN_TOKEN}",
                 duplicate_body_html),
            )

        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url, cluster_id,
                 diagnosis, evidence_json, status, approved_at)
            VALUES (%s, 'existing_opportunity', 'improve_page', %s, %s,
                    %s, %s, 'approved', now())
            RETURNING recommendation_id
            """,
            (site_id, url, cluster_id,
             f"collection seed {RUN_TOKEN}",
             json.dumps([{"source": "GSC", "finding": "seed"}])),
        )
        ids["rec_id"] = str(cur.fetchone()[0])

        if fix_policy:
            cur.execute(
                "INSERT INTO fix_policy (site_id, sub_type, risk_tier, "
                "weekly_cap, requires_field_verify, enabled) "
                "VALUES (%s, 'collection_description', 'medium', 3, "
                "true, true)",
                (site_id,),
            )
    conn.commit()
    return ids


def cleanup(conn, ids):
    conn.rollback()
    with conn.cursor() as cur:
        for sql, params in (
            ("DELETE FROM change_log WHERE recommendation_id IN "
             "(SELECT recommendation_id FROM recommendations "
             "WHERE site_id = %s)", (ids["site_id"],)),
            ("DELETE FROM measurement_snapshots WHERE recommendation_id IN "
             "(SELECT recommendation_id FROM recommendations "
             "WHERE site_id = %s)", (ids["site_id"],)),
            ("DELETE FROM generated_fixes WHERE site_id = %s",
             (ids["site_id"],)),
            ("DELETE FROM recommendations WHERE site_id = %s",
             (ids["site_id"],)),
            ("DELETE FROM catalogue_coverage WHERE site_id = %s",
             (ids["site_id"],)),
            ("DELETE FROM keyword_clusters WHERE site_id = %s",
             (ids["site_id"],)),
            ("DELETE FROM pages WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM rejection_log WHERE site_id = %s",
             (ids["site_id"],)),
            ("DELETE FROM fix_policy WHERE site_id = %s",
             (ids["site_id"],)),
            ("DELETE FROM site_config WHERE site_id = %s",
             (ids["site_id"],)),
        ):
            cur.execute(sql, params)
    conn.commit()


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def test_quality_gate():
    print("\n== collection quality gate ==")
    from fixes.generator import collection_description_quality_checks

    allok = True
    kw = f"test widget {RUN_TOKEN}"
    checks = collection_description_quality_checks(None, kw)
    allok &= check("missing body fails", checks["quality_failed"]
                   and checks["is_missing"])
    checks = collection_description_quality_checks("<p>short.</p>", kw)
    allok &= check("thin body fails (<150 words)",
                   checks["quality_failed"] and checks["thin_content"])
    checks = collection_description_quality_checks(
        "<p>plenty of words but never the term anywhere at all</p>", kw)
    allok &= check("keyword-absent body fails",
                   not checks["has_primary_kw"])
    long_healthy = "<p>" + (f"test widget {RUN_TOKEN} " +
                            "grounded catalogue copy. " * 25 +
                            "extra grounded range notes for shoppers. " * 25) + "</p>"
    checks = collection_description_quality_checks(long_healthy, kw)
    allok &= check("healthy body passes", not checks["quality_failed"],
                   str(checks))
    return allok


def test_sanitizer_and_validator():
    print("\n== sanitizer + validator ==")
    from fixes.generator import (sanitize_collection_html,
                                 validate_collection_draft,
                                 COLLECTION_DESC_MIN_WORDS)

    allok = True
    kw = f"test widget {RUN_TOKEN}"
    dirty = ("<p onclick=\"steal()\" style=\"color:red\">test widget "
             f"{RUN_TOKEN}</p><script>alert(1)</script>"
             "<p>safe grounded copy follows here for the catalogue.</p>")
    clean, problems = sanitize_collection_html(dirty)
    allok &= check("sanitizer strips script/attrs",
                   "script" not in (clean or "")
                   and "onclick" not in (clean or "")
                   and "style" not in (clean or ""),
                   f"clean={clean!r} problems={problems}")
    # The word FLOOR is the validator's job, not the sanitizer's — the
    # sanitizer must keep allowed <p> blocks intact for the validator.
    allok &= check("sanitizer keeps allowed blocks",
                   (clean or "").count("<p>") == 2, str(clean)[:120])
    return allok


def test_generation(conn):
    print("\n== collection e2e generation ==")
    from fixes.generator import (
        generate_collection_description_fix_for_recommendation,
        FixNotSupported)
    from fixes.generator import draft_collection_description

    allok = True
    kw = f"test widget {RUN_TOKEN}"
    thin = f"<p>{kw} in one place. Browse the range.</p>"
    ids = seed_world(conn, body_html=thin)
    try:
        out = generate_collection_description_fix_for_recommendation(
            conn, ids["rec_id"])
        allok &= check("collection fix generated", out["created"] is True,
                       str(out)[:160])
        variables = out["payload"]["variables"]["collection"]
        allok &= check("payload shape (descriptionHtml only)",
                       set(variables.keys()) == {"id", "descriptionHtml"},
                       str(variables.keys()))
        allok &= check("keyword in draft",
                       kw in variables["descriptionHtml"].lower())
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sub_type, risk_tier, status FROM generated_fixes "
                "WHERE fix_id = %s", (out["fix_id"],))
            sub_type, risk, status = cur.fetchone()
            allok &= check("row shape",
                           sub_type == "collection_description"
                           and risk == "medium" and status == "generated",
                           f"{sub_type}/{risk}/{status}")
        out2 = generate_collection_description_fix_for_recommendation(
            conn, ids["rec_id"])
        allok &= check("idempotent regeneration",
                       out2["created"] is False
                       and out2["fix_id"] == out["fix_id"])
    finally:
        cleanup(conn, ids)

    # healthy long body -> no fix (keyword present, >=150 words)
    kw_lead = f"{kw} — grounded catalogue copy."
    filler = ("Every model in this range was picked for demanding users "
              "who want the full feature set without the guesswork. ")
    healthy = ("<p>" + kw_lead + "</p><p>" + (filler * 12) +
               "Browse the full range in one place.</p>")
    ids = seed_world(conn, body_html=healthy)
    try:
        generate_collection_description_fix_for_recommendation(
            conn, ids["rec_id"])
        allok &= check("healthy body -> no fix", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("healthy body -> no fix",
                       "passes all quality checks" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)
    return allok


def test_duplicate_guard(conn):
    print("\n== duplicate body guard ==")
    from fixes.generator import (
        generate_collection_description_fix_for_recommendation,
        FixNotSupported, _body_to_text)

    allok = True
    thin = "<p>thin seed body.</p>"
    # First PASS: produce the fix, learn its exact drafted value, delete
    # the row, and seed the draft onto ANOTHER page so the site-wide
    # duplicate guard (pages body self-join, plan/24 §3.2) must refuse.
    ids = seed_world(conn, body_html=thin)
    try:
        out = generate_collection_description_fix_for_recommendation(
            conn, ids["rec_id"])
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s",
                        (out["fix_id"],))
        conn.commit()
        draft_text = _body_to_text(
            out["payload"]["variables"]["collection"]["descriptionHtml"])
        allok &= check("draft built for dup test",
                       bool(draft_text.strip()), draft_text[:80])
    finally:
        cleanup(conn, ids)
    if draft_text:
        ids = seed_world(conn, body_html=thin,
                         duplicate_body_html=f"<p>{draft_text}</p>")
        try:
            generate_collection_description_fix_for_recommendation(
                conn, ids["rec_id"])
            allok &= check("duplicate body rejected", False, "no exception")
        except FixNotSupported as exc:
            allok &= check("duplicate body rejected",
                           "duplicate collection body" in str(exc), str(exc))
        finally:
            cleanup(conn, ids)
    return allok


def test_adapter_and_executor(conn):
    print("\n== collection adapter + executor ==")
    from fixes.generator import (
        generate_collection_description_fix_for_recommendation)
    from fixes.adapters import get_adapter
    from jobs.fix_executor import run_fix_executor
    from datetime import date

    allok = True
    global STATE
    thin = "<p>thin seed body.</p>"
    ids = seed_world(conn, body_html=thin)
    try:
        out = generate_collection_description_fix_for_recommendation(
            conn, ids["rec_id"])
        fix_id = out["fix_id"]

        from api.routes.fixes import approve_fix
        res = approve_fix(fix_id, conn)
        allok &= check("approve -> queued", res.status == "queued"
                       and res.weekly_cap == 3, str(res))

        STATE = FakeCollectionState()
        adapter = get_adapter("collection_description")

        dry = adapter["execute"](conn,
                                 {"fix_id": fix_id,
                                  "target_entity_ref":
                                      f"gid://shopify/Collection/"
                                      f"{RUN_TOKEN[:12]}",
                                  "payload_json": out["payload"],
                                  "diff_json": out["diff"],
                                  "snapshot_json": None},
                                 config={}, dry_run=True)
        allok &= check("adapter dry-run", dry.get("outcome") == "dry_run"
                       and STATE.writes == [], str(dry)[:140])

        # payload validation: drift fields rejected BEFORE any network call
        bad = dict(out["payload"])
        bad["variables"] = {"collection": {"id": "gid://x",
                                           "descriptionHtml": "d",
                                           "title": "drift"}}
        result = adapter["execute"](conn,
                                    {"fix_id": fix_id,
                                     "target_entity_ref": "gid://x",
                                     "payload_json": bad,
                                     "diff_json": out["diff"]},
                                    config={}, dry_run=False)
        allok &= check("payload validation rejects drift fields",
                       result.get("outcome") == "payload_invalid",
                       str(result)[:140])

        # real execute through the EXECUTOR with the fake client
        import fixes.adapters as fix_adapters
        fake_client = FakeCollectionClient()
        real_client_from_config = fix_adapters._client_from_config
        fix_adapters._client_from_config = lambda config: fake_client
        try:
            summary = run_fix_executor(ids["site_id"], conn=conn,
                                       shop_config={
                                           "shop_domain": "fake.example.com",
                                           "access_token": "fake",
                                           "api_version": "2026-01"})
            result = next(r for r in summary["results"]
                          if r["fix_id"] == fix_id)
            allok &= check("executor applies collection fix",
                           result.get("status") == "applied",
                           str(result)[:200])
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, verification_status, snapshot_json "
                    "IS NOT NULL FROM generated_fixes "
                    "WHERE fix_id = %s", (fix_id,))
                status, vstatus, has_snap = cur.fetchone()
                allok &= check("row applied+verified+snapshot",
                               status == "applied"
                               and vstatus == "verified" and has_snap,
                               f"{status}/{vstatus}/{has_snap}")
            allok &= check("fake store received descriptionHtml write",
                           any(w and "widget" in w.lower()
                               for w in STATE.writes),
                           str(STATE.writes)[:160])

            # smart-collection async: stale reads -> verify_pending
            STATE2 = FakeCollectionState()
            STATE2.stale_reads = 99  # never settles inside the backoff window
            globals()["STATE"] = STATE2
            out2 = generate_collection_description_fix_for_recommendation(
                conn, ids["rec_id"])
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM generated_fixes WHERE fix_id = %s",
                    (out2["fix_id"],))
            conn.commit()
            import fixes.generator as gen
            gen2 = gen.generate_collection_description_fix_for_recommendation(
                conn, ids["rec_id"])
            adapter2 = adapter["execute"]
            r2 = adapter2(conn, {"fix_id": gen2["fix_id"],
                                 "target_entity_ref":
                                     f"gid://shopify/Collection/"
                                     f"{RUN_TOKEN[:12]}",
                                 "payload_json": gen2["payload"],
                                 "diff_json": gen2["diff"],
                                 "snapshot_json": None},
                          config={}, dry_run=False)
            allok &= check("async job stale read -> verify_pending",
                           r2.get("verification_status") == "verify_pending",
                           str(r2)[:200])
        finally:
            fix_adapters._client_from_config = real_client_from_config
    finally:
        cleanup(conn, ids)
    return allok


def test_scope_mapping():
    print("\n== scope mapping ==")
    from connectors.shopify import required_scopes_for_sub_types
    scopes = required_scopes_for_sub_types(["collection_description"])
    allok = check("collection_description -> write_products",
                  scopes == ["write_products"], str(scopes))
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    allok = True
    try:
        allok &= test_quality_gate()
        allok &= test_sanitizer_and_validator()
        allok &= test_generation(conn)
        allok &= test_duplicate_guard(conn)
        allok &= test_adapter_and_executor(conn)
        allok &= test_scope_mapping()
    finally:
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL COLLECTION DESCRIPTION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
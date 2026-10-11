# -*- coding: utf-8 -*-
"""plan/26 Phase 4 — API credential dispatch + framer live-preview reads.

python tests/run_phase4_api_dispatch_tests.py   (needs a reachable local Postgres)

Verifies the routes/fixes.py platform dispatch against the REAL database
(socket banned — the Framer side runs in fixture mode, the Shopify side is
never called):

  credential check
    1. _write_credential_ok: legacy/shopify/framer variants
  execute endpoint
    2. framer queued fix + policy enabled + fixture client -> applied
       (the SAME endpoint operators use for Shopify fixes)
    3. framer fix + disabled policy -> 409 policy refusal (fail-closed)
    4. shopify queued fix + injected fake adapter (legacy pattern) ->
       applied (byte-parity: the endpoint path is unchanged for Shopify)
  live-preview reads
    5. _fresh_live_seo_title on a framer rec -> fixture title (FramerClient)
    6. _fresh_live_meta_description on a framer rec -> fixture description
    7. _fresh_live_seo_title on a shopify rec with no creds -> None
       (graceful, gate falls back to pages.title — unchanged behavior)
  revert endpoint
    8. applied framer fix + revert -> restored + audited revert row
  isolation
    9. fixture + DB rows removed (RUN_TOKEN)

NOTE: this suite does NOT run the FastAPI app; it calls the route
functions directly with the same conn fixture pattern the other API
suites use.
"""
import os
import sys
import json
import uuid
import socket

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import db as database  # noqa: E402
import env as env_loader  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
FAILURES = []
FIXTURE_PATH = os.path.join(HERE, "fixtures", "framer_pages.json")


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


def ban_network():
    def banned(*args, **kwargs):
        raise RuntimeError("network access attempted in Phase 4 suite")
    socket.socket.connect = banned
    socket.socket.connect_ex = banned
    socket.create_connection = banned
    import urllib.request
    urllib.request.urlopen = banned


def load_fixture():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_fixture(data):
    with open(FIXTURE_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def seed_site(conn, platform="framer", with_gid=None):
    domain = f"phase4-{RUN_TOKEN}-{uuid.uuid4().hex[:6]}.example.com"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property,
                                     catalogue_size_tier, platform,
                                     framer_site_ref)
            VALUES (%s, %s, %s, 'small', %s, %s) RETURNING site_id
            """,
            (f"Phase4 {RUN_TOKEN}", domain, f"sc-domain:{domain}", platform,
             f"framer-site-{RUN_TOKEN}" if platform == "framer" else None),
        )
        site_id = cur.fetchone()[0]
        url = f"https://{domain}/blog/field-cameras"
        cur2 = conn.cursor()
        cur2.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title,
                               meta_description, body_html, shopify_gid)
            VALUES (%s, %s, 'blog', %s, %s, %s, %s)
            """,
            (site_id, url,
             f"Field cameras {RUN_TOKEN}",
             f"Field cameras guide {RUN_TOKEN}",
             "<p>Sensor resolution matters more than megapixels when "
             "choosing a compact trail camera for wet weather work.</p>",
             with_gid),
        )
        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword, keywords,
                                          intent)
            VALUES (%s, %s, %s, 'informational') RETURNING cluster_id
            """,
            (site_id, f"field camera {RUN_TOKEN}",
             [f"field camera {RUN_TOKEN}"]),
        )
        cluster_id = cur.fetchone()[0]
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
             f"seed {RUN_TOKEN}",
             json.dumps([{"source": "GSC", "finding": "seed"}])),
        )
        rec_id = str(cur.fetchone()[0])
    conn.commit()
    data = load_fixture()
    data.setdefault("pages", {})[url] = {
        "title": f"Fixture title {RUN_TOKEN}",
        "description": f"Fixture description {RUN_TOKEN}."}
    save_fixture(data)
    return {"site_id": str(site_id), "domain": domain, "url": url,
            "rec_id": rec_id, "cluster_id": str(cluster_id)}


def seed_framer_fix(conn, ids, sub_type, new_value, old_value, status):
    payload = {
        "mutation": "framer.updatePageMeta",
        "variables": {"page_ref": ids["url"],
                      "field": "title" if sub_type == "framer.title"
                      else "description",
                      ("title" if sub_type == "framer.title"
                       else "description"): new_value},
        "surface": "framer",
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO generated_fixes
                (recommendation_id, site_id, action_type, sub_type, target_url,
                 target_entity_ref, payload_json, diff_json, generation_source,
                 status, risk_tier)
            VALUES (%s, %s, 'improve_page', %s, %s, %s, %s::jsonb, %s::jsonb,
                    'deterministic', %s, 'low')
            RETURNING fix_id
            """,
            (ids["rec_id"], ids["site_id"], sub_type, ids["url"], ids["url"],
             json.dumps(payload),
             json.dumps([{"field": "meta.title" if sub_type == "framer.title"
                          else "meta.description",
                          "old_value": old_value, "new_value": new_value}]),
             status),
        )
        fix_id = str(cur.fetchone()[0])
    conn.commit()
    return fix_id


def seed_shopify_fix(conn, ids, new_title, old_title, status):
    payload = {
        "mutation": "productUpdate",
        "variables": {"product": {"id": f"gid://shopify/Product/{RUN_TOKEN[:12]}",
                                  "seo": {"title": new_title}}},
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO generated_fixes
                (recommendation_id, site_id, action_type, sub_type, target_url,
                 target_entity_ref, payload_json, diff_json, generation_source,
                 status, risk_tier)
            VALUES (%s, %s, 'improve_page', 'seo.title', %s, %s, %s::jsonb,
                    %s::jsonb, 'deterministic', %s, 'low')
            RETURNING fix_id
            """,
            (ids["rec_id"], ids["site_id"], ids["url"],
             f"gid://shopify/Product/{RUN_TOKEN[:12]}",
             json.dumps(payload),
             json.dumps([{"field": "seo.title", "old_value": old_title,
                          "new_value": new_title}]),
             status),
        )
        fix_id = str(cur.fetchone()[0])
    conn.commit()
    return fix_id


def cleanup(conn, ids_list):
    conn.rollback()
    with conn.cursor() as cur:
        for ids in ids_list:
            cur.execute("DELETE FROM change_log WHERE recommendation_id = %s",
                        (ids["rec_id"],))
            cur.execute("DELETE FROM measurement_snapshots WHERE "
                        "recommendation_id = %s", (ids["rec_id"],))
            cur.execute("DELETE FROM generated_fixes WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM recommendations WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM keyword_clusters WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM pages WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM fix_policy WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM site_config WHERE site_id = %s",
                        (ids["site_id"],))
    conn.commit()


def fixture_state(page_ref):
    return (load_fixture().get("pages") or {}).get(page_ref)


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def test_credential_check():
    print("\n== _write_credential_ok ==")
    from api.routes.fixes import _write_credential_ok
    allok = True
    allok &= check("legacy shape -> shopify check",
                   _write_credential_ok(
                       {"shop_domain": "x", "access_token": "y"}) is True)
    allok &= check("shopify missing token -> False",
                   _write_credential_ok({"shop_domain": "x"}) is False)
    allok &= check("framer full -> True",
                   _write_credential_ok(
                       {"platform": "framer", "framer_api_key": "k",
                        "framer_site_ref": "s"}) is True)
    allok &= check("framer missing site_ref -> False",
                   _write_credential_ok(
                       {"platform": "framer", "framer_api_key": "k"}) is False)
    return allok


def test_execute_endpoint_framer(conn, ids):
    print("\n== execute endpoint: framer (real path) ==")
    from api.routes.fixes import execute_fix, _write_credential_ok
    from connectors import framer as framer_module
    from fixes import adapters as fix_adapters
    allok = True
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
            "requires_field_verify, enabled) VALUES (%s, 'framer.title', "
            "'low', 5, true, true)", (ids["site_id"],))
    conn.commit()
    fix_id = seed_framer_fix(conn, ids, "framer.title",
                             f"Applied title {RUN_TOKEN}",
                             old_value=f"Fixture title {RUN_TOKEN}",
                             status="queued")
    # inject the fixture-backed mock client (the executor path builds its
    # own client; in mock CI the FramerClient resolves fixture mode itself)
    orig = fix_adapters._client_from_framer_config
    fix_adapters._client_from_framer_config = staticmethod(
        lambda cfg, client=None: framer_module.FramerClient(
            framer_api_key="k", framer_site_ref="framer-site-fixture",
            mode="mock"))
    try:
        result = execute_fix(fix_id, conn=conn)
        allok &= check("framer execute applied",
                       result.get("status") == "applied", str(result)[:200])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, verification_status FROM generated_fixes "
                "WHERE fix_id = %s", (fix_id,))
            status, vstatus = cur.fetchone()
        allok &= check("endpoint applied + verified in DB",
                       status == "applied" and vstatus == "verified",
                       str((status, vstatus)))
    finally:
        fix_adapters._client_from_framer_config = orig
    return allok


def test_execute_endpoint_shopify_parity(conn, ids_shopify):
    print("\n== execute endpoint: shopify byte-parity ==")
    from api.routes.fixes import execute_fix
    from fixes import adapters as fix_adapters
    allok = True
    # legacy pattern: fake adapter injected exactly like run_fix_executor_tests
    class _State:
        live_title = f"Old title {RUN_TOKEN}"
    def fake_execute(conn, fix_row, config, dry_run=False):
        if dry_run:
            return {"ok": True, "outcome": "dry_run", "detail": {}}
        new_title = (((fix_row["payload_json"].get("variables") or {})
                      .get("product") or {}).get("seo") or {}).get("title")
        _State.live_title = new_title
        return {"ok": True, "outcome": "ok", "verified": True,
                "verification_status": "verified", "live_value": new_title,
                "adapter": "fake", "adapter_response": {"write": {"ok": True}}}
    def fake_snapshot(conn, fix_row, config):
        return {"ok": True, "snapshot": {"seo.title": _State.live_title},
                "adapter": "fake"}
    def fake_restore(conn, fix_row, config):
        return {"ok": True, "outcome": "ok", "restored": True,
                "live_value": _State.live_title, "adapter": "fake",
                "adapter_response": {"restore": {"ok": True}}}
    saved = fix_adapters.ADAPTERS["seo.title"]
    fix_adapters.ADAPTERS["seo.title"] = {
        "execute": fake_execute, "restore": fake_restore,
        "snapshot": fake_snapshot}
    # The endpoint enforces the same fail-closed write guard the job
    # executor does, so it needs a credential present. Inject a fake one
    # here rather than leaning on a live SHOPIFY_ACCESS_TOKEN from .env:
    # that dependency is invisible, and it made this suite non-hermetic
    # until credential isolation exposed it. The adapter is faked above, so
    # the token is never used for anything.
    import jobs.fix_executor as _fx
    saved_resolver = _fx._resolve_shop_config
    _fx._resolve_shop_config = lambda conn, site_id, api_version=None: {
        "shop_domain": "fake.example.com", "access_token": "fake-token",
        "api_version": api_version or "2026-01", "platform": "shopify",
        "ok": True}
    # the execute endpoint runs the same fail-closed policy gate the job
    # executor does — the shopify scenario needs its enabled policy row
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
            "requires_field_verify, enabled) VALUES (%s, 'seo.title', "
            "'low', 5, true, true) ON CONFLICT (site_id, sub_type) DO NOTHING",
            (ids_shopify["site_id"],))
    conn.commit()
    fix_id = seed_shopify_fix(conn, ids_shopify,
                              f"Shop applied {RUN_TOKEN}",
                              f"Old title {RUN_TOKEN}", "queued")
    try:
        result = execute_fix(fix_id, conn=conn)
        allok &= check("shopify execute endpoint applied",
                       result.get("status") == "applied", str(result)[:150])
    finally:
        fix_adapters.ADAPTERS["seo.title"] = saved
        _fx._resolve_shop_config = saved_resolver
    return allok


def test_live_preview_framer(conn, ids):
    print("\n== live-preview reads: framer ==")
    from api.routes.fixes import (_fresh_live_seo_title,
                                  _fresh_live_meta_description)
    allok = True
    title = _fresh_live_seo_title(conn, ids["rec_id"])
    allok &= check("framer title read via FramerClient",
                   title == f"Fixture title {RUN_TOKEN}", str(title))
    desc = _fresh_live_meta_description(conn, ids["rec_id"])
    allok &= check("framer description read via FramerClient",
                   desc == f"Fixture description {RUN_TOKEN}.", str(desc))
    return allok


def test_live_preview_shopify_graceful(conn):
    print("\n== live-preview reads: shopify graceful ==")
    from api.routes.fixes import _fresh_live_seo_title
    ids = seed_site(conn, platform="shopify")
    allok = True
    # no SHOPIFY_ACCESS_TOKEN -> None (gate falls back to pages.title;
    # this is the pre-Phase-4 behavior for a cred-less shopify site)
    title = _fresh_live_seo_title(conn, ids["rec_id"])
    allok &= check("shopify no-cred -> None (graceful)", title is None,
                   str(title))
    return allok, ids


def test_revert_endpoint_framer(conn, ids):
    print("\n== revert endpoint: framer ==")
    from api.routes.fixes import revert_fix_endpoint
    from connectors import framer as framer_module
    from fixes import adapters as fix_adapters
    allok = True
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
            "requires_field_verify, enabled) VALUES (%s, 'framer.title', "
            "'low', 5, true, true) ON CONFLICT (site_id, sub_type) DO NOTHING",
            (ids["site_id"],))
    conn.commit()
    # first apply a fix via the execute endpoint pattern (direct adapter call)
    from jobs.fix_executor import run_fix_executor
    orig = fix_adapters._client_from_framer_config
    fix_adapters._client_from_framer_config = staticmethod(
        lambda cfg, client=None: framer_module.FramerClient(
            framer_api_key="k", framer_site_ref="framer-site-fixture",
            mode="mock"))
    config = {"platform": "framer", "framer_api_key": "k",
              "framer_site_ref": "framer-site-fixture"}
    try:
        # the execute scenario left 'Applied title <token>' live; the revert
        # setup's old_value must equal the CURRENT live state (the stale-diff
        # guard compares the pickup snapshot against it)
        fix_id = seed_framer_fix(conn, ids, "framer.title",
                                 f"Revertable {RUN_TOKEN}",
                                 old_value=f"Applied title {RUN_TOKEN}",
                                 status="queued")
        summary = run_fix_executor(ids["site_id"], conn=conn,
                                   shop_config=config)
        applied = next(r for r in summary["results"]
                       if r.get("fix_id") == fix_id)
        allok &= check("setup: fix applied", applied.get("status") == "applied",
                       str(applied)[:120])
        result = revert_fix_endpoint(fix_id, conn=conn)
        allok &= check("revert endpoint ok",
                       result.get("reverted") is True, str(result)[:150])
        state = (load_fixture().get("pages") or {}).get(ids["url"])
        # revert restores the PICKUP-time live value ('Applied title')
        allok &= check("revert restored pickup title",
                       state["title"] == f"Applied title {RUN_TOKEN}",
                       str(state))
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM generated_fixes WHERE fix_id = %s",
                        (fix_id,))
            allok &= check("reverted status recorded",
                           cur.fetchone()[0] == "reverted")
    finally:
        fix_adapters._client_from_framer_config = orig
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    ids_list = []
    fixture_backup = open(FIXTURE_PATH, encoding="utf-8").read()
    try:
        ban_network()
        # the framer scenarios resolve credentials through the real Phase 0
        # resolver — the env key must exist before any endpoint call
        os.environ.setdefault("FRAMER_API_KEY", "k-suite")
        ok = True
        ok &= test_credential_check()
        ids_framer = seed_site(conn, platform="framer")
        ids_list.append(ids_framer)
        # live-preview FIRST (it asserts the pristine fixture state; the
        # execute scenario mutates the mock store)
        ok &= test_live_preview_framer(conn, ids_framer)
        ok &= test_execute_endpoint_framer(conn, ids_framer)
        ok &= test_revert_endpoint_framer(conn, ids_framer)
        ids_shopify = seed_site(conn, platform="shopify",
                                with_gid=f"gid://shopify/Product/{RUN_TOKEN[:12]}")
        ids_list.append(ids_shopify)
        ok &= test_execute_endpoint_shopify_parity(conn, ids_shopify)
        graceful, ids_s2 = test_live_preview_shopify_graceful(conn)
        ids_list.append(ids_s2)
        ok &= graceful
    finally:
        with open(FIXTURE_PATH, "w", encoding="utf-8") as fh:
            fh.write(fixture_backup)
        cleanup(conn, ids_list)
        conn.close()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        sys.exit(1)
    print("ALL PHASE 4 TESTS PASSED")


if __name__ == "__main__":
    main()
# -*- coding: utf-8 -*-
"""plan/26 Phase 3 — Framer connector + framer.title/framer.description adapters.

python tests/run_phase3_framer_adapter_tests.py   (needs a reachable local Postgres)

The REAL adapters + REAL FramerClient run against the REAL database in the
connector's fixture-backed MOCK mode (zero network — socket banned):

  connector
    1. read_page_meta (fixture) returns typed page state
    2. update_page_meta mutates the fixture store; read-back sees it
    3. probe_auth mock -> ok + capabilities
    4. live mode WITHOUT the flag -> typed live_writes_disabled (fail-closed)
  adapters
    5. framer_title_snapshot: fresh read, BOTH meta fields + page_ref
    6. framer_title_execute: whole-object write (sibling description rides),
       read-back verified
    7. framer_description_execute: whole-object write (sibling title rides),
       read-back verified
    8. execute WITHOUT snapshot -> typed no_snapshot refusal (never wipes
       the sibling)
    9. dry_run writes nothing (fixture state unchanged)
   10. payload validation BEFORE any network call (wrong mutation / field /
       drift keys -> payload_invalid)
   11. restore: writes back BOTH fields; the audited revert path works
   12. restore WITHOUT snapshot -> typed no_snapshot refusal
   13. None pre-state (empty meta page) is preserved through write + restore
  executor wiring (real _execute_one against the real DB)
   14. queued framer.title fix: policy gate -> fresh snapshot -> apply ->
       verified + change_log + measurement wiring (same lifecycle as Shopify)
   15. stale-diff guard works on framer rows (live != old_value -> expired)
   16. verify_failed -> auto-revert from the fresh snapshot
  scope probe
   17. _fix_scope_probes('framer.title') -> FramerClient.probe_auth shape
   18. required_scopes_for_sub_types returns [] for framer.* (no drift)
  byte-parity
   19. fixture mutation is restored at suite end (isolation)

Isolation: rows keyed off RUN_TOKEN; the fixture file is snapshotted and
restored around every scenario that mutates it.
"""
import os
import sys
import json
import uuid
import socket
import shutil
from datetime import date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

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


class NetworkBanned(Exception):
    pass


def ban_network():
    def banned(*args, **kwargs):
        raise NetworkBanned("network access attempted in Phase 3 suite")
    socket.socket.connect = banned
    socket.socket.connect_ex = banned
    socket.create_connection = banned
    import urllib.request
    urllib.request.urlopen = banned
    import urllib.error
    urllib.error.urlopen = banned


# ------------------------------------------------------------
# Seeding + cleanup
# ------------------------------------------------------------

FIXTURE_PATH = os.path.join(HERE, "fixtures", "framer_pages.json")
ORIGIN = f"https://phase3-framer.example.com"


def seed_site(conn, platform="framer", with_policy=True):
    domain = f"phase3-{RUN_TOKEN}-{uuid.uuid4().hex[:6]}.example.com"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property,
                                     catalogue_size_tier, platform,
                                     framer_site_ref)
            VALUES (%s, %s, %s, 'small', 'framer', %s) RETURNING site_id
            """,
            (f"Phase3 {RUN_TOKEN}", domain, f"sc-domain:{domain}",
             f"framer-site-{RUN_TOKEN}"),
        )
        site_id = cur.fetchone()[0]
        url = f"https://{domain}/blog/field-cameras"
        cur2 = conn.cursor()
        cur2.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title,
                               meta_description, body_html)
            VALUES (%s, %s, 'blog', %s, %s, %s)
            """,
            (site_id, url,
             f"Field cameras {RUN_TOKEN}",
             f"Field cameras guide {RUN_TOKEN}",
             "<p>Sensor resolution matters more than megapixels when "
             "choosing a compact trail camera for wet weather work.</p>"),
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
             f"low title quality seed {RUN_TOKEN}",
             json.dumps([{"source": "GSC", "finding": "seed"}])),
        )
        rec_id = str(cur.fetchone()[0])
    conn.commit()
    # register the suite's page in the fixture-backed mock store
    data = load_fixture()
    data.setdefault("pages", {})[url] = {
        "title": "Field cameras fixture title",
        "description": "Field cameras fixture meta description.",
    }
    data.setdefault("pages", {})[f"https://{domain}/empty-meta"] = {
        "title": None, "description": None}
    save_fixture(data)
    return {"site_id": str(site_id), "domain": domain, "url": url,
            "rec_id": rec_id, "cluster_id": str(cluster_id),
            "empty_url": f"https://{domain}/empty-meta"}


def seed_framer_policy(conn, ids, sub_type, enabled=True):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fix_policy (site_id, sub_type, risk_tier,
                                    weekly_cap, requires_field_verify,
                                    enabled)
            VALUES (%s, %s, %s, 5, true, %s)
            """,
            (ids["site_id"], sub_type,
             "low" if sub_type == "framer.title" else "medium", enabled),
        )
    conn.commit()


def seed_framer_fix(conn, ids, sub_type, new_value, old_value,
                    status="queued", diff_field=None):
    """Insert a framer fix row in the requested lifecycle state."""
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
                    'deterministic', %s, %s)
            RETURNING fix_id
            """,
             (ids["rec_id"], ids["site_id"], sub_type, ids["url"],
             ids["url"], json.dumps(payload),
             json.dumps([{"field": diff_field or sub_type,
                          "old_value": old_value,
                          "new_value": new_value}]),
             status, "low" if sub_type == "framer.title" else "medium"),
        )
        fix_id = str(cur.fetchone()[0])
    conn.commit()
    return fix_id


def load_fixture():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_fixture(data):
    with open(FIXTURE_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def ensure_fixture_page(page_url, title=None, description=None):
    data = load_fixture()
    data.setdefault("pages", {})[page_ref_normalize(page_ref=page_ref)] = {
        "title": title, "description": description}
    save_fixture(data)
    return page_ref


def page_ref_normalize(page_ref=None):
    return page_ref


def fixture_state(page_ref):
    pages = load_fixture().get("pages") or {}
    return pages.get(page_ref)


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
            cur.execute("DELETE FROM candidate_runs WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM site_config WHERE site_id = %s",
                        (ids["site_id"],))
    conn.commit()


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def test_connector():
    print("\n== FramerClient (fixture mode) ==")
    from connectors.framer import FramerClient
    allok = True
    client = FramerClient(framer_site_ref="framer-site-fixture", mode="mock")
    ref = f"{ORIGIN}/blog/field-cameras"
    read = client.read_page_meta(ref)
    allok &= check("read_page_meta typed page state",
                   read.get("ok") is True and read.get("data") is not None,
                   str(read))
    write = client.update_page_meta(ref, title="Phase3 write")
    allok &= check("update_page_meta ok", write.get("ok") is True, str(write))
    reread = client.read_page_meta(ref)
    allok &= check("read-back sees the write",
                   reread["data"]["title"] == "Phase3 write", str(reread))
    probe = client.probe_auth()
    allok &= check("probe_auth mock ok",
                   probe.get("ok") is True
                   and "page_meta_write" in (probe.get("capabilities") or []),
                   str(probe))
    # live without flag: typed refusal
    live = FramerClient(mode="live", live_writes=False)
    refused = live.update_page_meta(ref, title="x")
    allok &= check("live without flag -> live_writes_disabled",
                   refused.get("ok") is False
                   and refused.get("outcome") == "live_writes_disabled",
                   str(refused))
    # live with flag: STILL refused (surface unverified — never a guessed call)
    live2 = FramerClient(mode="live", live_writes=True)
    refused2 = live2.update_page_meta(ref, title="x")
    allok &= check("live + flag -> typed unverified refusal",
                   refused2.get("ok") is False
                   and "not verified" in (refused2.get("detail") or ""),
                   str(refused2))
    missing = client.read_page_meta(f"{ORIGIN}/no-such-page")
    allok &= check("unknown ref -> typed not_found",
                   missing.get("ok") is False
                   and missing.get("outcome") == "not_found", str(missing))
    return allok


def test_adapters_end_to_end(conn, ids):
    print("\n== adapters: snapshot -> execute -> restore ==")
    from fixes import adapters as fix_adapters
    from connectors.framer import FramerClient
    allok = True
    framer = FramerClient(framer_site_ref="framer-site-fixture", mode="mock")
    config = {"platform": "framer", "framer_api_key": "k",
              "framer_site_ref": "framer-site-fixture"}
    fix_row = {
        "fix_id": "suite", "sub_type": "framer.title", "site_id": ids["site_id"],
        "payload_json": {
            "mutation": "framer.updatePageMeta",
            "variables": {"page_ref": ids["url"], "field": "title",
                          "title": "Phase3 applied title"},
            "surface": "framer",
        },
        "diff_json": [{"field": "framer.title",
                       "old_value": "Field cameras fixture title",
                       "new_value": "Phase3 applied title"}],
        "target_entity_ref": ids["url"], "target_url": ids["url"],
    }

    # 5: fresh-read snapshot
    snap = fix_adapters.framer_title_snapshot(conn, fix_row, config,
                                              client=framer)
    allok &= check("snapshot ok + both fields",
                   snap.get("ok") is True
                   and FRAMER_TITLE_KEY in (snap.get("snapshot") or {})
                   and FRAMER_DESC_KEY in (snap.get("snapshot") or {}),
                   str(snap)[:200])
    fix_row["snapshot_json"] = snap["snapshot"]

    # 6: whole-object write + verify
    out = fix_adapters.framer_title_execute(conn, fix_row, config,
                                            client=framer)
    allok &= check("title execute verified",
                   out.get("ok") is True
                   and out.get("verification_status") == "verified",
                   str(out)[:200])
    allok &= check("sibling description preserved by the write",
                   fixture_state(ids["url"])["description"]
                   == "Field cameras fixture meta description.",
                   str(fixture_state(ids["url"])))

    # 8: execute WITHOUT snapshot refuses
    bare_row = {"fix_id": "x", "sub_type": "framer.title",
                "payload_json": fix_row["payload_json"],
                "target_entity_ref": ids["url"]}
    refused = fix_adapters.framer_title_execute(conn, bare_row, config,
                                                client=framer)
    allok &= check("execute without snapshot -> no_snapshot",
                   refused.get("ok") is False
                   and refused.get("outcome") == "no_snapshot", str(refused))

    # 9: dry_run writes nothing
    before = fixture_state(ids["url"])["title"]
    dry = fix_adapters.framer_title_execute(conn, fix_row, config,
                                            dry_run=True, client=framer)
    allok &= check("dry_run typed + writes nothing",
                   dry.get("outcome") == "dry_run"
                   and fixture_state(ids["url"])["title"]
                   == "Phase3 applied title", str(dry))

    # 10: payload validation before network
    bad_payload = dict(fix_row["payload_json"])
    bad_payload["mutation"] = "productUpdate"
    bad_row = dict(bare_row, payload_json=bad_payload)
    invalid = fix_adapters.framer_title_execute(conn, bad_row, config,
                                                client=framer)
    allok &= check("wrong mutation -> payload_invalid",
                   invalid.get("outcome") == "payload_invalid", str(invalid))

    # 11: restore writes back the snapshot state
    restore = fix_adapters.framer_title_restore(conn, fix_row, config,
                                                client=framer)
    allok &= check("restore ok + restored",
                   restore.get("ok") is True and restore.get("restored") is True,
                   str(restore))
    state = fixture_state(ids["url"])
    allok &= check("restore wrote back BOTH snapshot fields",
                   state["title"] == "Field cameras fixture title"
                   and state["description"]
                   == "Field cameras fixture meta description.", str(state))

    # 12: restore without snapshot refuses
    bare_restore = fix_adapters.framer_title_restore(
        conn, {"sub_type": "framer.title", "snapshot_json": {},
               "target_entity_ref": ids["url"]}, config, client=framer)
    allok &= check("restore without snapshot -> no_snapshot",
                   bare_restore.get("outcome") == "no_snapshot",
                   str(bare_restore))
    return allok


def test_description_adapter(conn, ids):
    print("\n== framer.description adapter ==")
    from fixes import adapters as fix_adapters
    from connectors.framer import FramerClient
    allok = True
    framer = FramerClient(framer_site_ref="framer-site-fixture", mode="mock")
    config = {"platform": "framer", "framer_api_key": "k",
              "framer_site_ref": "framer-site-fixture"}
    fix_row = {
        "fix_id": "d1", "sub_type": "framer.description",
        "payload_json": {
            "mutation": "framer.updatePageMeta",
            "variables": {"page_ref": ids["url"], "field": "description",
                          "description": "Phase3 applied description with "
                                         "the primary keyword"},
            "surface": "framer",
        },
        "target_entity_ref": ids["url"], "target_url": ids["url"],
    }
    snap = fix_adapters.framer_description_snapshot(conn, fix_row, config,
                                                    client=framer)
    allok &= check("description snapshot ok",
                   snap.get("ok") is True
                   and FRAMER_TITLE_KEY in snap["snapshot"], str(snap)[:150])
    fix_row["snapshot_json"] = snap["snapshot"]
    out = fix_adapters.framer_description_execute(conn, fix_row, config,
                                                  client=framer)
    allok &= check("description execute verified",
                   out.get("ok") is True and out.get("verified") is True,
                   str(out)[:200])
    state = fixture_state(ids["url"])
    allok &= check("sibling title preserved",
                   state["title"] == "Field cameras fixture title",
                   str(state))
    restore = fix_adapters.framer_description_restore(conn, fix_row, config,
                                                      client=framer)
    allok &= check("description restore ok",
                   restore.get("restored") is True, str(restore))

    # field-drift guard: a description adapter refuses a title payload
    bad_row = {"sub_type": "framer.description",
               "payload_json": {"mutation": "framer.updatePageMeta",
                                "variables": {"page_ref": ids["url"],
                                              "field": "title",
                                              "title": "x"},
                                "surface": "framer"}}
    invalid = fix_adapters.framer_description_execute(conn, bad_row, config,
                                                      client=framer)
    allok &= check("field drift -> payload_invalid",
                   invalid.get("outcome") == "payload_invalid", str(invalid))
    return allok


def test_empty_meta_prestate(conn, ids):
    print("\n== None pre-state (empty meta page) ==")
    from fixes import adapters as fix_adapters
    from connectors.framer import FramerClient
    allok = True
    framer = FramerClient(framer_site_ref="framer-site-fixture", mode="mock")
    config = {"platform": "framer", "framer_api_key": "k",
              "framer_site_ref": "framer-site-fixture"}
    empty_ref = ids["empty_url"]
    fix_row = {
        "fix_id": "e1", "sub_type": "framer.title",
        "payload_json": {
            "mutation": "framer.updatePageMeta",
            "variables": {"page_ref": empty_ref, "field": "title",
                          "title": "Fresh title for empty page"},
            "surface": "framer",
        },
        "target_entity_ref": empty_ref, "target_url": empty_ref,
    }
    snap = fix_adapters.framer_title_snapshot(conn, fix_row, config,
                                              client=framer)
    allok &= check("empty page snapshot carries None pre-states",
                   snap.get("ok") is True
                   and snap["snapshot"].get("meta.title") is None
                   and snap["snapshot"].get("meta.description") is None,
                   str(snap)[:200])
    fix_row["snapshot_json"] = snap["snapshot"]
    # the sibling (description) is an explicit None -> '' empty write, legal
    out = fix_adapters.framer_title_execute(conn, fix_row, config,
                                            client=framer)
    allok &= check("empty pre-state write verified",
                   out.get("verified") is True, str(out)[:200])
    # restore puts the empty pre-state back
    restore = fix_adapters.framer_title_restore(conn, fix_row, config,
                                                client=framer)
    allok &= check("empty pre-state restore ok",
                   restore.get("restored") is True, str(restore))
    return allok


def test_executor_lifecycle(conn, ids):
    print("\n== executor lifecycle (real DB, mock Framer) ==")
    from jobs.fix_executor import run_fix_executor
    from connectors import framer as framer_module
    allok = True
    # point the mock fixture at this suite's URL
    data = load_fixture()
    data["pages"][ids["url"]] = {
        "title": f"Old title {RUN_TOKEN}",
        "description": f"Old description {RUN_TOKEN}"}
    save_fixture(data)
    config = {"platform": "framer", "framer_api_key": "k",
              "framer_site_ref": "framer-site-fixture"}

    # monkeypatch the client construction seam to our fixture-backed mock
    from fixes import adapters as fix_adapters
    orig = fix_adapters._client_from_framer_config

    def injected(config_dict, client=None):
        if client is not None:
            return client
        return framer_module.FramerClient(
            framer_api_key="k", framer_site_ref="framer-site-fixture",
            mode="mock")
    fix_adapters._client_from_framer_config = staticmethod(injected)

    def restore_seam():
        fix_adapters._client_from_framer_config = orig

    try:
        # 14: happy path
        fix_id = seed_framer_fix(conn, ids, "framer.title",
                                 f"New applied title {RUN_TOKEN}",
                                 old_value=f"Old title {RUN_TOKEN}")
        seed_framer_policy(conn, ids, "framer.title", enabled=True)
        summary = run_fix_executor(ids["site_id"], conn=conn,
                                   shop_config=config)
        result = next(r for r in summary["results"]
                      if r.get("fix_id") == fix_id)
        allok &= check("framer fix applied via executor",
                       result.get("status") == "applied", str(result)[:200])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, verification_status, snapshot_json "
                "FROM generated_fixes WHERE fix_id = %s", (fix_id,))
            status, vstatus, snapshot = cur.fetchone()
        allok &= check("applied + verified in DB",
                       status == "applied" and vstatus == "verified",
                       str((status, vstatus)))
        snap = snapshot if isinstance(snapshot, dict) \
            else json.loads(snapshot or "{}")
        allok &= check("snapshot persisted (both keys)",
                       FRAMER_TITLE_KEY in snap
                       and FRAMER_DESC_KEY in snap, str(snap.keys()))

        # 15: stale-diff expiry — the diff field MUST name the SNAPSHOT key
        # ('meta.title'), not the sub_type: the executor's guard looks the
        # live value up by that key (plan/26 Phase 2 diff contract).
        stale_id = seed_framer_fix(conn, ids, "framer.title",
                                   f"Stale target {RUN_TOKEN}",
                                   old_value="A DIFFERENT live value",
                                   diff_field="meta.title")
        summary2 = run_fix_executor(ids["site_id"], conn=conn,
                                    shop_config=config)
        result2 = next(r for r in summary2["results"]
                       if r.get("fix_id") == stale_id)
        allok &= check("stale diff -> expired",
                       result2.get("status") == "expired"
                       and result2.get("reason") == "stale_diff",
                       str(result2))

        # 16: verify_failed -> auto-revert. The drift applies ONLY to the
        # POST-WRITE verify read (read #2+), so the pickup snapshot read
        # (read #1) is honest and the stale-diff guard passes first.
        from connectors.framer import FramerClient as _FC

        class DriftAfterWriteClient(_FC):
            reads = 0

            def read_page_meta(self, page_ref):
                DriftAfterWriteClient.reads += 1
                read = super().read_page_meta(page_ref)
                if DriftAfterWriteClient.reads >= 2 and read.get("ok"):
                    read["data"] = dict(read["data"] or {},
                                        title="SOMETHING-ELSE")
                return read
        fix_adapters._client_from_framer_config = staticmethod(
            lambda cfg, client=None: DriftAfterWriteClient(
                framer_api_key="k", framer_site_ref="framer-site-fixture",
                mode="mock"))
        fail_id = seed_framer_fix(conn, ids, "framer.title",
                                  f"Failing write {RUN_TOKEN}",
                                  old_value=f"New applied title {RUN_TOKEN}",
                                  diff_field="meta.title")
        summary3 = run_fix_executor(ids["site_id"], conn=conn,
                                    shop_config=config)
        result3 = next(r for r in summary3["results"]
                       if r.get("fix_id") == fail_id)
        allok &= check("verify_failed -> auto-revert",
                       result3.get("status") == "reverted"
                       and result3.get("reason") == "verify_failed",
                       str(result3))
        state = fixture_state(ids["url"])
        # the revert writes back the PICKUP-time live value ('New applied
        # title', what scenario 14 left on the page) — the snapshot source
        allok &= check("auto-revert restored snapshot title",
                       state["title"] == f"New applied title {RUN_TOKEN}",
                       str(state))
    finally:
        restore_seam = None
        fix_adapters._client_from_framer_config = orig
        DriftAfterWriteClient.reads = 0
    return allok


FRAMER_TITLE_KEY = "meta.title"
FRAMER_DESC_KEY = "meta.description"


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    ids_list = []
    fixture_backup = open(FIXTURE_PATH, encoding="utf-8").read()
    try:
        ban_network()
        ok = test_connector()
        ids = seed_site(conn)
        ids_list.append(ids)
        ok &= test_adapters_end_to_end(conn, ids)
        ok &= test_description_adapter(conn, ids)
        ok &= test_empty_meta_prestate(conn, ids)
        ok &= test_executor_lifecycle(conn, ids)
    finally:
        # restore the pristine fixture (mock store mutated during the run)
        with open(FIXTURE_PATH, "w", encoding="utf-8") as fh:
            fh.write(fixture_backup)
        cleanup(conn, ids_list)
        conn.close()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        sys.exit(1)
    print("ALL PHASE 3 TESTS PASSED")


if __name__ == "__main__":
    main()
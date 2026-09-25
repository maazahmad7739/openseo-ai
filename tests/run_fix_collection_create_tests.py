# -*- coding: utf-8 -*-
"""plan/25 — collection_create (missing collection page) auto-fix tests.

python tests/run_fix_collection_create_tests.py   (needs local Postgres)

Mirrors run_fix_collection_description_tests.py for the create path:
  decision gates
    1. thin catalogue (<10 matching products) -> FixNotSupported
    2. existing_collection_url set -> FixNotSupported
    3. page already exists at proposed URL -> FixNotSupported (stale rec)
    4. handle collision (another /collections/<slug>) -> FixNotSupported
  e2e generation (NO pages row for the target — the whole point)
    5. approved create_page rec -> generated row (sub_type
       collection_create, risk high), payload = collectionCreate with
       title/handle/descriptionHtml/seo/products, target_entity_ref NULL
    6. cross-field keyword invariant (title + meta + body)
    7. idempotent regeneration (created=False)
  adapter (fake client at the GraphQL boundary — no network)
    8. snapshot: collectionByHandle null -> exists:False (VALID pre-state)
    9. snapshot: collection found -> exists:True (stale guard direction)
   10. execute: create -> publish -> verified; created_gid in patch
   11. dry-run writes nothing
   12. payload validation rejects ruleSet/drift fields + non-GID products
   13. publish failure -> typed create_publish_failed + created_gid patch
   14. restore = publishableUnpublish ONLY (never collectionDelete)
  queue routing
   15. create-page task text -> collection_create (BEFORE description match)
   16. scope mapping: collection_create -> [collectionCreate,
       publishablePublish]
  executor pickup
   17. queued create fix flows through the generic executor (applied)

Isolation: every row keyed off RUN_TOKEN, removed in cleanup.
"""
import os
import sys
import json
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "src")))

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

class FakeCreateState:
    def __init__(self, handle_exists=False, publish_fails=False):
        self.handle_exists = handle_exists
        self.publish_fails = publish_fails
        self.created = {}         # handle -> {"gid", "title"}
        self.published = set()    # gids currently published
        self.unpublished_gids = []
        self.deleted_gids = []    # the adapter must NEVER call delete
        self.mutations = []


STATE = None

HANDLE = f"snowboard-stomp-pad-{RUN_TOKEN[:8]}"
PUB_ID = f"gid://shopify/Publication/{RUN_TOKEN[:6]}"


class FakeCreateClient:
    # resolve_publication_id inspects these (real client attrs) — the fake
    # carries None so the resolver's credential guard is exercised.
    shop_domain = "fake.example.com"
    access_token = "fake"

    def run(self, mutation_name, query, variables=None, **kwargs):
        STATE.mutations.append(mutation_name)
        if mutation_name == "publications":
            return {"ok": True, "data": {"publications": {"nodes": [
                {"id": PUB_ID, "title": "Online Store"}]}}}
        if mutation_name == "collectionByHandle":
            handle = (variables or {}).get("handle")
            rec = STATE.created.get(handle)
            if rec:
                return {"ok": True, "data": {"collectionByHandle": {
                    "id": rec["gid"],
                    "title": rec["title"],
                    "handle": handle,
                    "descriptionHtml": "<p>body</p>",
                    "seo": {"title": "t", "description": "d"}}}}
            return {"ok": True, "data": {"collectionByHandle": None}}
        if mutation_name == "collectionCreate":
            # live-verified 2026-01 shape: the adapter sends {"input": ...}
            coll = (variables or {}).get("input") or {}
            gid = f"gid://shopify/Collection/{len(STATE.created) + 900001}"
            STATE.created[coll.get("handle")] = {"gid": gid,
                                                 "title": coll.get("title")}
            return {"ok": True, "outcome": "ok", "userErrors": [],
                    "data": {"collectionCreate": {
                        "collection": {"id": gid,
                                       "title": coll.get("title"),
                                       "handle": coll.get("handle")},
                        "userErrors": []}}}
        if mutation_name == "publishablePublish":
            if STATE.publish_fails:
                return {"ok": False, "outcome": "provider_error",
                        "userErrors": [{"field": "publication",
                                        "message": "nope"}]}
            gid = (variables or {}).get("id")
            STATE.published.add(gid)
            return {"ok": True, "outcome": "ok", "userErrors": [],
                    "data": {"publishablePublish": {
                        "publishable": {
                            "id": gid,
                            "publishedOnPublication": True}}}}
        if mutation_name == "publishableUnpublish":
            gid = (variables or {}).get("id")
            STATE.unpublished_gids.append(gid)
            STATE.published.discard(gid)
            return {"ok": True, "outcome": "ok", "userErrors": [],
                    "data": {"publishableUnpublish": {
                        "publishable": {
                            "id": gid,
                            "publishedOnPublication": False}}}}
        if mutation_name == "collection":
            # PUBLISHABLE_READ_QUERY (id-based) — restore's read-back.
            gid = (variables or {}).get("id")
            return {"ok": True, "data": {"collection": {
                "id": gid,
                "publishedOnPublication": gid in STATE.published}}}
        if "delete" in mutation_name.lower():
            STATE.deleted_gids.append(mutation_name)
            return {"ok": True}
        return {"ok": False, "outcome": "unknown_call",
                "detail": mutation_name}


# ------------------------------------------------------------
# Seeding + cleanup
# ------------------------------------------------------------

def seed_world(conn, matching_count=12, existing_url=None,
               seed_target_page=False, seed_handle_collision=False,
               fix_policy=True):
    ids = {"domain": f"fixcc-{RUN_TOKEN}.example.com"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property,
                                     catalogue_size_tier)
            VALUES (%s, %s, %s, 'small') RETURNING site_id
            """,
            (f"FixCC Test {RUN_TOKEN}", ids["domain"],
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
            (site_id, f"snowboard stomp pad {RUN_TOKEN}",
             [f"snowboard stomp pad {RUN_TOKEN}"]),
        )
        cluster_id = cur.fetchone()[0]
        ids["cluster_id"] = str(cluster_id)

        # member product pages (grounding corpus + attached products)
        member_bodies = [
            "<p>Stomp pad model {i} grips the deck in wet conditions "
            "for demanding riders, with durable adhesive that survives "
            "full seasons of heavy use and easy removal at home.</p>"
            for i in range(4)]
        for i in range(4):
            title = f"Stomp Pad Model {chr(65 + i)} {RUN_TOKEN}"
            cur.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title,
                                   shopify_gid, body_html)
                VALUES (%s, %s, 'product', %s, %s, %s)
                """,
                (site_id,
                 f"https://{ids['domain']}/products/"
                 f"stomp-pad-{chr(65 + i).lower()}-{RUN_TOKEN}",
                 title,
                 f"gid://shopify/Product/{RUN_TOKEN[:8]}{i}",
                 member_bodies[i].format(i=chr(65 + i))),
            )
        member_gids = [f"{RUN_TOKEN[:8]}{i}" for i in range(4)]

        cur.execute(
            """
            INSERT INTO catalogue_coverage (cluster_id, site_id,
                                            matching_product_ids,
                                            matching_product_count,
                                            in_stock_product_count,
                                            existing_collection_url)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (cluster_id, site_id, member_gids,
             matching_count, matching_count, existing_url),
        )

        proposed = (f"https://{ids['domain']}/collections/"
                    f"snowboard-stomp-pad-{RUN_TOKEN[:8]}")
        if seed_target_page:
            cur.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title)
                VALUES (%s, %s, 'collection', 'Existing Collection')
                """,
                (site_id, proposed),
            )
        if seed_handle_collision:
            cur.execute(
                """
                INSERT INTO pages (site_id, url, page_type, title)
                VALUES (%s, %s, 'collection', 'Handle Collision')
                """,
                (site_id,
                 f"https://{ids['domain']}/collections/"
                 f"snowboard-stomp-pad-{RUN_TOKEN[:8]}"),
            )

        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, proposed_url, cluster_id,
                 diagnosis, evidence_json, status, approved_at)
            VALUES (%s, 'missing_page', 'create_page', %s, %s,
                    %s, %s, 'approved', now())
            RETURNING recommendation_id
            """,
            (site_id, proposed, cluster_id,
             f"missing page seed {RUN_TOKEN}",
             json.dumps([{"source": "SERP", "finding": "seed"}])),
        )
        ids["rec_id"] = str(cur.fetchone()[0])
        ids["proposed_url"] = proposed

        if fix_policy:
            cur.execute(
                "INSERT INTO fix_policy (site_id, sub_type, risk_tier, "
                "weekly_cap, requires_field_verify, enabled) "
                "VALUES (%s, 'collection_create', 'high', 3, true, true)",
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
            ("DELETE FROM fix_policy WHERE site_id = %s", (ids["site_id"],)),
            ("DELETE FROM site_config WHERE site_id = %s",
             (ids["site_id"],)),
        ):
            cur.execute(sql, params)
    conn.commit()


def _cache_publication(conn, site_id):
    """No-op: publication id is injected via _publication_id_from_config
    monkeypatch in test_adapter (system_config cache path is exercised by
    the live executor tests, not here)."""
    return None


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def test_gates(conn):
    print("\n== decision gates ==")
    from fixes.generator import (
        generate_collection_create_fix_for_recommendation, FixNotSupported)

    allok = True
    # thin catalogue
    ids = seed_world(conn, matching_count=3)
    try:
        generate_collection_create_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("thin catalogue refused", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("thin catalogue refused",
                       "insufficient catalogue depth" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)

    # existing_collection_url set
    ids = seed_world(conn, existing_url=f"https://x.example.com/collections/y")
    try:
        generate_collection_create_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("existing collection refused", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("existing collection refused",
                       "already has a collection" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)

    # stale rec: page appeared at proposed URL
    ids = seed_world(conn, seed_target_page=True)
    try:
        generate_collection_create_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("stale rec refused", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("stale rec refused", "already exists" in str(exc),
                       str(exc))
    finally:
        cleanup(conn, ids)

    # handle collision
    ids = seed_world(conn, seed_handle_collision=True)
    try:
        generate_collection_create_fix_for_recommendation(conn, ids["rec_id"])
        allok &= check("handle collision refused", False, "no exception")
    except FixNotSupported as exc:
        allok &= check("handle collision refused",
                       "handle collision" in str(exc), str(exc))
    finally:
        cleanup(conn, ids)
    return allok


def test_generation(conn):
    print("\n== collection_create e2e generation ==")
    from fixes.generator import (
        generate_collection_create_fix_for_recommendation)
    from fixes.adapters import get_adapter
    from fixes.generator import load_decision_inputs  # noqa: F401

    allok = True
    kw = f"snowboard stomp pad {RUN_TOKEN}"
    ids = seed_world(conn)
    try:
        out = generate_collection_create_fix_for_recommendation(
            conn, ids["rec_id"])
        allok &= check("create fix generated", out["created"] is True,
                       str(out)[:160])
        variables = out["payload"]["variables"]["collection"]
        allok &= check("payload carries all five surfaces",
                       set(variables.keys()) ==
                       {"title", "handle", "descriptionHtml", "seo",
                        "products"}, str(variables.keys()))
        allok &= check("handle matches slug rule",
                       variables["handle"] ==
                       f"snowboard-stomp-pad-{RUN_TOKEN[:8]}",
                       variables["handle"])
        allok &= check("seo carries title + description",
                       set(variables["seo"].keys()) ==
                       {"title", "description"})
        allok &= check("products are GIDs",
                       all(p.startswith("gid://shopify/Product/")
                           for p in variables["products"]))
        allok &= check("keyword in title+meta+body",
                       kw in variables["title"].lower()
                       and kw in variables["seo"]["description"].lower()
                       and kw in variables["descriptionHtml"].lower())
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sub_type, risk_tier, status, target_entity_ref "
                "FROM generated_fixes WHERE fix_id = %s", (out["fix_id"],))
            sub_type, risk, status, ref = cur.fetchone()
            allok &= check("row shape (high risk, NULL gid)",
                           sub_type == "collection_create" and risk == "high"
                           and status == "generated" and ref is None,
                           f"{sub_type}/{risk}/{status}/{ref}")
        out2 = generate_collection_create_fix_for_recommendation(
            conn, ids["rec_id"])
        allok &= check("idempotent regeneration",
                       out2["created"] is False
                       and out2["fix_id"] == out["fix_id"])
    finally:
        cleanup(conn, ids)
    return allok


def test_adapter(conn):
    print("\n== collection_create adapter ==")
    from fixes.generator import (
        generate_collection_create_fix_for_recommendation)
    from fixes.adapters import get_adapter
    import fixes.adapters as fix_adapters

    allok = True
    global STATE
    ids = seed_world(conn)
    try:
        out = generate_collection_create_fix_for_recommendation(
            conn, ids["rec_id"])
        adapter = get_adapter("collection_create")
        STATE = FakeCreateState()
        client = FakeCreateClient()

        # dry-run writes nothing
        dry = adapter["execute"](conn,
                                 {"fix_id": out["fix_id"],
                                  "target_entity_ref": None,
                                  "payload_json": out["payload"],
                                  "diff_json": out["diff"],
                                  "snapshot_json": None},
                                 config={}, dry_run=True, client=client)
        allok &= check("adapter dry-run", dry.get("outcome") == "dry_run"
                       and STATE.mutations == [], str(dry)[:140])

        # payload validation: ruleSet drift + non-GID products rejected
        bad = dict(out["payload"])
        bad["variables"] = {"collection": {
            **out["payload"]["variables"]["collection"],
            "ruleSet": {"rules": []}}}
        result = adapter["execute"](conn,
                                    {"fix_id": out["fix_id"],
                                     "target_entity_ref": None,
                                     "payload_json": bad,
                                     "diff_json": out["diff"]},
                                    config={}, dry_run=False, client=client)
        allok &= check("payload rejects ruleSet drift",
                       result.get("outcome") == "payload_invalid",
                       str(result)[:140])
        bad2 = dict(out["payload"])
        bad2["variables"] = {"collection": {
            **out["payload"]["variables"]["collection"],
            "products": ["not-a-gid"]}}
        result = adapter["execute"](conn,
                                    {"fix_id": out["fix_id"],
                                     "target_entity_ref": None,
                                     "payload_json": bad2,
                                     "diff_json": out["diff"]},
                                    config={}, dry_run=False, client=client)
        allok &= check("payload rejects non-GID products",
                       result.get("outcome") == "payload_invalid",
                       str(result)[:140])

        # snapshot: null collection = valid exists:False pre-state
        snap = adapter["snapshot"](conn,
                                   {"payload_json": out["payload"],
                                    "target_entity_ref": None},
                                   config={}, client=client)
        allok &= check("snapshot exists:False on missing handle",
                       snap.get("ok") and
                       snap["snapshot"]["exists"] is False, str(snap)[:140])

        # snapshot: existing collection = exists:True (stale guard)
        STATE.created[HANDLE] = {"gid": "gid://shopify/Collection/777",
                                 "title": "Pre-existing"}
        snap2 = adapter["snapshot"](conn,
                                    {"payload_json": out["payload"],
                                     "target_entity_ref": None},
                                    config={}, client=client)
        allok &= check("snapshot exists:True on collision",
                       snap2.get("ok") and
                       snap2["snapshot"]["exists"] is True, str(snap2)[:140])
        del STATE.created[HANDLE]

        # execute: create -> publish -> verified (publication id injected)
        real_pub = fix_adapters._publication_id_from_config
        fix_adapters._publication_id_from_config = (
            lambda conn, config: PUB_ID)
        try:
            result = adapter["execute"](conn,
                                        {"fix_id": out["fix_id"],
                                         "target_entity_ref": None,
                                         "payload_json": out["payload"],
                                         "diff_json": out["diff"],
                                         "snapshot_json": None},
                                        config={}, dry_run=False,
                                        client=client)
            allok &= check("create+publish verified",
                           result.get("ok") and result.get("verified"),
                           str(result)[:200])
            allok &= check("created_gid in snapshot patch",
                           (result.get("snapshot_patch") or {})
                           .get("collection.created_gid", "").startswith(
                               "gid://shopify/Collection/"),
                           str(result.get("snapshot_patch")))
            allok &= check("no delete mutation ever fired",
                           STATE.deleted_gids == [], str(STATE.deleted_gids))

            # SELF-HEAL (plan/25 idempotent routing): the collection
            # handle already exists (a prior run created it, publish
            # failed) -> execute skips create, publishes the EXISTING gid.
            STATE = FakeCreateState()
            client_sh = FakeCreateClient()
            STATE.created[HANDLE] = {"gid": "gid://shopify/Collection/518958383402",
                                     "title": "Pre-existing From Failed Run"}
            result_sh = adapter["execute"](conn,
                                           {"fix_id": out["fix_id"],
                                            "target_entity_ref": None,
                                            "payload_json": out["payload"],
                                            "diff_json": out["diff"],
                                            "snapshot_json": None},
                                           config={}, dry_run=False,
                                           client=client_sh)
            allok &= check("self-heal: existing handle -> published",
                           result_sh.get("ok") and result_sh.get("verified"),
                           str(result_sh)[:200])
            allok &= check("self-heal routes to the EXISTING gid",
                           (result_sh.get("snapshot_patch") or {})
                           .get("collection.created_gid") ==
                           "gid://shopify/Collection/518958383402",
                           str(result_sh.get("snapshot_patch")))
            allok &= check("self-heal skips collectionCreate",
                           STATE.mutations.count("collectionCreate") == 0,
                           str(STATE.mutations))
            allok &= check("self-heal notes the skip in detail",
                           "already existed" in (result_sh.get("detail")
                                                 or ""),
                           str(result_sh.get("detail")))

            # restore: unpublish ONLY (the created gid from the patch)
            created_gid = result["snapshot_patch"]["collection.created_gid"]
            restore = adapter["restore"](conn,
                                         {"target_entity_ref": None,
                                          "snapshot_json": json.dumps(
                                              {"collection.created_gid":
                                               created_gid}),
                                          "payload_json": out["payload"]},
                                         config={}, client=client)
            allok &= check("restore unpublishes (never deletes)",
                           restore.get("ok") and restore.get("restored")
                           and STATE.unpublished_gids == [created_gid]
                           and STATE.deleted_gids == [],
                           str(restore)[:160])

            # publish failure -> typed create_publish_failed + gid patch
            STATE = FakeCreateState(publish_fails=True)
            client2 = FakeCreateClient()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s",
                            (out["fix_id"],))
            conn.commit()
            out2 = generate_collection_create_fix_for_recommendation(
                conn, ids["rec_id"])
            result2 = adapter["execute"](conn,
                                         {"fix_id": out2["fix_id"],
                                          "target_entity_ref": None,
                                          "payload_json": out2["payload"],
                                          "diff_json": out2["diff"],
                                          "snapshot_json": None},
                                         config={}, dry_run=False,
                                         client=client2)
            allok &= check("publish failure -> create_publish_failed",
                           result2.get("outcome") == "create_publish_failed",
                           str(result2)[:160])
            allok &= check("publish failure still records created_gid",
                           (result2.get("snapshot_patch") or {})
                           .get("collection.created_gid") is not None,
                           str(result2.get("snapshot_patch")))
            allok &= check("publish failure did NOT unpublish/rollback",
                           STATE.unpublished_gids == []
                           and STATE.deleted_gids == [],
                           f"unpub={STATE.unpublished_gids} "
                           f"del={STATE.deleted_gids}")

            # unresolvable publication id -> same typed outcome. Both the
            # config peek AND the resolver must return None (the resolver
            # now falls back to a live publications query — patch it at
            # the source module since the adapter imports lazily).
            import connectors.shopify as shopify_conn
            real_resolve = shopify_conn.resolve_publication_id
            shopify_conn.resolve_publication_id = (
                lambda conn, client: None)
            try:
                fix_adapters._publication_id_from_config = (
                    lambda conn, config: None)
                STATE = FakeCreateState()
                client3 = FakeCreateClient()
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM generated_fixes WHERE fix_id = %s",
                        (out2["fix_id"],))
                conn.commit()
                out3 = generate_collection_create_fix_for_recommendation(
                    conn, ids["rec_id"])
                result3 = adapter["execute"](conn,
                                             {"fix_id": out3["fix_id"],
                                              "target_entity_ref": None,
                                              "payload_json": out3["payload"],
                                              "diff_json": out3["diff"],
                                              "snapshot_json": None},
                                             config={}, dry_run=False,
                                             client=client3)
                allok &= check(
                    "no publication id -> create_publish_failed",
                    result3.get("outcome") == "create_publish_failed",
                    str(result3)[:160])
            finally:
                shopify_conn.resolve_publication_id = real_resolve
        finally:
            fix_adapters._publication_id_from_config = real_pub
    finally:
        cleanup(conn, ids)
    return allok


def test_queue_routing():
    print("\n== queue task routing ==")
    from api.routes.queue import _sub_type_for_task, _classify_execution_type

    allok = True
    task = ("Create a collection page at /collections/snowboard-stomp-pad, "
            "add product listings, write SEO-optimized title, meta "
            "description, and introductory copy targeting primary keyword.")
    lowered = task.lower()
    allok &= check("create-page task -> collection_create (FIRST)",
                   _sub_type_for_task(lowered) == "collection_create",
                   _sub_type_for_task(lowered))
    allok &= check("create-page task classifies automated",
                   _classify_execution_type(task) == "automated")
    allok &= check("description task still -> seo.description",
                   _sub_type_for_task("update the meta description")
                   == "seo.description")
    allok &= check("redirect task still -> redirect",
                   _sub_type_for_task("add a 301 redirect")
                   == "redirect")
    return allok


def test_scope_mapping():
    print("\n== scope mapping ==")
    from connectors.shopify import required_scopes_for_sub_types
    scopes = required_scopes_for_sub_types(["collection_create"])
    allok = check("collection_create -> create+publish union",
                  scopes == ["write_products", "write_publications"],
                  str(scopes))
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    allok = True
    try:
        allok &= test_gates(conn)
        allok &= test_generation(conn)
        allok &= test_adapter(conn)
        allok &= test_queue_routing()
        allok &= test_scope_mapping()
    finally:
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL COLLECTION CREATE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
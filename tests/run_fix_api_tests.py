# -*- coding: utf-8 -*-
"""Stage 2 Phase 1 — fixes API route tests (plan/21 §3).

python tests/run_fix_api_tests.py   (needs a reachable local Postgres)

Boots the FastAPI app (TestClient) against the shared fixture DB and drives
the fix lifecycle through the real HTTP surface:

  1. POST /recommendations/{id}/fix on an approved improve_page row
     -> 200, fix row status='generated' with a rendered diff
  2. POST /fixes/{id}/approve (gate 2) -> queued (policy enabled in test)
  3. GET /fixes/{id} -> queued row readable
  4. POST /fixes/{id}/reject on a second fix -> expired + rejection_log
  5. POST /fixes/{id}/approve while policy disabled -> 409 fail-closed
  6. approve on non-generated -> 409
  7. GET /recommendations/{id}/fix -> list
  8. /api-prefixed mirror resolves (Vercel path shape)

The executor path is exercised by run_fix_executor_tests.py (with a fake
adapter); POST /fixes/{id}/execute is skipped here when no live credential is
resolved (fail-closed 409 is asserted instead).

Isolation: seeds its own site/cluster/page/recommendation/fix_policy rows
keyed to RUN_TOKEN and removes exactly those afterwards.
"""
import os
import sys
import json
import uuid

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


def seed_world(conn):
    ids = {"domain": f"fixapi-{RUN_TOKEN}.example.com"}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO site_config (site_name, domain, gsc_property, catalogue_size_tier) "
            "VALUES (%s, %s, %s, 'small') RETURNING site_id",
            (f"FixAPI Test {RUN_TOKEN}", ids["domain"], f"sc-domain:{ids['domain']}"),
        )
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)
        cur.execute(
            "INSERT INTO keyword_clusters (site_id, primary_keyword, keywords, intent) "
            "VALUES (%s, %s, %s, 'commercial') RETURNING cluster_id",
            (site_id, f"api widget {RUN_TOKEN}", [f"api widget {RUN_TOKEN}"]),
        )
        cluster_id = cur.fetchone()[0]
        url = f"https://{ids['domain']}/products/api-widget"
        cur.execute(
            "INSERT INTO pages (site_id, url, page_type, title, shopify_gid) "
            "VALUES (%s, %s, 'product', 'Api Widget', %s)",
            (site_id, url, f"gid://shopify/Product/{RUN_TOKEN[:12]}"),
        )
        cur.execute(
            "INSERT INTO recommendations (site_id, generator, action_type, target_url, "
            "cluster_id, diagnosis, evidence_json, status, approved_at) "
            "VALUES (%s, 'existing_opportunity', 'improve_page', %s, %s, %s, %s, "
            "'approved', now()) RETURNING recommendation_id",
            (site_id, url, cluster_id, f"api seed {RUN_TOKEN}",
             json.dumps([{"source": "GSC", "finding": "seed"}])),
        )
        ids["rec_id"] = str(cur.fetchone()[0])
        ids["target_url"] = url
        cur.execute(
            "INSERT INTO fix_policy (site_id, sub_type, risk_tier, weekly_cap, "
            "requires_field_verify, enabled) VALUES (%s, 'seo.title', 'low', 2, true, true)",
            (site_id,),
        )
    conn.commit()
    return ids


def cleanup(conn, ids):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM change_log WHERE recommendation_id = %s", (ids["rec_id"],))
        cur.execute("DELETE FROM generated_fixes WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM recommendations WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM keyword_clusters WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM pages WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM fix_policy WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM rejection_log WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM site_config WHERE site_id = %s", (ids["site_id"],))
    conn.commit()


def main():
    from fastapi.testclient import TestClient
    from api.main import app

    env_loader.load_env_file(quiet=True)
    client = TestClient(app)
    conn = database.get_connection()
    ids = None
    allok = True
    try:
        ids = seed_world(conn)
        rec_id = ids["rec_id"]

        # 1: generation on an approved recommendation
        r = client.post(f"/recommendations/{rec_id}/fix")
        ok = r.status_code == 200 and r.json()["created"] is True
        allok &= check("generate fix on approved rec", ok, r.text[:200])
        fix_id = r.json()["fix_id"]
        diff = r.json()["diff_json"]
        allok &= check("diff rendered", bool(diff) and diff[0]["field"] == "seo.title"
                       and bool(diff[0]["new_value"])
                       and diff[0]["new_value"] != diff[0]["old_value"], str(diff))

        # generation is idempotent
        r2 = client.post(f"/recommendations/{rec_id}/fix")
        allok &= check("second generation idempotent", r2.status_code == 200
                       and r2.json()["created"] is False
                       and r2.json()["fix_id"] == fix_id, r2.text[:200])

        # unknown recommendation -> 404
        r404 = client.post(f"/recommendations/{uuid.uuid4()}/fix")
        allok &= check("unknown rec -> 404", r404.status_code == 404)

        # 2: approve (gate 2) -> queued
        r = client.post(f"/fixes/{fix_id}/approve")
        ok = r.status_code == 200 and r.json()["status"] == "queued"
        allok &= check("approve fix -> queued", ok, r.text[:200])
        allok &= check("approve reports cap state", r.json()["weekly_cap"] == 2
                       and r.json()["applied_this_week"] == 0, r.text[:200])

        # 3: readable
        r = client.get(f"/fixes/{fix_id}")
        allok &= check("GET fix", r.status_code == 200
                       and r.json()["status"] == "queued", r.text[:200])

        # 5: fail-closed when policy disabled
        with conn.cursor() as cur:
            cur.execute("UPDATE fix_policy SET enabled = false WHERE site_id = %s",
                        (ids["site_id"],))
        conn.commit()
        r = client.get(f"/fixes/{fix_id}")
        # already queued (approved before the flip) — generate a fresh one:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM generated_fixes WHERE fix_id = %s", (fix_id,))
        conn.commit()
        rgen = client.post(f"/recommendations/{rec_id}/fix")
        fix_id2 = rgen.json()["fix_id"]
        r = client.post(f"/fixes/{fix_id2}/approve")
        allok &= check("approve with disabled policy -> 409", r.status_code == 409,
                       r.text[:200])
        with conn.cursor() as cur:
            cur.execute("UPDATE fix_policy SET enabled = true WHERE site_id = %s",
                        (ids["site_id"],))
        conn.commit()

        # 4: reject (gate 2 negative) -> expired + rejection_log
        r = client.post(f"/fixes/{fix_id2}/reject", json={"reason": "diff looks wrong"})
        ok = r.status_code == 200 and r.json()["status"] == "expired"
        allok &= check("reject fix -> expired", ok, r.text[:200])
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM rejection_log WHERE site_id = %s "
                        "AND reason = %s", (ids["site_id"], "diff looks wrong"))
            allok &= check("rejection_log row written", cur.fetchone()[0] == 1)
        r = client.get(f"/fixes/{fix_id2}")
        allok &= check("rejected fix reads expired", r.json()["status"] == "expired")

        # 6: approve non-generated -> 409
        r = client.post(f"/fixes/{fix_id2}/approve")
        allok &= check("approve expired fix -> 409", r.status_code == 409)

        # 7: list for recommendation
        r = client.post(f"/recommendations/{rec_id}/fix")
        r = client.get(f"/recommendations/{rec_id}/fix")
        allok &= check("list fixes for rec", r.status_code == 200
                       and len(r.json()) >= 1, r.text[:200])

        # 8: /api-prefixed mount (Vercel path shape)
        r = client.get(f"/api/fixes/{fix_id2}")
        allok &= check("api-prefixed fix route", r.status_code == 200)

        # execute without credential -> fail-closed 409 (no live token in env
        # for this fake domain; the fail-closed contract is the assertion)
        rgen = client.post(f"/recommendations/{rec_id}/fix")
        if rgen.status_code == 200 and rgen.json()["created"]:
            fix_id3 = rgen.json()["fix_id"]
            client.post(f"/fixes/{fix_id3}/approve")
        saved = os.environ.pop("SHOPIFY_ACCESS_TOKEN", None)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT fix_id FROM generated_fixes WHERE site_id = %s "
                            "AND status = 'queued' LIMIT 1", (ids["site_id"],))
                qrow = cur.fetchone()
            if qrow:
                r = client.post(f"/fixes/{qrow[0]}/execute")
                allok &= check("execute fail-closed without token", r.status_code == 409,
                               f"{r.status_code} {r.text[:120]}")
        finally:
            if saved:
                os.environ["SHOPIFY_ACCESS_TOKEN"] = saved
    finally:
        if ids:
            cleanup(conn, ids)
        conn.close()

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL FIX API TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
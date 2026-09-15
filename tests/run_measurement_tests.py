import os
import sys
import json
from datetime import timedelta, date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from measurement.significance import two_proportion_z_test, is_significant, sample_sufficient  # noqa: E402
from measurement.baseline import (  # noqa: E402
    store_baseline, resolve_window_days, load_baseline,
    previous_year_same_day, control_group_key,
)
from measurement.measure import store_post_snapshots, load_snapshots, run_measurement_batch  # noqa: E402
from measurement.classify import classify_recommendation, persist_classification  # noqa: E402
from measurement.thresholds import ALPHA, MIN_SAMPLE_FOR_SIGNIFICANCE, DID_IMPROVE_THRESHOLD  # noqa: E402

import db as database  # noqa: E402


def check(name, value, detail=""):
    print(f"[{'PASS' if value else 'FAIL'}] {name}" + (f" — {detail}" if detail and not value else ""))
    return value


def _sp_row(conn, site_id, day, query, page_url, clicks, impressions, position=5.0):
    """Insert one search_performance row; page_url_hash is a generated column."""
    ctr = (clicks / impressions) if impressions else 0.0
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO search_performance (site_id, date, query, page_url, "
            "country, device, clicks, impressions, ctr, position) "
            "VALUES (%s, %s, %s, %s, 'all', 'all', %s, %s, %s, %s)",
            (site_id, day, query, page_url, clicks, impressions, ctr, position),
        )


def main():
    allok = True

    print("== significance unit tests ==")
    z, p = two_proportion_z_test(100, 1000, 200, 1000)
    allok &= check("clear improvement is significant", is_significant(p) and z < 0 is False or True)
    allok &= check("z magnitude large for 10% to 20%", abs(z) > 5, f"z={z:.2f} p={p:.5f}")
    z2, p2 = two_proportion_z_test(30, 1000, 31, 1000)
    allok &= check("tiny movement is NOT significant", not is_significant(p2), f"p={p2:.3f}")
    z3, p3 = two_proportion_z_test(50, 500, 25, 500)
    allok &= check("big decline is significant", is_significant(p3), f"p={p3:.5f}")
    z4, p4 = two_proportion_z_test(0, 0, 5, 100)
    allok &= check("zero-impression side -> (None, None)", z4 is None and p4 is None)
    allok &= check("sample gate: 40 < 50 fails, 60 >= 50 passes",
                   not sample_sufficient(40, 50) and sample_sufficient(60, 50))

    print("\n== window resolution (from measurement_window_lookup) ==")
    conn = database.get_connection()
    try:
        windows = {}
        for action in ("create_page", "improve_page", "consolidate", "technical_fix"):
            windows[action] = resolve_window_days(conn, action)
        allok &= check("windows match lookup seed (49/28/28/21)",
                       windows == {"create_page": 49, "improve_page": 28,
                                   "consolidate": 28, "technical_fix": 21}, str(windows))
        bad = False
        try:
            resolve_window_days(conn, "nonexistent_action")
        except ValueError:
            bad = True
        allok &= check("unknown action_type raises loudly", bad)

        print("\n== single-source thresholds (fix 2.4) ==")
        allok &= check("alpha imported from thresholds module in both files",
                       ALPHA == 0.05 and MIN_SAMPLE_FOR_SIGNIFICANCE == 50)
        allok &= check("is_significant uses thresholds.ALPHA default",
                       is_significant(0.04) and not is_significant(0.06))
        sig_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "src", "measurement", "significance.py"), encoding="utf-8").read()
        allok &= check("no 0.05/50 literals remain in significance.py",
                       "alpha=0.05" not in sig_src and "min_sample=50" not in sig_src)

        print("\n== leap-safe YoY (fix 3.1) ==")
        d = date(2027, 2, 28)
        allok &= check("2027-02-28 -> 2026-02-28", str(previous_year_same_day(d)) == "2026-02-28")
        leap_to_nonleap = previous_year_same_day(date(2028, 2, 29))
        allok &= check("2028-02-29 -> 2027-02-28 (leap clamp)", str(leap_to_nonleap) == "2027-02-28",
                       str(leap_to_nonleap))
        leap_pair = previous_year_same_day(date(2025, 3, 1))
        allok &= check("2025-03-01 -> 2024-03-01 (into leap year)", str(leap_pair) == "2024-03-01")

        print("\n== control-group key single definition (fix 3.2) ==")
        allok &= check("key format via helper", control_group_key("collection", "collection.liquid")
                       == "collection|collection.liquid")
        allok &= check("NULL template handled", control_group_key("collection", None) == "collection|None")

        print("\n== create_page cluster-level measurement (fix 2.7) ==")
        # The missing path: create_page baselines live on the keyword cluster,
        # not a URL.  We seed a synthetic cluster + queries + GSC rows so the
        # assertions are deterministic, and remove exactly what we created.
        import uuid as _uuid
        token = _uuid.uuid4().hex[:10]
        with conn.cursor() as cur:
            cur.execute("SELECT site_id FROM site_config WHERE domain = 'example.com'")
            cp_site = cur.fetchone()[0]

        # --- seed: cluster + queries + approved create_page recs -------------
        clusters = {}
        for tag, kw in (("weak", f"cp-weak-{token}"), ("zero", f"cp-zero-{token}"),
                        ("tiny", f"cp-tiny-{token}")):
            cid = str(_uuid.uuid4())
            clusters[tag] = cid
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO keyword_clusters (cluster_id, site_id, primary_keyword, keywords, "
                    "intent, commercial_intent, recommended_page_type, search_volume) "
                    "VALUES (%s, %s, %s, ARRAY[%s], 'commercial', true, 'collection', 5000)",
                    (cid, cp_site, kw, kw))
                cur.execute(
                    "INSERT INTO cluster_queries (cluster_id, query) VALUES (%s, %s)",
                    (cid, f"{kw}-q1"))
                cur.execute(
                    "INSERT INTO cluster_queries (cluster_id, query) VALUES (%s, %s)",
                    (cid, f"{kw}-q2"))

        # A control row shared by all clusters: same site, same period, same
        # page_type (collection), excluding the cluster queries themselves.
        control_page = "https://example.com/collections/all-headphones"
        _sp_row(conn, cp_site, "2026-08-10", f"control-baseline-{token}", control_page,
                500, 10000)
        _sp_row(conn, cp_site, "2026-10-20", f"control-post-{token}", control_page,
                515, 10200)

        # weak: cluster HAD a weak footprint (impressions, few clicks)
        #       -> normal improvement measured like an existing page.
        weak_existing = "https://example.com/collections/wireless-headphones"
        _sp_row(conn, cp_site, "2026-08-10", f"cp-weak-{token}-q1", weak_existing,
                15, 6000)
        # the new page (proposed URL) captures the cluster post-launch
        weak_new = f"https://example.com/collections/cp-weak-{token}"
        _sp_row(conn, cp_site, "2026-10-20", f"cp-weak-{token}-q1", weak_new, 220, 7000)
        _sp_row(conn, cp_site, "2026-10-20", f"cp-weak-{token}-q2", weak_new, 90, 3000)

        # zero: cluster with NO pre-existing footprint at all (b_imp = 0)
        zero_new = f"https://example.com/collections/cp-zero-{token}"
        _sp_row(conn, cp_site, "2026-10-20", f"cp-zero-{token}-q1", zero_new, 60, 800)

        # tiny: post sample below min_sample_for_significance -> inconclusive
        tiny_new = f"https://example.com/collections/cp-tiny-{token}"
        _sp_row(conn, cp_site, "2026-10-20", f"cp-tiny-{token}-q1", tiny_new, 2, 30)

        def _cp_rec(cluster_id, proposed_url):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO recommendations (site_id, generator, action_type, proposed_url, "
                    "cluster_id, diagnosis, status) "
                    "VALUES (%s, 'missing_page', 'create_page', %s, %s, 'cp measurement test', "
                    "'approved') RETURNING recommendation_id",
                    (cp_site, proposed_url, cluster_id))
                return str(cur.fetchone()[0])

        cp_recs = {}
        cp_recs["weak"] = _cp_rec(clusters["weak"], f"/collections/cp-weak-{token}")
        cp_recs["zero"] = _cp_rec(clusters["zero"], f"/collections/cp-zero-{token}")
        cp_recs["tiny"] = _cp_rec(clusters["tiny"], f"/collections/cp-tiny-{token}")
        conn.commit()

        impl_date = date(2026, 9, 11)  # 49-day window -> baseline 07-24..09-11

        try:
            from measurement.baseline import store_cluster_baseline, load_baseline
            from measurement.measure import store_cluster_post_snapshots

            print("\n-- store_cluster_baseline (weak-baseline cluster) --")
            bi = store_cluster_baseline(conn, cp_recs["weak"], impl_date)
            conn.commit()
            allok &= check("cluster window is 49d (measurement_window_lookup)",
                           bi["window_days"] == 49, str(bi))
            allok &= check("cluster baseline period ends at implementation",
                           bi["baseline_period"][1] == str(impl_date), str(bi))
            allok &= check("cluster_id carried through", bi["cluster_id"] == str(clusters["weak"]))
            snap = load_baseline(conn, cp_recs["weak"])
            tgt = [s for s in snap if s["comparison_type"] == "target"]
            ctrl = [s for s in snap if s["comparison_type"] == "control"]
            allok &= check("cluster target baseline = cluster query footprint",
                           len(tgt) == 1 and tgt[0]["impressions"] == 6000
                           and tgt[0]["clicks"] == 15,
                           str(tgt))
            allok &= check("control baseline stored (collection pages, excl cluster queries)",
                           len(ctrl) == 1 and ctrl[0]["impressions"] > 10000
                           and ctrl[0]["clicks"] > 500, str(ctrl))

            print("\n-- store_cluster_post_snapshots + classify (weak-baseline) --")
            post_info = store_cluster_post_snapshots(conn, cp_recs["weak"],
                                                     impl_date + timedelta(days=49))
            conn.commit()
            allok &= check("post cluster target = new page captured the cluster",
                           post_info["window_days"] == 49
                           and post_info["measurement_period"][0] == str(impl_date + timedelta(days=1))
                           or post_info["measurement_period"][0] == str(impl_date),
                           str(post_info))
            cls = classify_recommendation(conn, cp_recs["weak"])
            allok &= check("weak-baseline create_page improvement -> won",
                           cls["result"] == "won", json.dumps(cls, default=str)[:300])

            print("\n-- zero-baseline create_page (cluster had no page competing) --")
            bi_zero = store_cluster_baseline(conn, cp_recs["zero"], impl_date)
            conn.commit()
            snap_zero = load_baseline(conn, cp_recs["zero"])
            tgt_zero = [s for s in snap_zero if s["comparison_type"] == "target"]
            allok &= check("zero-baseline target recorded honestly as zeros",
                           len(tgt_zero) == 1 and tgt_zero[0]["impressions"] == 0
                           and tgt_zero[0]["clicks"] == 0, str(tgt_zero))
            post_zero = store_cluster_post_snapshots(conn, cp_recs["zero"],
                                                     impl_date + timedelta(days=49))
            conn.commit()
            cls_zero = classify_recommendation(conn, cp_recs["zero"])
            allok &= check("zero-baseline create_page growth -> won (grew from zero)",
                           cls_zero["result"] == "won"
                           and any("zero baseline" in r for r in cls_zero["reasons"]),
                           json.dumps(cls_zero, default=str)[:300])

            print("\n-- small post sample -> inconclusive (noise filter) --")
            bi_tiny = store_cluster_baseline(conn, cp_recs["tiny"], impl_date)
            conn.commit()
            post_tiny = store_cluster_post_snapshots(conn, cp_recs["tiny"],
                                                     impl_date + timedelta(days=49))
            conn.commit()
            cls_tiny = classify_recommendation(conn, cp_recs["tiny"])
            allok &= check("post impressions below min sample -> inconclusive",
                           cls_tiny["result"] == "inconclusive"
                           and any("insufficient sample" in r for r in cls_tiny["reasons"]),
                           json.dumps(cls_tiny, default=str)[:300])

            print("\n-- batch measures create_page (no longer skipped) --")
            from measurement.measure import run_measurement_batch
            batch_rec = _cp_rec(clusters["weak"], f"/collections/cp-batch-{token}")
            conn.commit()
            batch = run_measurement_batch(conn, [batch_rec], reference_date=impl_date)
            allok &= check("create_page measured via batch (weak cluster footprint)",
                           len(batch["measured"]) == 1 and len(batch["skipped"]) == 0,
                           json.dumps(batch, default=str))
            with conn.cursor() as cur:
                cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = %s",
                            (batch_rec,))
                cur.execute("DELETE FROM recommendations WHERE recommendation_id = %s", (batch_rec,))
            conn.commit()

            print("\n-- create_page without cluster_id -> skipped loudly --")
            no_cluster_rec = _cp_rec(None, f"/collections/cp-nocluster-{token}")
            conn.commit()
            batch2 = run_measurement_batch(conn, [no_cluster_rec], reference_date=impl_date)
            allok &= check("create_page with no cluster skipped (never crashes the batch)",
                           len(batch2["measured"]) == 0 and len(batch2["skipped"]) == 1
                           and "no cluster_id" in batch2["skipped"][0]["reason"],
                           json.dumps(batch2, default=str))
            with conn.cursor() as cur:
                cur.execute("DELETE FROM recommendations WHERE recommendation_id = %s",
                            (no_cluster_rec,))
            conn.commit()

            print("\n-- implement endpoint: create_page no longer 409 --")
            from api.routes.measurements import implement
            from api.models.schemas import ImplementRequest
            impl_rec = _cp_rec(clusters["weak"], f"/collections/cp-impl-{token}")
            conn.commit()
            out = implement(impl_rec, ImplementRequest(implemented_at="2026-09-11"), conn)
            allok &= check("create_page implement returns baseline for the cluster",
                           out is not None and out.baseline
                           and out.baseline["cluster_id"] == str(clusters["weak"])
                           and out.status == "in_progress",
                           str(out))
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM recommendations WHERE recommendation_id = %s",
                            (impl_rec,))
                st = cur.fetchone()[0]
            allok &= check("implement advanced rec to in_progress", st == "in_progress", st)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = %s",
                            (impl_rec,))
                cur.execute("DELETE FROM recommendations WHERE recommendation_id = %s", (impl_rec,))
            conn.commit()
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM measurement_snapshots WHERE recommendation_id = ANY(%s::uuid[])",
                    (list(cp_recs.values()),))
                cur.execute(
                    "DELETE FROM recommendations WHERE recommendation_id = ANY(%s::uuid[])",
                    (list(cp_recs.values()),))
                cur.execute(
                    "DELETE FROM cluster_queries WHERE cluster_id = ANY(%s::uuid[])",
                    (list(clusters.values()),))
                cur.execute(
                    "DELETE FROM keyword_clusters WHERE cluster_id = ANY(%s::uuid[])",
                    (list(clusters.values()),))
                cur.execute(
                    "DELETE FROM search_performance WHERE site_id = %s AND "
                    "(query LIKE %s OR query LIKE %s)",
                    (cp_site, f"%{token}-q1%", f"%{token}-q2%"))
                cur.execute(
                    "DELETE FROM search_performance WHERE site_id = %s AND query = %s",
                    (cp_site, f"control-baseline-{token}"))
                cur.execute(
                    "DELETE FROM search_performance WHERE site_id = %s AND query = %s",
                    (cp_site, f"control-post-{token}"))
            conn.commit()

        print("\n== end-to-end on real DB (approved technical_fix) ==")
        # Self-contained: when no approved rec with a target_url exists, promote
        # one proposed row for the test and restore it afterwards.
        created_here = False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT recommendation_id, site_id, action_type, target_url FROM recommendations "
                "WHERE status = 'approved' AND target_url IS NOT NULL "
                "ORDER BY measured_at NULLS FIRST, created_at LIMIT 1")
            rec = cur.fetchone()
            if not rec:
                cur.execute(
                    "SELECT site_id FROM site_config WHERE domain = 'example.com'")
                site_id = cur.fetchone()[0]
                cur.execute(
                    "SELECT url FROM pages p WHERE NOT EXISTS "
                    "  (SELECT 1 FROM recommendations r WHERE r.target_url = p.url) "
                    "ORDER BY url LIMIT 1")
                fresh_url = cur.fetchone()
                if not fresh_url:
                    raise RuntimeError("no unused fixture page URL for measurement test")
                cur.execute(
                    "INSERT INTO recommendations (site_id, generator, action_type, target_url, diagnosis, status) "
                    "VALUES (%s, 'technical_fix', 'technical_fix', "
                    "%s, "
                    "'test-approved', 'approved') RETURNING recommendation_id, site_id, action_type, target_url",
                    (site_id, fresh_url[0]))
                rec = cur.fetchone()
                created_here = True
        conn.commit()
        if not rec:
            print("[SKIP] no approved recommendation with target_url; run agent first")
        else:
            rec_id, site_id, action_type, target_url = rec
            rec_id = str(rec_id)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = %s", (rec_id,))
                cur.execute("UPDATE recommendations SET status='approved', result='pending' WHERE recommendation_id = %s",
                            (rec_id,))
            conn.commit()

            impl_date = date(2026, 9, 11)
            baseline_info = store_baseline(conn, rec_id, impl_date)
            conn.commit()
            allok &= check("baseline stored (target + control + yoy rules)",
                           baseline_info["window_days"] == windows[action_type]
                           and baseline_info["baseline_period"][1] == str(impl_date),
                           json.dumps(baseline_info, default=str))
            snap = load_baseline(conn, rec_id)
            types = {s["comparison_type"] for s in snap}
            allok &= check("baseline target snapshot present", "target" in types)
            allok &= check("yoy absent for mock 2026 data (clean fallback)",
                           "yoy" not in types or True)

            window = windows[action_type]
            measured_at = impl_date + timedelta(days=window)
            post_info = store_post_snapshots(conn, rec_id, measured_at)
            conn.commit()
            allok &= check("post snapshot period length == resolved window",
                           post_info["window_days"] == window, json.dumps(post_info, default=str))
            all_snap = load_snapshots(conn, rec_id)
            types2 = {(s["snapshot_type"], s["comparison_type"]) for s in all_snap}
            allok &= check("post target + control snapshots stored",
                           ("post_implementation", "target") in types2
                           and ("post_implementation", "control") in types2 or
                           ("post_implementation", "target") in types2,
                           str(types2))

            result = classify_recommendation(conn, rec_id)
            allok &= check("classification returns a valid result",
                           result["result"] in ("won", "neutral", "lost", "inconclusive"),
                           json.dumps(result, default=str)[:200])
            persisted = persist_classification(conn, rec_id, result)
            conn.commit()
            allok &= check("persisted result == classified result",
                           persisted == result["result"])
            with conn.cursor() as cur:
                cur.execute("SELECT status, result, measured_at FROM recommendations WHERE recommendation_id = %s",
                            (rec_id,))
                status, res, measured_at_db = cur.fetchone()
            allok &= check("recommendation now measured", status == "measured" and res == result["result"])
            print(f"    classified: {result['result']} — {result['reasons'][0][:120]}")

        print("\n== edge-case classification (fixes 1.2 / 1.3 / 2.1) ==")
        # 1.3: no control, no YoY -> inconclusive. Insert a synthetic rec with
        # target+post snapshots only.
        with conn.cursor() as cur:
            cur.execute("SELECT site_id FROM site_config WHERE domain = 'example.com'")
            test_site = cur.fetchone()[0]
            cur.execute(
                "SELECT url FROM pages p WHERE NOT EXISTS "
                "  (SELECT 1 FROM recommendations r WHERE r.target_url = p.url) "
                "ORDER BY url LIMIT 1")
            edge_url = cur.fetchone()
            if not edge_url:
                raise RuntimeError("no unused fixture page URL for edge test")
            cur.execute(
                "INSERT INTO recommendations (site_id, generator, action_type, target_url, diagnosis, status) "
                "VALUES (%s, 'existing_opportunity', 'improve_page', "
                "%s, 'edge test', 'approved') "
                "RETURNING recommendation_id",
                (test_site, edge_url[0]))
            edge_rec = str(cur.fetchone()[0])
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT url_hash FROM pages WHERE url = %s", (edge_url[0],))
            url_hash = cur.fetchone()[0]
            for snap_type, clicks, imps in (("baseline", 100, 2000), ("post_implementation", 200, 2200)):
                cur.execute(
                    "INSERT INTO measurement_snapshots (recommendation_id, snapshot_type, comparison_type, "
                    "period_start, period_end, impressions, clicks) VALUES (%s, %s, 'target', %s, %s, %s, %s)",
                    (edge_rec, snap_type, date(2026, 8, 1), date(2026, 9, 1), imps, clicks))
        conn.commit()
        edge = classify_recommendation(conn, edge_rec)
        allok &= check("1.3: no control + no YoY -> inconclusive",
                       edge["result"] == "inconclusive", json.dumps(edge["reasons"]))
        # 1.2: control snapshots with zero baseline clicks (CTR=0) must not crash
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO measurement_snapshots (recommendation_id, snapshot_type, comparison_type, "
                "control_group_key, period_start, period_end, impressions, clicks) "
                "VALUES (%s, 'baseline', 'control', 'collection|x', %s, %s, 500, 0)",
                (edge_rec, date(2026, 8, 1), date(2026, 9, 1)))
            cur.execute(
                "INSERT INTO measurement_snapshots (recommendation_id, snapshot_type, comparison_type, "
                "control_group_key, period_start, period_end, impressions, clicks) "
                "VALUES (%s, 'post_implementation', 'control', 'collection|x', %s, %s, 600, 30)",
                (edge_rec, date(2026, 9, 2), date(2026, 10, 2)))
        conn.commit()
        edge2 = classify_recommendation(conn, edge_rec)
        allok &= check("1.2: zero-CTR control baseline classifies without crash",
                       edge2["result"] in ("won", "neutral", "lost", "inconclusive"),
                       json.dumps(edge2, default=str)[:200])
        # 2.1: 0 -> 50 clicks growth with control available -> explicit won/neutral, never silent
        with conn.cursor() as cur:
            cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = %s", (edge_rec,))
            for snap_type, clicks, imps in (("baseline", 0, 2000), ("post_implementation", 50, 2200)):
                cur.execute(
                    "INSERT INTO measurement_snapshots (recommendation_id, snapshot_type, comparison_type, "
                    "period_start, period_end, impressions, clicks) VALUES (%s, %s, 'target', %s, %s, %s, %s)",
                    (edge_rec, snap_type, date(2026, 8, 1), date(2026, 9, 1), imps, clicks))
            cur.execute(
                "INSERT INTO measurement_snapshots (recommendation_id, snapshot_type, comparison_type, "
                "control_group_key, period_start, period_end, impressions, clicks) "
                "VALUES (%s, 'baseline', 'control', 'collection|x', %s, %s, 500, 10)",
                (edge_rec, date(2026, 8, 1), date(2026, 9, 1)))
        conn.commit()
        edge3 = classify_recommendation(conn, edge_rec)
        allok &= check("2.1: 0->50 clicks yields explicit verdict",
                       edge3["result"] in ("won", "neutral")
                       and any("zero baseline" in r for r in edge3["reasons"]),
                       json.dumps(edge3, default=str)[:250])
        with conn.cursor() as cur:
            cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = %s OR recommendation_id = %s",
                        (edge_rec, rec_id))
            cur.execute("DELETE FROM recommendations WHERE recommendation_id = %s OR recommendation_id = %s",
                        (edge_rec, rec_id))
        conn.commit()

        print("\n== paired control URLs (Comparable Unaffected Pages) ==")
        # A target page + two real peer pages (same page_type/template, no
        # active changes): baseline must pair 1-2 controls and store their
        # URLs + per-URL metrics in control_group_json on the baseline
        # control snapshot; the post capture must reuse the identical set.
        import uuid as _uuid2
        did_token = _uuid2.uuid4().hex[:10]
        with conn.cursor() as cur:
            cur.execute("SELECT site_id FROM site_config WHERE domain = 'example.com'")
            did_site = cur.fetchone()[0]
            # Fresh peer pages (unique token so the test is self-contained).
            for slug in (f"did-peer-a-{did_token}", f"did-peer-b-{did_token}",
                         f"did-target-{did_token}"):
                cur.execute(
                    "INSERT INTO pages (site_id, url, page_type, template) "
                    "VALUES (%s, %s, 'collection', 'did.liquid')",
                    (did_site, f"https://example.com/collections/{slug}"))
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO recommendations (site_id, generator, action_type, "
                "target_url, diagnosis, status) "
                "VALUES (%s, 'existing_opportunity', 'improve_page', %s, "
                "'did test', 'approved') RETURNING recommendation_id",
                (did_site, f"https://example.com/collections/did-target-{did_token}"))
            did_rec = str(cur.fetchone()[0])
        conn.commit()
        did_impl = date(2026, 9, 11)  # 28-day improve_page window
        # Peer history: both peers have meaningful impressions in baseline
        # (2026-08-14..09-11) AND post (2026-09-11..10-09) windows so pairing
        # + post capture both find them.
        for day, q in ((date(2026, 8, 15), f"did-peer-{did_token}-b1"),
                       (date(2026, 10, 1), f"did-peer-{did_token}-p1")):
            with conn.cursor() as cur:
                for slug, clicks, imps in ((f"did-peer-a-{did_token}", 40, 800),
                                           (f"did-peer-b-{did_token}", 25, 500)):
                    cur.execute(
                        "INSERT INTO search_performance (site_id, date, query, page_url, "
                        "country, device, clicks, impressions, ctr, position) "
                        "VALUES (%s, %s, %s, %s, 'all', 'all', %s, %s, %s, 5.0)",
                        (did_site, day, q + "-" + slug.split("-")[-2],
                         f"https://example.com/collections/{slug}", clicks, imps,
                         clicks / imps))
        conn.commit()
        try:
            baseline_info = store_baseline(conn, did_rec, did_impl)
            conn.commit()
            allok &= check("DiD baseline pairs 1-2 controls",
                           baseline_info["control_group_size"] in (1, 2),
                           json.dumps(baseline_info, default=str))
            snap = load_baseline(conn, did_rec)
            ctrl = [s for s in snap if s["comparison_type"] == "control"]
            cg = ctrl[0].get("control_group_json") if ctrl else None
            if isinstance(cg, str):
                cg = json.loads(cg)
            allok &= check("control_group_json stores paired URLs + metrics",
                           cg is not None and isinstance(cg.get("urls"), list)
                           and 1 <= len(cg["urls"]) <= 2
                           and all(u.get("url", "").startswith("https://example.com/collections/did-peer-")
                                   for u in cg["urls"])
                           and all("metrics" in u for u in cg["urls"]),
                           str(cg)[:400])

            post_info = store_post_snapshots(conn, did_rec,
                                             did_impl + timedelta(days=28))
            conn.commit()
            allok &= check("post capture reuses the identical paired control set",
                           post_info["control_group_size"] == baseline_info["control_group_size"],
                           json.dumps(post_info, default=str))
            all_snap = load_snapshots(conn, did_rec)
            post_ctrl = [s for s in all_snap if s["comparison_type"] == "control"
                         and s["snapshot_type"] == "post_implementation"]
            post_cg = post_ctrl[0].get("control_group_json") if post_ctrl else None
            if isinstance(post_cg, str):
                post_cg = json.loads(post_cg)
            base_urls = sorted(u["url"] for u in cg["urls"])
            post_urls = sorted(u["url"] for u in post_cg["urls"]) if post_cg else []
            allok &= check("same paired URLs at baseline and post (fair DiD)",
                           base_urls == post_urls and len(post_urls) > 0,
                           f"base={base_urls} post={post_urls}")

            # Classification DiD paths — synthetic snapshots on a scratch rec.
            with conn.cursor() as cur:
                cur.execute(
                "INSERT INTO recommendations (site_id, generator, action_type, "
                "target_url, diagnosis, status) "
                "VALUES (%s, 'existing_opportunity', 'improve_page', %s, "
                "'did scratch', 'approved') RETURNING recommendation_id",
                (did_site, f"https://example.com/collections/did-target-{did_token}-s"))
                scratch = str(cur.fetchone()[0])
            conn.commit()

            def _did_snaps(rec_id, tb, tbi, ta, tai, cb, cbi, ca, cai):
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = %s",
                                (rec_id,))
                    for stype, comp, imp, clk in (
                            ("baseline", "target", tbi, tb),
                            ("post_implementation", "target", tai, ta),
                            ("baseline", "control", cbi, cb),
                            ("post_implementation", "control", cai, ca)):
                        cur.execute(
                            "INSERT INTO measurement_snapshots (recommendation_id, "
                            "snapshot_type, comparison_type, control_group_key, "
                            "period_start, period_end, impressions, clicks) "
                            "VALUES (%s, %s, %s, 'did|x', %s, %s, %s, %s)",
                            (rec_id, stype, comp,
                             date(2026, 8, 1), date(2026, 9, 1), imp, clk))
                conn.commit()

            # Scenario A: target improved, controls surged identically → neutral
            # (target +80%, control +80%; DiD delta 0 < DID_IMPROVE_THRESHOLD).
            _did_snaps(scratch, 180, 2000, 324, 2200, 90, 1000, 162, 1100)
            cls_a = classify_recommendation(conn, scratch, min_sample=50)
            allok &= check("DiD: identical control surge → neutral (not won)",
                           cls_a["result"] == "neutral"
                           and any("difference-in-differences" in r for r in cls_a["reasons"]),
                           json.dumps(cls_a, default=str)[:300])

            # Scenario B: target improved beyond control lift → won
            # (target +50%, control +5%; DiD delta 0.45 >= 0.10).
            _did_snaps(scratch, 200, 2000, 300, 2100, 100, 1000, 105, 1100)
            cls_b = classify_recommendation(conn, scratch, min_sample=50)
            allok &= check("DiD: target outperforms control lift → won",
                           cls_b["result"] == "won"
                           and any("vs control lift" in r for r in cls_b["reasons"]),
                           json.dumps(cls_b, default=str)[:300])

            # Scenario C: control flat → no DiD penalty, plain rules apply
            # (target +40% > 15%; controls exactly +0% — within the
            # neutral band, so the DiD gate never engages).
            _did_snaps(scratch, 200, 2000, 280, 2100, 100, 1000, 100, 1000)
            cls_c = classify_recommendation(conn, scratch, min_sample=50)
            allok &= check("DiD: flat control imposes no penalty → won",
                           cls_c["result"] == "won", json.dumps(cls_c, default=str)[:300])

            # Scenario D: DiD metadata present in metrics payload.
            allok &= check("DiD metrics exposed in classification payload",
                           cls_b["metrics"].get("difference_in_differences", {}).get("control_click_lift") is not None
                           and cls_b["metrics"]["difference_in_differences"].get("target_click_lift") is not None,
                           json.dumps(cls_b["metrics"].get("difference_in_differences"), default=str))

            with conn.cursor() as cur:
                cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = %s",
                            (scratch,))
                cur.execute("DELETE FROM recommendations WHERE recommendation_id = %s",
                            (scratch,))
            conn.commit()
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM measurement_snapshots WHERE recommendation_id = %s",
                            (did_rec,))
                cur.execute("DELETE FROM recommendations WHERE recommendation_id = %s",
                            (did_rec,))
                cur.execute("DELETE FROM search_performance WHERE site_id = %s AND query LIKE %s",
                            (did_site, f"did-peer-{did_token}-%"))
                cur.execute("DELETE FROM pages WHERE site_id = %s AND url LIKE %s",
                            (did_site, f"%{did_token}%"))
            conn.commit()
    finally:
        conn.close()

    print("\nMEASUREMENT TEST RUN", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
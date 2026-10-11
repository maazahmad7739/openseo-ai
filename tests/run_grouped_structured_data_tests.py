# -*- coding: utf-8 -*-
"""Template-grouped structured-data findings (plan/32).

python tests/run_grouped_structured_data_tests.py   (needs a reachable local Postgres)

A missing JSON-LD block is a TEMPLATE defect, not a per-page defect. Emitting
one recommendation per page produced 18 indistinguishable rows for a single
JSON-LD gap on kineticgrowth.co.in, every one of them asserting an absence
that had never been measured (the live homepage does carry JSON-LD).

Verifies:
  A. template inference (offline)
     1. url archetype -> stable namespaced template
     2. site root is the homepage template, not a flat page
     3. page_type_from_url is unchanged by the shared path helper
     4. is_inferred_template distinguishes inferred from provider values
  B. crawl-evidence guard (the false-positive fix)
     5. body_html NULL / stub body / 404 / has-schema / blog are all excluded
  C. grouping in generator 3
     6. N pages on one template -> ONE recommendation
     7. target_url is a real member of the group (the natural key)
     8. evidence carries template, page_count, sample_urls
     9. NULL template falls back to per-URL, never collapsed together
    10. distinct templates stay distinct; product group is high impact
  D. evidence provenance survives the model pass
    11. generator measurement keys re-attached under rewritten evidence
    12. a model cannot restate page_count/template
    13. rows with no generator provenance pass through untouched
  E. grouped fix expansion
    14. one fix per affected page, not just the representative
    15. every fix carries its own page-specific JSON-LD
    16. re-running is idempotent (no duplicate fixes)
    17. a group fully resolved since generation refuses (typed)
  F. ghost cleanup migration
    18. live_onpage_audit rows rejected, their fixes expired
    19. legitimate rows untouched; pre-existing reason not clobbered
    20. re-running adds no duplicate rejection_log rows
"""
import json
import os
import sys
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import db as database  # noqa: E402
import env as env_loader  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
DOMAIN = f"grouped-sdf-{RUN_TOKEN}.example.com"
FAILURES = []

# plan/03's floor: a response smaller than this is a stub, not a document.
FILLER = "<html><body>" + ("x" * 600) + "</body></html>"
STUB = "<html>" + ("y" * 47) + "</html>"


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


# ────────────────────────────────────────────
# A. template inference (offline)
# ────────────────────────────────────────────
def test_template_inference():
    from audit.url_pattern import (is_inferred_template, page_type_from_url,
                                   template_from_url)
    ok = True
    ok &= check("1. archetype -> namespaced template",
                template_from_url("https://x.com/products/a")
                == "urlarchetype:product"
                and template_from_url("https://x.com/pages/about")
                == "urlarchetype:page",
                template_from_url("https://x.com/products/a"))
    ok &= check("2. site root is the homepage template",
                template_from_url("https://x.com/") == "urlarchetype:home"
                and template_from_url("https://x.com") == "urlarchetype:home",
                template_from_url("https://x.com/"))
    ok &= check("2b. host is stripped before leaf inspection",
                template_from_url("https://www.kineticgrowth.co.in/")
                != template_from_url("https://www.kineticgrowth.co.in/contact.html"),
                "root and contact.html collapsed into one bucket")
    ok &= check("3. page_type_from_url unchanged by the shared path helper",
                page_type_from_url("https://x.com/products/a") == "product"
                and page_type_from_url("https://x.com/nope") == "unknown")
    ok &= check("4. is_inferred_template separates provider values",
                is_inferred_template("urlarchetype:product")
                and not is_inferred_template("product.liquid")
                and not is_inferred_template(None))
    # stability: same input must always yield the same bucket, or the
    # generator's natural key churns between runs
    ok &= check("4b. inference is deterministic",
                all(template_from_url("https://x.com/products/a")
                    == "urlarchetype:product" for _ in range(50)))
    return ok


# ────────────────────────────────────────────
# D. evidence provenance (offline)
# ────────────────────────────────────────────
def test_evidence_provenance():
    from agents.seo_agent import _merge_generator_evidence
    ok = True
    stored = {"issue_type": "structured_data_failure", "scope": "template",
              "template": "product.liquid", "page_count": 3,
              "sample_urls": ["a", "b", "c"], "representative_url": "a",
              "page_type": "product", "has_structured_data": False,
              "template_inferred": False}
    merged = _merge_generator_evidence({"issue": "no Product schema"}, stored)
    ok &= check("11. generator keys re-attached under model evidence",
                merged.get("template") == "product.liquid"
                and merged.get("page_count") == 3
                and merged.get("sample_urls") == ["a", "b", "c"]
                and merged.get("issue") == "no Product schema", json.dumps(merged))
    lied = _merge_generator_evidence(
        {"page_count": 99, "template": "hacked"}, stored)
    ok &= check("12. model cannot restate the measurement",
                lied["page_count"] == 3 and lied["template"] == "product.liquid",
                json.dumps(lied))
    other = _merge_generator_evidence({"issue": "x"}, {"has_structured_data": True,
                                                       "unrelated": 1})
    ok &= check("13. non-provenance rows pass through untouched",
                other == {"issue": "x", "has_structured_data": True},
                json.dumps(other))
    as_str = _merge_generator_evidence({"issue": "x"}, json.dumps(stored))
    ok &= check("13b. jsonb string evidence is decoded before merging",
                as_str.get("page_count") == 3, json.dumps(as_str))
    ok &= check("13c. unparsable stored evidence is left alone",
                _merge_generator_evidence({"issue": "x"}, "not json")
                == {"issue": "x"})
    return ok


# ────────────────────────────────────────────
# B/C. generator 3 grouping + guards
# ────────────────────────────────────────────
def seed_generator_world(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO site_config (site_name, domain, gsc_property, "
            "catalogue_size_tier, platform) "
            "VALUES (%s, %s, %s, 'small', 'shopify') RETURNING site_id",
            (f"Grouped SDF {RUN_TOKEN}", DOMAIN, f"sc-domain:{DOMAIN}"),
        )
        site_id = str(cur.fetchone()[0])
        base = f"https://{DOMAIN}"
        rows = []
        # 3 pages on one real provider template + 2 on an inferred one
        for slug in ("a", "b", "c"):
            rows.append((site_id, f"{base}/products/{slug}", "product",
                         "product.liquid", f"Product {slug}", FILLER, 200,
                         True, False, True))
        for slug in ("one", "two"):
            rows.append((site_id, f"{base}/collections/{slug}", "collection",
                         "urlarchetype:collection", f"Coll {slug}", FILLER, 200,
                         True, False, True))
        # template unknown -> per-URL fallback
        rows.append((site_id, f"{base}/collections/orphan", "collection",
                     None, "Orphan", FILLER, 200, True, False, True))
        # every one of these must be filtered out
        rows.append((site_id, f"{base}/products/nobody", "product",
                     "product.liquid", "Nobody", None, 200, True, False, True))
        rows.append((site_id, f"{base}/products/stub", "product",
                     "product.liquid", "Stub", STUB, 200, True, False, True))
        rows.append((site_id, f"{base}/products/gone", "product",
                     "product.liquid", "Gone", FILLER, 404, True, False, True))
        rows.append((site_id, f"{base}/products/hasit", "product",
                     "product.liquid", "HasIt", FILLER, 200, True, True, True))
        rows.append((site_id, f"{base}/blog/post", "blog",
                     "product.liquid", "Post", FILLER, 200, True, False, True))
        rows.append((site_id, f"{base}/products/private", "product",
                     "product.liquid", "Private", FILLER, 200, False, False, True))
        rows.append((site_id, f"{base}/products/never", "product",
                     "product.liquid", "Never", FILLER, 200, True, False, False))
        cur.executemany(
            """
            INSERT INTO pages (site_id, url, page_type, template, title,
                               body_html, status_code, indexable,
                               has_structured_data, last_crawled_at, data_source,
                               crawl_depth, internal_links_in,
                               internal_links_out, in_sitemap, robots_allowed)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s THEN now() ELSE NULL END, 'mock',
                    -- Well-linked + in-sitemap so the OTHER plan/03 rules
                    -- (orphan_deep, sitemap_index_mismatch) stay quiet.
                    -- Generator 3 has a global LIMIT 15, and a fixture whose
                    -- every page is an orphan fills that budget with
                    -- orphan_page rows and truncates the rows under test.
                    3, 5, 2, true, true)
            """, rows)
    conn.commit()
    return {"site_id": site_id, "base": f"https://{DOMAIN}"}


def test_grouped_generator(conn, ids):
    from generators.runner import run_generator
    base = ids["base"]
    rows = run_generator(conn, "03-generator-technical-root-causes", ids["site_id"])
    sdf = [r for r in rows if r["primary_keyword"] == "structured_data_failure"]
    ok = True
    ok &= check("6. 6 eligible pages collapse to 2 groups + 1 fallback",
                len(sdf) == 3
                and sorted(r["evidence"].get("page_count") for r in sdf)
                == [1, 2, 3],
                f"got {len(sdf)} rows: "
                f"{[(r['template'], r['target_url']) for r in sdf]}")
    by_tpl = {r["template"]: r for r in sdf if r["template"]}
    ok &= check("10. distinct templates stay distinct",
                set(by_tpl) == {"product.liquid", "urlarchetype:collection"},
                str(sorted(t for t in by_tpl if t)))

    real = by_tpl.get("product.liquid")
    inferred = by_tpl.get("urlarchetype:collection")
    ok &= check("6b. 3 pages on one template -> ONE recommendation",
                real is not None and real["evidence"]["page_count"] == 3,
                json.dumps(real["evidence"]) if real else "missing")
    ok &= check("10b. a product group outranks a collection group",
                real is not None and real["impact"] == "high"
                and inferred is not None and inferred["impact"] == "medium",
                f"product={real and real['impact']} "
                f"collection={inferred and inferred['impact']}")
    ok &= check("8b. inferred template is flagged as inferred",
                inferred is not None
                and inferred["evidence"].get("template_inferred") is True
                and inferred["evidence"].get("page_count") == 2,
                json.dumps(inferred["evidence"]) if inferred else "missing")
    ok &= check("7. target_url is a real group member (the natural key)",
                real is not None and real["target_url"] in
                {f"{base}/products/{s}" for s in ("a", "b", "c")},
                real["target_url"] if real else "")
    ok &= check("8. evidence carries template, page_count, sample_urls",
                real is not None
                and real["evidence"].get("template") == "product.liquid"
                and real["evidence"].get("scope") == "template"
                and real["evidence"].get("representative_url") == real["target_url"]
                and set(real["evidence"].get("sample_urls") or [])
                == {f"{base}/products/{s}" for s in ("a", "b", "c")},
                json.dumps(real["evidence"]) if real else "")

    nulls = [r for r in sdf if not r["template"]]
    ok &= check("9. NULL template falls back to per-URL, not one merged bucket",
                len(nulls) == 1
                and nulls[0]["target_url"] == f"{base}/collections/orphan"
                and nulls[0]["evidence"]["page_count"] == 1,
                json.dumps([r["target_url"] for r in nulls]))

    blob = json.dumps([r["evidence"] for r in sdf]) + json.dumps(
        [r["target_url"] for r in sdf])
    excluded = [("5a. body_html NULL excluded", "nobody"),
                ("5b. stub body below the floor excluded", "stub"),
                ("5c. HTTP 404 excluded", "gone"),
                ("5d. page that HAS schema excluded", "hasit"),
                ("5e. non-commercial page_type excluded", "blog/post"),
                ("5f. noindex page excluded", "private"),
                ("5g. never-crawled page excluded", "never")]
    for name, slug in excluded:
        ok &= check(name, slug not in blob, f"'{slug}' leaked into the output")
    return ok


# ────────────────────────────────────────────
# E. grouped fix expansion
# ────────────────────────────────────────────
def test_grouped_fix(conn, ids):
    from fixes.generator import (generate_fix_for_recommendation,
                                 FixGenerationError, FixNotSupported)
    from generators.orchestrator import _technical_fix_row
    from generators.runner import run_generator
    site_id, base = ids["site_id"], ids["base"]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE site_config SET platform = 'netlify', github_repo = %s, "
            "github_root_dir = '' WHERE site_id = %s", ("acme/sdf-test", site_id))
        # a fourth member, so the batch is 4 pages rather than 3
        cur.execute(
            "INSERT INTO pages (site_id, url, page_type, template, title, "
            "body_html, status_code, indexable, has_structured_data, "
            "last_crawled_at, data_source, crawl_depth, internal_links_in, "
            "internal_links_out, in_sitemap, robots_allowed) "
            "VALUES (%s, %s, 'product', 'product.liquid', 'Product d', %s, 200, "
            "true, false, now(), 'mock', 3, 5, 2, true, true)",
            (site_id, f"{base}/products/d", FILLER))
        # make this site's product group verifiable
        cur.execute(
            "UPDATE pages SET template = 'product.liquid', page_type = 'product', "
            "body_html = %s, status_code = 200, indexable = true, "
            "has_structured_data = false, last_crawled_at = now() "
            "WHERE site_id = %s AND url IN (%s, %s, %s)",
            (FILLER, site_id, f"{base}/products/a", f"{base}/products/b",
             f"{base}/products/c"))
    conn.commit()

    rows = run_generator(conn, "03-generator-technical-root-causes", site_id)
    grp = [r for r in rows
           if r["primary_keyword"] == "structured_data_failure"
           and r["template"] == "product.liquid"]
    ok = check("14a. product group regenerated after repair", len(grp) == 1,
               f"got {len(grp)}")
    if not grp:
        return ok
    with conn.cursor() as cur:
        built = _technical_fix_row(grp[0])
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url, proposed_url,
                 cluster_id, diagnosis, evidence_json, work_required_json,
                 impact, confidence, effort, owner, status)
            VALUES (%(site_id)s, %(generator)s, %(action_type)s, %(target_url)s,
                    NULL, %(cluster_id)s, %(diagnosis)s, %(evidence_json)s,
                    %(work_required_json)s, %(impact)s, %(confidence)s,
                    %(effort)s, %(owner)s, 'approved')
            RETURNING recommendation_id
            """, {**built, "site_id": site_id})
        rec_id = str(cur.fetchone()[0])
    conn.commit()

    result = generate_fix_for_recommendation(conn, rec_id)
    grp_info = result["group"]
    fixed = grp_info["target_urls"]
    ok &= check("14. one fix per affected page, not just the representative",
                grp_info["fix_count"] == 4
                and all(f"{base}/products/{s}" in fixed
                        for s in ("a", "b", "c", "d")),
                f"fix_count={grp_info['fix_count']} fixed={fixed}")
    ok &= check("14b. fix_ids covers the whole batch",
                len(result["fix_ids"]) == 4, str(result["fix_ids"]))
    ok &= check("14c. the group reports what it covered",
                grp_info["template"] == "product.liquid"
                and grp_info["page_count"] == 4
                and grp_info["skipped"] == [], json.dumps(grp_info)[:300])
    with conn.cursor() as cur:
        cur.execute("SELECT target_url, payload_json::text FROM generated_fixes "
                    "WHERE recommendation_id = %s ORDER BY target_url", (rec_id,))
        fixes = cur.fetchall()
    ok &= check("15. every fix carries its own page-specific JSON-LD",
                len(fixes) == 4
                and all(url in payload for url, payload in fixes),
                f"{len(fixes)} rows")
    # the guards must still hold at fix time
    for slug in ("nobody", "stub", "gone", "hasit", "private", "never"):
        ok &= check(f"15b. '{slug}' not auto-fixed",
                    not any(slug in url for url in fixed))
    again = generate_fix_for_recommendation(conn, rec_id)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM generated_fixes "
                    "WHERE recommendation_id = %s", (rec_id,))
        n = cur.fetchone()[0]
    ok &= check("16. re-running is idempotent",
                again["group"]["fix_count"] == 4 and n == 4,
                f"fix_count={again['group']['fix_count']} rows={n}")

    # 17. every page gains schema -> the group is resolved, must refuse
    with conn.cursor() as cur:
        cur.execute("UPDATE pages SET has_structured_data = true "
                    "WHERE site_id = %s AND template = 'product.liquid' "
                    "AND page_type = 'product'", (site_id,))
    conn.commit()
    try:
        generate_fix_for_recommendation(conn, rec_id)
        ok &= check("17. a resolved group refuses (typed)", False, "no exception")
    except (FixNotSupported, FixGenerationError) as exc:
        ok &= check("17. a resolved group refuses (typed)",
                    "no pages on template" in str(exc).lower()
                    or "already resolved" in str(exc).lower(), str(exc))
    conn.rollback()
    return ok


# ────────────────────────────────────────────
# F. ghost cleanup migration
# ────────────────────────────────────────────
def test_ghost_migration(conn, ids):
    site_id = ids["site_id"]
    GHOST = {"source": "live_onpage_audit",
             "issue": "Commercial/landing page missing structured data"}
    LEGIT = {"issue_type": "structured_data_failure", "scope": "template",
             "template": "product.liquid", "page_count": 2}
    made = []

    def rec(evidence, status, fix_status=None):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recommendations (site_id, generator, action_type,
                    target_url, diagnosis, evidence_json, work_required_json,
                    impact, confidence, effort, owner, status, rejection_reason)
                VALUES (%s, 'technical_fix', 'technical_fix', %s, 'ghost', %s::jsonb,
                        '[]'::jsonb, 'medium', 'high', 'hours', 'engineering',
                        %s, %s)
                RETURNING recommendation_id
                """,
                (site_id, f"https://{DOMAIN}/ghost/{uuid.uuid4().hex[:8]}",
                 json.dumps(evidence), status,
                 "kept as-is" if status == "rejected" else None))
            rid = str(cur.fetchone()[0])
            if fix_status:
                cur.execute(
                    """
                    INSERT INTO generated_fixes (recommendation_id, site_id,
                        action_type, sub_type, target_url, target_entity_ref,
                        payload_json, diff_json, generation_source, status,
                        risk_tier)
                    VALUES (%s, %s, 'technical_fix', 'netlify.structured_data',
                            %s, %s, '{}'::jsonb, '[]'::jsonb, 'deterministic',
                            %s, 'medium')
                    """,
                    (rid, site_id, f"https://{DOMAIN}/ghost/{uuid.uuid4().hex[:8]}",
                     f"pages/{uuid.uuid4().hex[:8]}.html", fix_status))
        made.append(rid)
        return rid

    def counts():
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM recommendations "
                        "WHERE site_id = %s AND status = 'rejected' "
                        "AND evidence_json->>'source' = 'live_onpage_audit'",
                        (site_id,))
            rejected = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM generated_fixes f JOIN recommendations r "
                "ON r.recommendation_id = f.recommendation_id "
                "WHERE r.site_id = %s "
                "AND r.evidence_json->>'source' = 'live_onpage_audit' "
                "AND f.status = 'expired'", (site_id,))
            expired = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM rejection_log "
                        "WHERE site_id = %s AND run_id = "
                        "'plan32-retire-live-onpage-audit'", (site_id,))
            logs = cur.fetchone()[0]
        return rejected, expired, logs

    rec(GHOST, "proposed", fix_status="queued")
    rec(GHOST, "proposed", fix_status="generated")
    rec(GHOST, "proposed")
    kept = rec(GHOST, "rejected")
    good = rec(LEGIT, "proposed", fix_status="generated")
    other = rec({"source": "something_else"}, "proposed")
    conn.commit()
    before = counts()

    with open(os.path.join(ROOT, "plan",
                           "32-retire-live-onpage-audit-ghosts.sql"),
              encoding="utf-8") as fh:
        sql = fh.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    after = counts()

    ok = True
    ok &= check("18. ghost rows rejected, their fixes expired",
                after[0] == 4 and after[1] == 2,
                f"rejected/expired/logs = {after}, was {before}")
    ok &= check("19a. only newly-rejected rows are logged (idempotent input)",
                after[2] == 3, f"{after[2]} log rows")
    with conn.cursor() as cur:
        cur.execute("SELECT rejection_reason FROM recommendations "
                    "WHERE recommendation_id = %s", (kept,))
        ok &= check("19b. a pre-existing rejection reason is not clobbered",
                    cur.fetchone()[0] == "kept as-is")
        cur.execute("SELECT status FROM recommendations "
                    "WHERE recommendation_id = %s", (good,))
        ok &= check("19c. a legitimate grouped row is untouched",
                    cur.fetchone()[0] == "proposed")
        cur.execute("SELECT status FROM generated_fixes "
                    "WHERE recommendation_id = %s", (good,))
        ok &= check("19d. its queued fix is untouched", cur.fetchone()[0] == "generated")
        cur.execute("SELECT status FROM recommendations "
                    "WHERE recommendation_id = %s", (other,))
        ok &= check("19e. another source's row is untouched",
                    cur.fetchone()[0] == "proposed")

    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    ok &= check("20. re-running the migration is a no-op",
                counts() == after, f"{after} -> {counts()}")
    conn.rollback()
    return ok


def cleanup(conn, ids):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM generated_fixes WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM rejection_log WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM recommendations WHERE site_id = %s",
                    (ids["site_id"],))
        cur.execute("DELETE FROM pages WHERE site_id = %s", (ids["site_id"],))
        cur.execute("DELETE FROM site_config WHERE site_id = %s",
                    (ids["site_id"],))
    conn.commit()


def main():
    env_loader.load_env_file(quiet=True)
    ok = test_template_inference()
    ok &= test_evidence_provenance()
    conn = database.get_connection()
    ids = None
    try:
        ids = seed_generator_world(conn)
        ok &= test_grouped_generator(conn, ids)
        ok &= test_grouped_fix(conn, ids)
        ok &= test_ghost_migration(conn, ids)
    finally:
        if ids:
            cleanup(conn, ids)
        conn.close()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        sys.exit(1)
    print("ALL GROUPED STRUCTURED-DATA TESTS PASSED")


if __name__ == "__main__":
    main()

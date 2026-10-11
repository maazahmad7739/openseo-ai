# -*- coding: utf-8 -*-
"""plan/27 — Netlify / static-site GitHub-PR write path.

python tests/run_phase5_netlify_pr_tests.py   (needs a reachable local Postgres)

The REAL adapters + REAL GitHubPRClient run against the REAL database in the
connector's fixture-backed MOCK mode (zero network — socket banned):

  connector
    1. read_file (fixture) returns typed file state
    2. open_pr records the PR + files; close_pr closes it
    3. probe_auth mock -> ok + capabilities
    4. live mode WITHOUT the flag -> typed live_writes_disabled (fail-closed)
  url -> file resolution
    5. file_path_for_url: root/index.html, contact/ -> contact/index.html,
       root_dir prefix honored, .md preserved
  HTML transforms
    6. title_meta_rewrite replaces BOTH tags; absent tags inserted
    7. structured_data_inject inserts JSON-LD; duplicate @type+name refused
    8. noindex_tag inserts robots meta; already-noindex refused (no-op loop)
    9. no <head> -> typed refusal (never a guessed insertion point)
  adapters
   10. snapshot: fresh read + meta.title/meta.description + file.content
   11. execute: transform -> ONE PR -> verified + snapshot_patch pr.*
   12. execute WITHOUT snapshot -> typed no_snapshot refusal
   13. dry_run opens no PR (fixture state unchanged)
   14. payload validation BEFORE any network call (wrong mutation/action/
       drift keys -> payload_invalid)
   15. restore: closes the recorded PR; without pr.number -> no_pr_recorded
  generator routing
   16. _write_target netlify: sub_type netlify.title_meta_rewrite, target =
       repo path, payload carries page_url + file_path + action
   17. missing github_repo -> FixNotSupported at generation
   18. technical_fix structured_data_failure -> netlify.structured_data
  executor wiring (real _execute_one against the real DB)
   19. queued netlify fix: policy gate -> fresh snapshot -> apply ->
       verified + change_log + measurement wiring (same lifecycle)
   20. stale-diff guard works on netlify rows (live != old_value -> expired)
   21. credential resolution: netlify site resolves github config; missing
       GITHUB_TOKEN -> no_credentials (fail-closed)

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

REPO = f"openseo-demo/static-site-{RUN_TOKEN}"

HTML_WITH_META = (
    "<!DOCTYPE html>\n<html>\n<head>\n"
    "    <title>Old Title</title>\n"
    '    <meta name="description" content="Old description here">\n'
    "</head>\n<body><h1>Page</h1></body>\n</html>"
)

HTML_EMPTY_HEAD = (
    "<!DOCTYPE html>\n<html>\n<head>\n</head>\n"
    "<body><h1>Page</h1></body>\n</html>"
)


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
        raise NetworkBanned("network access attempted in Phase 5 suite")
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

FIXTURE_PATH = os.path.join(HERE, "fixtures", "github_repos.json")
PAGE_URL = "https://static-demo.example.com/services.html"


def seed_site(conn, with_repo=True):
    domain = f"phase5-{RUN_TOKEN}-{uuid.uuid4().hex[:6]}.example.com"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property,
                                     catalogue_size_tier, platform,
                                     github_repo, github_branch,
                                     github_root_dir,
                                     business_service_types,
                                     business_areas_served,
                                     business_offer_name)
            VALUES (%s, %s, %s, 'small', 'netlify', %s, 'main', '',
                    %s, %s, %s)
            RETURNING site_id
            """,
            (f"Phase5 {RUN_TOKEN}", domain, f"sc-domain:{domain}",
             REPO if with_repo else None,
             [f"Static Demo Service {RUN_TOKEN}"],
             ["US"],
             f"Static Demo Offering {RUN_TOKEN}"),
        )
        site_id = cur.fetchone()[0]
        cur2 = conn.cursor()
        cur2.execute(
            """
            INSERT INTO pages (site_id, url, page_type, title,
                               meta_description, body_html)
            VALUES (%s, %s, 'page', %s, %s, %s)
            """,
            (site_id, PAGE_URL,
             f"Old Title {RUN_TOKEN}",
             f"Old description here {RUN_TOKEN}",
             "<p>Static demo body for the netlify PR path.</p>"),
        )
        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword, keywords,
                                          intent)
            VALUES (%s, %s, %s, 'informational') RETURNING cluster_id
            """,
            (site_id, f"static demo {RUN_TOKEN}",
             [f"static demo {RUN_TOKEN}"]),
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
            (site_id, PAGE_URL, cluster_id,
             f"low title quality seed {RUN_TOKEN}",
             json.dumps([{"source": "GSC", "finding": "seed"}])),
        )
        rec_id = str(cur.fetchone()[0])
    conn.commit()
    # register the suite's page in the fixture-backed mock repo
    data = load_fixture()
    data.setdefault("repos", {})[REPO] = {
        "base_branch": "main",
        "files": {PAGE_URL.rsplit("/", 1)[-1]: HTML_WITH_META},
        "prs": [],
        "next_pr_number": 1,
    }
    save_fixture(data)
    return {"site_id": str(site_id), "domain": domain, "url": PAGE_URL,
            "rec_id": rec_id, "cluster_id": str(cluster_id)}


def seed_netlify_policy(conn, ids, sub_type, enabled=True):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fix_policy (site_id, sub_type, risk_tier,
                                    weekly_cap, requires_field_verify,
                                    enabled)
            VALUES (%s, %s, %s, 5, true, %s)
            """,
            (ids["site_id"], sub_type, "low", enabled),
        )
    conn.commit()


def seed_netlify_fix(conn, ids, sub_type, variables, old_value, new_value,
                     status="queued", diff_field=None, risk_tier="low"):
    """Insert a netlify fix row in the requested lifecycle state."""
    payload = {
        "mutation": "netlify.gitPr",
        "variables": dict(variables, **{
            "page_url": ids["url"],
        }),
        "surface": "netlify",
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
             variables["file_path"], json.dumps(payload),
             json.dumps([{"field": diff_field or sub_type,
                          "old_value": old_value,
                          "new_value": new_value}]),
             status, risk_tier),
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


def fixture_file(path):
    repo = load_fixture().get("repos", {}).get(REPO) or {}
    return (repo.get("files") or {}).get(path)


def fixture_prs():
    repo = load_fixture().get("repos", {}).get(REPO) or {}
    return repo.get("prs") or []


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


NETLIFY_CONFIG = {"platform": "netlify", "github_token": "k-suite",
                  "github_repo": REPO, "github_branch": "main",
                  "github_root_dir": ""}


def build_client():
    from connectors.github_static import GitHubPRClient
    return GitHubPRClient(github_token="k-suite", github_repo=REPO,
                          github_branch="main", github_root_dir="",
                          mode="mock")


def title_meta_payload(url_path, title, description):
    return {
        "mutation": "netlify.gitPr",
        "variables": {"page_url": PAGE_URL, "file_path": url_path,
                      "action": "title_meta_rewrite",
                      "title": title, "description": description},
        "surface": "netlify",
    }


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def test_connector():
    print("\n== GitHubPRClient (fixture mode) ==")
    from connectors.github_static import GitHubPRClient
    allok = True
    # register a dedicated connector-test repo in the fixture store
    demo_repo = f"connector-test/static-site-{RUN_TOKEN}"
    data = load_fixture()
    data.setdefault("repos", {})[demo_repo] = {
        "base_branch": "main",
        "files": {"services.html": HTML_WITH_META},
        "prs": [],
        "next_pr_number": 1,
    }
    save_fixture(data)
    client = GitHubPRClient(github_token="k-suite", github_repo=demo_repo,
                            github_branch="main", github_root_dir="",
                            mode="mock")
    read = client.read_file("services.html")
    allok &= check("read_file typed file state",
                   read.get("ok") is True
                   and "Old Title" in (read.get("data") or {}).get("content", ""),
                   str(read)[:150])
    read_missing = client.read_file("no-such.html")
    allok &= check("unknown path -> typed not_found",
                   read_missing.get("ok") is False
                   and read_missing.get("outcome") == "not_found",
                   str(read_missing))
    pr = client.open_pr("fix(seo): test", "body", "openseo/test-1", "main",
                        [{"path": "services.html",
                          "old_content": "a", "new_content": "b"}])
    allok &= check("open_pr ok", pr.get("ok") is True
                   and (pr.get("data") or {}).get("pr_number") == 1,
                   str(pr)[:200])
    closed = client.close_pr(1)
    allok &= check("close_pr ok", closed.get("ok") is True, str(closed))
    probe = client.probe_auth()
    allok &= check("probe_auth mock ok",
                   probe.get("ok") is True
                   and "pr_write" in (probe.get("capabilities") or []),
                   str(probe))
    # live without flag: typed refusal
    live = GitHubPRClient(mode="live", live_writes=False)
    refused = live.open_pr("t", "b", "b1", "main",
                           [{"path": "x", "new_content": "y"}])
    allok &= check("live without flag -> live_writes_disabled",
                   refused.get("ok") is False
                   and refused.get("outcome") == "live_writes_disabled",
                   str(refused))
    # live WITH flag: attempts a real network call -> the ban surfaces as
    # a typed provider_error (never a crash) — proving the live path fires
    # while the suite stays offline.
    live2 = GitHubPRClient(mode="live", live_writes=True)
    refused2 = live2.read_file("services.html")
    allok &= check("live + flag attempts network (typed failure under ban)",
                   refused2.get("ok") is False
                   and refused2.get("outcome") == "provider_error",
                   str(refused2))
    return allok


def test_file_path_resolution():
    print("\n== file_path_for_url ==")
    from connectors.github_static import file_path_for_url
    allok = True
    allok &= check("root -> index.html",
                   file_path_for_url("https://s.example.com/") == "index.html")
    allok &= check("explicit file preserved",
                   file_path_for_url("https://s.example.com/services.html")
                   == "services.html")
    allok &= check("trailing slash -> directory convention",
                   file_path_for_url("https://s.example.com/contact/")
                   == "contact/index.html")
    allok &= check("extensionless -> directory convention",
                   file_path_for_url("https://s.example.com/contact")
                   == "contact/index.html")
    allok &= check("root_dir prefixes",
                   file_path_for_url("https://s.example.com/a", root_dir="site")
                   == "site/a/index.html")
    allok &= check("markdown preserved",
                   file_path_for_url("https://s.example.com/blog/post.md")
                   == "blog/post.md")
    return allok


def test_html_transforms():
    print("\n== HTML transforms ==")
    from fixes.adapters import apply_netlify_html_edit
    allok = True
    # 6: title+meta rewrite
    new, s = apply_netlify_html_edit(
        HTML_WITH_META, "title_meta_rewrite",
        {"title": "New Title", "description": "New description"})
    allok &= check("title_meta_rewrite changes both",
                   new is not None and "<title>New Title</title>" in new
                   and 'content="New description"' in new,
                   str(s) + " :: " + str(new))
    allok &= check("rewrite summary carries both fields",
                   isinstance(s, dict) and set(s.get("fields") or []) ==
                   {"title", "meta_description"}, str(s))
    # absent tags get inserted
    new_ins, s_ins = apply_netlify_html_edit(
        HTML_EMPTY_HEAD, "title_meta_rewrite",
        {"title": "T", "description": "D"})
    allok &= check("absent title/meta inserted",
                   new_ins is not None and "<title>T</title>" in new_ins
                   and 'content="D"' in new_ins, str(s_ins))
    # 7: structured data
    ld = json.dumps({"@type": "Organization", "name": "KG"})
    new2, s2 = apply_netlify_html_edit(
        HTML_WITH_META, "structured_data_inject", {"jsonld": ld})
    allok &= check("jsonld injected before </head>",
                   new2 is not None
                   and 'type="application/ld+json"' in new2
                   and new2.index("ld+json") < new2.index("</head>"),
                   str(s2))
    dup, sdup = apply_netlify_html_edit(
        new2, "structured_data_inject", {"jsonld": ld})
    allok &= check("identical jsonld -> already_present refusal",
                   dup is None and sdup == "already_present", str(sdup))
    # 8: noindex
    new3, s3 = apply_netlify_html_edit(
        HTML_WITH_META, "noindex_tag", {"noindex": True})
    allok &= check("robots meta inserted",
                   new3 is not None
                   and 'name="robots" content="noindex, follow"' in new3,
                   str(s3))
    again, sagain = apply_netlify_html_edit(
        new3, "noindex_tag", {"noindex": True})
    allok &= check("already noindex -> no-op loop guard",
                   again is None and sagain == "already_noindex",
                   str(sagain))
    # 9: no <head> refuses
    bad, sbad = apply_netlify_html_edit(
        "<p>no head</p>", "noindex_tag", {"noindex": True})
    allok &= check("no <head> -> typed refusal",
                   bad is None and sbad == "no_head_section", str(sbad))
    return allok


def test_adapters_end_to_end(conn, ids):
    print("\n== adapters: snapshot -> execute -> restore ==")
    from fixes import adapters as fix_adapters
    allok = True
    client = build_client()
    payload = title_meta_payload("services.html", "Phase5 title",
                                 "Phase5 description with the keyword")
    fix_row = {
        "fix_id": "suite", "sub_type": "netlify.title_meta_rewrite",
        "site_id": ids["site_id"],
        "payload_json": payload,
        "diff_json": [{"field": "meta.title", "old_value": "Old Title",
                       "new_value": "Phase5 title"}],
        "target_entity_ref": "services.html", "target_url": ids["url"],
    }
    # 10: fresh-read snapshot
    snap = fix_adapters.netlify_title_meta_rewrite_snapshot(
        conn, fix_row, NETLIFY_CONFIG, client=client)
    snap_data = snap.get("snapshot") or {}
    allok &= check("snapshot ok + file.content + meta keys",
                   snap.get("ok") is True
                   and "file.content" in snap_data
                   and "meta.title" in snap_data
                   and "meta.description" in snap_data, str(snap)[:200])
    fix_row["snapshot_json"] = snap_data

    # 11: execute -> one PR, verified
    out = fix_adapters.netlify_title_meta_rewrite_execute(
        conn, fix_row, NETLIFY_CONFIG, client=client)
    allok &= check("execute ok + verified",
                   out.get("ok") is True
                   and out.get("verification_status") == "verified",
                   str(out)[:250])
    allok &= check("snapshot_patch carries pr.number + branch",
                   (out.get("snapshot_patch") or {}).get("pr.number") == 1
                   and (out.get("snapshot_patch") or {}).get("pr.branch"),
                   str(out.get("snapshot_patch")))
    prs = fixture_prs()
    allok &= check("PR titled fix(seo): ... for page-path",
                   len(prs) == 1 and prs[0]["title"].startswith("fix(seo):")
                   and "services.html" in prs[0]["title"]
                   and "recommendation_id" in prs[0]["body"],
                   str([p["title"] for p in prs]))

    # 12: execute WITHOUT snapshot refuses
    bare_row = {"fix_id": "x", "sub_type": "netlify.title_meta_rewrite",
                "payload_json": payload,
                "target_entity_ref": "services.html"}
    refused = fix_adapters.netlify_title_meta_rewrite_execute(
        conn, bare_row, NETLIFY_CONFIG, client=client)
    allok &= check("execute without snapshot -> no_snapshot",
                   refused.get("ok") is False
                   and refused.get("outcome") == "no_snapshot", str(refused))

    # 13: dry_run writes nothing (no new PR)
    pr_count_before = len(fixture_prs())
    dry = fix_adapters.netlify_title_meta_rewrite_execute(
        conn, fix_row, NETLIFY_CONFIG, dry_run=True, client=client)
    allok &= check("dry_run typed + no PR opened",
                   dry.get("outcome") == "dry_run"
                   and len(fixture_prs()) == pr_count_before, str(dry))

    # 14: payload validation before network
    bad_payload = dict(payload)
    bad_payload["mutation"] = "productUpdate"
    bare_with_snap = dict(bare_row, snapshot_json=snap_data)
    invalid2 = fix_adapters.netlify_title_meta_rewrite_execute(
        conn, {"fix_id": "y", "sub_type": "netlify.title_meta_rewrite",
               "payload_json": bad_payload,
               "snapshot_json": snap_data,
               "target_entity_ref": "services.html"},
        NETLIFY_CONFIG, client=client)
    allok &= check("wrong mutation -> payload_invalid",
                   invalid2.get("outcome") == "payload_invalid",
                   str(invalid2))
    drift = dict(payload)
    drift["variables"] = dict(drift["variables"], rogue_field=1)
    invalid3 = fix_adapters.netlify_title_meta_rewrite_execute(
        conn, {"fix_id": "z", "sub_type": "netlify.title_meta_rewrite",
               "payload_json": drift, "snapshot_json": snap_data,
               "target_entity_ref": "services.html"},
        NETLIFY_CONFIG, client=client)
    allok &= check("drift fields -> payload_invalid",
                   invalid3.get("outcome") == "payload_invalid",
                   str(invalid3))

    # 15: restore closes the recorded PR
    pr_num = out["snapshot_patch"]["pr.number"]
    snap_patched = dict(snap_data)
    snap_patched.update(out["snapshot_patch"])
    restore_row = dict(fix_row, snapshot_json=snap_patched)
    restore = fix_adapters.netlify_title_meta_rewrite_restore(
        conn, restore_row, NETLIFY_CONFIG, client=client)
    allok &= check("restore ok (PR closed)",
                   restore.get("ok") is True and restore.get("restored") is True,
                   str(restore))
    closed_pr = next((p for p in fixture_prs()
                      if p.get("number") == pr_num), None)
    allok &= check("PR state flipped to closed",
                   (closed_pr or {}).get("state") == "closed",
                   str(closed_pr))
    # restore WITHOUT pr.number refuses
    bare_restore = fix_adapters.netlify_title_meta_rewrite_restore(
        conn, {"sub_type": "netlify.title_meta_rewrite",
               "snapshot_json": {"file.content": "x",
                                 "file.path": "services.html"},
               "target_entity_ref": "services.html"},
        NETLIFY_CONFIG, client=client)
    allok &= check("restore without pr.number -> no_pr_recorded",
                   bare_restore.get("outcome") == "no_pr_recorded",
                   str(bare_restore))
    return allok


def test_generator_routing(conn, ids):
    print("\n== generator routing ==")
    from fixes.generator import (_write_target, generate_netlify_technical_fix,
                                 FixNotSupported)
    allok = True
    rec = {"site_id": ids["site_id"], "platform": "netlify",
           "target_url": ids["url"]}
    sub_type, target, payload = _write_target(conn, rec, "title")
    allok &= check("netlify title target -> title_meta_rewrite + repo path",
                   sub_type == "netlify.title_meta_rewrite"
                   and target == "services.html"
                   and payload["surface"] == "netlify"
                   and payload["variables"]["action"] == "title_meta_rewrite",
                   str((sub_type, target, payload)))
    rec_no_repo = {"site_id": None, "platform": "netlify",
                   "target_url": ids["url"]}
    # 17: missing repo binding -> (None, None, None)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO site_config (site_name, domain, gsc_property, "
            "platform) VALUES (%s, %s, %s, 'netlify') RETURNING site_id",
            (f"Phase5-norepo {RUN_TOKEN}",
             f"norepo-{RUN_TOKEN}.example.com",
             f"sc-domain:norepo-{RUN_TOKEN}.example.com"))
        norepo_id = cur.fetchone()[0]
    conn.commit()
    rec_no_repo["site_id"] = str(norepo_id)
    sub_none, _, _ = _write_target(conn, rec_no_repo, "title")
    allok &= check("missing github_repo -> typed None triple",
                   sub_none is None, str(sub_none))
    # 18: technical_fix structured_data_failure -> netlify.structured_data
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, target_url,
                 diagnosis, evidence_json, status, approved_at)
            VALUES (%s, 'technical_fix', 'technical_fix', %s,
                    %s, %s, 'approved', now())
            RETURNING recommendation_id
            """,
            (ids["site_id"], ids["url"],
             f"structured data seed {RUN_TOKEN}",
             json.dumps({"issue_type": "structured_data_failure",
                         "has_structured_data": False})),
        )
        tech_rec_id = str(cur.fetchone()[0])
    conn.commit()
    from fixes.generator import load_decision_inputs
    tech_rec = load_decision_inputs(conn, tech_rec_id)
    tech_rec["platform"] = "netlify"
    tech_rec["evidence"] = {"issue_type": "structured_data_failure"}
    result = generate_netlify_technical_fix(conn, tech_rec)
    allok &= check("structured_data_failure -> netlify.structured_data row",
                   result.get("created") is True, str(result)[:200])
    with conn.cursor() as cur:
        cur.execute("SELECT payload_json FROM generated_fixes "
                    "WHERE fix_id = %s", (result["fix_id"],))
        prow = cur.fetchone()
    pv = (prow[0].get("variables") if isinstance(prow[0], dict)
          else json.loads(prow[0]).get("variables")) if prow else {}
    allok &= check("jsonld payload is valid serialized JSON-LD",
                   isinstance(pv.get("jsonld"), str)
                   and isinstance(json.loads(pv["jsonld"]), dict),
                   str(pv)[:150])
    # plan/27 grounding: every URL in the JSON-LD carries the CLIENT's own
    # origin (the seeded page's host) — never a hardcoded agency domain.
    ld = json.loads(pv["jsonld"])
    seeded_host = "static-demo.example.com"
    urls = []

    def _walk_urls(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("url", "item") and isinstance(v, str) \
                        and v.startswith("http"):
                    urls.append(v)
                _walk_urls(v)
        elif isinstance(node, list):
            for item in node:
                _walk_urls(item)

    _walk_urls(ld)
    allok &= check("jsonld URLs ground on the client's own origin",
                   urls and all(u"static-demo.example.com" in u
                                for u in urls),
                   str(urls)[:200])
    allok &= check("no agency domain in emitted jsonld",
                   not any("kineticgrowth" in u.lower() for u in urls),
                   str(urls)[:200])
    # plan/30 grounding: /services serviceType + areaServed carry the
    # CLIENT's business profile (seeded above) — never agency copy.
    svc_nodes = [g for g in (ld.get("@graph") or [])
                 if g.get("@type") == "Service"]
    allok &= check("services route emits a Service block",
                   len(svc_nodes) == 1, str(graph_types := [
                       g.get("@type") for g in (ld.get("@graph") or [])]))
    if svc_nodes:
        svc = svc_nodes[0]
        allok &= check("serviceType grounds on client's business profile",
                       svc.get("serviceType")
                       == [f"Static Demo Service {RUN_TOKEN}"],
                       str(svc.get("serviceType")))
        allok &= check("areaServed grounds on client's regions",
                       svc.get("areaServed") == ["US"],
                       str(svc.get("areaServed")))
        ld_text = json.dumps(ld)
        allok &= check("no agency copy in emitted jsonld",
                       "Digital Marketing & AI Growth Services" not in ld_text
                       and "Digital marketing services" not in ld_text,
                       ld_text[:200])
    # cleanup the norepo site row + its generated fix
    with conn.cursor() as cur:
        cur.execute("DELETE FROM generated_fixes WHERE site_id = %s",
                    (norepo_id,))
        cur.execute("DELETE FROM fix_policy WHERE site_id = %s", (norepo_id,))
        cur.execute("DELETE FROM site_config WHERE site_id = %s",
                    (norepo_id,))
    conn.commit()
    return allok


def test_executor_lifecycle(conn, ids):
    print("\n== executor lifecycle (real DB, mock GitHub) ==")
    from jobs.fix_executor import run_fix_executor
    allok = True
    config = dict(NETLIFY_CONFIG)

    # monkeypatch the client construction seam to our fixture-backed mock
    from fixes import adapters as fix_adapters
    orig = fix_adapters._client_from_netlify_config

    def injected(config_dict, client=None):
        if client is not None:
            return client
        return build_client()
    fix_adapters._client_from_netlify_config = staticmethod(injected)

    def restore_seam():
        fix_adapters._client_from_netlify_config = orig

    try:
        # 19: happy path
        variables = {"page_url": ids["url"], "file_path": "services.html",
                     "action": "title_meta_rewrite",
                     "title": f"New applied title {RUN_TOKEN}",
                     "description": f"New applied description {RUN_TOKEN}"}
        fix_id = seed_netlify_fix(
            conn, ids, "netlify.title_meta_rewrite", variables,
            old_value="Old Title",
            new_value=f"New applied title {RUN_TOKEN}",
            diff_field="meta.title")
        seed_netlify_policy(conn, ids, "netlify.title_meta_rewrite",
                            enabled=True)
        summary = run_fix_executor(ids["site_id"], conn=conn,
                                   shop_config=config)
        result = next(r for r in summary["results"]
                      if r.get("fix_id") == fix_id)
        allok &= check("netlify fix applied via executor",
                       result.get("status") == "applied", str(result)[:200])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, verification_status, snapshot_json, "
                "adapter_response FROM generated_fixes WHERE fix_id = %s",
                (fix_id,))
            status, vstatus, snapshot, adapter_response = cur.fetchone()
        allok &= check("applied + verified in DB",
                       status == "applied" and vstatus == "verified",
                       str((status, vstatus)))
        snap = snapshot if isinstance(snapshot, dict) \
            else json.loads(snapshot or "{}")
        allok &= check("snapshot persisted (file + pr keys)",
                       "file.content" in snap
                       and "pr.number" in snap, str(snap.keys()))
        resp = adapter_response if isinstance(adapter_response, dict) \
            else json.loads(adapter_response or "{}")
        allok &= check("adapter_response carries PR coordinates",
                       isinstance(resp.get("pr_number"), int)
                       and resp.get("pr_url")
                       and "pull" in str(resp.get("pr_url")),
                       str(resp)[:200])

        # 20: stale-diff expiry
        stale_variables = dict(variables,
                               title=f"Stale target {RUN_TOKEN}")
        stale_id = seed_netlify_fix(
            conn, ids, "netlify.title_meta_rewrite", stale_variables,
            old_value="A DIFFERENT live value",
            new_value=f"Stale target {RUN_TOKEN}",
            diff_field="meta.title")
        summary2 = run_fix_executor(ids["site_id"], conn=conn,
                                    shop_config=config)
        result2 = next(r for r in summary2["results"]
                       if r.get("fix_id") == stale_id)
        allok &= check("stale diff -> expired",
                       result2.get("status") == "expired"
                       and result2.get("reason") == "stale_diff",
                       str(result2))
    finally:
        fix_adapters._client_from_netlify_config = orig
    return allok


def test_credentials(conn, ids):
    print("\n== credential resolution ==")
    from connectors.write_credentials import resolve_write_credentials
    allok = True
    # with token present in env
    os.environ["GITHUB_TOKEN"] = "k-suite"
    resolved = resolve_write_credentials(conn, ids["site_id"])
    allok &= check("netlify site resolves github config",
                   resolved.get("ok") is True
                   and resolved.get("platform") == "netlify"
                   and (resolved.get("config") or {}).get("github_repo")
                   == REPO, str(resolved)[:200])
    # without token -> no_credentials naming GITHUB_TOKEN (never the value)
    del os.environ["GITHUB_TOKEN"]
    resolved2 = resolve_write_credentials(conn, ids["site_id"])
    allok &= check("missing GITHUB_TOKEN -> no_credentials",
                   resolved2.get("ok") is False
                   and resolved2.get("outcome") == "no_credentials"
                   and "GITHUB_TOKEN" in (resolved2.get("detail") or ""),
                   str(resolved2)[:200])
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    ids_list = []
    fixture_backup = open(FIXTURE_PATH, encoding="utf-8").read()
    try:
        ban_network()
        ok = test_connector()
        ok &= test_file_path_resolution()
        ok &= test_html_transforms()
        ids = seed_site(conn)
        ids_list.append(ids)
        ok &= test_adapters_end_to_end(conn, ids)
        ok &= test_generator_routing(conn, ids)
        ok &= test_credentials(conn, ids)
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
    print("ALL PHASE 5 TESTS PASSED")


if __name__ == "__main__":
    main()
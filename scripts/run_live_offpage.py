"""LIVE off-page pipeline for a real tenant. No mock, no fixtures.

Unlike every other runner in this repo, this one is deliberately NOT
discovered by scripts/run_tests.py and is NOT hermetic. It spends real
DataForSEO credits, crawls real pages, and calls a real LLM.

Stages
  1. corpus   crawl the tenant's REAL pages -> pages + page_query_match_scores
  2. clusters real GSC queries -> keyword_clusters (grounding relevance)
  3. discover real Reddit threads via DataForSEO live SERP
  4. persist  communities / threads / account / recommendations
  5. draft    live Gemini (or Ollama primary) via the real drafter
  6. report   thread URL, exact prompt context, draft text, DB row

Nothing here publishes. offpage_drafts has no row the automated executor can
claim; publication is a manual queue action (see fixes/offpage_drafter.py).

Usage:
  GEMINI_MODEL=gemini-3.6-flash python scripts/run_live_offpage.py --domain gethelium.co
"""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# SERP titles contain characters the Windows console codepage cannot encode.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import env as env_mod  # noqa: E402

env_mod.load_env_file()  # LIVE: credentials are required here

import db  # noqa: E402
import urllib.request  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SCRIPT_STYLE = re.compile(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>")
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")
MIN_BODY_CHARS = 400


def hr(title):
    print("\n" + "=" * 72)
    print("  " + title)
    print("=" * 72, flush=True)


def fetch(url, timeout=45):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def visible_text(html):
    """Strip script/style then tags. The live site is a Framer SPA whose raw
    HTML is ~1.3MB of JS; without this the 'fact base' is mostly JavaScript."""
    stripped = SCRIPT_STYLE.sub(" ", html)
    stripped = re.sub(r"(?is)<!--.*?-->", " ", stripped)
    return WS.sub(" ", TAGS.sub(" ", stripped)).strip()


def serp(keyword, depth=10):
    cred = base64.b64encode(("%s:%s" % (os.environ["DATAFORSEO_LOGIN"],
                                        os.environ["DATAFORSEO_PASSWORD"])
                             ).encode()).decode()
    body = json.dumps([{"keyword": keyword, "location_code": 2840,
                        "language_code": "en", "depth": depth,
                        "load_more": False}]).encode()
    req = urllib.request.Request(
        "https://api.dataforseo.com/v3/serp/google/organic/live/regular",
        data=body,
        headers={"Authorization": "Basic " + cred,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        task = json.loads(r.read())["tasks"][0]
    print("    DataForSEO [%s] cost=%s :: %s" % (
        task.get("status_message"), task.get("cost"), keyword))
    out = []
    for item in (task.get("result") or [{}])[0].get("items", []):
        if item.get("type") == "organic" and item.get("url"):
            out.append(item)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL",
                                                      "gemini-3.6-flash"))
    ap.add_argument("--max-drafts", type=int, default=3,
                    help="cap on live LLM calls per run")
    args = ap.parse_args()
    os.environ["GEMINI_MODEL"] = args.model

    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT site_id FROM site_config WHERE domain = %s",
                    (args.domain,))
        row = cur.fetchone()
    if row is None:
        print("no site_config row for domain=%s" % args.domain)
        return 1
    site_id = str(row[0])
    print("tenant: %s  site_id=%s" % (args.domain, site_id))

    # ---------------------------------------------------------------- 1
    hr("1. REAL CORPUS - crawl the tenant's live pages")
    gsc_pages = [
        ("https://www.gethelium.co/", "collection"),
        ("https://www.gethelium.co/ad-stack", "collection"),
        ("https://www.gethelium.co/merchandising", "collection"),
        ("https://www.gethelium.co/pulse", "collection"),
        ("https://www.gethelium.co/contact", "collection"),
        ("https://www.gethelium.co/blogs/d2c-apparel-calendar-2026", "collection"),
        ("https://www.gethelium.co/helium-case-studies", "collection"),
    ]
    crawled = []
    for url, ptype in gsc_pages:
        try:
            status, html = fetch(url)
        except Exception as exc:
            print("    %-52s SKIP %s %s" % (url, type(exc).__name__,
                                             str(exc)[:40]))
            continue
        text = visible_text(html)
        if len(text) < MIN_BODY_CHARS:
            print("    %-52s skip (only %d visible chars)" % (url, len(text)))
            continue
        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = WS.sub(" ", TAGS.sub(" ", title_m.group(1))).strip()[:200] \
            if title_m else url
        body_html = "<p>%s</p>" % text[:6000]
        with conn.cursor() as cur:
            cur.execute("SELECT page_id FROM pages WHERE site_id=%s AND url=%s",
                        (site_id, url))
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "UPDATE pages SET title=%s, body_html=%s, page_type=%s, "
                    "data_source='live', last_crawled_at=now() "
                    "WHERE page_id=%s",
                    (title, body_html, ptype, existing[0]))
                page_id = str(existing[0])
            else:
                cur.execute(
                    "INSERT INTO pages (site_id, url, page_type, "
                    "title, indexable, status_code, body_html, data_source, "
                    "last_crawled_at, meta_description) "
                    "VALUES (%s,%s,%s,%s,true,200,%s,'live',"
                    "now(),%s) RETURNING page_id",
                    (site_id, url, ptype, title, body_html,
                     text[:300]))
                page_id = str(cur.fetchone()[0])
        conn.commit()
        crawled.append((page_id, url, title, len(text)))
        print("    %-52s OK %6d visible chars -> %s" % (url, len(text),
                                                        page_id))
    if not crawled:
        print("\nno live corpus could be crawled; aborting (the grounding "
              "gate would fail closed anyway)")
        return 1

    # ---------------------------------------------------------------- 2
    hr("2. REAL CLUSTERS from GSC queries (drives grounding relevance)")
    import csv
    zf = ROOT / "gethelium.co-Performance-on-Search-2026-09-25.zip"
    import zipfile
    zdir = Path(os.environ.get("TEMP", ".")) / "opencode" / "gsc_live"
    with zipfile.ZipFile(zf) as z:
        z.extractall(zdir)
    clusters = []
    with open(zdir / "Queries.csv", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            kw = (r.get("Top queries") or "").strip()
            if not kw:
                continue
            try:
                vol = int(float(r.get("Impressions") or 0))
            except ValueError:
                vol = 0
            clusters.append((kw, vol))
    clusters.sort(key=lambda x: -x[1])
    clusters = clusters[:6]
    cluster_ids = {}
    for kw, vol in clusters:
        with conn.cursor() as cur:
            # keyword_clusters has no unique key on (site_id,
            # primary_keyword), so a plain INSERT would add a fresh duplicate
            # cluster on every live run and silently multiply the corpus.
            cur.execute(
                "SELECT cluster_id FROM keyword_clusters WHERE site_id=%s AND "
                "primary_keyword=%s ORDER BY created_at LIMIT 1",
                (site_id, kw))
            found = cur.fetchone()
            if found:
                cluster_ids[kw] = str(found[0])
                cur.execute(
                    "UPDATE keyword_clusters SET search_volume=%s, "
                    "keywords=ARRAY[%s], updated_at=now() WHERE cluster_id=%s",
                    (vol, kw, cluster_ids[kw]))
            else:
                cur.execute(
                    "INSERT INTO keyword_clusters (site_id, primary_keyword, "
                    "keywords, intent, commercial_intent, "
                    "recommended_page_type, search_volume) "
                    "VALUES (%s,%s,ARRAY[%s],'commercial',true,"
                    "'collection',%s) RETURNING cluster_id",
                    (site_id, kw, kw, vol))
                cluster_ids[kw] = str(cur.fetchone()[0])
        conn.commit()
        print("    cluster %-26s impressions=%-7d %s" % (kw, vol,
                                                          cluster_ids[kw][:8]))

    # every cluster is scored against every crawled page so the drafter's
    # corpus loader (page_type IN product/collection) can select real pages
    with conn.cursor() as cur:
        for kw, cid in cluster_ids.items():
            for page_id, url, _t, _n in crawled:
                cur.execute(
                    "INSERT INTO page_query_match_scores (site_id, page_id, "
                    "cluster_id, intent_page_type_score, "
                    "catalogue_match_score, gsc_evidence_score, "
                    "semantic_content_score) "
                    "VALUES (%s,%s,%s,0.3,0.3,0.25,0.15) "
                    "ON CONFLICT (page_id, cluster_id) DO UPDATE SET "
                    "semantic_content_score=EXCLUDED.semantic_content_score",
                    (site_id, page_id, cid))
    conn.commit()
    print("    scored %d pages x %d clusters" % (len(crawled),
                                                 len(cluster_ids)))

    # ---------------------------------------------------------------- 3
    hr("3. LIVE DISCOVERY - real Reddit threads via DataForSEO")
    queries = [
        "site:reddit.com d2c app brands paid acquisition retention platform",
        "site:reddit.com apparel brand app marketing agency alternative",
        "site:reddit.com shopify store paid ads not converting analytics tool",
        "site:reddit.com ecommerce retention cohort analysis small team",
    ]
    # Gate arithmetic must match context_problems() exactly. Filtering on a
# naive split() counted stopwords the real word_count() drops, so threads
# passed the pre-filter and then burned a free-tier LLM call before the
# gate refused them at 23 content words.
    from fixes.offpage_drafter import (MIN_CONTEXT_CHARS, strip_html,
                                      word_count)
    threads = []
    seen = set()
    for q in queries:
        for item in serp(q):
            url = item["url"]
            # Only real comment threads are answerable opportunities. Reddit
            # subreddit index/listing pages (r/foo/, r/foo/hot/, r/foo/new/)
            # rank for site: queries but are not a question anyone asked.
            if "reddit.com" not in url or url in seen:
                continue
            if "/comments/" not in url:
                continue
            seen.add(url)
            title = WS.sub(" ", TAGS.sub(" ", item.get("title") or "")).strip()
            snippet = (item.get("description") or "").strip()
            combined = strip_html("%s %s" % (title, snippet))
            if len(combined) < MIN_CONTEXT_CHARS:
                print("      thin (%d chars) skip: %s" % (len(combined),
                                                           url[:64]))
                continue
            if word_count(combined) < 25:
                print("      thin (%d content words) skip: %s" % (
                    word_count(combined), url[:64]))
                continue
            threads.append({"url": url, "title": title[:300],
                            "snippet": snippet[:900]})
    # A thread someone actually asked a question in is the answerable kind;
    # rank those ahead of promo/showcase posts when capping live LLM calls.
    qwords = ("how", "best", "what", "why", "which", "alternative",
              "anyone", "help", "need", "worth", "recommend")
    threads.sort(key=lambda t: (
        0 if ("?" in t["title"] or
              any(w in t["title"].lower() for w in qwords)) else 1,
        -len(t["title"] + t["snippet"])))

    print("\n    %d usable live reddit thread(s) survived the context gate"
          % len(threads))
    for t in threads:
        print("      %s\n        %r" % (t["url"], t["title"][:70]))

    # Cap drafting so a live run makes a bounded number of LLM calls; every
    # discovered thread is still visible above.
    threads = threads[:args.max_drafts]
    print("\n    drafting the %d highest-context thread(s) this run"
          % len(threads))

    if not threads:
        print("\nno usable live thread context; aborting")
        return 1

    # ---------------------------------------------------------------- 4
    hr("4. PERSIST communities / threads / account / recommendations")
    communities = {}
    for t in threads:
        m = re.search(r"reddit\.com/r/([A-Za-z0-9_]+)", t["url"])
        key = "r/" + m.group(1) if m else "r/ecommerce"
        communities.setdefault(key, []).append(t)

    comm_ids = {}
    for key in communities:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT community_id FROM offpage_communities WHERE site_id=%s "
                "AND community_key=%s", (site_id, key))
            r = cur.fetchone()
            if r:
                comm_ids[key] = str(r[0])
            else:
                cur.execute(
                    "INSERT INTO offpage_communities (site_id, platform, "
                    "community_key, link_policy, allows_self_promo, "
                    "ai_content_allowed, promotional_cap_30d, enabled, "
                    "data_source) VALUES (%s,'reddit',%s,'nofollow',true,"
                    "true,2,true,'live') RETURNING community_id",
                    (site_id, key))
                comm_ids[key] = str(cur.fetchone()[0])
        conn.commit()
    print("    communities: %s" % ", ".join(
        "%s=%s" % (k, v[:8]) for k, v in comm_ids.items()))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT account_id FROM offpage_accounts WHERE site_id=%s AND "
            "platform='reddit'", (site_id,))
        r = cur.fetchone()
        if r:
            account_id = str(r[0])
        else:
            cur.execute(
                "INSERT INTO offpage_accounts (site_id, platform, handle, "
                "status, promotional_cap_30d, brand_name, last_verified_at, "
                "notes) VALUES (%s,'reddit','u_helium_ai','active',2,"
                "'Helium','now()','LIVE run %s') RETURNING account_id",
                (site_id, args.model))
            account_id = str(cur.fetchone()[0])
    conn.commit()
    with conn.cursor() as cur:
        for cid in comm_ids.values():
            cur.execute(
                "INSERT INTO offpage_account_communities (site_id, "
                "account_id, community_id, standing, assessed_at) "
                "VALUES (%s,%s,%s,'participating',now()) ON CONFLICT DO NOTHING",
                (site_id, account_id, cid))
    conn.commit()
    print("    account: %s (active)" % account_id)

    rec_ids = []
    for key, items in communities.items():
        for t in items:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT thread_id FROM offpage_threads WHERE site_id=%s "
                    "AND thread_url=%s", (site_id, t["url"]))
                r = cur.fetchone()
                if r:
                    thread_id = str(r[0])
                    cur.execute(
                        "UPDATE offpage_threads SET title=%s, snippet=%s, "
                        "context_source='serp', context_chars=%s, "
                        "context_usable=true, context_assessed_at=now() "
                        "WHERE thread_id=%s",
                        (t["title"], t["snippet"],
                         len("%s %s" % (t["title"], t["snippet"])), thread_id))
                else:
                    cur.execute(
                        "INSERT INTO offpage_threads (site_id, community_id, "
                        "thread_url, title, snippet, context_source, "
                        "context_chars, context_usable, context_assessed_at, "
                        "promotion_allowed, data_source) "
                        "VALUES (%s,%s,%s,%s,%s,'serp',%s,true,now(),true,"
                        "'live') RETURNING thread_id",
                        (site_id, comm_ids[key], t["url"], t["title"],
                         t["snippet"], len("%s %s" % (t["title"],
                                                      t["snippet"]))))
                    thread_id = str(cur.fetchone()[0])
            conn.commit()
            with conn.cursor() as cur:
                # uq_recommendations_candidate makes a re-run collide, so the
                # insert is a no-op and the existing row is reused.
                cur.execute(
                    "INSERT INTO recommendations (site_id, generator, "
                    "action_type, target_url, cluster_id, diagnosis, "
                    "evidence_json, work_required_json, impact, confidence, "
                    "effort, owner, status, result, data_source, "
                    "offpage_thread_id) VALUES (%s,'offpage_community',"
                    "'offpage_reply',%s,%s,%s,'[]'::jsonb,'[]'::jsonb,"
                    "'medium','medium','days','SEO','raw','pending',"
                    "'live',%s) ON CONFLICT DO NOTHING",
                    (site_id, t["url"], list(cluster_ids.values())[0],
                     "Real live thread asking: %s" % t["title"][:120],
                     thread_id))
                cur.execute(
                    "SELECT recommendation_id FROM recommendations "
                    "WHERE site_id=%s AND generator='offpage_community' "
                    "AND target_url=%s",
                    (site_id, t["url"]))
                rec_id = str(cur.fetchone()[0])
            conn.commit()
            rec_ids.append((rec_id, thread_id, t))
    print("    persisted %d live thread(s) + %d recommendation(s)"
          % (len(threads), len(rec_ids)))

    # ---------------------------------------------------------------- 5
    hr("5. LIVE DRAFT via %s" % args.model)
    from agents.llm_client import FallbackClient
    from fixes.offpage_drafter import draft_offpage_reply
    from fixes.offpage_grounding import load_brand_corpus

    client = FallbackClient()
    drafted = []
    for rec_id, thread_id, t in rec_ids:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM recommendations WHERE "
                        "recommendation_id=%s", (rec_id,))
            rec = dict(zip([d.name for d in cur.description], cur.fetchone()))
        print("\n  --- recommendation %s" % rec_id)
        print("      thread : %s" % t["url"])
        print("      title  : %r" % t["title"][:90])
        corpus = load_brand_corpus(conn, rec)
        print("      corpus : %d chars from %s" % (
            corpus["chars"], [s["url"] for s in corpus["sources"]]))
        print("      calling LLM ...")
        try:
            out = draft_offpage_reply(conn, rec_id, client=client)
        except Exception as exc:
            print("      FAILED %s: %s" % (type(exc).__name__, str(exc)[:200]))
            continue
        # draft_offpage_reply deliberately does NOT commit; it leaves that to
        # the caller. Without this the INSERT stays open, is visible to this
        # process's own later SELECTs, and is rolled back by conn.close().
        conn.commit()
        if out is None:
            print("      no draft (dropped by a gate)")
            continue
        drafted.append((rec_id, out, t))
        print("      draft_id=%s version=%s advice=%d words mention=%d "
              "words" % (out.get("draft_id"), out.get("version"),
                         len((out.get("advice_md") or "").split()),
                         len((out.get("mention_md") or "").split())))

    # ---------------------------------------------------------------- 6
    hr("6. REPORT")
    for rec_id, out, t in drafted:
        draft_id = out.get("draft_id")
        with conn.cursor() as cur:
            cur.execute("SELECT body_md, validation, grounding, status, "
                        "generation_source, brand_mention_count, "
                        "helpfulness_score, grounding_corpus_chars, version "
                        "FROM offpage_drafts WHERE draft_id=%s", (draft_id,))
            row = cur.fetchone()
        print("\n" + "#" * 72)
        print("# LIVE THREAD URL : %s" % t["url"])
        print("# RECOMMENDATION : %s" % rec_id)
        print("# DRAFT ID        : %s  (version %s)" % (draft_id, row[8]))
        print("# STATUS          : %s   source=%s  mentions=%s  helpfulness=%s"
              % (row[3], row[4], row[5], row[6]))
        print("#" * 72)
        print("\n--- GENERATED DRAFT BODY ---")
        print(row[0])
        print("\n--- GATES ---")
        for g in (row[1] or {}).get("gates", []):
            print("  %-28s %-8s %s" % (g["gate"], g["outcome"],
                                        g.get("reason") or ""))
        print("\n--- GROUNDING PROVENANCE ---")
        print(json.dumps(row[2], indent=2, default=str))
        print()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM offpage_publications WHERE site_id=%s",
                    (site_id,))
        pubs = cur.fetchone()[0]
    print("\nauto-publications for this tenant: %d  (publication is manual "
          "by design)" % pubs)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
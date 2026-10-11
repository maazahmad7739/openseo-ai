# -*- coding: utf-8 -*-
"""Off-page discovery job (plan/35): seam sweep + quota + minting.

python tests/run_offpage_discovery_tests.py   (needs a reachable local Postgres)

Every assertion is scoped to its OWN freshly-seeded tenant, never to global
job totals: the DB accumulates enabled legs between invocations, so summary
counts are inherently non-deterministic (the draft suite has the same
discipline - per-tenant ids, per-tenant counts).

Verifies:
  A. Seam sweep + minting
    1. enabled leg mints communities (fail-closed: enabled=false, rules
       unread) + threads + a raw offpage_community recommendation linked to
       the thread and the tenant's keyword cluster
    2. host rule drops cross/host results (reddit URL under an x leg)
    3. thread carries discovery_query + context_source/serp provenance
    4. web_search seam writes context_source='web_search' and honours
       post_kind hints (article)
    5. recommendation.cluster_id links the tenant keyword cluster
  B. Rate limiting
    6. the per-window quota holds ACROSS runs (second run in the same
       minute makes no further provider calls for this tenant)
    7. a skipped seam (no endpoint configured) claims no quota and logs
       nothing
  C. Idempotency + provenance
    8. re-run in a fresh window mints nothing (ON CONFLICT DO NOTHING on
       communities/threads, WHILE NOT EXISTS on recommendations)
    9. search_log records query, result_count, provider, latency
   10. tenant without a keyword cluster gets an inert rec (cluster NULL ->
       the drafter's grounding gate refuses it, which is the correct state)
  D. Scheduling home
   11. offpage_discovery is registered in SCHEDULE + LOCK_KEYS
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import db as database  # noqa: E402

from jobs import offpage_discovery as od  # noqa: E402
from jobs.locks import LOCK_KEYS  # noqa: E402
from jobs.scheduler import SCHEDULE  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


class FakeSeam:
    """Injectable provider. Records every call for quota assertions."""

    def __init__(self, results=None, skip=False):
        self.results = results or []
        self.calls = []
        self.skip = skip

    def __call__(self, site, platform, query, limit):
        self.calls.append((str(site), platform, query, limit))
        if self.skip:
            return {"skipped": True, "reason": "fake skip"}
        return {
            "ok": True, "provider": "fake_provider", "data_source": "mock",
            "results": [dict(r) for r in self.results],
            "http_status": 200, "latency_ms": 12, "error": None,
        }

    def available(self, site, platform):
        return not self.skip

    def calls_for(self, site_id):
        return [c for c in self.calls if c[0] == str(site_id)]


def seed_site(conn, domain, label, keyword):
    """Minimal discovery-ready tenant: site_config with target_keywords +
    a keyword_clusters row for the target keyword. Returns ids."""
    ids = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property,
                                     catalogue_size_tier, platform,
                                     target_keywords)
            VALUES (%s, %s, %s, 'small', 'shopify', %s)
            RETURNING site_id
            """,
            (f"Discovery {label} {RUN_TOKEN}", domain, f"sc-domain:{domain}",
             [keyword]))
        site_id = cur.fetchone()[0]
        ids["site_id"] = str(site_id)
        ids["domain"] = domain
        ids["keyword"] = keyword

        cur.execute(
            """
            INSERT INTO keyword_clusters (site_id, primary_keyword, keywords,
                                          intent)
            VALUES (%s, %s, %s, 'informational')
            RETURNING cluster_id
            """,
            (site_id, keyword, [keyword]))
        ids["cluster_id"] = str(cur.fetchone()[0])
    return ids


def enable_leg(conn, site_id, platform, seams, calls_per_day=1):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_platform_settings
                (site_id, platform, enabled, discovery_seams,
                 discovery_calls_per_day)
            VALUES (%s, %s, true, %s, %s)
            ON CONFLICT (site_id, platform) DO NOTHING
            """,
            (site_id, platform, seams, calls_per_day),
        )


def count(conn, table, site_id):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM {table} WHERE site_id = %s", (site_id,))
        return cur.fetchone()[0]


def thread_urls(conn, site_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT thread_url FROM offpage_threads WHERE site_id = %s",
            (site_id,))
        return {r[0] for r in cur.fetchall()}


def run(conn):
    now = datetime.now(timezone.utc)

    # ---- A. seam sweep + minting (scoped to t_x; cap=1 -> one query) ----
    t_x = seed_site(conn, f"disc-x-{RUN_TOKEN}.example.com", "X", "leather bag care")
    enable_leg(conn, t_x["site_id"], "x", ["dataforseo"], calls_per_day=1)
    x_url = f"https://x.com/{RUN_TOKEN}Vendor/status/1"
    tw_url = f"https://twitter.com/altshop/status/99"
    fake_x = FakeSeam([
        {"url": x_url, "title": "how to treat leather", "snippet": "long enough",
         "position": 1},
        {"url": "https://reddit.com/r/leather/comments/zz/thread",
         "title": "cross host", "snippet": "must drop", "position": 2},
        {"url": tw_url, "title": "twitter alias", "snippet": "ok", "position": 3},
    ])
    r1 = od.run(conn=conn, now=now, seams={"dataforseo": fake_x})
    check("A1 x leg ran its single query",
          len(fake_x.calls_for(t_x["site_id"])) == 1, str(r1))
    urls = thread_urls(conn, t_x["site_id"])
    check("A2 cross-host reddit result dropped",
          urls == {x_url, tw_url}, str(urls))
    check("A2b communities minted per distinct landing area",
          count(conn, "offpage_communities", t_x["site_id"]) == 2)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.discovery_query, t.context_source, t.post_kind,
                   c.enabled, c.link_policy
            FROM offpage_threads t
            JOIN offpage_communities c USING (site_id, community_id)
            WHERE t.site_id = %s AND t.thread_url = %s
            """, (t_x["site_id"], x_url))
        row = cur.fetchone()
    check("A3 provenance: discovery_query + serp context_source",
          row and row[0] == "site:x.com leather bag care"
          and row[1] == "serp" and row[2] == "reply", str(row))
    check("A3b community is fail-closed: disabled + rules unread",
          row and row[3] is False and row[4] == "unknown", str(row))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.cluster_id, r.target_url FROM recommendations r
            WHERE r.site_id = %s AND generator = 'offpage_community'
              AND target_url = %s AND status = 'raw'
            """, (t_x["site_id"], x_url))
        rec_x = cur.fetchone()
    check("A5 rec links the tenant keyword cluster + thread target",
          rec_x and str(rec_x[0]) == t_x["cluster_id"]
          and rec_x[1] == x_url, str(rec_x))
    check("A5b exactly one rec per minted URL",
          count(conn, "offpage_threads", t_x["site_id"]) == 2)

    # ---- web_search seam + post_kind hint (scoped to t_med) ----
    t_med = seed_site(conn, f"disc-med-{RUN_TOKEN}.example.com", "Medium", "supply chain guide")
    enable_leg(conn, t_med["site_id"], "medium", ["web_search"])
    med_url = "https://medium.com/@acme-pub/threading-a-supply-chain"
    fake_ws = FakeSeam([
        {"url": med_url, "title": "threading supply chains",
         "snippet": "rich enough", "post_kind": "article"},
    ])
    r4 = od.run(conn=conn, now=now, seams={"web_search": fake_ws})
    check("A4 web_search seam ran for the medium leg",
          len(fake_ws.calls_for(t_med["site_id"])) == 1, str(r4))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT context_source, post_kind, thread_url FROM offpage_threads
            WHERE site_id = %s
            """, (t_med["site_id"],))
        trow = cur.fetchone()
    check("A4b context_source='web_search' + article post_kind",
          trow and trow[0] == "web_search" and trow[1] == "article",
          str(trow))

    # ---- B. rate limiting ----
    # Hourly quota holds across runs in the same minute: the second run's
    # claim for t_x fails at the window ceiling, so NO further provider call
    # happens for this tenant even though the row never got deleted.
    calls_tx_before = len(fake_x.calls_for(t_x["site_id"]))
    r7 = od.run(conn=conn, now=now, seams={"dataforseo": fake_x})
    check("B6 per-window quota holds across runs",
          len(fake_x.calls_for(t_x["site_id"])) == calls_tx_before, str(r7))
    check("B6b quota state persisted per window",
          count(conn, "offpage_search_quota", t_x["site_id"]) >= 1)

    # A skipped seam (no endpoint configured -> provider returns skipped)
    # claims no quota and logs nothing for its tenant.
    t_skip = seed_site(conn, f"disc-skip-{RUN_TOKEN}.example.com", "Skip", "widget advice")
    enable_leg(conn, t_skip["site_id"], "x", ["web_search"])
    fake_skip_ws = FakeSeam(skip=True)
    r8 = od.run(conn=conn, now=now, seams={"web_search": fake_skip_ws})
    check("B7 skipped seam is not called, not logged, not claimed",
          len(fake_skip_ws.calls_for(t_skip["site_id"])) == 0
          and count(conn, "offpage_search_log", t_skip["site_id"]) == 0
          and count(conn, "offpage_search_quota", t_skip["site_id"]) == 0
          and count(conn, "offpage_threads", t_skip["site_id"]) == 0,
          str(r8))

    # ---- C. idempotency + provenance ----
    # A fresh window (next minute) resets the quota, so the leg genuinely
    # re-searches the same URL - and must mint nothing.
    now2 = now + timedelta(minutes=2)
    r9 = od.run(conn=conn, now=now2, seams={"dataforseo": fake_x})
    check("C8 re-run in a fresh window mints nothing for t_x",
          thread_urls(conn, t_x["site_id"]) == {x_url, tw_url}
          and len(fake_x.calls_for(t_x["site_id"])) == calls_tx_before + 1,
          str(r9))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM recommendations
            WHERE site_id = %s AND generator = 'offpage_community'
            """, (t_x["site_id"],))
        check("C8b recommendation count stable after re-run",
              cur.fetchone()[0] == 2)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT seam, query, result_count, provider, latency_ms
            FROM offpage_search_log WHERE site_id = %s ORDER BY created_at
            """, (t_x["site_id"],))
        logs = cur.fetchall()
    check("C9 ledger records each call with query/provider/latency",
          len(logs) == 2 and all(
              l[0] == "dataforseo" and l[1] == "site:x.com leather bag care"
              and l[3] == "fake_provider" and l[4] == 12 for l in logs),
          str(logs))

    # A tenant with NO cluster keeps an inert rec (NULL cluster -> the
    # drafter's grounding gate refuses it, so the opportunity is visible but
    # never drafted).
    t_nc = seed_site(conn, f"disc-nc-{RUN_TOKEN}.example.com", "NoCluster", "paper stock")
    enable_leg(conn, t_nc["site_id"], "x", ["dataforseo"])
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM keyword_clusters WHERE site_id = %s",
            (t_nc["site_id"],))
    nc_url = f"https://x.com/ncVendor/status/3"
    fake_nc = FakeSeam([{"url": nc_url, "title": "paper stock", "snippet": "ok"}])
    od.run(conn=conn, now=now, seams={"dataforseo": fake_nc})
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cluster_id, target_url FROM recommendations
            WHERE site_id = %s AND generator = 'offpage_community'
            """, (t_nc["site_id"],))
        nc = cur.fetchone()
    check("C10 no-cluster rec is created inert (cluster NULL)",
          nc and nc[0] is None and nc[1] == nc_url, str(nc))

    # ---- D. scheduling registration ----
    check("D11 scheduled daily@07:00 UTC",
          SCHEDULE.get("offpage_discovery") == {"cadence": "daily",
                                                "hour_utc": 7},
          str(SCHEDULE.get("offpage_discovery")))
    check("D11b advisory lock key registered",
          "offpage_discovery" in LOCK_KEYS)


def main():
    conn = database.get_connection()
    conn.autocommit = True
    try:
        run(conn)
    finally:
        conn.close()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all off-page discovery tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
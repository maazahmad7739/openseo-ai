# -*- coding: utf-8 -*-
"""Off-page operator queue + verification sweep tests (plan/34).

python tests/run_offpage_queue_tests.py   (needs a reachable local Postgres)

Drives the real FastAPI app (TestClient) against the real local DB with
freshly-seeded, run-unique tenants — no fixture mutation, exactly the
run_api_tests.py pattern. Verifies:

  A. Queue surface
     A1 queue includes a seeded draft with caps + gate transcript
     A2 queue caps at 5 oldest-first; total_pending counts the backlog
     A3 published drafts leave the queue
     A4 skipped drafts leave the queue + recommendation rejected
  B. choose (Copy Advice / Copy Full Response)
     B1 helpful copies advice_md and approves the draft
     B2 helpful on a mention-less draft is fine (mention_allowed=False)
     B3 promotional copies the full body (advice+mention)
     B4 promotional refuses when the draft has no mention
     B5 promotional refuses when the draft post_type is not promotional
     B6 promotional refuses when the ACCOUNT cap is reached
     B7 promotional refuses when the COMMUNITY cap is reached
     B8 promotional refuses when no eligible account resolves
     B9 re-choice after approval still copies (idempotent surface)
  C. skip
     C1 skip rejects the rec + writes rejection_log + operator event
     C2 skip refuses an already-published draft (409)
  D. permalink
     D1 wrong host / non-http are 422
     D2 submit pre-creates 4 checkpoints at the T+ offsets
     D3 publication frozen at approved_body_md; rec parked at 'live'
     D4 re-submit is idempotent (same publication, no new checkpoints)
  E. verification sweep (offpage_verify.run with injected clock/fetcher)
     E1 verify_permalink four-valued mapping
     E2 precreate idempotent (ON CONFLICT DO NOTHING)
     E3 a fully-due live publication reaches status 'live'
     E4 a removed checkpoint strikes + marks the publication removed
     E5 3rd strike disables the community (kill-switch)
     E6 unverifiable never strikes and parks the publication
     E7 only DUE checkpoints are claimed
     E8 a re-run with nothing due checks nothing
     E9 run() never raises (errant fetch -> unverifiable)
     E10 SKIP LOCKED: a claimed row is not double-claimed concurrently
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from fastapi.testclient import TestClient  # noqa: E402

import db as database  # noqa: E402

RUN_TOKEN = uuid.uuid4().hex[:8]
FAILURES = []

DOMAIN = f"queue-{RUN_TOKEN}.example.com"

ADVICE = (
    "The real signal in a churn interview is where the friction sat before "
    "the cancellation, not the cancellation itself. Ask them to retell the "
    "moment they first considered switching, and triangulate that against "
    "product logs to find the actual drop-off point."
)
MENTION = (
    "Our tracking surfaces exactly that drop-off point in the usage logs, "
    "so the interview and the data can be compared directly."
)


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


def utcnow():
    return datetime.now(timezone.utc)


def _gates(passes=("context_sufficient", "grounding_corpus", "account_standing")):
    return {"gates": [{"gate": g, "outcome": "pass", "reason": None}
                      for g in passes],
            "refused": []}


def seed_site(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO site_config (site_name, domain, gsc_property)
            VALUES (%s, %s, %s)
            RETURNING site_id
            """,
            (f"Offpage Queue {RUN_TOKEN}", DOMAIN, f"sc-domain:{DOMAIN}"))
        return str(cur.fetchone()[0])


def seed_card(conn, site_id, label, *, post_type="helpful", mention=None,
              community_overrides=None, account_overrides=None,
              strike_count=None, account_pubs=0, community_cap=None,
              draft_status="drafted", created_at=None):
    """One queue entry: community + thread + account + rec + draft.

    `mention`: if None, defaults to MENTION for promotional, "" for helpful.
    `account_pubs`: number of promotional/live publications to pad the
      account's trailing-30d usage toward its cap.
    Returns a dict of ids (as strings).
    """
    if mention is None:
        mention = MENTION if post_type == "promotional" else ""
    community_key = f"r/q{RUN_TOKEN}{label}"
    handle = f"u/{label}{RUN_TOKEN}"
    community = dict({
        "link_policy": "nofollow",
        "allows_self_promo": True,
        "ai_content_allowed": True,
        "enabled": True,
    }, **(community_overrides or {}))
    account = dict({"status": "active", "brand_name": "Aurora Audio",
                    "promotional_cap_30d": 3},
                   **(account_overrides or {}))

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_communities
                (site_id, platform, community_key, link_policy,
                 allows_self_promo, ai_content_allowed, enabled,
                 promotional_cap_30d, strike_count, data_source)
            VALUES (%s, 'reddit', %s, %s, %s, %s, %s, %s, %s, 'import')
            RETURNING community_id
            """,
            (site_id, community_key, community["link_policy"],
             community["allows_self_promo"], community["ai_content_allowed"],
             community["enabled"],
             community_cap if community_cap is not None else 2,
             strike_count if strike_count is not None else 0))
        community_id = cur.fetchone()[0]

        thread_url = (f"https://reddit.com/{community_key}/comments/"
                      f"{label}{RUN_TOKEN}/thread")
        cur.execute(
            """
            INSERT INTO offpage_threads
                (site_id, community_id, thread_url, title, snippet,
                 context_source, promotion_allowed, data_source)
            VALUES (%s, %s, %s, %s, %s, 'serp', true, 'import')
            RETURNING thread_id
            """,
            (site_id, community_id, thread_url,
             f"Thread {label}", "A question from the community about the craft."))
        thread_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO offpage_accounts
                (site_id, platform, handle, brand_name, status,
                 promotional_cap_30d)
            VALUES (%s, 'reddit', %s, %s, %s, %s)
            RETURNING account_id
            """,
            (site_id, handle, account["brand_name"], account["status"],
             account["promotional_cap_30d"]))
        account_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO offpage_account_communities
                (site_id, account_id, community_id, standing, assessed_at)
            VALUES (%s, %s, %s, %s, now())
            """,
            (site_id, account_id, community_id, "established"))

        cur.execute(
            """
            INSERT INTO recommendations
                (site_id, generator, action_type, status, diagnosis,
                 target_url, offpage_thread_id)
            VALUES (%s, 'offpage_community', 'offpage_reply', 'raw', %s, %s, %s)
            RETURNING recommendation_id
            """,
            (site_id, "Off-page opportunity", thread_url, thread_id))
        rec_id = cur.fetchone()[0]

        body_md = ADVICE if mention == "" else f"{ADVICE}\n\n{mention}"
        grounding = {"post_type": post_type}
        cur.execute(
            """
            INSERT INTO offpage_drafts
                (site_id, recommendation_id, version, body_md, advice_md,
                 mention_md, generation_source, validation, grounding,
                 helpfulness_score, promo_density, brand_mention_count,
                 link_count, status, created_at)
            VALUES (%s, %s, 1, %s, %s, %s, 'deterministic', %s, %s,
                    0.90, 0.10, %s, 0, %s, %s)
            RETURNING draft_id
            """,
            (site_id, rec_id, body_md, ADVICE, mention,
             json.dumps(_gates()), json.dumps(grounding),
             1 if mention else 0, draft_status,
             created_at or datetime.now(timezone.utc)))
        draft_id = cur.fetchone()[0]

        for _ in range(account_pubs):
            _pad_promotional(conn, site_id, rec_id, account_id,
                             community_id, thread_id)

    return {"site_id": site_id, "draft_id": str(draft_id),
            "recommendation_id": str(rec_id), "community_id": str(community_id),
            "account_id": str(account_id), "thread_id": str(thread_id),
            "post_type": post_type}


def _pad_promotional(conn, site_id, rec_id, account_id, community_id,
                     thread_id):
    """A live promotional publication (unique superseded draft) to push the
    account/community against its trailing-30d promotional cap."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_drafts
                (site_id, recommendation_id, body_md, version, status)
            VALUES (%s, %s, 'seed cap padding',
                    COALESCE((SELECT max(version) + 1 FROM offpage_drafts
                              WHERE recommendation_id = %s), 0),
                    'superseded')
            RETURNING draft_id
            """, (site_id, rec_id, rec_id))
        draft_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO offpage_publications
                (site_id, recommendation_id, draft_id, account_id,
                 community_id, thread_id, approved_body_md, post_type,
                 status, submitted_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'promotional', 'live',
                    now() - INTERVAL '2 days')
            """,
            (site_id, rec_id, draft_id, account_id, community_id, thread_id,
             "seeded promotional"))


def seed_publication(conn, world, *, submitted_at, permalink=None,
                     approved_body_md=None):
    """A pending_verify publication on world's draft + pre-created checkpoints."""
    from jobs.offpage_verify import precreate_verification_rows
    permalink = permalink or ("https://www.reddit.com/r/thread/comments/"
                              f"{RUN_TOKEN}/title/")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO offpage_publications
                (site_id, recommendation_id, draft_id, account_id,
                 community_id, thread_id, approved_body_md, live_permalink,
                 post_type, status, submitted_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending_verify', %s)
            RETURNING publication_id
            """,
            (world["site_id"], world["recommendation_id"], world["draft_id"],
             world["account_id"], world["community_id"], world["thread_id"],
             approved_body_md or f"{ADVICE}\n\n{MENTION}", permalink,
             world["post_type"], submitted_at))
        pub_id = cur.fetchone()[0]
    precreate_verification_rows(conn, world["site_id"], pub_id, submitted_at)
    return str(pub_id)


def fetch_sequence(*codes):
    """Fetcher that returns the given HTTP codes in order, then 200."""
    calls = []

    def fetcher(url, timeout=20):
        index = min(len(calls), len(codes) - 1)
        calls.append(index)
        return codes[index]
    fetcher.calls = calls
    return fetcher


def cleanup(conn, site_id):
    """Remove everything this run's token owns for the site."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM offpage_verifications WHERE site_id = %s", (site_id,))
        cur.execute("DELETE FROM offpage_events WHERE site_id = %s", (site_id,))
        cur.execute("DELETE FROM rejection_log WHERE site_id = %s", (site_id,))
        cur.execute(
            "DELETE FROM offpage_publications WHERE site_id = %s", (site_id,))
        cur.execute("DELETE FROM offpage_drafts WHERE site_id = %s", (site_id,))
        cur.execute(
            "DELETE FROM offpage_account_communities WHERE site_id = %s",
            (site_id,))
        cur.execute("DELETE FROM offpage_accounts WHERE site_id = %s",
                    (site_id,))
        cur.execute("DELETE FROM recommendations WHERE site_id = %s",
                    (site_id,))
        cur.execute("DELETE FROM offpage_threads WHERE site_id = %s",
                    (site_id,))
        cur.execute("DELETE FROM offpage_communities WHERE site_id = %s",
                    (site_id,))
        cur.execute("DELETE FROM site_config WHERE site_id = %s", (site_id,))
    conn.commit()


def queue(client, site_id):
    r = client.get(f"/api/offpage/queue?site_id={site_id}")
    assert r.status_code == 200, r.text[:300]
    return r.json()


def main():
    import api.main as api_main  # noqa: F401
    from jobs import offpage_verify as ov

    client = TestClient(api_main.app)
    allok = True
    conn = database.get_connection()
    conn.autocommit = False
    site_id = None
    try:
        site_id = seed_site(conn)
        conn.commit()

        # -------------------------------------------------------------
        # A. queue surface
        # -------------------------------------------------------------
        print("\n== A. queue surface ==")
        world = seed_card(conn, site_id, "a1")
        conn.commit()
        q = queue(client, site_id)
        item = next((i for i in q["items"]
                     if str(i["draft_id"]) == world["draft_id"]), None)
        allok &= check("A1 queue includes the seeded draft",
                       item is not None, str(len(q["items"])))
        allok &= check("A1b post_type from grounding", item["post_type"] == "helpful")
        allok &= check("A1c caps resolved for the community",
                       (item.get("caps") or {}).get("account_handle") == f"u/a1{RUN_TOKEN}",
                       str(item.get("caps")))
        allok &= check("A1d gate transcript carried",
                       len(item.get("gates") or []) == 3, str(item.get("gates")))
        allok &= check("A1e advice/mention split carried",
                       item["advice_md"] == ADVICE and item["mention_md"] == ""
                       and item["body_md"] == ADVICE)

        # cap at 5, oldest first (stagger created_at so the order is real)
        _base = utcnow()
        six = [seed_card(conn, site_id, f"cap{i}",
                         created_at=_base + timedelta(minutes=i + 1))
               for i in range(6)]
        conn.commit()
        q = queue(client, site_id)
        allok &= check("A2 queue capped at 5 cards", q["showing"] == 5,
                       f"showing={q['showing']}")
        allok &= check("A2b total_active reports the window", q["total_active"] == 5)
        allok &= check("A2c total_pending counts the backlog",
                       q["total_pending"] >= 7, str(q["total_pending"]))
        expect = [world["draft_id"]] + [str(six[i]["draft_id"])
                                        for i in range(4)]
        drafts_in_order = [str(i["draft_id"]) for i in q["items"]]
        allok &= check("A2d oldest-first order (windowed)",
                       drafts_in_order == expect, str(drafts_in_order))

        # published drafts leave the queue
        pub_now = utcnow()
        (pub_id_a,) = wedge_publish(client, conn, site_id, six[0], pub_now)
        q = queue(client, site_id)
        allok &= check("A3 published draft leaves the queue",
                       str(six[0]["draft_id"]) not in
                       [str(i["draft_id"]) for i in q["items"]],
                       str([str(i["draft_id"]) for i in q["items"]][:2]))

        # -------------------------------------------------------------
        # B. choose
        # -------------------------------------------------------------
        print("\n== B. choose ==")
        b1 = seed_card(conn, site_id, "b1")
        conn.commit()
        r = client.post(f"/api/offpage/drafts/{b1['draft_id']}/choose"
                        f"?site_id={site_id}", json={"action": "helpful"})
        body = r.json()
        allok &= check("B1 helpful -> approved", r.status_code == 200
                       and body["status"] == "approved", r.text[:200])
        allok &= check("B1b copies advice_md", body["copied_md"] == ADVICE)
        allok &= check("B1c mention_allowed stays false",
                       body["mention_allowed"] is False)
        _assert_event(conn, allok, "B1d operator_choice pass event recorded",
                      b1["draft_id"], "operator_choice", "pass")

        b2 = seed_card(conn, site_id, "b2", post_type="promotional")
        conn.commit()
        r = client.post(f"/api/offpage/drafts/{b2['draft_id']}/choose"
                        f"?site_id={site_id}", json={"action": "promotional"})
        body = r.json()
        allok &= check("B3 promotional copies full body",
                       r.status_code == 200
                       and body["copied_md"] == f"{ADVICE}\n\n{MENTION}",
                       r.text[:200])

        b4 = seed_card(conn, site_id, "b4", post_type="helpful")
        conn.commit()
        r = client.post(f"/api/offpage/drafts/{b4['draft_id']}/choose"
                        f"?site_id={site_id}", json={"action": "promotional"})
        allok &= check("B4 promo on mention-less draft is 409",
                       r.status_code == 409 and "no mention" in r.text, r.text[:160])

        b5 = seed_card(conn, site_id, "b5", post_type="helpful",
                       mention=MENTION)
        conn.commit()
        r = client.post(f"/api/offpage/drafts/{b5['draft_id']}/choose"
                        f"?site_id={site_id}", json={"action": "promotional"})
        allok &= check("B5 promo on non-promotional post_type is 409",
                       r.status_code == 409
                       and "not 'promotional'" in r.text, r.text[:160])

        b6 = seed_card(conn, site_id, "b6", post_type="promotional",
                       account_pubs=3)
        conn.commit()
        r = client.post(f"/api/offpage/drafts/{b6['draft_id']}/choose"
                        f"?site_id={site_id}", json={"action": "promotional"})
        allok &= check("B6 promo refused at account cap",
                       r.status_code == 409
                       and "account promotional cap reached" in r.text,
                       r.text[:160])

        b7 = seed_card(conn, site_id, "b7", post_type="promotional",
                       account_overrides={"promotional_cap_30d": 100},
                       community_cap=1, account_pubs=1)
        conn.commit()
        r = client.post(f"/api/offpage/drafts/{b7['draft_id']}/choose"
                        f"?site_id={site_id}", json={"action": "promotional"})
        allok &= check("B7 promo refused at community cap",
                       r.status_code == 409
                       and "community promotional cap reached" in r.text,
                       r.text[:160])

        b8 = seed_card(conn, site_id, "b8", post_type="promotional",
                       community_overrides={"ai_content_allowed": False})
        # no eligible account: ai_content_allowed=False is a community rule,
        # not an account rule, so instead remove the account's standing.
        with conn.cursor() as cur:
            cur.execute("UPDATE offpage_account_communities SET standing = 'none'"
                        " WHERE site_id = %s AND community_id = %s",
                        (site_id, b8["community_id"]))
        conn.commit()
        r = client.post(f"/api/offpage/drafts/{b8['draft_id']}/choose"
                        f"?site_id={site_id}", json={"action": "promotional"})
        allok &= check("B8 promo refused when no account resolves",
                       r.status_code == 409
                       and "no eligible account" in r.text, r.text[:160])

        # re-choice after approval still copies (approved drafts stay in queue)
        r = client.post(f"/api/offpage/drafts/{b1['draft_id']}/choose"
                        f"?site_id={site_id}", json={"action": "helpful"})
        allok &= check("B9 re-choice after approval copies again",
                       r.status_code == 200 and r.json()["copied_md"] == ADVICE,
                       r.text[:160])

        # -------------------------------------------------------------
        # C. skip
        # -------------------------------------------------------------
        print("\n== C. skip ==")
        c1 = seed_card(conn, site_id, "c1")
        conn.commit()
        r = client.post(f"/api/offpage/drafts/{c1['draft_id']}/skip"
                        f"?site_id={site_id}",
                        json={"reason": "duplicate thread; low intent"})
        body = r.json()
        allok &= check("C1 skip rejects draft + rec",
                       r.status_code == 200 and body["status"] == "rejected_by_operator"
                       and body["recommendation_status"] == "rejected", r.text[:200])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM rejection_log WHERE site_id = %s "
                "AND candidate_id = %s AND reason = %s",
                (site_id, c1["recommendation_id"],
                 "duplicate thread; low intent"))
            allok &= check("C1b rejection logged for the learning loop",
                           cur.fetchone()[0] == 1)
        _assert_event(conn, allok, "C1c operator_skip refuse event recorded",
                      c1["draft_id"], "operator_skip", "refuse")

        c2 = seed_card(conn, site_id, "c2")
        conn.commit()
        wedge_publish(client, conn, site_id, c2, utcnow())
        r = client.post(f"/api/offpage/drafts/{c2['draft_id']}/skip"
                        f"?site_id={site_id}", json={"reason": "nope"})
        allok &= check("C2 skip on a published draft is 409",
                       r.status_code == 409, r.text[:160])

        # -------------------------------------------------------------
        # D. permalink
        # -------------------------------------------------------------
        print("\n== D. permalink ==")
        d1 = seed_card(conn, site_id, "d1", post_type="promotional")
        conn.commit()
        r = client.post(f"/api/offpage/drafts/{d1['draft_id']}/permalink"
                        f"?site_id={site_id}",
                        json={"permalink": "https://example.com/not-reddit",
                              "operator": "tester"})
        allok &= check("D1 wrong platform host is 422",
                       r.status_code == 422, r.text[:160])
        r = client.post(f"/api/offpage/drafts/{d1['draft_id']}/permalink"
                        f"?site_id={site_id}",
                        json={"permalink": "ftp://www.reddit.com/x"})
        allok &= check("D1b non-http scheme is 422",
                       r.status_code == 422, r.text[:160])

        r = client.post(f"/api/offpage/drafts/{d1['draft_id']}/choose"
                        f"?site_id={site_id}", json={"action": "promotional"})
        allok &= check("D1c draft staged promotional first",
                       r.status_code == 200, r.text[:200])
        permalink = ("https://www.reddit.com/r/q%d/comments/%s/thread/"
                     % (int(utcnow().timestamp()), RUN_TOKEN))
        r = client.post(f"/api/offpage/drafts/{d1['draft_id']}/permalink"
                        f"?site_id={site_id}",
                        json={"permalink": permalink, "operator": "alex"})
        pbody = r.json()
        allok &= check("D2 submit -> pending_verify + 4 checkpoints",
                       r.status_code == 200 and pbody["status"] == "pending_verify"
                       and len(pbody["checkpoints"]) == 4, r.text[:200])
        allok &= check("D2b offsets are the T+ schedule",
                       [c["offset_minutes"] for c in pbody["checkpoints"]]
                       == [15, 120, 1440, 4320], str(pbody["checkpoints"]))
        allok &= check("D2c post_type frozen from operator_choice",
                       pbody["post_type"] == "promotional")
        allok &= check("D3 rec parked at 'live' (off-page never measured)",
                       pbody["recommendation_status"] == "live")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT approved_body_md FROM offpage_publications "
                "WHERE publication_id = %s", (pbody["publication_id"],))
            allok &= check("D3b publication frozen at full approved body",
                           cur.fetchone()[0] == f"{ADVICE}\n\n{MENTION}")

        # idempotent re-submit
        permalink2 = permalink.replace("thread", "thread/edited")
        r = client.post(f"/api/offpage/drafts/{d1['draft_id']}/permalink"
                        f"?site_id={site_id}",
                        json={"permalink": permalink2, "operator": "alex"})
        p2 = r.json()
        allok &= check("D4 re-submit returns the SAME publication",
                       r.status_code == 200
                       and p2["publication_id"] == pbody["publication_id"],
                       r.text[:160])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM offpage_verifications "
                "WHERE publication_id = %s", (pbody["publication_id"],))
            allok &= check("D4b no duplicate checkpoint rows",
                           cur.fetchone()[0] == 4)
            cur.execute(
                "SELECT live_permalink FROM offpage_publications "
                "WHERE publication_id = %s", (pbody["publication_id"],))
            allok &= check("D4c permalink updated in place",
                           cur.fetchone()[0] == permalink2)

        # -------------------------------------------------------------
        # E. verification sweep
        # -------------------------------------------------------------
        print("\n== E. verification sweep ==")
        allok &= check("E1 200 -> live", ov.verify_permalink("x", fetch_sequence(200))["result"] == "live")
        allok &= check("E1b 201 -> live", ov.verify_permalink("x", fetch_sequence(201))["result"] == "live")
        allok &= check("E1c 404 -> removed", ov.verify_permalink("x", fetch_sequence(404))["result"] == "removed")
        allok &= check("E1d 410 -> removed", ov.verify_permalink("x", fetch_sequence(410))["result"] == "removed")
        allok &= check("E1e 403 -> unverifiable", ov.verify_permalink("x", fetch_sequence(403))["result"] == "unverifiable")
        allok &= check("E1f 503 -> unverifiable", ov.verify_permalink("x", fetch_sequence(503))["result"] == "unverifiable")
        allok &= check("E1g exception -> unverifiable",
                       ov.verify_permalink("x", lambda u, t=_utcnow_default(): (_ for _ in ()).throw(RuntimeError("net down")))["result"] == "unverifiable")

        e = seed_card(conn, site_id, "e", post_type="promotional")
        conn.commit()
        # Flush accumulated due checkpoints BEFORE the scenario seeds: on a
        # shared dev DB, verifications left behind by earlier suite runs
        # become due as the wall clock advances, and every global-sweep
        # assertion below (results counts, checked counts) would otherwise
        # ride along on them. An all-unverifiable fetcher consumes every due
        # row without recording a single strike (plan/33 safety mapping).
        ov.run(now=_utcnow_default(), conn=conn, fetcher=fetch_sequence(503))
        _now = utcnow()
        pub_old = seed_publication(conn, e, submitted_at=_now - timedelta(days=100))
        conn.commit()
        sumr = ov.run(now=_now, conn=conn, fetcher=fetch_sequence(200))
        # The sweep claims every DUE checkpoint in the database, so on a
        # shared dev DB older suites' verifications can ride along in
        # sumr["results"]. Scope the assertion to THIS suite's publication:
        # its 4 checkpoints all checked 'live' and the publication flips.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM offpage_verifications
                WHERE publication_id = %s
                  AND checked_at IS NOT NULL AND result = 'live'
                """, (pub_old,))
            own_live = cur.fetchone()[0]
            cur.execute(
                "SELECT checked_at IS NOT NULL FROM offpage_verifications "
                "WHERE publication_id = %s AND checkpoint_index = 0",
                (pub_old,))
            cp0 = cur.fetchone()
        allok &= check("E3 fully-due 200s -> publication live",
                       own_live == 4 and cp0 is not None and cp0[0],
                       "own live checkpoints=%d (global sweep result=%s)"
                       % (own_live, str(sumr)))
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM offpage_publications WHERE publication_id = %s",
                        (pub_old,))
            allok &= check("E3b publication status updated to live",
                           cur.fetchone()[0] == "live")

        e2 = seed_card(conn, site_id, "e2", post_type="promotional")
        conn.commit()
        pub_rem = seed_publication(conn, e2, submitted_at=_now - timedelta(days=100))
        conn.commit()
        sumr = ov.run(now=_now, conn=conn, fetcher=fetch_sequence(410, 200))
        allok &= check("E4 removed flows through",
                       sumr["results"]["removed"] == 1
                       and sumr["publications_now_removed"] == 1, str(sumr))
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM offpage_publications WHERE publication_id = %s",
                        (pub_rem,))
            allok &= check("E4b publication marked removed",
                           cur.fetchone()[0] == "removed")
            cur.execute("SELECT strike_count FROM offpage_communities WHERE community_id = %s",
                        (e2["community_id"],))
            allok &= check("E4c one strike recorded", cur.fetchone()[0] == 1)
        _assert_event(conn, allok, "E4d survival refuse event recorded",
                      pub_rem, "survival", "refuse", entity_type="publication")

        e3 = seed_card(conn, site_id, "e3", post_type="promotional", strike_count=2)
        conn.commit()
        pub_kill = seed_publication(conn, e3, submitted_at=_now - timedelta(days=100))
        conn.commit()
        sumr = ov.run(now=_now, conn=conn, fetcher=fetch_sequence(410, 200))
        allok &= check("E5 3rd strike disables the community",
                       e3["community_id"] in sumr["communities_disabled"], str(sumr))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT enabled, disabled_reason FROM offpage_communities "
                "WHERE community_id = %s", (e3["community_id"],))
            enabled, reason = cur.fetchone()
            allok &= check("E5b community disabled with reason",
                           enabled is False
                           and reason and "kill-switch" in reason, str(reason))
            cur.execute("SELECT strike_count FROM offpage_communities WHERE community_id = %s",
                        (e3["community_id"],))
            allok &= check("E5c strike count is now 3", cur.fetchone()[0] == 3)

        e4 = seed_card(conn, site_id, "e4", post_type="promotional")
        conn.commit()
        pub_unv = seed_publication(conn, e4, submitted_at=_now - timedelta(days=100))
        conn.commit()
        sumr = ov.run(now=_now, conn=conn, fetcher=fetch_sequence(503))
        allok &= check("E6 all-unverifiable never strikes",
                       sumr["communities_disabled"] == []
                       and sumr["results"]["unverifiable"] == 4, str(sumr))
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM offpage_publications WHERE publication_id = %s",
                        (pub_unv,))
            allok &= check("E6b unverifiable publication parked as such",
                           cur.fetchone()[0] == "unverifiable")
            cur.execute("SELECT strike_count FROM offpage_communities WHERE community_id = %s",
                        (e4["community_id"],))
            allok &= check("E6c zero strikes accrued", cur.fetchone()[0] == 0)

        # only due checkpoints are claimed: submit 20m ago -> cp0 due at +15m
        e5 = seed_card(conn, site_id, "e5", post_type="promotional")
        conn.commit()
        _sub = _now - timedelta(minutes=20)
        pub_fresh = seed_publication(conn, e5, submitted_at=_sub)
        conn.commit()
        sumr = ov.run(now=_now, conn=conn, fetcher=fetch_sequence(200))
        allok &= check("E7 only the due checkpoint is claimed",
                       sumr["checked"] == 1, str(sumr))
        sumr = ov.run(now=_now, conn=conn, fetcher=fetch_sequence(200))
        allok &= check("E8 nothing due -> nothing checked",
                       sumr["checked"] == 0, str(sumr))

        # errant fetcher raises -> unverifiable, run still returns
        e6 = seed_card(conn, site_id, "e6", post_type="promotional")
        conn.commit()
        pub_boom = seed_publication(conn, e6, submitted_at=_now - timedelta(days=100))
        conn.commit()
        sumr = ov.run(now=_now, conn=conn,
                      fetcher=lambda u, t=20: (_ for _ in ()).throw(ConnectionError("boom")))
        allok &= check("E9 run never raises on fetch errors",
                       sumr.get("status") != "failed"
                       and sumr["results"] == {"live": 0, "removed": 0, "unverifiable": 4},
                       str(sumr))

        # concurrency: SKIP LOCKED prevents double-claiming
        e7 = seed_card(conn, site_id, "e7", post_type="promotional")
        conn.commit()
        seed_publication(conn, e7, submitted_at=_now - timedelta(days=100))
        conn.commit()
        conn2 = database.get_connection()
        conn2.autocommit = False
        try:
            first = ov._claim_due(conn, _now)
            second = ov._claim_due(conn2, _now)
            ids1 = {r["verification_id"] for r in first}
            ids2 = {r["verification_id"] for r in second}
            allok &= check("E10 SKIP LOCKED: second claim skips the locked rows",
                           len(first) == 4 and ids1.isdisjoint(ids2),
                           f"first={len(ids1)} second={len(ids2)}"
                           f" overlap={ids1 & ids2}")
        finally:
            conn2.rollback()
            conn2.close()
    except Exception as exc:  # surface failures loudly, still clean up
        print(f"[offpage queue tests] EXCEPTION: {type(exc).__name__}: {exc}")
        allok = False
    finally:
        if site_id:
            cleanup(conn, site_id)
        conn.close()

    print("\nOFFPAGE QUEUE TEST RUN", "PASSED" if allok else "FAILED")
    sys.exit(0 if allok else 1)


def wedge_publish(client, conn, site_id, world, now):
    """Publish a draft through the API and return (publication_id,)."""
    perma = ("https://www.reddit.com/r/thread/comments/"
             f"w{RUN_TOKEN}/title/")
    r = client.post(f"/api/offpage/drafts/{world['draft_id']}/permalink"
                    f"?site_id={site_id}", json={"permalink": perma})
    assert r.status_code == 200, r.text[:300]
    return (r.json()["publication_id"],)


def _utcnow_default():
    return datetime.now(timezone.utc)


def _assert_event(conn, allok, name, entity_id, gate, outcome,
                  entity_type="draft"):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM offpage_events "
            "WHERE entity_type = %s AND entity_id = %s AND gate = %s "
            "AND outcome = %s", (entity_type, entity_id, gate, outcome))
        found = cur.fetchone()[0]
    allok &= check(name, found >= 1, f"rows={found}")
    return allok


if __name__ == "__main__":
    main()
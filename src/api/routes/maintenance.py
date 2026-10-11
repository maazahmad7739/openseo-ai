"""TEMPORARY off-page demo seed (operator UI preview).

Mints clearly-labelled demo drafts for ONE site so the Off-page queue
cards can be previewed on environments whose tenant has no drafts yet.
Follows exactly the row shapes of tests/run_offpage_queue_tests.py.

Every seeded row carries an unambiguous marker (community_key
'demo-ui-preview-*', titles prefixed '[UI DEMO]', discovery_query
'demo-seed'); `op=clear` removes precisely those rows. No real discovery
runs; nothing publishes; no credentials are involved beyond the runtime's
own DB env.

REMOVE together with the demo data once the operator has previewed the UI.
"""

import json
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

import db as database

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

_DEMO_KEY_PREFIX = "demo-ui-preview"
_MARKERS = {
    "community_prefix": _DEMO_KEY_PREFIX,
    "discovery_query": "demo-seed",
}

_ADVICE_1 = (
    "Before you audition any new cans, check the seal: most \"muddy bass\" "
    "reports on the cheaper side of the market are pad fit and headband "
    "pressure, not the driver. A pad that breathes will flatten the low end "
    "long before the driver itself gives up.\n\n"
    "Second, listen for how the tuning handles speech: drop a podcast or a "
    "talky playlist over them for twenty minutes. Over-ear ANC designs that "
    "sound great on synths can make voices sound like they are speaking "
    "through a cardboard tube, and that fatigue shows up fast in real use."
)
_ADVICE_2 = (
    "Run the sweep test first: any ANC set worth its price should let you "
    "A/B the isolation with the music muted, and you should hear the room "
    "simply get quieter, not hiss up. If the hiss rises when you tap the "
    "side panel, the mic array is doing the work, and that is usually where "
    "budget sets save their money.\n\n"
    "Then compare battery behaviour over a week, not a spec sheet: quick "
    "charge, multipoint behaviour and the auto-off timer matter more day to "
    "day than an extra three hours of rated runtime."
)
_MENTION_2 = (
    "The comparison pages on the [all-headphones collection]"
    "(https://example.com/collections/all-headphones) at Aurora Audio break "
    "down exactly this trade-off per model."
)
_ARTICLE_3 = (
    "Audiophile culture loves to skip from sealed headphones to open-back "
    "planars, but the step most buyers actually need is a well-tuned ANC "
    "set that behaves across commute, desk and aircraft cabin. This guide "
    "picks the tuning targets that matter: vocal presence that survives "
    "long podcasts, bass that stays honest when isolation is switched "
    "on, and comfort that does not depend on a clamp spec.\n\n"
    "Start with the driver-and-seal relationship. A large driver cannot buy "
    "back what a bad pad fit leaks; the earcup geometry decides whether the "
    "low end has anything to push against. Listen for kick-drum attack with "
    "the isolation engaged: a correct seal keeps the transient tight, while "
    "a leaky one smears it into a low hum.\n\n"
    "ANC implementation divides the field sharper than price does. "
    "Feed-forward mics handle airplane hiss gracefully but struggle with "
    "wind; hybrid designs adapt better but demand better gain staging. "
    "Whatever the stack, listen for the moment the noise floor drops: you "
    "should hear silence arrive, not a new noise take over.\n\n"
    "Finally, weigh the practical kit: a fast-charge habit, a case that "
    "does not flex, replaceable pads and a warranty that treats a hinge as "
    "a part rather than a write-off. The budget ANC sets that survive a "
    "year are the ones whose maker planned for wear."
)
_MENTION_3 = (
    "For concrete pairings, Aurora Audio's [budget ANC comparison]"
    "(https://example.com/collections/budget-noise-cancelling) maps each "
    "tuning target above to a model that meets it."
)


def _site_exists(conn, site_id):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM site_config WHERE site_id = %s", (site_id,))
        return cur.fetchone() is not None


def _brand(conn, site_id):
    with conn.cursor() as cur:
        cur.execute("SELECT site_name FROM site_config WHERE site_id = %s",
                    (site_id,))
        row = cur.fetchone()
    return (row[0].replace(" (mock)", "") if row and row[0] else "The brand")


def _clear(conn, site_id):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM offpage_drafts d USING recommendations r, "
            "offpage_threads t, offpage_communities c "
            "WHERE d.recommendation_id = r.recommendation_id "
            "AND d.site_id = r.site_id "
            "AND r.offpage_thread_id = t.thread_id "
            "AND t.community_id = c.community_id "
            "AND c.community_key LIKE %s",
            (_DEMO_KEY_PREFIX + "%",))
        cur.execute(
            "DELETE FROM recommendations r USING offpage_threads t, "
            "offpage_communities c WHERE r.offpage_thread_id = t.thread_id "
            "AND t.community_id = c.community_id "
            "AND c.community_key LIKE %s",
            (_DEMO_KEY_PREFIX + "%",))
        cur.execute(
            "DELETE FROM offpage_account_communities ac USING "
            "offpage_accounts a, offpage_communities c "
            "WHERE ac.account_id = a.account_id "
            "AND ac.community_id = c.community_id "
            "AND c.community_key LIKE %s",
            (_DEMO_KEY_PREFIX + "%",))
        # Demo accounts carry the constant marker brand_name; the earlier
        # community-key join never matched it, orphaning handles and making
        # every re-seed violate offpage_accounts_site_id_platform_handle_key.
        cur.execute(
            "DELETE FROM offpage_account_communities ac USING "
            "offpage_accounts a WHERE ac.account_id = a.account_id "
            "AND a.brand_name = %s",
            (_DEMO_KEY_PREFIX + ":brand",))
        cur.execute(
            "DELETE FROM offpage_accounts WHERE brand_name = %s",
            (_DEMO_KEY_PREFIX + ":brand",))
        cur.execute(
            "DELETE FROM offpage_threads t USING offpage_communities c "
            "WHERE t.community_id = c.community_id "
            "AND c.community_key LIKE %s",
            (_DEMO_KEY_PREFIX + "%",))
        cur.execute(
            "DELETE FROM offpage_communities WHERE community_key LIKE %s",
            (_DEMO_KEY_PREFIX + "%",))
    conn.commit()


def _seed_card(conn, site_id, brand_name, card):
    platform = card["platform"]
    key = f"{_DEMO_KEY_PREFIX}-{platform}-{card['tag']}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO offpage_communities (site_id, platform, "
            "community_key, link_policy, allows_self_promo, "
            "ai_content_allowed, enabled, promotional_cap_30d, strike_count, "
            "data_source) VALUES (%s, %s, %s, 'allowed', true, true, true, "
            "3, 0, 'import') RETURNING community_id",
            (site_id, platform, key))
        community_id = cur.fetchone()[0]
        thread_url = (f"https://www.{platform}.com/{key}/comments/"
                      f"demo{card['tag']}/thread")
        cur.execute(
            "INSERT INTO offpage_threads (site_id, community_id, thread_url, "
            "title, snippet, context_source, context_chars, "
            "promotion_allowed, post_kind, discovery_query, data_source) "
            "VALUES (%s, %s, %s, %s, %s, 'operator_import', %s, true, %s, "
            "'demo-seed', 'import') RETURNING thread_id",
            (site_id, community_id, thread_url, card["title"],
             card["snippet"], len(card["snippet"]), card["post_kind"]))
        thread_id = cur.fetchone()[0]
        if card.get("account_status"):
            cur.execute(
                "INSERT INTO offpage_accounts (site_id, platform, handle, "
                "brand_name, status, promotional_cap_30d) "
                "VALUES (%s, %s, %s, %s, %s, 3) RETURNING account_id",
                (site_id, platform, f"u/aurora-demo-{card['tag']}",
                 _DEMO_KEY_PREFIX + ":brand", card["account_status"]))
            account_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO offpage_account_communities (site_id, "
                "account_id, community_id, standing, assessed_at) "
                "VALUES (%s, %s, %s, 'established', now())",
                (site_id, account_id, community_id))
        cur.execute(
            "INSERT INTO recommendations (site_id, generator, action_type, "
            "status, diagnosis, target_url, offpage_thread_id) "
            "VALUES (%s, 'offpage_community', 'offpage_reply', 'raw', %s, %s, "
            "%s) RETURNING recommendation_id",
            (site_id, f"[UI DEMO] Off-page opportunity ({platform})",
             thread_url, thread_id))
        rec_id = cur.fetchone()[0]
        body = card["advice"] if not card.get("mention") else \
            card["advice"] + "\n\n" + card["mention"]
        gates = list(card["gates"]) + ["medium_longform_grounded"] \
            if card["post_kind"] == "article" \
            else list(card["gates"])
        validation = {"gates": [{"gate": g, "outcome": "pass", "reason": None}
                                for g in gates], "refused": []}
        grounding = {"post_type": card["post_type"], "brand_name": brand_name}
        cur.execute(
            "INSERT INTO offpage_drafts (site_id, recommendation_id, version, "
            "body_md, advice_md, mention_md, generation_source, validation, "
            "grounding, helpfulness_score, promo_density, "
            "brand_mention_count, link_count, status, created_at) "
            "VALUES (%s, %s, 1, %s, %s, %s, 'deterministic', %s, %s, "
            "%s, %s, %s, %s, 'drafted', now() - (%s || ' hours')::interval) "
            "RETURNING draft_id",
            (site_id, rec_id, body, card["advice"],
             card.get("mention") or "",
             json.dumps(validation), json.dumps(grounding),
             card["helpfulness"], card["promo_density"],
             1 if card.get("mention") else 0,
             1 if card.get("mention") else 0, str(card["age_hours"])))
        return str(cur.fetchone()[0])


@router.get("/offpage-demo")
def offpage_demo(site_id, op: str = "seed"):
    """Seed (op=seed) or remove (op=clear) the UI-preview demo cards."""
    if op not in ("seed", "clear", "clears"):
        raise HTTPException(status_code=422, detail="op must be seed|clear")
    conn = database.get_connection()
    try:
        if not _site_exists(conn, site_id):
            raise HTTPException(status_code=404, detail="unknown site_id")
        brand = _brand(conn, site_id)
        _clear(conn, site_id)
        if op == "clear":
            conn.commit()
            return {"ok": True, "site_id": site_id, "cleared": True}
        cards = [
            {"tag": "a", "platform": "reddit", "post_kind": "reply",
             "post_type": "helpful",
             "title": "[UI DEMO] Best way to break in budget ANC headphones?",
             "snippet": ("Just picked up my first active noise cancelling "
                         "pair and left ear sounds slightly duller than the "
                         "right. Is this normal out of the box, and is "
                         "there anything like a break-in routine that "
                         "actually helps? Also curious how people judge "
                         "seal quality on over-ear pads at home."),
             "advice": _ADVICE_1, "mention": None, "account_status": "aging",
             "gates": ("channel_enabled", "ai_content_allowed",
                       "context_sufficient", "grounding_corpus",
                       "account_standing"),
             "helpfulness": "0.92", "promo_density": "0.00",
             "age_hours": 3},
            {"tag": "b", "platform": "reddit", "post_kind": "reply",
             "post_type": "promotional",
             "title": "[UI DEMO] How do you A/B test ANC quality in-store?",
             "snippet": ("Store demos never tell the whole story - the "
                         "background music drowns the noise floor. What "
                         "do you listen for between two pairs when both "
                         "spec sheets look the same, and is there a quick "
                         "sweep or track that instantly reveals weak "
                         "isolation or mic hiss?"),
             "advice": _ADVICE_2, "mention": _MENTION_2,
             "account_status": "active",
             "gates": ("channel_enabled", "ai_content_allowed",
                       "context_sufficient", "grounding_corpus",
                       "account_standing", "community_allows_promotion",
                       "thread_allows_promotion"),
             "helpfulness": "0.82", "promo_density": "0.14",
             "age_hours": 27},
            {"tag": "c", "platform": "medium", "post_kind": "article",
             "post_type": "promotional",
             "title": "[UI DEMO] What actually makes budget ANC headphones "
                      "worth keeping a year later",
             "snippet": ("An editorial long-form guide: tuning targets that "
                         "matter on noise cancelling designs, how ANC "
                         "implementation divides the field, and the "
                         "practical kit decisions that separate sets "
                         "people keep from sets people re-list."),
             "advice": _ARTICLE_3, "mention": _MENTION_3,
             "account_status": "active",
             "gates": ("channel_enabled", "ai_content_allowed",
                       "context_sufficient", "grounding_corpus",
                       "account_standing"),
             "helpfulness": "0.75", "promo_density": "0.10",
             "age_hours": 51},
        ]
        seeded = [_seed_card(conn, site_id, brand, card) for card in cards]
        conn.commit()
        return {"ok": True, "site_id": site_id, "seeded_drafts": seeded}
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
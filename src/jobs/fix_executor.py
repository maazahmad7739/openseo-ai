"""Fix executor job (plan/21 §3 + §5): queued -> applied, the ONLY writer.

Contract (unchanged from the rehearsal reference implementation, now generic):
  * Advisory-locked: global fix_executor key + per-site key (jobs/locks.py);
    two executors can never run the same site simultaneously.
  * Fail-closed policy gate: fix_policy row must exist, be enabled, and sit
    under its weekly cap at PICKUP (re-checked here, not just at queue time).
  * Fresh-read snapshot at pickup: snapshot_json is captured from a live
    read NOW — generation-time old_values are never trusted. Live value
    != diff old_value -> auto-expire (stale-diff protection, verify_failed).
  * payload schema-validated per sub_type BEFORE any network call.
  * queued -> applied ONLY here (never inline in an API request).
  * On applied: same measurement wiring as /implement
    (_transition_to_in_progress + baseline freeze; implemented_at =
    execution time). The clock's due condition is unchanged.
  * Read-back verification: verified / verify_failed. verify_failed ->
    auto-revert from the fresh snapshot (auto-revert on failed read-back
    verification ONLY).
  * change_log rollback_reference = fix_id; revert rows carry rollback_of.

Usage:
    python -m jobs.fix_executor [--site_id ...] [--dry-run] [--limit N]
    [--reference_date YYYY-MM-DD]
"""

import os
import sys
import json
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as database
import env as env_loader


def _normalize_shop_domain(domain):
    """site_config may store the bare handle ('my-store'); GraphQL needs the
    myshopify.com FQDN. The sync path tolerates both (REST base formats with
    .format()), so the executor accepts both too."""
    if not domain:
        return domain
    if "." in domain:
        return domain
    return f"{domain}.myshopify.com"


def _resolve_shop_config(conn, site_id, api_version=None):
    """Per-site Shopify GraphQL credential (site_config + env; mode-layer rules).

    The executor is the ONLY writer: it refuses to run live without
    credentials (fail-closed), and honors INTEGRATION_MODE for dry runs in
    CI/mock environments.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT shopify_domain FROM site_config WHERE site_id = %s", (site_id,))
        row = cur.fetchone()
    shop_domain = _normalize_shop_domain(
        (row[0] if row else None) or os.environ.get("SHOPIFY_DOMAIN"))
    access_token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
    if api_version is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT config_value FROM system_config WHERE config_key = 'shopify.api_version'"
            )
            vrow = cur.fetchone()
        api_version = (vrow[0] if vrow else None) or "2026-01"
    return {"shop_domain": shop_domain, "access_token": access_token,
            "api_version": api_version}


def _fix_scope_probes(conn, site_id, sub_type):
    """Startup diagnostics (plan/21 §4): granted-vs-required scopes."""
    from connectors.shopify import (ShopifyGraphQLClient,
                                    required_scopes_for_sub_types,
                                    granted_covers_required)

    def probe(client, required):
        granted = client.probe_scopes()
        if not granted.get("ok"):
            return {"ok": False, "error": granted.get("error"),
                    "detail": granted.get("detail")}
        missing, unsatisfied_groups = granted_covers_required(
            granted.get("scopes") or [], required)
        return {"ok": True, "granted": granted.get("scopes") or [],
                "missing": missing, "unsatisfied_any_of": unsatisfied_groups}

    required = required_scopes_for_sub_types([sub_type])
    if not required:
        # Unknown sub_type: the scope mapping cannot advise — say so loudly
        # instead of silently passing the gate.
        return {"ok": False, "error": "unknown_sub_type",
                "required": [], "detail": f"no scope mapping for {sub_type!r}"}
    cfg = _resolve_shop_config(conn, site_id)
    if not cfg.get("shop_domain") or not cfg.get("access_token"):
        return {"ok": False, "error": "no_credentials", "required": required}
    client = ShopifyGraphQLClient(shop_domain=cfg["shop_domain"],
                                  access_token=cfg["access_token"],
                                  api_version=cfg["api_version"])
    out = probe(client, required)
    out["required"] = required
    return out


def _execute_one(conn, fix_row, config, dry_run=False):
    """Execute one queued fix end-to-end. Returns a typed result dict.

    Steps (plan/21 §5.3, mirroring rehearsal step 3 + step 5):
      pickup policy gate -> fresh-read snapshot -> stale-diff guard ->
      write (unless dry_run) -> read-back verify -> change_log row ->
      measurement wiring (same logic as /implement).
    """
    from fixes import adapters as fix_adapters
    from fixes.policy import PolicyBlocked, check_execution_allowed

    fix_id = fix_row["fix_id"]
    site_id = fix_row["site_id"]
    sub_type = fix_row["sub_type"]

    # --- policy gate AT PICKUP (caps can be consumed between queue and run;
    # also the cheaper check — a disabled sub_type skips before any read) ---
    try:
        policy = check_execution_allowed(conn, site_id, sub_type,
                                         fix_row.get("reference_date"))
    except PolicyBlocked as exc:
        return {"fix_id": str(fix_id), "status": "skipped", "reason": exc.reason,
                "detail": exc.detail}

    # Adapter resolution inside the typed skip path: an unknown sub_type in
    # a queued row is a data anomaly (skip + mark failed), never a crash
    # that poisons the rest of the site's run.
    try:
        adapter = fix_adapters.get_adapter(sub_type)
    except fix_adapters.WriteAdapterError as exc:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE generated_fixes SET status = 'failed', "
                "error_detail = %s WHERE fix_id = %s AND status = 'queued'",
                (f"no write adapter for sub_type {sub_type!r}", fix_id),
            )
        conn.commit()
        return {"fix_id": str(fix_id), "status": "failed",
                "reason": "no_adapter", "detail": str(exc)}

    # --- fresh-read snapshot (NEVER trusted from generation) ---
    snap = adapter_snapshot(adapter, conn, fix_row, config)
    if not snap.get("ok"):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE generated_fixes SET status = 'failed', "
                "error_detail = %s WHERE fix_id = %s AND status = 'queued'",
                (f"snapshot_failed: {snap.get('outcome')}", fix_id),
            )
        conn.commit()
        return {"fix_id": str(fix_id), "status": "failed",
                "reason": "snapshot_failed", "detail": snap.get("detail")}
    snapshot = snap["snapshot"]
    if not dry_run:
        # Dry-run writes NOTHING anywhere (plan/21 §5.4) — the fresh read
        # only feeds the in-memory stale-diff guard; the persisted snapshot
        # is taken on real runs.
        with conn.cursor() as cur:
            cur.execute("UPDATE generated_fixes SET snapshot_json = %s::jsonb "
                        "WHERE fix_id = %s",
                        (json.dumps(snapshot, default=str), fix_id))
        conn.commit()

    # --- stale-diff guard: live value must equal the generation-time old_value
    diff = fix_row.get("diff_json") or []
    if isinstance(diff, str):
        try:
            diff = json.loads(diff)
        except (TypeError, ValueError):
            diff = []
    for entry in diff if isinstance(diff, list) else []:
        field = entry.get("field")
        old_value = entry.get("old_value")
        live_value = snapshot.get(field)
        if live_value is not None and old_value is not None and live_value != old_value:
            detail = (f"stale diff: live {field}={live_value!r} != "
                      f"generation-time old_value={old_value!r} — fix expired, "
                      "will regenerate next cycle")
            _expire_stale(conn, fix_id, detail)
            return {"fix_id": str(fix_id), "status": "expired",
                    "reason": "stale_diff", "detail": detail}

    # --- execute (adapter validates payload before any network call) ---
    # The adapters read the FRESH pickup snapshot (sibling seo fields ride on
    # it — productUpdate.seo replaces the whole object): pass the in-memory
    # snapshot on the claimed row dict so they never read a stale value.
    fix_row["snapshot_json"] = snapshot
    result = adapter_execute(adapter, conn, fix_row, config, dry_run=dry_run)
    if dry_run or result.get("outcome") == "dry_run":
        return {"fix_id": str(fix_id), "status": "dry_run",
                "detail": result.get("detail")}

    executed_ok = bool(result.get("ok"))
    verification_status = result.get("verification_status") if executed_ok else None

    # Adapter-provided snapshot patch (e.g. the redirect adapter records the
    # created redirect id as the rollback source): merged into the persisted
    # snapshot BEFORE any failure path can need a revert.
    patch = result.get("snapshot_patch")
    if executed_ok and isinstance(patch, dict) and patch:
        try:
            snapshot = dict(snapshot or {})
            snapshot.update(patch)
            with conn.cursor() as cur:
                cur.execute("UPDATE generated_fixes SET snapshot_json = %s::jsonb "
                            "WHERE fix_id = %s",
                            (json.dumps(snapshot, default=str), fix_id))
            conn.commit()
        except Exception as exc:
            # Rollback-source persistence is best-effort at this point (the
            # write already succeeded); a miss is logged loudly so the
            # revert path can refuse rather than guess.
            print(f"[fix_executor] snapshot patch failed for {fix_id}: {exc}",
                  flush=True)

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE generated_fixes
            SET executed_at = now(),
                applied_at = CASE WHEN %s THEN now() ELSE NULL END,
                adapter_response = %s::jsonb,
                status = CASE WHEN %s THEN 'applied' ELSE 'failed' END,
                verification_status = %s,
                error_detail = CASE WHEN %s THEN NULL ELSE %s END
            WHERE fix_id = %s AND status = 'queued'
            """,
            (executed_ok,
             json.dumps(result.get("adapter_response") or result, default=str),
             executed_ok, verification_status, executed_ok,
             _error_text(result) if not executed_ok else None,
             fix_id),
        )
    conn.commit()

    if not executed_ok:
        # Scope-miss = typed skip -> failed + 3-strike disable (plan/21 §0).
        if result.get("outcome") == "access_denied":
            _record_scope_strike(conn, site_id, sub_type)
        return {"fix_id": str(fix_id), "status": "failed",
                "outcome": result.get("outcome"),
                "detail": result.get("detail")}

    # --- change_log audit (rollback_reference finally populated) ---
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO change_log (recommendation_id, url, before_snapshot,
                                        after_snapshot, implementation_date,
                                        implemented_by, rollback_reference)
                VALUES (%s, %s, %s::jsonb, %s::jsonb, now()::date, 'fix_executor', %s)
                """,
                (fix_row["recommendation_id"], fix_row["target_url"],
                 json.dumps({"snapshot": snapshot}),
                 json.dumps({"verified": verification_status == "verified",
                             "live_value": result.get("live_value")}),
                 str(fix_id)),
            )
    except Exception as exc:
        # Best-effort audit (same pattern as routes/measurements implement).
        print(f"[fix_executor] change_log write skipped for {fix_id}: {exc}",
              flush=True)
    conn.commit()

    # --- measurement wiring: same logic as /implement (plan/21 §3) ---
    if verification_status == "verified":
        _wire_measurement(conn, fix_row)
        followup = _mint_unpublish_followup(conn, fix_row, snapshot,
                                            generation_source="deterministic")
    else:
        # Auto-revert on failed read-back verification ONLY. The snapshot
        # stored moments ago is the restore source — re-read it from the DB
        # (the claimed row dict predates the snapshot UPDATE).
        with conn.cursor() as cur:
            cur.execute("SELECT snapshot_json FROM generated_fixes WHERE fix_id = %s",
                        (fix_id,))
            srow = cur.fetchone()
        if srow and srow[0] is not None:
            fix_row["snapshot_json"] = srow[0]
        revert = revert_fix(conn, fix_row, config, reason="verify_failed")
        return {"fix_id": str(fix_id), "status": "reverted",
                "reason": "verify_failed", "revert": revert}
    out = {"fix_id": str(fix_id), "status": "applied",
           "verification": verification_status}
    if followup is not None:
        out["followup"] = followup
    return out


# ------------------------------------------------------------
# Unpublish-after-redirect sequencing (Phase 4 item 3, plan/21 §2.5):
# a consolidate 301 that is APPLIED + VERIFIED live mints a follow-up
# unpublish fix row for the retired SOURCE page. The sequencing contract
# is structural: the follow-up row CANNOT exist until the redirect
# adapter returned verified=True (minted only in this branch, never in
# the failure/revert paths), and it enters as 'generated' — the operator's
# diff approval (gate 2) still applies before it can ever be queued.
# ------------------------------------------------------------

UNPUBLISH_SUB_TYPES = ("product_unpublish", "collection_unpublish")


def _mint_unpublish_followup(conn, fix_row, snapshot,
                             generation_source="deterministic"):
    """Mint the sequenced unpublish follow-up row for a verified redirect.

    Returns a typed dict for the executor summary (or None when no follow-up
    applies — never raises: sequencing is best-effort at mint time, the
    unpublish row then runs through the FULL normal lifecycle: policy gate,
    fresh snapshot, diff approval, executor pickup, revert semantics).

    Entity resolution (data -> home, never guessed):
      * the redirect SOURCE page row (pages) supplies page_type + shopify_gid;
      * product -> product_unpublish (productUpdate status ARCHIVED);
      * collection -> collection_unpublish (publishableUnpublish) with the
        publication id from system_config (resolved by earlier probes; a None
        publication id means generation cannot mint — logged, skipped);
      * a page row without a GID, or an unknown page_type, skips (typed).
    """
    if fix_row.get("sub_type") != "redirect":
        return None
    source_url = fix_row["target_url"]
    site_id = fix_row["site_id"]
    rec_id = fix_row["recommendation_id"]

    # Idempotency: never mint twice for the same redirect fix.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM generated_fixes WHERE rollback_of = %s AND "
            "sub_type IN %s LIMIT 1", (fix_row["fix_id"], UNPUBLISH_SUB_TYPES))
        if cur.fetchone():
            return None
        cur.execute(
            "SELECT page_type, shopify_gid FROM pages "
            "WHERE site_id = %s AND url = %s", (site_id, source_url))
        page = cur.fetchone()
    if not page:
        print(f"[fix_executor] unpublish follow-up skipped: source page not "
              f"in pages ({source_url})", flush=True)
        return None
    page_type, gid = page[0], page[1]
    if not gid:
        print(f"[fix_executor] unpublish follow-up skipped: source page has "
              f"no shopify_gid ({source_url})", flush=True)
        return None

    from fixes import adapters as fix_adapters
    from connectors.shopify import resolve_publication_id, ShopifyGraphQLClient
    import os

    if page_type == "product":
        sub_type = "product_unpublish"
        payload = fix_adapters.build_product_unpublish_payload(gid)
        diff = [{"field": "product.status",
                 "old_value": (snapshot or {}).get("product.status") or "ACTIVE",
                 "new_value": "ARCHIVED"}]
        entity_ref = gid
    elif page_type == "collection":
        sub_type = "collection_unpublish"
        shop_domain = (fix_row.get("shop_domain")
                       or os.environ.get("SHOPIFY_DOMAIN"))
        token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
        publication_id = None
        if shop_domain and token:
            client = ShopifyGraphQLClient(shop_domain=shop_domain,
                                          access_token=token)
            publication_id = resolve_publication_id(conn, client)
        if not publication_id:
            print(f"[fix_executor] unpublish follow-up skipped: no publication "
                  f"id for collection {source_url}", flush=True)
            return None
        payload = fix_adapters.build_collection_unpublish_payload(gid, publication_id)
        diff = [{"field": "publishable.published_on_publication",
                 "old_value": (snapshot or {}).get("publishable.published_on_publication"),
                 "new_value": False}]
        entity_ref = gid
    else:
        print(f"[fix_executor] unpublish follow-up skipped: unsupported "
              f"page_type {page_type!r} ({source_url})", flush=True)
        return None

    payload["grounding"] = {
        "route": "unpublish_after_redirect",
        "redirect_fix_id": str(fix_row["fix_id"]),
        "redirect_verified": True,
        "source_url": source_url,
    }
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO generated_fixes
                    (recommendation_id, site_id, action_type, sub_type, target_url,
                     target_entity_ref, payload_json, diff_json, generation_source,
                     status, risk_tier, rollback_of)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s, 'generated', 'medium', %s)
                ON CONFLICT (site_id, target_url, COALESCE(sub_type, ''))
                    WHERE status IN ('generated','approved','queued') DO NOTHING
                RETURNING fix_id, status
                """,
                (rec_id, site_id, fix_row["action_type"], sub_type, source_url,
                 gid, json.dumps(payload), json.dumps(diff), generation_source,
                 fix_row["fix_id"]),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"[fix_executor] unpublish follow-up mint failed for "
              f"{fix_row['fix_id']}: {exc}", flush=True)
        return None
    if not row:
        # Conflict on the partial unique index: an unpublish row already
        # owns this (site, source url) — nothing to mint.
        return None
    print(f"[fix_executor] unpublish follow-up minted: {row[0]} ({sub_type} "
          f"for {source_url}; redirect {fix_row['fix_id']} verified)",
          flush=True)
    return {"fix_id": str(row[0]), "sub_type": sub_type, "status": row[1]}


def adapter_execute(adapter, conn, fix_row, config, dry_run=False):
    try:
        return adapter["execute"](conn, fix_row, config, dry_run=dry_run)
    except Exception as exc:  # never-raise adapter contract
        return {"ok": False, "outcome": "adapter_exception",
                "detail": f"{type(exc).__name__}: {exc}"}


def adapter_snapshot(adapter, conn, fix_row, config):
    try:
        return adapter["snapshot"](conn, fix_row, config)
    except Exception as exc:
        return {"ok": False, "outcome": "adapter_exception",
                "detail": f"{type(exc).__name__}: {exc}"}


def _error_text(result):
    parts = []
    if result.get("userErrors"):
        parts.append(json.dumps(result["userErrors"])[:300])
    if result.get("errors"):
        parts.append(json.dumps(result["errors"])[:300])
    if result.get("detail"):
        parts.append(str(result["detail"])[:300])
    return "; ".join(parts) or result.get("outcome") or "unknown"


def _expire_stale(conn, fix_id, detail):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE generated_fixes SET status = 'expired', "
            "verification_status = 'verify_failed', error_detail = %s "
            "WHERE fix_id = %s AND status = 'queued'",
            (detail, fix_id),
        )
    conn.commit()


def _record_scope_strike(conn, site_id, sub_type):
    """3-strike auto-disable: ACCESS_DENIED on a sub_type bumps a counter in
    system_config (fix.scopemiss.<site>.<sub_type>); the third strike flips
    fix_policy.enabled = false (kill-switch, fail-closed)."""
    key = f"fix.scopemiss.{site_id}.{sub_type}"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT config_value FROM system_config WHERE config_key = %s", (key,))
            row = cur.fetchone()
            strikes = int(row[0]) if row else 0
            strikes += 1
            if row:
                cur.execute("UPDATE system_config SET config_value = %s "
                            "WHERE config_key = %s", (str(strikes), key))
            else:
                cur.execute("INSERT INTO system_config (config_key, config_value) "
                            "VALUES (%s, %s)", (key, str(strikes)))
            if strikes >= 3:
                cur.execute(
                    "UPDATE fix_policy SET enabled = false "
                    "WHERE site_id = %s AND sub_type = %s", (site_id, sub_type))
        conn.commit()
        if strikes >= 3:
            print(f"[fix_executor] 3-strike scope disable: {sub_type} on {site_id}",
                  flush=True)
    except Exception as exc:
        conn.rollback()
        print(f"[fix_executor] scope-strike recording failed: {exc}", flush=True)


def _wire_measurement(conn, fix_row):
    """On applied: the SAME logic as POST /implement (plan/21 §3).

    _transition_to_in_progress (recomputed measurement_due_at, single
    statement) + baseline freeze. implemented_at = execution time; the
    clock's due condition stays identical to today.
    """
    from api.routes.measurements import _transition_to_in_progress
    from measurement.baseline import store_baseline

    rec_id = fix_row["recommendation_id"]
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM recommendations WHERE recommendation_id = %s",
                    (rec_id,))
        row = cur.fetchone()
    if not row:
        print(f"[fix_executor] recommendation {rec_id} vanished — measurement "
              "wiring skipped", flush=True)
        return
    if row[0] != "approved":
        # Already implemented/in_progress (operator path) — nothing to wire.
        return
    implemented_at = date.today()
    with conn.cursor() as cur:
        _transition_to_in_progress(cur, rec_id, implemented_at, "fix_executor")
    try:
        store_baseline(conn, rec_id, implemented_at)
    except Exception as exc:
        # Baseline is best-effort (implement-safe pattern): the failed
        # baseline transaction is rolled back (which also undoes the
        # transition inside it), then the transition is re-applied alone
        # and committed below. Degraded measurement data never blocks a
        # write.
        conn.rollback()
        with conn.cursor() as cur:
            _transition_to_in_progress(cur, rec_id, implemented_at, "fix_executor")
        print(f"[fix_executor] baseline degraded for {rec_id}: "
              f"{type(exc).__name__}: {exc}", flush=True)
    conn.commit()


# ------------------------------------------------------------
# Revert (plan/21 §5.3): adapter + snapshot -> new audited row
# ------------------------------------------------------------

def _active_unpublish_followup(conn, redirect_fix_id):
    """The sequenced unpublish row for this redirect fix, if still active.

    Only 'applied' rows block: generated/approved/queued follow-ups are
    expired here (the redirect is being undone — retiring its source would
    orphan the entity); applied rows return for an adapter-level undo.
    Returns (row_dict, undo_required) or (None, False).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fix_id, recommendation_id, site_id, action_type, sub_type,
                   target_url, target_entity_ref, payload_json, diff_json,
                   risk_tier, snapshot_json, status
            FROM generated_fixes
            WHERE rollback_of = %s AND sub_type IN %s
              AND status IN ('applied', 'generated', 'approved', 'queued')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (redirect_fix_id, UNPUBLISH_SUB_TYPES),
        )
        columns = [d[0] for d in cur.description]
        row = cur.fetchone()
    if not row:
        return None, False
    followup = dict(zip(columns, row))
    if followup["status"] == "applied":
        return followup, True
    # Not yet executed: expire it (the redirect is being undone — the
    # sequenced unpublish must never run after that).
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE generated_fixes SET status = 'expired', error_detail = %s "
            "WHERE fix_id = %s AND status IN ('generated','approved','queued')",
            (f"expired: redirect {redirect_fix_id} was reverted before this "
             "sequenced unpublish ran", followup["fix_id"]),
        )
    conn.commit()
    return followup, False


def revert_unpublish_before_redirect(conn, redirect_row, config):
    """Rollback lineage (plan/21 §2.5 sequencing): undo the sequenced
    unpublish BEFORE the redirect is deleted.

    Strict reverse order — undoing the redirect first would leave the source
    page unpublished with NO redirect live (worst intermediate state). The
    unpublish row's own snapshot restores the entity to its pickup state
    (product ACTIVE / collection republished) via its own audited revert.

    Returns a typed summary for the API response (None when no active
    follow-up exists). Failures are typed, never raised: a failed unpublish
    undo is reported and the redirect revert still proceeds (the operator
    sees both outcomes).
    """
    if redirect_row.get("sub_type") != "redirect":
        return None
    followup, undo_required = _active_unpublish_followup(
        conn, redirect_row["fix_id"])
    if followup is None:
        return None
    if not undo_required:
        return {"skipped": "not_yet_applied",
                "followup_fix_id": str(followup["fix_id"]),
                "action": "expired"}
    result = revert_fix(conn, followup, config,
                        reason="redirect_revert_sequencing")
    return {"followup_fix_id": str(followup["fix_id"]),
            "sub_type": followup["sub_type"],
            "action": "reverted", "result": result}


def revert_fix(conn, fix_row, config, reason="operator"):
    """Restore a fix from its fresh-read snapshot; record an audited revert row.

    Creates a NEW generated_fixes row with rollback_of = original fix_id
    (reverts are audited rows, never a silent overwrite).
    """
    from fixes import adapters as fix_adapters

    try:
        adapter = fix_adapters.get_adapter(fix_row["sub_type"])
        result = adapter["restore"](conn, fix_row, config)
    except Exception as exc:
        result = {"ok": False, "outcome": "adapter_exception",
                  "detail": f"{type(exc).__name__}: {exc}"}

    with conn.cursor() as cur:
        if result.get("ok"):
            cur.execute(
                "UPDATE generated_fixes SET status = 'reverted', reverted_at = now(), "
                "verification_status = 'verified' WHERE fix_id = %s",
                (fix_row["fix_id"],),
            )
        else:
            cur.execute(
                "UPDATE generated_fixes SET error_detail = %s "
                "WHERE fix_id = %s",
                (f"revert failed: {result.get('outcome')} "
                 f"{(result.get('detail') or '')[:200]}", fix_row["fix_id"]),
            )
    conn.commit()

    if not result.get("ok"):
        return {"reverted": False, "reason": result.get("outcome"),
                "detail": result.get("detail")}

    # Audited revert row (rollback_of chain; snapshot cleared — the restore
    # source must never be reused).
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO generated_fixes
                    (recommendation_id, site_id, action_type, sub_type, target_url,
                     target_entity_ref, payload_json, diff_json, generation_source,
                     status, risk_tier, rollback_of, executed_at, applied_at,
                     adapter_response, verification_status)
                SELECT recommendation_id, site_id, action_type, sub_type, target_url,
                       target_entity_ref, payload_json, diff_json, 'deterministic',
                       'applied', risk_tier, fix_id, now(), now(),
                       %s::jsonb, 'verified'
                FROM generated_fixes WHERE fix_id = %s
                """,
                (json.dumps({"revert_of": str(fix_row["fix_id"]),
                             "reason": reason, "ok": True}),
                 fix_row["fix_id"]),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"[fix_executor] revert audit row failed for {fix_row['fix_id']}: {exc}",
              flush=True)
    return {"reverted": True, "restored": result.get("live_value"),
            "reason": reason}


# ------------------------------------------------------------
# Job entry points
# ------------------------------------------------------------

def _claim_queued(conn, site_id, limit):
    """Queued fixes for one site, highest risk tier first (deterministic order).

    The partial unique index guarantees at most one active fix per
    (site, target_url, field); the executor additionally enforces one fix
    per (site, url) per run so a single run can never double-write a page.

    rollback_of IS NULL excludes revert-audit rows (never executable), but
    the sequenced unpublish rows (Phase 4 item 3) carry rollback_of = their
    redirect fix and ARE executable — allowed explicitly by sub_type.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fix_id, recommendation_id, site_id, action_type, sub_type,
                   target_url, target_entity_ref, payload_json, diff_json,
                   risk_tier, rollback_of, snapshot_json
            FROM generated_fixes
            WHERE site_id = %s AND status = 'queued'
              AND (rollback_of IS NULL OR sub_type IN %s)
            ORDER BY CASE risk_tier WHEN 'high' THEN 1 WHEN 'medium' THEN 2
                      WHEN 'low' THEN 3 ELSE 4 END, created_at
            LIMIT %s
            """,
            (site_id, UNPUBLISH_SUB_TYPES, limit),
        )
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    return rows


MAX_APPLIES_PER_RUN = 5  # plan/21 §5.1 per-run ceiling


def run_fix_executor(site_id, conn=None, dry_run=False, limit=MAX_APPLIES_PER_RUN,
                     reference_date=None, api_version=None, shop_config=None):
    """Run the executor for one site (caller holds the per-site advisory lock).

    shop_config: injected credential dict (tests/typed harnesses); when
    omitted the per-site Shopify credential is resolved from site_config + env.
    """
    from fixes.policy import check_execution_allowed, PolicyBlocked

    own_conn = conn is None
    if own_conn:
        env_loader.load_env_file(quiet=True)
        conn = database.get_connection()
    try:
        config = shop_config or _resolve_shop_config(conn, site_id,
                                                      api_version=api_version)
        if not dry_run and (not config.get("shop_domain")
                            or not config.get("access_token")):
            print("[fix_executor] no Shopify credential resolved — refusing to run "
                  "(fail-closed)", flush=True)
            return {"status": "no_credentials", "site_id": str(site_id)}

        # Startup diagnostics: scope introspection + version drift warning.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sub_type FROM generated_fixes WHERE site_id = %s "
                "AND status = 'queued' AND rollback_of IS NULL LIMIT 1", (site_id,))
            prow = cur.fetchone()
        if prow:
            scopes = _fix_scope_probes(conn, site_id, prow[0])
            if scopes.get("ok"):
                parts = []
                if scopes.get("missing"):
                    parts.append(f"missing scopes {scopes['missing']}")
                if scopes.get("unsatisfied_any_of"):
                    parts.append(f"unsatisfied any-of groups "
                                 f"{scopes['unsatisfied_any_of']}")
                if parts:
                    print(f"[fix_executor] WARNING: {'; '.join(parts)} "
                          f"(required: {scopes['required']})", flush=True)
            elif not scopes.get("ok") and os.environ.get("INTEGRATION_MODE") == "live":
                print(f"[fix_executor] scope probe failed: {scopes.get('error')}",
                      flush=True)

        fixes = _claim_queued(conn, site_id, limit)
        seen_urls = set()
        results = []
        applied = 0
        for fix in fixes:
            if str(fix["target_url"]) in seen_urls:
                # Strictly one fix per (site, url) per run (plan/21 §5.2).
                results.append({"fix_id": str(fix["fix_id"]), "status": "skipped",
                                "reason": "duplicate_target_in_run"})
                continue
            seen_urls.add(str(fix["target_url"]))
            if applied >= MAX_APPLIES_PER_RUN:
                results.append({"fix_id": str(fix["fix_id"]), "status": "deferred",
                                "reason": "run_ceiling"})
                continue
            fix["reference_date"] = reference_date
            out = _execute_one(conn, fix, config, dry_run=dry_run)
            results.append(out)
            if out.get("status") == "applied":
                applied += 1
        summary = {
            "site_id": str(site_id),
            "dry_run": bool(dry_run),
            "queued": len(fixes),
            "applied": sum(1 for r in results if r.get("status") == "applied"),
            "results": results,
        }
        return summary
    finally:
        if own_conn:
            conn.close()


def run(site_id=None, dry_run=False, limit=MAX_APPLIES_PER_RUN, reference_date=None):
    env_loader.load_env_file(quiet=True)
    from jobs.locks import job_lock, LOCK_KEYS, already_running
    from jobs.notify import send_summary
    from jobs.run_multi import run_across_sites

    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            if site_id:
                cur.execute("SELECT site_id FROM site_config WHERE site_id = %s", (site_id,))
            else:
                cur.execute("SELECT site_id FROM site_config")
            sites = [str(r[0]) for r in cur.fetchall()]

        def locked(sid):
            # run_multi contract: per-site lock on the worker's own connection.
            wconn = database.get_connection()
            try:
                with job_lock(wconn, LOCK_KEYS["fix_executor"], site_id=sid) as got:
                    if not got:
                        return already_running("fix_executor", sid)
                    return run_fix_executor(sid, conn=wconn, dry_run=dry_run,
                                            limit=limit, reference_date=reference_date)
            finally:
                wconn.close()

        out = run_across_sites(sites, locked)
        send_summary("fix_executor", out)
        return out
    finally:
        conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site_id", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="log payloads end-to-end, write nothing (plan/21 §5.4)")
    parser.add_argument("--limit", type=int, default=MAX_APPLIES_PER_RUN)
    parser.add_argument("--reference_date", default=None,
                        help="injectable clock (YYYY-MM-DD)")
    parser.add_argument("--api_version", default=None)
    args = parser.parse_args()
    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else None
    summary = run(site_id=args.site_id, dry_run=args.dry_run,
                  limit=args.limit, reference_date=reference_date)
    print("fix_executor:", json.dumps(summary, default=str, indent=1))


if __name__ == "__main__":
    main()
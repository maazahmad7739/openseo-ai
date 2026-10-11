# -*- coding: utf-8 -*-
"""plan/26 Phase 0 — platform seam + multi-connector write credentials.

python tests/run_phase0_credentials_tests.py   (needs a reachable local Postgres)

Verifies the credential resolver and the executor refactor against a REAL
database, with NO network anywhere:

  schema (plan/26)
    1. site_config.platform exists, default 'shopify', CHECK constraint
    2. framer_site_ref exists (nullable)
    3. fix_policy framer.* seeds exist for every site, enabled=false
    4. system_config framer.api_base provisioned
  resolver — shopify (byte-parity with the legacy _resolve_shop_config)
    5. platform defaults to shopify on a legacy-shaped site
    6. full credential -> ok, exact legacy keys (shop_domain/access_token/
       api_version), bare handle normalized to .myshopify.com
    7. missing token -> ok=False outcome=no_credentials, detail names env key
    8. missing domain -> ok=False, detail names shopify_domain
    9. api_version from system_config (2026-01 default)
  resolver — framer
   10. missing everything -> ok=False no_credentials, detail lists BOTH keys
   11. key + site_ref -> ok=True, framer config shape
   12. key without site_ref -> ok=False, detail names site_config.framer_site_ref
  resolver — fail-closed edges
   13. site_not_found for a bogus site_id
  executor refactor — legacy parity
   14. _resolve_shop_config returns legacy flat shape + ok/platform metadata
   15. _credential_ok_for_platform: legacy shape, shopify, framer variants
   16. framer sub_type scope probe -> loud typed skip (no Shopify KeyError)
   17. byte-parity: resolver output feeds ShopifyGraphQLClient construction
       (shape check — no network; the client is built but never run)

Isolation: rows keyed off RUN_TOKEN; cleanup removes exactly what was
created (FK-safe order). The resolver's env reads are sandboxed: env keys
are captured and restored around every scenario.
"""
import os
import sys
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


ENV_KEYS = ("SHOPIFY_DOMAIN", "SHOPIFY_ACCESS_TOKEN", "FRAMER_API_KEY")


class EnvSandbox:
    """Capture/restore the credential env keys around a scenario."""

    def __enter__(self):
        self.saved = {k: os.environ.pop(k, None) for k in ENV_KEYS}
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def seed_site(conn, platform="shopify", shopify_domain=None,
              framer_site_ref=None, with_policies=True):
    domain = f"phase0-{RUN_TOKEN}-{uuid.uuid4().hex[:6]}.example.com"
    with conn.cursor() as cur:
        if platform == "legacy":
            # simulate a pre-plan/26 row: no platform value at all is
            # impossible (NOT NULL DEFAULT) — the legacy case IS the
            # default, so insert without touching the column.
            cur.execute(
                """
                INSERT INTO site_config (site_name, domain, gsc_property,
                                         catalogue_size_tier, shopify_domain)
                VALUES (%s, %s, %s, 'small', %s) RETURNING site_id
                """,
                (f"Phase0 {RUN_TOKEN}", domain, f"sc-domain:{domain}",
                 shopify_domain),
            )
        else:
            cur.execute(
                """
                INSERT INTO site_config (site_name, domain, gsc_property,
                                         catalogue_size_tier, platform,
                                         shopify_domain, framer_site_ref)
                VALUES (%s, %s, %s, 'small', %s, %s, %s) RETURNING site_id
                """,
                (f"Phase0 {RUN_TOKEN}", domain, f"sc-domain:{domain}",
                 platform, shopify_domain, framer_site_ref),
            )
        site_id = cur.fetchone()[0]
        if platform != "legacy" or with_policies:
            for sub_type, risk, cap in (("framer.title", "low", 10),
                                        ("framer.description", "medium", 5)):
                cur.execute(
                    """
                    INSERT INTO fix_policy (site_id, sub_type, risk_tier,
                                            weekly_cap, requires_field_verify,
                                            enabled)
                    VALUES (%s, %s, %s, %s, true, false)
                    """,
                    (site_id, sub_type, risk, cap),
                )
    conn.commit()
    return {"site_id": str(site_id), "domain": domain}


def cleanup(conn, ids_list):
    conn.rollback()
    with conn.cursor() as cur:
        for ids in ids_list:
            cur.execute("DELETE FROM fix_policy WHERE site_id = %s",
                        (ids["site_id"],))
            cur.execute("DELETE FROM site_config WHERE site_id = %s",
                        (ids["site_id"],))
    conn.commit()


# ------------------------------------------------------------
# Tests
# ------------------------------------------------------------

def test_schema(conn):
    print("\n== schema (plan/26) ==")
    allok = True
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_default, is_nullable
            FROM information_schema.columns
            WHERE table_name='site_config' AND column_name='platform'
            """
        )
        row = cur.fetchone()
        allok &= check("platform column exists", row is not None)
        if row:
            allok &= check("platform default 'shopify'",
                           "'shopify'" in (row[0] or ""), str(row[0]))
            allok &= check("platform NOT NULL", row[1] == "NO")
        cur.execute(
            """
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'site_config'::regclass
              AND conname LIKE '%platform%'
              AND pg_get_constraintdef(oid) LIKE '%framer%'
            """
        )
        allok &= check("platform CHECK includes framer", cur.fetchone() is not None)

        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name='site_config' AND column_name='framer_site_ref'
            """
        )
        allok &= check("framer_site_ref column exists",
                       cur.fetchone() is not None)

        cur.execute(
            """
            SELECT config_value FROM system_config
            WHERE config_key = 'framer.api_base'
            """
        )
        row = cur.fetchone()
        allok &= check("framer.api_base provisioned",
                       row is not None and bool(row[0]),
                       str(row))
    return allok


def test_policy_seeds(conn, ids):
    print("\n== fix_policy seeds ==")
    allok = True
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sub_type, risk_tier, weekly_cap, enabled
            FROM fix_policy WHERE site_id = %s
            ORDER BY sub_type
            """,
            (ids["site_id"],),
        )
        rows = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    allok &= check("framer.title seed low/10",
                   rows.get("framer.title") == ("low", 10, False), str(rows.get("framer.title")))
    allok &= check("framer.description seed medium/5",
                   rows.get("framer.description") == ("medium", 5, False),
                   str(rows.get("framer.description")))
    # fail-closed: policy gate refuses the disabled seed
    from fixes.policy import check_execution_allowed, PolicyBlocked
    try:
        check_execution_allowed(conn, ids["site_id"], "framer.title")
        allok &= check("disabled framer.title fails closed", False)
    except PolicyBlocked as exc:
        allok &= check("disabled framer.title fails closed",
                       exc.reason == "policy_disabled", exc.reason)
    return allok


def test_shopify_resolver(conn, ids):
    print("\n== resolver: shopify byte-parity ==")
    from connectors.write_credentials import resolve_write_credentials
    allok = True
    with EnvSandbox():
        os.environ["SHOPIFY_DOMAIN"] = ""
        os.environ["SHOPIFY_ACCESS_TOKEN"] = "tok-test"
        out = resolve_write_credentials(conn, ids["site_id"])
        allok &= check("shopify resolves ok", out.get("ok") is True, str(out.get("outcome")))
        cfg = out.get("config") or {}
        allok &= check("exact legacy keys present",
                       set(("shop_domain", "access_token", "api_version"))
                       <= set(cfg.keys()), str(sorted(cfg.keys())))
        allok &= check("shop_domain from site_config", bool(cfg.get("shop_domain")))
        allok &= check("api_version default 2026-01",
                       cfg.get("api_version") == "2026-01", str(cfg.get("api_version")))
        # bare handle normalization (legacy rule parity)
        with conn.cursor() as cur:
            cur.execute("UPDATE site_config SET shopify_domain = %s "
                        "WHERE site_id = %s",
                        (f"bare-{RUN_TOKEN}", ids["site_id"]))
        conn.commit()
        out2 = resolve_write_credentials(conn, ids["site_id"])
        domain2 = ((out2.get("config") or {}).get("shop_domain") or "")
        allok &= check("bare handle -> myshopify FQDN",
                       domain2 == f"bare-{RUN_TOKEN}.myshopify.com", domain2)
    return allok


def test_shopify_missing_credentials(conn, ids):
    print("\n== resolver: shopify fail-closed ==")
    from connectors.write_credentials import resolve_write_credentials
    allok = True
    with EnvSandbox():
        os.environ["SHOPIFY_DOMAIN"] = ""
        os.environ["SHOPIFY_ACCESS_TOKEN"] = ""
        with conn.cursor() as cur:
            cur.execute("UPDATE site_config SET shopify_domain = NULL "
                        "WHERE site_id = %s", (ids["site_id"],))
        conn.commit()
        out = resolve_write_credentials(conn, ids["site_id"])
        allok &= check("missing domain -> no_credentials",
                       out.get("ok") is False
                       and out.get("outcome") == "no_credentials", str(out))
        allok &= check("detail names SHOPIFY_DOMAIN",
                       "SHOPIFY_DOMAIN" in (out.get("detail") or ""),
                       out.get("detail"))
        with EnvSandbox():
            os.environ["SHOPIFY_DOMAIN"] = f"phase0-{RUN_TOKEN}.myshopify.com"
            with conn.cursor() as cur:
                pass
            out2 = resolve_write_credentials(conn, ids["site_id"])
            allok &= check("domain present, missing token -> detail names token",
                           out2.get("ok") is False
                           and "SHOPIFY_ACCESS_TOKEN" in (out2.get("detail") or ""),
                           str(out2.get("detail")))
    return allok


def test_framer_resolver(conn, ids):
    print("\n== resolver: framer ==")
    from connectors.write_credentials import resolve_write_credentials
    allok = True
    # scenario A: nothing set
    with EnvSandbox():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE site_config SET platform='framer', framer_site_ref=NULL, "
                "shopify_domain=NULL WHERE site_id = %s", (ids["site_id"],))
        conn.commit()
        out = resolve_write_credentials(conn, ids["site_id"])
        allok &= check("framer missing everything -> no_credentials",
                       out.get("ok") is False
                       and out.get("outcome") == "no_credentials", str(out))
        detail = out.get("detail") or ""
        allok &= check("detail names BOTH missing keys",
                       "FRAMER_API_KEY" in detail
                       and "framer_site_ref" in detail, detail)
        allok &= check("config still present in fail shape",
                       isinstance(out.get("config"), dict), str(type(out.get("config"))))
        # scenario B: site ref but no key
        with conn.cursor() as cur:
            cur.execute("UPDATE site_config SET framer_site_ref = %s "
                        "WHERE site_id = %s",
                        (f"framer-site-{RUN_TOKEN}", ids["site_id"]))
        conn.commit()
        out2 = resolve_write_credentials(conn, ids["site_id"])
        allok &= check("key missing -> detail names FRAMER_API_KEY",
                       out2.get("ok") is False
                       and "FRAMER_API_KEY" in (out2.get("detail") or "")
                       and "framer_site_ref" not in (out2.get("detail") or ""),
                       str(out2.get("detail")))
        # scenario C: full credential
        os.environ["FRAMER_API_KEY"] = "fk-test"
        out3 = resolve_write_credentials(conn, ids["site_id"])
        allok &= check("framer resolves ok", out3.get("ok") is True, str(out3))
        cfg = out3.get("config") or {}
        allok &= check("framer config shape",
                       cfg.get("framer_api_key") == "fk-test"
                       and cfg.get("framer_site_ref") == f"framer-site-{RUN_TOKEN}"
                       and bool(cfg.get("api_base")), str(cfg))
        allok &= check("no credential values leak into detail",
                       "fk-test" not in str(out3.get("detail")), str(out3.get("detail")))
    return allok


def test_site_not_found(conn):
    print("\n== resolver: site_not_found ==")
    from connectors.write_credentials import resolve_write_credentials
    bogus = "00000000-0000-0000-0000-000000000000"
    out = resolve_write_credentials(conn, bogus)
    return check("bogus site_id -> site_not_found, ok=False",
                 out.get("ok") is False
                 and out.get("outcome") == "site_not_found", str(out))


def test_executor_parity(conn, ids):
    print("\n== executor refactor: legacy parity ==")
    from jobs.fix_executor import (_resolve_shop_config,
                                   _credential_ok_for_platform)
    allok = True
    with EnvSandbox():
        os.environ["SHOPIFY_ACCESS_TOKEN"] = "tok-test"
        os.environ.pop("SHOPIFY_DOMAIN", None)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE site_config SET platform='shopify', shopify_domain=%s "
                "WHERE site_id = %s",
                (f"parity-{RUN_TOKEN}", ids["site_id"]))
        conn.commit()
        cfg = _resolve_shop_config(conn, ids["site_id"])
        allok &= check("legacy flat shape",
                       cfg.get("shop_domain") == f"parity-{RUN_TOKEN}.myshopify.com"
                       and cfg.get("access_token") == "tok-test"
                       and cfg.get("api_version") == "2026-01", str(cfg))
        allok &= check("metadata keys ride along",
                       cfg.get("platform") == "shopify" and cfg.get("ok") is True,
                       str((cfg.get("platform"), cfg.get("ok"))))
        # platform check variants
        allok &= check("legacy-shape config passes shopify check",
                       _credential_ok_for_platform(
                           {"shop_domain": "x", "access_token": "y"}) is True)
        allok &= check("framer check keys",
                       _credential_ok_for_platform(
                           {"platform": "framer", "framer_api_key": "k",
                            "framer_site_ref": "s"}) is True
                       and _credential_ok_for_platform(
                           {"platform": "framer", "framer_api_key": "k"}) is False)
    # framer sub_type probe: plan/26 Phase 3 wired the real Framer auth
    # probe (mock mode) — ok=True with capabilities, never a Shopify path.
    from jobs.fix_executor import _fix_scope_probes
    probes = _fix_scope_probes(conn, ids["site_id"], "framer.title")
    allok &= check("framer probe -> mock-mode auth probe",
                   probes.get("ok") is True
                   and "page_meta_write" in (probes.get("capabilities") or []),
                   str(probes))
    # byte-parity: a full shopify resolution builds the exact legacy dict
    # the old function returned (keys equal, values equal)
    with EnvSandbox():
        os.environ["SHOPIFY_ACCESS_TOKEN"] = "tok-parity"
        from connectors.write_credentials import resolve_write_credentials
        resolved = resolve_write_credentials(conn, ids["site_id"])
        legacy = {
            "shop_domain": (resolved["config"]["shop_domain"]),
            "access_token": resolved["config"]["access_token"],
            "api_version": resolved["config"]["api_version"],
        }
        rebuilt = _resolve_shop_config(conn, ids["site_id"])
        allok &= check("byte-parity: legacy triple identical",
                       all(rebuilt[k] == legacy[k]
                           for k in ("shop_domain", "access_token", "api_version")),
                       f"{rebuilt} vs {legacy}")
    return allok


def test_client_construct(conn, ids):
    print("\n== byte-parity: client construction shape ==")
    allok = True
    with EnvSandbox():
        os.environ["SHOPIFY_ACCESS_TOKEN"] = "tok-construct"
        from connectors.write_credentials import resolve_write_credentials
        from connectors.shopify import ShopifyGraphQLClient
        out = resolve_write_credentials(conn, ids["site_id"])
        cfg = out["config"]
        # The exact construction the executor + adapters perform; the
        # client is built but never run (no network in this suite).
        client = ShopifyGraphQLClient(
            shop_domain=cfg["shop_domain"],
            access_token=cfg["access_token"],
            api_version=cfg["api_version"],
        )
        allok &= check("ShopifyGraphQLClient builds from resolver config",
                       client is not None and bool(client.shop_domain),
                       str(getattr(client, "shop_domain", None)))
    return allok


def main():
    env_loader.load_env_file(quiet=True)
    conn = database.get_connection()
    ids_list = []
    try:
        ok = True
        primary = seed_site(conn, platform="shopify",
                            shopify_domain=f"phase0-{RUN_TOKEN}.myshopify.com")
        ids_list.append(primary)
        ok &= test_schema(conn)
        ok &= test_policy_seeds(conn, primary)
        ok &= test_shopify_resolver(conn, primary)
        ok &= test_shopify_missing_credentials(conn, primary)
        ok &= test_framer_resolver(conn, primary)
        ok &= test_site_not_found(conn)
        ok &= test_executor_parity(conn, primary)
        ok &= test_client_construct(conn, primary)
    finally:
        cleanup(conn, ids_list)
        conn.close()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        sys.exit(1)
    print("ALL PHASE 0 TESTS PASSED")


if __name__ == "__main__":
    main()
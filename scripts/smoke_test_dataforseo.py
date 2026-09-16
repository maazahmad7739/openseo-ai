"""One-shot DataForSEO smoke test (plan/11 §7 verification).

Runs EXACTLY two live calls — 1 keyword_volume + 1 SERP — against the real
DataForSEO API using the shared OpenSEO credential, then logs both costs to
api_costs and reads them back.

Verifies, in order:
  1. credential resolution via get_openseo_adapter (OPENSEO_SECRET_VALUE)
  2. live auth: provider status_code 20000 (no HTTP 401/402)
  3. response normalization (adapter contract: ok=True, typed data)
  4. billing parse: real `cost` extracted from the raw task payload
  5. cost logging: rows land in api_costs; weekly_spend() sees them

Isolated by design: no repo .env loading, no db.py import — the only env
vars read are DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD (or OPENSEO_SECRET_VALUE)
and PGPASSWORD for the Postgres connection. Credentials are never printed.

Usage:
  DATAFORSEO_LOGIN=... DATAFORSEO_PASSWORD=... PGPASSWORD=... \
      python scripts/smoke_test_dataforseo.py
"""

import base64
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from connectors.openseo import get_openseo_adapter  # noqa: E402
from connectors.openseo_rest_adapter import OpenseoRestAdapter  # noqa: E402
from connectors.costlog import log_cost, weekly_spend  # noqa: E402
import psycopg2  # noqa: E402

DB = {
    "host": "aws-0-ap-northeast-1.pooler.supabase.com",
    "port": "6543",
    "user": "postgres.fzuckbubqrzcjukgwepr",
    "dbname": "postgres",
    "sslmode": "require",
}
SITE_ID = "c171d087-2264-4be8-b827-16fb663ba986"  # Aurora Audio (demo site)
KEYWORD = "wireless headphones"
LOCATION_CODE = 2840  # United States
LANGUAGE_CODE = "en"

FAILURES = []


def check(label, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


class RawCapturingAdapter(OpenseoRestAdapter):
    """Production _post() untouched; captures the raw payload so the real
    provider `cost` field (which fetch() normalization drops) can be parsed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_raw = None

    def _post(self, capability, params):
        payload = super()._post(capability, params)
        self.last_raw = payload
        return payload


def provider_cost(raw):
    """DataForSEO reports cost per task (and top-level). Extract the first
    non-null task cost; fall back to the envelope-level cost."""
    if not raw:
        return None
    for task in raw.get("tasks", []) or []:
        if task.get("cost") is not None:
            return task["cost"]
    return raw.get("cost")


def run_call(adapter, label, capability, params):
    print(f"\n{'=' * 64}\nSMOKE CALL: {label}  (capability='{capability}')\n{'=' * 64}")
    adapter.last_raw = None
    result = adapter.fetch(capability, params)
    raw = adapter.last_raw
    status = (raw or {}).get("status_code")
    message = (raw or {}).get("status_message")
    cost = provider_cost(raw)

    print(f"  provider status_code : {status} ({message})")
    print(f"  ok                   : {result.get('ok')}")
    if not result.get("ok"):
        print(f"  error                : {result.get('error')} — {result.get('detail')}")
    else:
        data = result.get("data")
        print(f"  normalized items     : {len(data)}")
        for item in data[:3]:
            print(f"    {item}")
        if len(data) > 3:
            print(f"    … (+{len(data) - 3} more)")
    print(f"  provider cost        : {cost}")

    check(f"{label}: auth+status 20000", status == 20000,
          f"got {status} ({message})")
    check(f"{label}: normalized ok", bool(result.get("ok")))
    check(f"{label}: cost parsed", cost is not None, f"cost={cost}")
    return result, cost


def main():
    print("OpenSEO/DataForSEO smoke test — 1 keyword_volume + 1 SERP, live\n")

    # ── 1. credential resolution (official path, no network) ────────────
    login = os.environ.get("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if os.environ.get("OPENSEO_SECRET_VALUE"):
        secret = os.environ["OPENSEO_SECRET_VALUE"]
    elif login and password:
        secret = base64.b64encode(f"{login}:{password}".encode()).decode()
    else:
        print("FAIL: set DATAFORSEO_LOGIN + DATAFORSEO_PASSWORD (or OPENSEO_SECRET_VALUE)")
        return 1

    adapter_probe = get_openseo_adapter(config={"openseo.mode": "live"},
                                        secret_value=secret)
    check("credential resolved via get_openseo_adapter",
          adapter_probe.credential == secret and not adapter_probe.mock_mode)
    print(f"  base_url: {adapter_probe.base_url}")
    print(f"  Authorization: Basic {secret[:6]}…{secret[-4:]} (masked)")
    print(f"  capabilities: {adapter_probe.capabilities}")

    # ── 2-4. the two live calls ─────────────────────────────────────────
    adapter = RawCapturingAdapter(credential=secret, mock_mode=False)

    kw_result, kw_cost = run_call(
        adapter, "keyword_volume", "keyword_volume",
        {"keywords": [KEYWORD], "location_code": LOCATION_CODE,
         "language_code": LANGUAGE_CODE},
    )
    serp_result, serp_cost = run_call(
        adapter, "serp", "serp",
        {"query": KEYWORD, "limit": 10, "location_code": LOCATION_CODE,
         "language_code": LANGUAGE_CODE},
    )

    # ── 5. cost logging into api_costs (real log_cost path) ─────────────
    print(f"\n{'=' * 64}\nCOST LOGGING (api_costs)\n{'=' * 64}")
    if not (kw_result.get("ok") and serp_result.get("ok")):
        print("  SKIP: at least one call failed; nothing logged to DB.")
        return 1

    conn = psycopg2.connect(host=DB["host"], port=DB["port"], user=DB["user"],
                            password=os.environ["PGPASSWORD"],
                            dbname=DB["dbname"], sslmode="require")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT site_name, domain FROM site_config WHERE site_id = %s",
                (SITE_ID,))
            site = cur.fetchone()
        if not site:
            print(f"FAIL: site {SITE_ID} not found in site_config; not logging.")
            return 1
        print(f"  site: {site[0]} ({site[1]})")

        for capability, cost in (("keyword_volume", kw_cost),
                                 ("serp", serp_cost)):
            log_cost(conn, SITE_ID, f"openseo_{capability}",
                     call_type=capability, calls=1, cost=cost,
                     metadata={"smoke_test": True, "keyword": KEYWORD})
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT timestamp, service, call_type, calls, cost "
                "FROM api_costs WHERE site_id = %s AND metadata::text LIKE %s "
                "ORDER BY timestamp DESC LIMIT 2",
                (SITE_ID, "%smoke_test%"))
            rows = cur.fetchall()
        print("  logged rows (this test):")
        for r in rows:
            print(f"    {r[0]} | {r[1]} | {r[2]} | calls={r[3]} | cost={r[4]}")
        check("cost rows logged", len(rows) == 2, f"{len(rows)}/2 rows")

        spend = weekly_spend(conn, site_id=SITE_ID)
        print("  weekly spend (this site):")
        for service, total, calls in spend:
            print(f"    {service}: ${float(total or 0):.4f} across {calls} calls")
    finally:
        conn.close()

    # ── summary ─────────────────────────────────────────────────────────
    print(f"\n{'=' * 64}\nSUMMARY\n{'=' * 64}")
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} check(s) FAILED: {FAILURES}")
        return 1
    print("RESULT: ALL CHECKS PASSED — credentials, live calls, normalization,")
    print("        billing cost parsing, and api_costs logging all work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
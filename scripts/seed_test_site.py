"""Idempotent CI/test seed: the example.com tenant suites assume on a
fresh database (run_api_tests, run_measurement_tests, the day-in-the-life
suites).

Mirrors the local dev DB's example.com row so suites behave identically in
CI and locally. Safe to run repeatedly: the insert is guarded by a
NOT EXISTS on domain, and the sync step upserts.

Default behavior = seed site + one mock sync (populates pages /
search_performance that the measurement control-group pairing reads).
Pass --site-only to insert the site row without syncing.

Usage: python scripts/seed_test_site.py   (POSTGRES_* env vars from src/db.py)
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db as database  # noqa: E402

INSERT_SQL = """
INSERT INTO site_config
    (site_name, domain, gsc_property, platform,
     integration_mode, catalogue_size_tier, shopify_domain, ga4_property_id)
SELECT 'Aurora Audio (mock)', 'example.com', 'sc-domain:example.com', 'shopify',
       'mock', 'small', 'example.com', 'mock-property-1'
WHERE NOT EXISTS (
    SELECT 1 FROM site_config WHERE domain = 'example.com'
)
"""


def seed_site(conn):
    """Insert the example.com tenant row if missing. Returns 'inserted'/'kept'."""
    with conn.cursor() as cur:
        cur.execute(INSERT_SQL)
        inserted = cur.rowcount
    conn.commit()
    return "inserted" if inserted else "kept"


def run_seed_sync():
    """Populate pages / search_performance the way a real daily sync does
    (mock mode). run_day_in_the_life and the measurement suites declare
    'run sync first' — a bare site_config row is not enough: control-group
    pairing reads collection pages from `pages`."""
    from connectors.sync import run_sync

    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT site_id FROM site_config WHERE domain = 'example.com'")
            row = cur.fetchone()
        conn.commit()
        if row is None:
            print("seed_test_site: example.com missing; seed first")
            return 1
        counts = run_sync(site_id=str(row[0]))
        print(f"seed_test_site: mock sync complete (pages={counts.get('pages')}, "
              f"search_performance={counts.get('search_performance')}, "
              f"page_business_performance={counts.get('page_business_performance')})")
        return 0
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass


def main():
    conn = database.get_connection()
    try:
        state = seed_site(conn)
        print(f"seed_test_site: example.com ready ({state})")
        return 0
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    if "--site-only" in sys.argv:
        sys.exit(main())
    rc = main()
    if rc == 0:
        rc = run_seed_sync()
    sys.exit(rc)
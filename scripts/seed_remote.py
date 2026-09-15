# -*- coding: utf-8 -*-
"""Seed the production Supabase DB with the mock-fixture dataset.

Replicates the local pipeline end-to-end:
  sync (fixtures) -> generators (raw candidates) -> agent promotion
  -> 5 proposed rows + 1 completed measured record (baseline + post +
  control snapshots, classified verdict) via the SAME pipeline functions
  used in production, against the remote DB only.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import env as env_loader

env_loader.load_env_file(quiet=True)

# Hard-bind the remote DB for this run (never the local defaults).
import db as database

database.DB_PARAMS_DEFAULT.update({
    "host": os.environ["SEED_DB_HOST"],
    "port": os.environ["SEED_DB_PORT"],
    "dbname": os.environ["SEED_DB_NAME"],
    "user": os.environ["SEED_DB_USER"],
    "password": os.environ["SEED_DB_PASSWORD"],
})

from connectors.sync import run_sync  # noqa: E402
from generators.orchestrator import run_candidate_generation  # noqa: E402
from agents.seo_agent import run_agent  # noqa: E402

FIXTURE_SITE = {
    "site_name": "Aurora Audio",
    "domain": "example.com",
    "gsc_property": "sc-domain:example.com",
    "shopify_domain": "example.com",
    "ga4_property_id": "seed-property-1",
    "catalogue_size_tier": "small",
    "min_in_stock_products": 1,
}


def main():
    print("=== 1. sync (mock fixtures -> remote DB) ===")
    counts = run_sync()
    print(counts)

    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT site_id FROM site_config WHERE domain = 'example.com'")
            site_id = str(cur.fetchone()[0])
    finally:
        conn.close()
    print("site_id:", site_id)

    print("=== 2. generators (raw candidates) ===")
    gen = run_candidate_generation(site_id)
    print(gen)

    print("=== 3. agent promotion (Ollama Cloud) ===")
    summary = run_agent(site_id)
    print(summary)

    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT recommendation_id, generator, action_type, impact "
                "FROM recommendations WHERE site_id = %s AND status = 'proposed' "
                "ORDER BY created_at",
                (site_id,),
            )
            proposed = cur.fetchall()
        print(f"proposed rows: {len(proposed)}")
        for r in proposed:
            print("  ", r[0], r[1], r[2], r[3])
    finally:
        conn.close()

    print("=== 4. implement one approved row (baseline) ===")
    if proposed:
        target = proposed[0][0]
        conn = database.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE recommendations SET status='approved', approved_at=now() "
                    "WHERE recommendation_id=%s", (target,))
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("SELECT action_type FROM recommendations WHERE recommendation_id=%s", (target,))
                action_type = cur.fetchone()[0]
            if action_type == "create_page":
                from measurement.baseline import store_cluster_baseline
                info = store_cluster_baseline(conn, target, None)
            else:
                from measurement.baseline import store_baseline
                info = store_baseline(conn, target, None)
            conn.commit()
            print("baseline stored:", info)

            print("=== 5. measure + classify (post snapshots) ===")
            if action_type == "create_page":
                from measurement.measure import store_cluster_post_snapshots
                post = store_cluster_post_snapshots(conn, target, None)
            else:
                from measurement.measure import store_post_snapshots
                post = store_post_snapshots(conn, target, None)
            conn.commit()
            print("post stored:", post)

            from measurement.classify import classify_recommendation, persist_classification
            cls = classify_recommendation(conn, target)
            verdict = persist_classification(conn, target, cls)
            conn.commit()
            print("verdict:", verdict, "| reasons:", cls["reasons"][:2])
        finally:
            conn.close()

    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            for label, sql in (
                ("sites", "SELECT count(*) FROM site_config"),
                ("pages", "SELECT count(*) FROM pages"),
                ("search_performance", "SELECT count(*) FROM search_performance"),
                ("keyword_clusters", "SELECT count(*) FROM keyword_clusters"),
                ("recommendations", "SELECT count(*) FROM recommendations"),
                ("proposed", "SELECT count(*) FROM recommendations WHERE status='proposed'"),
                ("measured", "SELECT count(*) FROM recommendations WHERE status='measured'"),
                ("snapshots", "SELECT count(*) FROM measurement_snapshots"),
            ):
                cur.execute(sql)
                print(label, "=", cur.fetchone()[0])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
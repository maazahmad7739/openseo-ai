"""Semantic scoring job skeleton (brief §3): embed clusters + pages →
semantic_content_score in page_query_match_scores.

Per plan/09. THE single model call in this pipeline: the embedding step.
Until an embedding provider is configured the job updates nothing and logs
that clearly — Generator 1's non-semantic scoring stand-in remains in place.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EMBEDDING_ENV = "EMBEDDING_ENDPOINT"


def run(site_id=None, reference_date=None):
    from jobs.locks import job_lock, LOCK_KEYS, already_running
    endpoint = os.environ.get(EMBEDDING_ENV)
    if not endpoint:
        print("[semantic_scoring] embedding endpoint not configured — "
              "semantic_content_score unchanged (stand-in scoring remains)", flush=True)
        return {"skipped": "embedding_endpoint_not_configured"}
    # Skeleton: real implementation marks the one model call it makes.
    # Lock acquisition precedes any model spend so an overlapping run can
    # never double-bill the embedding call.
    conn = __import__("db").get_connection()
    try:
        with job_lock(conn, LOCK_KEYS["semantic_scoring"]) as got:
            if not got:
                return already_running("semantic_scoring")
            raise NotImplementedError(
                "semantic scoring requires the embedding contract decision (founder); "
                "endpoint is configured but the embedding call is not implemented yet")
    finally:
        conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--site_id", default=None)
    parser.add_argument("--reference_date", default=None)
    args = parser.parse_args()
    print("semantic_scoring:", run(args.site_id, args.reference_date))


if __name__ == "__main__":
    main()
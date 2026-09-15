"""Bounded per-site execution helper (Phase 5: multi-brand/site isolation).

run_across_sites() runs a per-site function for every site with:

  - per-site fault isolation: one site's exception is captured into a
    ``{"status": "failed", "site_id": ..., "error": ...}`` result and every
    other site still runs;
  - optional bounded concurrency (env ``JOB_MAX_WORKERS``, default 1 = strictly
    sequential, the safe baseline for DataForSEO rate limits; raise deliberately,
    never run naive full-parallelism across unlimited sites);
  - optional per-site wall-clock timeout (env ``JOB_TIMEOUT_SECONDS``, default
    unset = no timeout; a site that exceeds it is recorded as failed while the
    rest of the batch continues).

Concurrency contract: each site's ``work(site_id)`` MUST open and own its own DB
connection (call the per-site function with ``conn=None``). A psycopg2
connection is single-threaded — sharing one ``conn`` across worker threads is
unsafe and corrupts transactions. The site list itself is read on the calling
thread before any worker starts.

Timeout semantics: ThreadPoolExecutor cannot terminate a running thread. A site
whose ``JOB_TIMEOUT_SECONDS`` elapses is recorded as failed immediately and the
rest of the batch is still processed, but its work keeps running on its own
connection; shutdown(wait=True) then joins stragglers so no late DB commit is
lost. A truly hung site therefore still blocks process exit — bound the whole
job at the process level (cron/scheduler kill timeout) if that is required.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError


def run_across_sites(site_ids, work, on_failure=None):
    """Execute work(site_id) for each site, isolating failures per site.

    Returns an ordered dict {site_id: result}. A site whose work() raises is
    recorded as {"status": "failed", "site_id": ..., "error": ...} and the
    remaining sites still run. on_failure(site_id, result), when given, is
    invoked for each failed site (used to push per-site failure alerts).
    """
    max_workers = _parse_env_int("JOB_MAX_WORKERS", 1)
    timeout_seconds = _parse_env_float("JOB_TIMEOUT_SECONDS")
    print(f"[run_multi] sites={len(site_ids)} max_workers={max_workers}"
          + (f" timeout={timeout_seconds:g}s" if timeout_seconds else " no timeout"),
          flush=True)

    def _one(sid):
        try:
            return work(sid)
        except Exception as exc:
            result = {"status": "failed", "site_id": str(sid),
                      "error": f"{type(exc).__name__}: {exc}"}
            if on_failure is not None:
                try:
                    on_failure(str(sid), result)
                except Exception:
                    print(f"[run_multi] failure notifier raised for site {sid}", flush=True)
            return result

    out = {}
    if max_workers >= 2:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_one, sid): str(sid) for sid in site_ids}
            for fut in futures:
                sid = futures[fut]
                try:
                    out[sid] = fut.result(timeout=timeout_seconds)
                except FutureTimeoutError:
                    out[sid] = {"status": "failed", "site_id": sid,
                                "error": f"timed out after {timeout_seconds:g}s"}
                except Exception as exc:
                    out[sid] = {"status": "failed", "site_id": sid,
                                "error": f"{type(exc).__name__}: {exc}"}
    else:
        for sid in site_ids:
            out[str(sid)] = _one(sid)
    return out


def _parse_env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[run_multi] ignoring invalid {name}={raw!r}; using {default}", flush=True)
        return default


def _parse_env_float(name):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        print(f"[run_multi] ignoring invalid {name}={raw!r}; no timeout", flush=True)
        return None
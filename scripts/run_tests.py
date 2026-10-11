"""Sequential runner for the bespoke test suites in tests/ (2.17 CI prep).

The repo's 38 suites are standalone scripts with a shared `check()` +
`exit-code` harness (NOT pytest), each run directly as
`python tests/run_X_tests.py`. Most require a reachable Postgres matching
src/db.py defaults; a few are offline-only; exactly one needs live LLM
credentials (run_live_agent.py) and is skipped everywhere.

This runner executes them in deterministic order, tees each suite's own
[PASS]/[FAIL] output, and fails with a non-zero exit if any suite fails.
CI (see .github/workflows/ci.yml) runs this after python scripts/migrate.py.

Usage:
    python scripts/run_tests.py
    python scripts/run_tests.py run_api_tests run_fix_executor_tests   # subset
"""

import os
import subprocess
import sys
import time
from pathlib import Path

# Windows consoles default to cp1252: suite stderr can carry U+FFFD (from
# errors="replace" decoding) and crash the runner's own print. UTF-8 with
# replacement keeps the run alive.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"

sys.path.insert(0, str(ROOT / "src"))
from env import SECRETS_OPT_IN_ENV, secret_env_names  # noqa: E402

# Suites that must not run in CI / local full runs, with reasons.
# run_live_agent.py calls the hosted LLM provider (needs OLLAMA_API_KEY).
SKIPPED_SUITES = {
    "run_live_agent.py": "needs live LLM credentials (OLLAMA_API_KEY)",
}

# Suites known safe without a database; used only for reporting.
OFFLINE_SUITES = {
    "run_gsc_tests.py",
    "run_graphql_shell_tests.py",
    "run_integration_mock_tests.py",
    "run_llm_drafting_tests.py",
    "run_mock_tests.py",
    "run_no_socket_tests.py",
    "run_phase6_canonical_tag_tests.py",
}

# CI runs mock-mode only (INTEGRATION_MODE=mock, no external network);
# these suites hit live third-party APIs and are skipped there.
CI_SKIPPED_SUITES = {
    "run_serp_grounding_tests.py": "hits live DataForSEO (CI is offline/mock)",
}

# Suites that get deterministic CLI args in the automated runner.
# run_day_in_the_life.py's default path stages a LIVE Ollama Cloud agent
# evaluation and hard-fails when the LLM approves 0 of ~7 candidates —
# the LLM's approve/reject split is non-deterministic, so asserting on it
# is flaky by design (~4 of 5 runs fail on a clean pipeline). Its own
# documented --skip-agent mode simulates the agent deterministically and
# still exercises every other stage (sync -> generate -> approve ->
# measure -> classify). Live-agent behavior is verified manually /
# standalone (see WALKTHROUGH.md § test guidelines).
SUITE_ARGS = {
    "run_day_in_the_life.py": ["--skip-agent"],
}

# Suites that legitimately need live credentials. Everything else runs
# hermetic. The runner enforces it two ways because either alone leaks:
#   - scrub the child's env copy, which covers credentials exported in the
#     developer's shell (os.environ.copy() otherwise inherits them);
#   - set OPENSEO_LOAD_SECRETS=0, which covers .env, since the scrub cannot
#     stop load_env_file() from reading the file into the child.
# Getting this wrong is not a slow test, it is a live provider call: with a
# key present, drafting dialled ollama.com and hung for the full client
# timeout while the suite printed "no real network calls will be made".
LIVE_SUITES = {
    "run_live_agent.py",
}


def discover_suites():
    # Standalone suites: run_*_tests.py (the majority) plus the three
    # day-in-the-life / live-agent scripts that don't carry the suffix.
    return sorted(
        p for p in TESTS_DIR.glob("run_*.py")
        if p.is_file() and p.name != "__init__.py"
    )


def run_suite(path, extra_args=None, extra_env=None):
    env = os.environ.copy()
    env.setdefault("INTEGRATION_MODE", "mock")
    if Path(path).name in LIVE_SUITES:
        env[SECRETS_OPT_IN_ENV] = "1"
    else:
        removed = [key for key in secret_env_names(env)]
        for key in removed:
            env.pop(key, None)
        env[SECRETS_OPT_IN_ENV] = "0"
        if removed:
            print(
                f"[env] stripped {len(removed)} credential-shaped key(s) from "
                f"the child environment for {Path(path).name}.",
                flush=True,
            )
    env.update(extra_env or {})
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(path)] + (extra_args or []),
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    elapsed = time.monotonic() - start
    return proc, elapsed


def main(argv):
    requested = set(argv)
    suites = discover_suites()
    if requested:
        suites = [s for s in suites if s.stem in requested or s.name in requested]
        unknown = requested - {s.stem for s in suites} - {s.name for s in suites}
        if unknown:
            print(f"no suite matched: {', '.join(sorted(unknown))}")
            return 2

    os.environ.setdefault("INTEGRATION_MODE", "mock")
    results = []
    for path in suites:
        name = path.name
        skip_reason = SKIPPED_SUITES.get(name) or CI_SKIPPED_SUITES.get(name)
        if skip_reason:
            print(f"[SKIP] {name} - {skip_reason}")
            results.append(("skip", name, 0.0))
            continue
        print(f"[RUN ] {name}")
        # Suite output passes through so its own [PASS]/[FAIL] lines show.
        proc, elapsed = run_suite(path, SUITE_ARGS.get(name, []))
        if proc.returncode == 0:
            print(f"[PASS] {name} ({elapsed:.1f}s)")
            results.append(("pass", name, elapsed))
        else:
            print(f"[FAIL] {name} ({elapsed:.1f}s) exit={proc.returncode}")
            if proc.stdout:
                print("  --- stdout (tail) ---")
                print("\n".join("  " + ln for ln in proc.stdout.splitlines()[-40:]))
            if proc.stderr:
                print("  --- stderr (tail) ---")
                print("\n".join("  " + ln for ln in proc.stderr.splitlines()[-40:]))
            results.append(("fail", name, elapsed))

    passed = [r for r in results if r[0] == "pass"]
    failed = [r for r in results if r[0] == "fail"]
    skipped = [r for r in results if r[0] == "skip"]
    print()
    print(
        f"summary: {len(passed)} passed, {len(failed)} failed, "
        f"{len(skipped)} skipped, {len(results)} total"
    )
    for state, name, _ in results:
        if state == "fail":
            print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
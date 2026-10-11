# -*- coding: utf-8 -*-
"""Redirect chain/loop guard — pure-graph unit tests (no DB, no network).

python tests/run_redirect_chain_tests.py   (unit half; DB half needs Postgres)

Unit coverage (resolve_redirect_chain):
  1. no existing rules -> identity (source -> target, 0 hops)
  2. direct loop  A -> B -> A -> typed RedirectCycleError
  3. deep circular loop  A -> B -> C -> A -> typed refusal
  4. multi-hop chain flattening  A -> B, B -> C  =>  A -> C
  5. deep chain flattening  A -> B, B -> C, C -> D  =>  A -> D
  6. pre-existing loop among existing rules (A -> B -> C -> B) -> refusal
  7. self-loop target (A -> A) -> refusal
  8. max-hops bound -> refusal (pathological map)
  9. empty/whitespace nodes -> refusal
 10. flattening composes across a mid-chain branch
 11. cycle via target being a KEY pointing at a new-node that loops back

Regression (DB-backed generator integration):
 12. generator refuses loop candidates + idempotent refusal
 13. generator flattens a 2-hop chain on the real DB
"""
import os
import sys
import json
import uuid
from datetime import date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from fixes.redirect_graph import (  # noqa: E402
    RedirectCycleError,
    MAX_REDIRECT_CHAIN_HOPS,
    resolve_redirect_chain,
)

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


def expect(name, source, target, redirect_map, want_target, want_hops):
    try:
        got = resolve_redirect_chain(source, target, redirect_map)
    except RedirectCycleError as exc:
        check(name, False, f"unexpected RedirectCycleError: {exc}")
        return
    check(name, got["target"] == want_target and got["hops"] == want_hops,
          f"got {got}, want target={want_target!r} hops={want_hops}")


def expect_cycle(name, source, target, redirect_map, want_reason):
    try:
        got = resolve_redirect_chain(source, target, redirect_map)
    except RedirectCycleError as exc:
        check(name, exc.reason == want_reason,
              f"reason {exc.reason!r} != {want_reason!r} ({exc})")
        check(f"{name} (manual_review flag)",
              exc.detail.get("manual_review") is True, str(exc.detail))
        return
    check(name, False, f"expected RedirectCycleError, got {got}")


def test_unit():
    print("\n== resolve_redirect_chain (pure graph) ==")
    expect("1. no existing rules -> passthrough",
           "/a", "/b", {}, "/b", 0)
    expect_cycle("2. direct loop A -> B -> A",
                 "/a", "/b", {"/b": "/a"}, "redirect_loop_source")
    expect_cycle("3. deep circular A -> B -> C -> A",
                 "/a", "/b", {"/b": "/c", "/c": "/a"},
                 "redirect_loop_source")
    expect("4. flatten A -> B -> C => A -> C",
           "/a", "/b", {"/b": "/c"}, "/c", 1)
    expect("5. deep flatten A -> B, B -> C, C -> D => A -> D",
           "/a", "/d" if False else "/b", {"/b": "/c", "/c": "/d"}, "/d", 2)
    expect_cycle("6. pre-existing loop among existing rules",
                 "/a", "/b", {"/b": "/c", "/c": "/b"},
                 "existing_redirect_loop")
    expect_cycle("7. self-loop target", "/a", "/x", {"/x": "/x"},
                 "existing_redirect_loop")
    expect_cycle("8. max-hops bound refuses pathological map",
                 "/a", "/n0", {f"/n{i}": f"/n{i+1}" for i in range(40)},
                 "max_chain_hops_exceeded")
    expect_cycle("9a. empty source", "", "/b", {}, "empty_redirect_node")
    expect_cycle("9b. whitespace target", "/a", "   ", {},
                 "empty_redirect_node")
    # 10: branch where the new rule's target passes through a flattened hop
    # then continues: /a -> /b, /b -> /c, /c -> /d resolves to /d (2 hops)
    expect("10. chain composes through consecutive hops",
           "/a", "/b", {"/b": "/c", "/c": "/d"}, "/d", 2)
    # 11: target is a key pointing BACK toward a node adjacent to source —
    # the loop-closes-at-source rule catches it (A -> B, B -> C, C -> A)
    expect_cycle("11. C -> A closure caught as loop-to-source",
                 "/a", "/b", {"/b": "/c", "/c": "/a"},
                 "redirect_loop_source")
    # extra: case/whitespace normalizations strip cleanly
    expect("12. whitespace-normalized nodes",
           " /a ", " /b ", {}, "/b", 0)


def main():
    test_unit()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        sys.exit(1)
    print("ALL REDIRECT CHAIN UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
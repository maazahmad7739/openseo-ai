"""Redirect chain + loop guard for the 301 generator (plan/21 §2.5 closure).

Shopify urlRedirectCreate keys rules on (path -> target). When the system
mints a new rule whose TARGET is itself the source of an existing rule, the
store ends up with multi-hop chains (A -> B -> C) — and if the chain ever
leads back to the source, an infinite loop (A -> B -> A) that burns crawl
budget and can kill indexation of every URL on the cycle.

Two deterministic behaviors, evaluated BEFORE any fix row is written:

  * FLATTEN: if the new rule's target already redirects onward, the new
    rule is compressed to point at the chain's final destination
    (A -> B + B -> C  =>  A -> C). One hop, always.
  * REFUSE: if the target's chain eventually reaches the SOURCE (or the
    chain contains a pre-existing loop among the existing rules), the
    generation aborts with RedirectCycleError (the generator surfaces it
    as typed FixNotSupported -> API 422) and the payload carries
    manual_review=True for operator triage.

Pure logic, no DB/network: the map is a plain {path: target} dict the
caller loads from generated_fixes (see fixes.generator._existing_redirect_map).
"""

# Bound for pathological existing mappings; beyond this the graph is a
# manual-review case, never auto-flattened.
MAX_REDIRECT_CHAIN_HOPS = 10


class RedirectCycleError(Exception):
    """Typed chain/loop refusal. .reason + .detail are stable, typed strings
    the generator embeds in its refusal; detail carries manual_review."""

    def __init__(self, reason, detail):
        self.reason = reason
        self.detail = detail or {}
        detail_text = ", ".join(f"{k}={v}" for k, v in self.detail.items())
        super().__init__(f"{reason} ({detail_text})")


def resolve_redirect_chain(source_path, target_path, redirect_map,
                           max_hops=MAX_REDIRECT_CHAIN_HOPS):
    """Resolve the new rule source->target against existing mappings.

    Returns {'source', 'target' (flattened), 'hops', 'chain'} where
    'target' is the FINAL destination after following every existing rule
    reachable from target_path. Raises RedirectCycleError when the chain
    loops back to the source, self-loops, contains a pre-existing cycle,
    or exceeds max_hops.
    """
    source = (source_path or "").strip()
    target = (target_path or "").strip()
    if not source or not target:
        raise RedirectCycleError(
            "empty_redirect_node",
            {"source": source, "target": target, "manual_review": True})
    if source == target:
        raise RedirectCycleError(
            "redirect_loop_source",
            {"chain": f"{source} -> {target}", "manual_review": True})

    chain = [source, target]
    visited = {source, target}
    current = target
    hops = 0
    while current in redirect_map:
        if hops >= max_hops:
            raise RedirectCycleError(
                "max_chain_hops_exceeded",
                {"chain": " -> ".join(chain),
                 "max_hops": max_hops, "manual_review": True})
        nxt = (redirect_map[current] or "").strip()
        if not nxt:
            break
        if nxt == source:
            # the chain eventually loops back to the SOURCE: A -> B -> A
            chain.append(nxt)
            raise RedirectCycleError(
                "redirect_loop_source",
                {"chain": " -> ".join(chain), "manual_review": True})
        if nxt in visited:
            # pre-existing loop among EXISTING rules (A -> B -> C -> B):
            # the destination itself is broken — never inherit it.
            chain.append(nxt)
            raise RedirectCycleError(
                "existing_redirect_loop",
                {"chain": " -> ".join(chain), "manual_review": True})
        chain.append(nxt)
        visited.add(nxt)
        current = nxt
        hops += 1

    return {"source": source, "target": current, "hops": hops,
            "chain": chain}
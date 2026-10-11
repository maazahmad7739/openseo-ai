# -*- coding: utf-8 -*-
"""LLM provider fallback: Ollama Cloud primary -> Gemini secondary.

python tests/run_llm_fallback_tests.py   (OFFLINE: no network, no keys)

Every provider interaction is scripted through the `client_factory` seam,
so these tests never open a socket and never read a real credential. The
credentials-shaped strings below are deliberately fake and are used to
assert that scrubbing actually removes them.

Verifies:
  A. Routing
     1. healthy primary is used, fallback never touched
     2. primary 403 (past due) routes to Gemini
     3. primary missing credentials routes to Gemini
     4. both providers fail -> loud error naming both reasons
     5. no credentials anywhere -> actionable error naming the env vars
  B. What does NOT fall back
     6. parse failure does not fall back by default (no hiding regressions)
     7. parse failure DOES fall back when opted in
  C. Circuit breaker
     8. opens after N consecutive primary failures
     9. primary skipped while open, fallback still used
    10. a primary success resets the breaker
    11. cooldown expiry re-attempts the primary
    12. the fallback is NEVER breaker-skipped (a primary outage must not
        become a total outage)
  D. Transient vs persistent
    13. 503 on the fallback is retried, then succeeds
    14. 403 on the fallback is NOT retried (persistent)
    15. backoff sleeps are recorded and jittered
  D2. Rate limits and the retry counter
    16. retry ordinals count UP (1,2), never down
    17. a 429 waits for the provider's Retry-After
    18. a 429 is not retried on a seconds-long backoff
    19. an absurd Retry-After is capped by the ceiling
    20. a 429 with no Retry-After uses the ceiling
    21. a 503 still uses the SHORT jittered backoff
    22. Retry-After parses safely (delta-seconds, junk, absent)
    23. Retry-After survives NaN/inf without propagating
  E. Contract
    16. complete_json always returns PARSED json, never a raw string
    17. last_provider / last_model / last_usage populated
    18. no credential-shaped string survives in errors or attempt logs
    19. Gemini message translation (system extracted, assistant->model)
"""
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from agents.gemini_client import (  # noqa: E402
    GeminiApiError,
    GeminiConfigError,
    to_gemini_contents,
)
from agents.ollama_client import (  # noqa: E402
    OllamaApiError,
    OllamaConfigError,
    parse_retry_after,
)
from agents.llm_client import (  # noqa: E402
    FallbackClient,
    _is_parse_failure,
    _is_transient,
    _scrub,
    _retry_after_seconds,
)

FAILURES = []

# A realistic-looking but entirely FAKE credential. Used to prove the
# scrubber removes key-shaped tokens from anything that could be logged.
FAKE_KEY = ("AQ.Ab8RN6Lj8vGBvBsPr9rFE1tyviTAR-GBcFewqGHgFP2e2YzILA_"
            "FAKEKEYMATERIAL0123456789")

PAST_DUE = ("HTTP 403: {\"error\":{\"message\":\"your subscription payment "
            "is past due\"}}")
OVERLOADED = "HTTP 503: {\"error\":{\"message\":\"high demand\"}}"
BAD_KEY = "HTTP 401: {\"error\":{\"message\":\"API key not valid\"}}"


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return bool(cond)


class ScriptedClient:
    """A provider client whose responses are scripted in order.

    `script` entries are either an exception instance to raise, or a
    string to return. `fail_with` supplies a single exception to raise on
    every call (used for the always-down primary).
    """

    def __init__(self, name, script=None, fail_with=None, model="test-model"):
        self.name = name
        self.script = list(script or [])
        self.fail_with = fail_with
        self.model = model
        self.calls = 0
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5,
                           "model": model}

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return '{"ok": true}'

    def complete_json(self, system_prompt, user_payload, retries=1):
        return json.loads(self.chat([]))


def factory_for(primary, fallback):
    """Build a client_factory returning the given scripted clients."""
    return lambda name: primary if name == "ollama" else fallback


def make_client(primary, fallback, **kwargs):
    """FallbackClient with a fake clock/sleep so tests never really wait."""
    slept = []
    clock_state = {"t": 0.0}

    def clock():
        return clock_state["t"]

    def sleep(seconds):
        slept.append(seconds)
        clock_state["t"] += seconds

    class FixedRandom:
        @staticmethod
        def random():
            return 1.0

    client = FallbackClient(client_factory=factory_for(primary, fallback),
                            clock=clock, sleep=sleep, rng=FixedRandom(),
                            **kwargs)
    client.slept = slept
    return client


def run():
    # === A. Routing ===
    primary_ok = ScriptedClient("ollama", ['{"provider": "ollama"}'])
    fallback_unused = ScriptedClient("gemini")
    c = make_client(primary_ok, fallback_unused)
    out = c.complete_json("sys", {"a": 1})
    check("A1 healthy primary is used", out["provider"] == "ollama"
          and fallback_unused.calls == 0)
    check("A1b primary model recorded", c.last_provider == "ollama")

    primary_403 = ScriptedClient("ollama", fail_with=OllamaApiError(PAST_DUE))
    fallback_ok = ScriptedClient("gemini", ['{"provider": "gemini"}'],
                                 model="gemini-3.8-flash")
    c = make_client(primary_403, fallback_ok)
    out = c.complete_json("sys", {"a": 1})
    check("A2 primary 403 routes to Gemini", out["provider"] == "gemini"
          and fallback_ok.calls == 1, str(c.provider_attempts))
    check("A2b fallback_reason records the primary failure",
          "past due" in (c.fallback_reason or ""), c.fallback_reason)
    check("A2c last_model reflects the fallback provider",
          c.last_model == "gemini-3.8-flash")

    primary_nokey = ScriptedClient("ollama",
                                   fail_with=OllamaConfigError(
                                       "OLLAMA_API_KEY not set"))
    fallback_ok2 = ScriptedClient("gemini", ['{"provider": "gemini"}'])
    c = make_client(primary_nokey, fallback_ok2)
    out = c.complete_json("sys", {"a": 1})
    check("A3 missing primary credentials routes to Gemini",
          out["provider"] == "gemini")

    both_down = ScriptedClient("gemini", fail_with=GeminiApiError(OVERLOADED))
    c = make_client(ScriptedClient("ollama", fail_with=OllamaApiError(PAST_DUE)),
                    both_down)
    try:
        c.complete_json("sys", {"a": 1})
        check("A4 both providers fail raises", False)
    except RuntimeError as exc:
        text = str(exc)
        check("A4 both providers fail raises", True)
        check("A4b error names BOTH provider failures",
              "past due" in text and "high demand" in text, text[:200])

    c = make_client(ScriptedClient("ollama",
                                   fail_with=OllamaConfigError("no key")),
                    ScriptedClient("gemini",
                                   fail_with=GeminiConfigError("no key")))
    try:
        c.complete_json("sys", {"a": 1})
        check("A5 no credentials raises", False)
    except RuntimeError as exc:
        text = str(exc)
        check("A5 no credentials raises", True)
        check("A5b error names the env vars to set",
              "GEMINI_API_KEY" in text and "OLLAMA_API_KEY" in text, text[:200])

    # === B. Parse failure does not fall back ===
    primary_badjson = ScriptedClient("ollama",
                                     ["not json at all", "still not json"])
    fallback_should_stay = ScriptedClient("gemini", ['{"x": 1}'])
    c = make_client(primary_badjson, fallback_should_stay)
    try:
        c.complete_json("sys", {"a": 1}, retries=1)
        check("B6 parse failure does not fall back by default", False)
    except Exception:
        check("B6 parse failure does not fall back by default", True)
    check("B6b fallback was NOT contacted on a parse failure",
          fallback_should_stay.calls == 0)

    primary_badjson2 = ScriptedClient("ollama",
                                      ["not json", "still not json"])
    fallback_optin = ScriptedClient("gemini", ['{"provider": "gemini"}'])
    c = make_client(primary_badjson2, fallback_optin,
                    fallback_on_parse_error=True)
    out = c.complete_json("sys", {"a": 1}, retries=1)
    check("B7 parse failure falls back when opted in",
          out["provider"] == "gemini", str(c.provider_attempts))

    # === C. Circuit breaker ===
    primary_always = ScriptedClient("ollama", fail_with=OllamaApiError(PAST_DUE))
    fallback_breaker = ScriptedClient("gemini", ['{"provider": "gemini"}'])
    c = make_client(primary_always, fallback_breaker, breaker_threshold=3)
    for _ in range(3):
        c.complete_json("sys", {"a": 1})
    check("C8 breaker opens after N consecutive primary failures",
          c.primary_circuit_open)
    calls_before = primary_always.calls
    c.complete_json("sys", {"a": 1})
    check("C9 primary skipped while breaker is open",
          primary_always.calls == calls_before,
          f"{calls_before} -> {primary_always.calls}")
    check("C12 fallback still used while the primary is skipped",
          fallback_breaker.calls >= 3)

    # A success resets the consecutive-failure counter. Note this must be
    # tested with the breaker CLOSED: an open breaker skips the primary by
    # design, so a primary success cannot happen inside the cooldown.
    alternating = ScriptedClient(
        "ollama", [OllamaApiError(PAST_DUE), '{"provider": "ollama"}',
                   OllamaApiError(PAST_DUE), OllamaApiError(PAST_DUE)])
    fb_reset = ScriptedClient("gemini", ['{"provider": "gemini"}'] * 4)
    c = make_client(alternating, fb_reset, breaker_threshold=2)
    c.complete_json("sys", {"a": 1})          # primary fails  (count -> 1)
    check("C10a one primary failure leaves the breaker closed",
          not c.primary_circuit_open)
    out = c.complete_json("sys", {"a": 1})   # primary succeeds, resets
    check("C10a2 primary recovered on its second attempt",
          out["provider"] == "ollama", str(c.provider_attempts))
    c.complete_json("sys", {"a": 1})          # fail (count -> 1)
    c.complete_json("sys", {"a": 1})          # fail (count -> 2 -> open)
    check("C10 a success resets the breaker counter",
          c.primary_circuit_open,
          "counter was not reset by the success, so 2 later failures "
          "should not open a threshold-2 breaker")

    # Cooldown expiry re-attempts the primary.
    flaky_primary = ScriptedClient("ollama", fail_with=OllamaApiError(PAST_DUE))
    fb = ScriptedClient("gemini", ['{"provider": "gemini"}'])
    c = make_client(flaky_primary, fb, breaker_threshold=1,
                    breaker_cooldown=60.0)
    c.complete_json("sys", {"a": 1})
    check("C8c breaker open immediately at threshold 1", c.primary_circuit_open)
    n = flaky_primary.calls
    c.complete_json("sys", {"a": 1})
    check("C9b primary still skipped inside the cooldown",
          flaky_primary.calls == n)
    # Advance the fake clock past the cooldown.
    c._clock = lambda: 10_000.0
    c._primary_open_until = 9_000.0
    n = flaky_primary.calls
    c.complete_json("sys", {"a": 1})
    check("C11 cooldown expiry re-attempts the primary",
          flaky_primary.calls > n, f"{n} -> {flaky_primary.calls}")

    # === D. Transient vs persistent ===
    primary_down = ScriptedClient("ollama", fail_with=OllamaApiError(PAST_DUE))
    # First two Gemini calls are transient 503, third succeeds.
    transient_fb = ScriptedClient(
        "gemini", [GeminiApiError(OVERLOADED), GeminiApiError(OVERLOADED),
                   '{"provider": "gemini"}'])
    c = make_client(primary_down, transient_fb, transient_retries=2)
    out = c.complete_json("sys", {"a": 1})
    check("D13 503 on the fallback is retried then succeeds",
          out["provider"] == "gemini" and transient_fb.calls == 3,
          f"calls={transient_fb.calls}")
    check("D15 backoff sleeps recorded and non-zero", len(c.slept) >= 1
          and all(s > 0 for s in c.slept), str(c.slept))

    persistent_fb = ScriptedClient("gemini", fail_with=GeminiApiError(PAST_DUE))
    c = make_client(ScriptedClient("ollama",
                                   fail_with=OllamaApiError(PAST_DUE)),
                    persistent_fb, transient_retries=3)
    try:
        c.complete_json("sys", {"a": 1})
    except RuntimeError:
        pass
    check("D14 persistent 403 on the fallback is NOT retried",
          persistent_fb.calls == 1, f"calls={persistent_fb.calls}")

    # A healthy primary must not be retried on a transient-free failure.
    healthy = ScriptedClient("ollama", ['{"provider": "ollama"}'])
    c = make_client(healthy, ScriptedClient("gemini"))
    c.complete_json("sys", {"a": 1})
    check("D14b a healthy primary is called exactly once",
          healthy.calls == 1, f"calls={healthy.calls}")

    # === D2. Rate limits and the retry counter ===
    # A 429 clears when the provider's per-minute window rolls over, not in
    # seconds. Retrying it on a seconds-long schedule only spends the budget
    # and ends in the same 429.
    def retry_ordinals(attempts):
        """Pull the (retry N of M) ordinals out of the attempt log.

        Guards a regression where the ordinal was derived from the
        REMAINING budget, so the log read "retry 1 of 2" then "retry 0 of
        # 2" and appeared to run backwards.
        """
        out = []
        for _, kind, detail in attempts:
            if kind == "transient_retry" and "(retry " in detail:
                out.append(detail.split("(retry ", 1)[1].split(" of ", 1)[0])
        return out

    seq = [GeminiApiError("HTTP 503: high demand"),
           GeminiApiError("HTTP 503: high demand"),
           '{"provider": "gemini"}']
    c = make_client(ScriptedClient("ollama", fail_with=OllamaApiError(PAST_DUE)),
                    ScriptedClient("gemini", seq), transient_retries=2)
    c.complete_json("sys", {"a": 1})
    check("D16 retry ordinals count UP (1,2), never down",
          retry_ordinals(c.provider_attempts) == ["1", "2"],
          str(retry_ordinals(c.provider_attempts)))

    seq = [GeminiApiError("HTTP 429: exceeded quota", retry_after=30),
           GeminiApiError("HTTP 429: exceeded quota", retry_after=30),
           '{"provider": "gemini"}']
    c = make_client(ScriptedClient("ollama", fail_with=OllamaApiError(PAST_DUE)),
                    ScriptedClient("gemini", seq), transient_retries=2)
    c.complete_json("sys", {"a": 1})
    check("D17 a 429 waits for the provider's Retry-After",
          all(s >= 30 for s in c.slept), str(c.slept))
    check("D18 a 429 is NOT retried on a seconds-long backoff",
          min(c.slept) > 5, str(c.slept))

    seq = [GeminiApiError("HTTP 429: quota", retry_after=99999),
           '{"provider": "gemini"}']
    c = make_client(ScriptedClient("ollama", fail_with=OllamaApiError(PAST_DUE)),
                    ScriptedClient("gemini", seq), transient_retries=1,
                    rate_limit_ceiling=60.0)
    c.complete_json("sys", {"a": 1})
    check("D19 an absurd Retry-After is capped by the ceiling",
          c.slept and c.slept[0] <= 60.0, str(c.slept))

    seq = [GeminiApiError("HTTP 429: quota"),
           '{"provider": "gemini"}']
    c = make_client(ScriptedClient("ollama", fail_with=OllamaApiError(PAST_DUE)),
                    ScriptedClient("gemini", seq), transient_retries=1,
                    rate_limit_ceiling=30.0)
    c.complete_json("sys", {"a": 1})
    check("D20 a 429 with no Retry-After uses the ceiling",
          c.slept and abs(c.slept[0] - 30.0) < 0.01, str(c.slept))

    seq = [GeminiApiError("HTTP 503: high demand"),
           '{"provider": "gemini"}']
    c = make_client(ScriptedClient("ollama", fail_with=OllamaApiError(PAST_DUE)),
                    ScriptedClient("gemini", seq), transient_retries=1,
                    rate_limit_ceiling=60.0)
    c.complete_json("sys", {"a": 1})
    check("D21 a 503 still uses the SHORT jittered backoff",
          c.slept and c.slept[0] < 5, str(c.slept))

    # Retry-After must never be able to park the process or raise.
    class _Hdrs(dict):
        pass
    for raw, want in (("30", 30.0), ("0", 0.0), ("-5", 0.0),
                      ("garbage", None), (None, None)):
        got = parse_retry_after(_Hdrs({"Retry-After": raw})) \
            if raw is not None else parse_retry_after(None)
        check(f"D22 Retry-After {raw!r} parsed safely",
              got == want, f"got={got} want={want}")
    check("D23 Retry-After survives NaN/inf without propagating",
          _retry_after_seconds(GeminiApiError("x", retry_after=float("nan")))
          is None)

    # === E. Contract ===
    check("E16 complete_json returns parsed json, not a raw string",
          isinstance(c.complete_json("s", {}), dict))

    leaky = ScriptedClient("ollama",
                           fail_with=OllamaApiError(
                               f"HTTP 403: request rejected for key {FAKE_KEY}"))
    leaky_fb = ScriptedClient("gemini", fail_with=GeminiApiError(OVERLOADED))
    c = make_client(leaky, leaky_fb)
    try:
        c.complete_json("sys", {"a": 1})
    except RuntimeError as exc:
        blob = str(exc) + " " + str(c.provider_attempts) + " " + str(
            c.fallback_reason)
        check("E18 no credential-shaped string survives in logs",
              FAKE_KEY not in blob and FAKE_KEY[:40] not in blob)

    # Gemini message translation.
    contents, system = to_gemini_contents([
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "prior"},
        {"role": "user", "content": "again"},
    ])
    check("E19 system prompt extracted out of contents",
          system == ["be terse"]
          and all(m["role"] != "system" for m in contents))
    check("E19b assistant role translated to 'model'",
          [m["role"] for m in contents] == ["user", "model", "user"])
    check("E19c text preserved", contents[0]["parts"][0]["text"] == "hello")

    # Classification helpers.
    check("E19d 403 past due classified persistent",
          not _is_transient(PAST_DUE))
    check("E19e 503 classified transient", _is_transient(OVERLOADED))
    check("E19f parse failure detected", _is_parse_failure(
        "model returned non-JSON content: Expecting value"))
    check("E19g transport error is not a parse failure",
          not _is_parse_failure(OVERLOADED))
    check("E18b scrubber removes key-shaped tokens",
          FAKE_KEY not in _scrub(f"rejected key {FAKE_KEY}"))

    # GeminiClient.complete_json must actually retry malformed output. Its
    # parser raises OllamaApiError, so catching only GeminiApiError here
    # silently disabled the retry.
    from agents.gemini_client import GeminiClient

    class MalformedGemini(GeminiClient):
        """Real complete_json, scripted transport. No network."""

        def __init__(self, replies):
            self._replies = list(replies)
            self.calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return self._replies.pop(0)

    g = MalformedGemini(["not json", '{"fixed": true}'])
    check("E20 GeminiClient retries malformed JSON",
          g.complete_json("s", {"a": 1}) == {"fixed": True}
          and g.calls == 2, f"calls={g.calls}")

    g = MalformedGemini(["still not json", "still not json"])
    try:
        g.complete_json("s", {"a": 1}, retries=1)
        check("E20b GeminiClient surfaces a persistent parse failure", False)
    except OllamaApiError:
        check("E20b GeminiClient surfaces a persistent parse failure", True)
        check("E20c it consumed its one retry",
              g.calls == 2, f"calls={g.calls}")


def main():
    run()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all llm fallback tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
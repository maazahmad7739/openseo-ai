"""Provider-agnostic LLM facade: Ollama Cloud primary, Gemini fallback.

The agent targets Ollama Cloud (per the roadmap decision that the agent
always uses a hosted provider). This facade makes the choice of provider a
runtime concern instead of a code change, so a provider outage or a billing
failure degrades throughput rather than stopping the pipeline.

ORDER: Ollama first, then Gemini. The primary is always attempted first so
the configured primary model remains the one that normally does the work.

WHAT TRIGGERS A FALLBACK
  - `OllamaApiError` / `OllamaConfigError`: the primary is unavailable or
    misconfigured (HTTP 403 on a lapsed subscription, 5xx, timeout,
    connection refused, missing credentials).
  - `GeminiApiError` on the fallback is NOT caught again: there is no third
    provider, and swallowing it would turn an outage into silence.

WHAT DOES NOT TRIGGER A FALLBACK
  - A model returning unparseable JSON. That is a prompt/model-quality
    problem, not an availability problem, and silently retrying it against
    a different model hides a regression behind a green run. Set
    `fallback_on_parse_error=True` to opt in.
  - `ConfigError` on BOTH providers: with no credentials anywhere there is
    nothing to fall back TO, and pretending otherwise would mask a real
    misconfiguration behind retries.

WHY THE FACADE CALLS `chat()` INSTEAD OF `complete_json()`
`OllamaClient.complete_json` raises the same `OllamaApiError` type for a
transport failure and for a parse failure, and on its retry path returns a
raw string instead of parsed JSON. Calling `chat()` directly makes the
failure stage unambiguous - which matters twice: the circuit breaker must
count AVAILABILITY failures only, and the return value must always be
parsed. This incidentally removes the raw-string trap that
`offpage_drafter.normalize_llm_response` has to defend against.

CIRCUIT BREAKER
A billing failure (HTTP 403) is PERSISTENT, not transient. Without a
breaker every subsequent call would pay a doomed HTTP round-trip before
falling back, doubling latency for the whole batch. After
`breaker_threshold` consecutive availability failures the primary is
skipped for `breaker_cooldown` seconds. The breaker is per-process and
resets immediately on a successful primary call.

RESILIENCE
`transient_retries` bounds retries inside a single call, with jittered
exponential backoff (`backoff_base`). Rate limits (429 / quota) are the one
case that needs a different SHAPE: they clear when the provider's per-minute
window rolls over, not in seconds, so they wait for the provider's
Retry-After header and otherwise up to `rate_limit_ceiling`. Retrying a 429
on a seconds-long schedule does not clear it, it just spends the budget.

SECRETS
API keys are read from the environment and placed only in request headers.
They are never logged, echoed, persisted, or included in any error message
raised here. Provider error DETAIL text is surfaced (it is provider text,
never request headers) because "which provider refused and why" is the
operational question, but it is scrubbed of anything key-shaped first.
"""

import json
import os
import random
import time

from agents.gemini_client import (
    GeminiApiError,
    GeminiClient,
    GeminiConfigError,
)
from agents.ollama_client import (
    OllamaApiError,
    OllamaClient,
    OllamaConfigError,
    parse_json_content,
)

# Populated by env_loader; present so `.env` additions are discoverable.
GEMINI_KEY_ENV = "GEMINI_API_KEY"
OLLAMA_KEY_ENV = "OLLAMA_API_KEY"

DEFAULT_BREAKER_THRESHOLD = 3
DEFAULT_BREAKER_COOLDOWN = 300.0
DEFAULT_TRANSIENT_RETRIES = 2
DEFAULT_BACKOFF_BASE = 1.5

# A 429 is a RATE limit (per-minute), not a momentary blip, so the seconds-
# scale exponential backoff above is the wrong shape for it: measured against
# a real provider it re-collided within the same rate-limit window and turned
# one throttled call into a guaranteed failure. Rate limits get their own,
# much longer ceiling, and the provider's own Retry-After wins when it sends
# one. Cap rather than trust it blindly - a hostile or buggy header must not
# park the process for an hour.
DEFAULT_RATE_LIMIT_CEILING = 60.0
_RATE_LIMIT_MARKERS = ("http 429", "rate limit", "quota", "resource_exhausted")

# Marks "this attempt did not produce a result, move to the next provider".
_SENTINEL = object()

# Statuses that mean "try again shortly", as opposed to "this will never
# work". A 503/429 is retried; a 403/401 is NOT, because a lapsed
# subscription or a bad key will still be broken in thirty seconds and
# retrying only adds latency to a failure the operator already needs to see.
_TRANSIENT_MARKERS = (
    "http 429", "http 500", "http 502", "http 503", "http 504",
    "unavailable", "overloaded", "high demand", "rate limit",
    "resource_exhausted", "timeout", "timed out", "connection reset",
    "connection aborted", "connection failed",
)

# Persistent markers: never worth retrying inside one call.
_PERSISTENT_MARKERS = (
    "http 400", "http 401", "http 403", "http 404",
    "past due", "payment", "forbidden", "invalid",
)


def _is_transient(reason):
    lowered = (reason or "").lower()
    if any(marker in lowered for marker in _PERSISTENT_MARKERS):
        return False
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def _is_rate_limited(reason):
    """429 / quota / resource_exhausted - throttled, so wait longer.

    Checked separately from _is_transient because the two need very
    different backoff shapes: a 503 clears in seconds, a rate limit does
    not clear until the provider's window rolls over.
    """
    lowered = (reason or "").lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def _retry_after_seconds(exc):
    """Provider-stated Retry-After from an exception, if it carries one.

    Duck-typed rather than isinstance-checked so the fallback facade does
    not need to know every provider's error type, and so a test double
    raising a plain Exception still works.
    """
    value = getattr(exc, "retry_after", None)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None      # NaN / inf from a bad header
    return max(0.0, value)


def _backoff_delay(base, ordinal, rng, exc=None, reason="",
                   rate_limit_ceiling=DEFAULT_RATE_LIMIT_CEILING):
    """Seconds to wait before retry `ordinal` (1-based).

    Exponential with full jitter, so simultaneous clients do not re-collide
    on a fixed schedule. A rate limit overrides the shape entirely: it waits
    for the provider's stated Retry-After, else a long jittered value, else
    the ceiling - because a seconds-long retry against a per-minute window
    is wasted latency that still ends in the same 429.
    """
    delay = base ** max(0, ordinal - 1)
    delay *= 0.5 + rng.random()
    if not _is_rate_limited(reason):
        return delay
    stated = _retry_after_seconds(exc)
    if stated is None:
        # No Retry-After: use the full ceiling rather than the short
        # exponential, since waiting ~2s cannot clear a per-minute limit.
        return rate_limit_ceiling
    # Trust the provider's number, but never exceed the ceiling, and never
    # wait LESS than the jittered exponential already computed.
    return min(rate_limit_ceiling, max(delay, stated))


def _scrub(text):
    """Remove anything key-shaped from text that may reach a log.

    Provider error bodies echo request-adjacent text and misconfiguration
    messages sometimes quote env values. Scrubbing defensively means a
    credential cannot reach an operator's terminal through this module even
    if a future provider includes it in an error payload.
    """
    out = []
    for token in (text or "").split():
        if len(token) >= 24 and any(
                marker in token.upper()
                for marker in ("KEY", "TOKEN", "SECRET")):
            out.append("[redacted]")
        else:
            out.append(token)
    return " ".join(out)


class FallbackClient:
    """Ollama Cloud with an automatic Gemini fallback."""

    def __init__(self, config=None,
                 fallback_on_parse_error=False,
                 breaker_threshold=DEFAULT_BREAKER_THRESHOLD,
                 breaker_cooldown=DEFAULT_BREAKER_COOLDOWN,
                 transient_retries=DEFAULT_TRANSIENT_RETRIES,
                 backoff_base=DEFAULT_BACKOFF_BASE,
                 rate_limit_ceiling=DEFAULT_RATE_LIMIT_CEILING,
                 clock=time.monotonic, sleep=time.sleep, rng=random,
                 client_factory=None):
        self._config = config
        self._fallback_on_parse_error = fallback_on_parse_error
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown
        self._transient_retries = transient_retries
        self._backoff_base = backoff_base
        self._rate_limit_ceiling = rate_limit_ceiling
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        # Injection seam: tests substitute scripted clients so fallback
        # routing, the breaker, and backoff are testable with no network.
        self._client_factory = client_factory

        self._last_usage = {}
        self.last_provider = None
        self.last_model = None
        self.fallback_reason = None
        self.provider_attempts = []

        # Circuit-breaker state, per-process.
        self._primary_open_until = None
        self._consecutive_primary_failures = 0
        # Set once a ConfigError proves a provider is unusable this run;
        # never retried, so a missing key costs one check rather than a
        # stack trace per call.
        self._unavailable = {}

    # -- observability -------------------------------------------------
    @property
    def last_usage(self):
        return self._last_usage

    @property
    def model(self):
        return self.last_model

    @property
    def primary_circuit_open(self):
        if self._primary_open_until is None:
            return False
        return self._clock() < self._primary_open_until

    def reset_breaker(self):
        self._primary_open_until = None
        self._consecutive_primary_failures = 0

    # -- internals -----------------------------------------------------
    def _client(self, name):
        if self._client_factory is not None:
            return self._client_factory(name)
        if name == "ollama":
            return OllamaClient(self._config)
        return GeminiClient(self._config)

    def _record_primary_ok(self):
        self._consecutive_primary_failures = 0
        self._primary_open_until = None

    def _record_primary_failed(self, reason):
        self._consecutive_primary_failures += 1
        if self._consecutive_primary_failures >= self._breaker_threshold:
            self._primary_open_until = self._clock() + self._breaker_cooldown
            self._conavailable_note = reason

    def _unavailable_reason(self, name):
        return self._unavailable.get(name)

    # -- public API ----------------------------------------------------
    def chat(self, messages, temperature=0.2, json_mode=True,
             max_tokens=32768):
        """Raw text completion with the same fallback routing."""
        content, _ = self._run(
            lambda client: client.chat(messages=messages,
                                       temperature=temperature,
                                       json_mode=json_mode,
                                       max_tokens=max_tokens))
        return content

    def complete_json(self, system_prompt, user_payload, retries=1,
                      temperature=0.2):
        """JSON completion with fallback. Always returns PARSED JSON.

        Guarantees the contract that `OllamaClient.complete_json` does not:
        the retry path is parsed too, so a caller never receives a raw
        string and never has to defend against one. `temperature` rides
        through for callers that tune sampling per content kind.
        """
        payload, _ = self._run(
            lambda client: self._complete_json_via(client, system_prompt,
                                                   user_payload, retries,
                                                   temperature))
        return payload

    def _complete_json_via(self, client, system_prompt, user_payload, retries,
                           temperature=0.2):
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload,
                                                   default=str)},
        ]
        content = client.chat(messages=messages, temperature=temperature)
        try:
            return parse_json_content(content)
        except OllamaApiError:
            if retries <= 0:
                raise
            retry = client.chat(messages=messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "Your previous answer was cut off "
                 "or invalid JSON. Return ONLY the complete valid JSON "
                 "object, same schema, no truncation."},
            ])
            # Parsed, unlike the OllamaClient retry path.
            return parse_json_content(retry)

    def _run(self, call):
        """Try the primary, then the fallback. Returns (result, provider)."""
        order = ["ollama", "gemini"]
        config_errors = (OllamaConfigError, GeminiConfigError)
        availability_errors = (OllamaApiError, GeminiApiError)

        self.provider_attempts = []
        self.fallback_reason = None
        last_error = None
        # Distinguishes "a provider was tried and failed" from "no provider
        # could be tried at all". A ConfigError means the provider never got
        # a chance to serve, so it must NOT count as an attempt: otherwise a
        # total misconfiguration reports the generic "all providers failed"
        # instead of the actionable "set GEMINI_API_KEY or OLLAMA_API_KEY".
        served_any = False

        for name in order:
            if name in self._unavailable:
                self.provider_attempts.append(
                    (name, "skipped", _scrub(self._unavailable[name])))
                continue
            # Skip the primary while its breaker is open, but never skip the
            # fallback: that would turn a primary outage into a total one.
            if name == "ollama" and self.primary_circuit_open:
                self.provider_attempts.append(
                    ("ollama", "breaker_open",
                     f"skipped after {self._consecutive_primary_failures} "
                     f"consecutive failures"))
                self.fallback_reason = (
                    self.fallback_reason
                    or "primary circuit breaker open")
                continue

            try:
                client = self._client(name)
            except config_errors as exc:
                self._unavailable[name] = str(exc)
                last_error = exc
                self.provider_attempts.append((name, "config_error",
                                               _scrub(str(exc))))
                if name == "ollama":
                    self.fallback_reason = self.fallback_reason or _scrub(
                        str(exc))
                continue

            # Bounded retry for transient statuses only. Without this, a
            # momentary 503 on the FALLBACK takes down the whole run, which
            # defeats the purpose of having a fallback at all. Persistent
            # failures (403 past due, 401, 404) are never retried here.
            attempts_left = self._transient_retries
            retry_ordinal = 0
            while True:
                try:
                    result = call(client)
                    served_any = True
                    break
                except config_errors as exc:
                    self._unavailable[name] = str(exc)
                    last_error = exc
                    self.provider_attempts.append((name, "config_error",
                                                   _scrub(str(exc))))
                    result = _SENTINEL
                    break
                except availability_errors as exc:
                    # Reached the provider and it answered with a failure,
                    # so this counts as "the provider was exercised".
                    served_any = True
                    # A parse failure is raised by _complete_json_via as
                    # OllamaApiError, indistinguishable by type from a
                    # transport failure, so attribute by message shape.
                    reason = _scrub(str(exc))
                    last_error = exc
                    if _is_parse_failure(reason):
                        if not self._fallback_on_parse_error:
                            # Bad JSON from a healthy provider: a prompt /
                            # model-quality problem, not an outage. Surface
                            # it rather than routing to another model and
                            # hiding the regression behind a green run.
                            self.provider_attempts.append(
                                (name, "parse_error", reason))
                            raise
                        # Opted in: this provider has already had its own
                        # internal JSON retry consumed (see
                        # _complete_json_via). Hand off to the next provider
                        # rather than spending another attempt here, which
                        # is what "fall back on parse error" means.
                        self.provider_attempts.append(
                            (name, "parse_error", reason))
                        result = _SENTINEL
                        break
                    elif _is_transient(reason) and attempts_left > 0:
                        attempts_left -= 1
                        # Ordinal is counted SEPARATELY from
                        # attempts_left: decrementing first made the log
                        # read "retry 1 of 2" then "retry 0 of 2", which
                        # looks like the budget went backwards and hides
                        # how many calls were actually spent.
                        retry_ordinal += 1
                        delay = _backoff_delay(
                            self._backoff_base, retry_ordinal, self._rng,
                            exc=exc, reason=reason,
                            rate_limit_ceiling=self._rate_limit_ceiling)
                        self.provider_attempts.append(
                            (name, "transient_retry",
                             f"{reason} (retry {retry_ordinal} of "
                             f"{self._transient_retries} in {delay:.1f}s)"))
                        self._sleep(delay)
                        continue

                    self.provider_attempts.append((name, "api_error", reason))
                    if name == "ollama":
                        self.fallback_reason = (self.fallback_reason or reason)
                        self._record_primary_failed(reason)
                    result = _SENTINEL
                    break

            if result is _SENTINEL:
                continue

            # Success.
            if name == "ollama":
                self._record_primary_ok()
            self.last_provider = name
            self.last_model = getattr(client, "model", None)
            self._last_usage = dict(getattr(client, "last_usage", {}) or {})
            self.provider_attempts.append((name, "ok", ""))
            return result, name

        if not served_any and not self._unavailable:
            raise RuntimeError("no LLM provider was attempted")

        detail = f"; last error: {last_error}" if last_error else ""
        if not served_any:
            # No provider could even be CONSTRUCTED, so this is a
            # misconfiguration rather than an outage. It must name the
            # variables to set: "all providers failed" sends an operator
            # chasing a network problem that does not exist.
            reasons = "; ".join(
                f"{k}={v}" for k, v in self._unavailable.items()
                if not k.endswith(":parse"))
            raise RuntimeError(
                f"no LLM provider is configured ({reasons}){detail}. "
                f"Set {OLLAMA_KEY_ENV} or {GEMINI_KEY_ENV} in the "
                f"environment or .env file.")
        raise RuntimeError(
            f"all LLM providers failed{detail}. Attempts: "
            f"{self.provider_attempts}")


def _is_parse_failure(reason):
    """True when the raised error is the JSON PARSER, not the transport.

    `parse_json_content` raises a stable, distinctive message; transport
    errors always carry an HTTP status or a connection phrase.
    """
    lowered = (reason or "").lower()
    return ("non-json" in lowered or "json" in lowered
            and "http" not in lowered and "connection" not in lowered)


def resolve_client(config=None, **kwargs):
    """Build the facade. The single place call sites should import from.

    Kept as a function so tests can construct one without importing the
    class, matching `_llm_client()` in `fixes.generator`.
    """
    return FallbackClient(config=config, **kwargs)


def has_any_credentials():
    """True when at least one provider key is present in the environment."""
    return bool(os.environ.get(OLLAMA_KEY_ENV)
                or os.environ.get(GEMINI_KEY_ENV))
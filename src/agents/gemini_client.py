"""Gemini (Google Generative Language API) client — FALLBACK provider.

Secondary provider for the SEO agent. Same surface as
`agents.ollama_client.OllamaClient` (`chat`, `complete_json`, `last_usage`,
`model`) so `agents.llm_client.FallbackClient` can treat the two
interchangeably and every existing call site works unchanged.

Auth: `x-goog-api-key: <GEMINI_API_KEY>` against
https://generativelanguage.googleapis.com/v1beta.

Credential rules, matching ollama_client:
  - GEMINI_API_KEY is resolved at construction and raises
    GeminiConfigError loudly when missing.
  - The key is never logged, echoed, or persisted. It is only ever placed
    in a request header.

Message-shape note: the Ollama/OpenAI wire format uses roles
system/user/assistant; Gemini uses systemInstruction + user/model. The
translation happens in `to_gemini_contents`, so callers keep writing the
OpenAI shape they already write everywhere else.
"""

import json
import os
import urllib.request
import urllib.error

GEMINI_API_BASE_ENV = "GEMINI_API_BASE"
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_MODEL_ENV = "GEMINI_MODEL"

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
DEFAULT_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
HTTP_TIMEOUT_SECONDS = 300

# NOTE on model choice: Google retires model ids for new users, and a
# retired id fails with HTTP 404 whose body names the replacement (not a
# 401, so it is unambiguously a model problem rather than a bad key). The
# default below was verified working; override with GEMINI_MODEL and read
# the 404 detail when Google changes the lineup again.

# Gemini caps maxOutputTokens per model; 32768 is safe on 2.5-flash and
# matches the Ollama client's default so the two providers see the same
# budget and a long draft is not silently truncated by the fallback.
DEFAULT_MAX_TOKENS = 32768


class GeminiConfigError(Exception):
    pass


class GeminiApiError(Exception):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        # Seconds from the provider's Retry-After header, when present.
        # A 429 is a RATE limit measured per minute, so the caller's
        # exponential backoff is usually far shorter than the real wait and
        # simply re-collides; the provider states the actual number here.
        self.retry_after = retry_after


def resolve_gemini_config(config=None):
    cfg = config or {}
    base = cfg.get("gemini.api_base") or os.environ.get(
        GEMINI_API_BASE_ENV) or DEFAULT_GEMINI_BASE
    key = cfg.get("gemini.api_key") or os.environ.get(GEMINI_API_KEY_ENV)
    model = cfg.get("gemini.model") or os.environ.get(
        GEMINI_MODEL_ENV) or DEFAULT_GEMINI_MODEL
    if not key:
        raise GeminiConfigError(
            f"Gemini credentials not configured: set {GEMINI_API_KEY_ENV}")
    return {"base": base.rstrip("/"), "key": key, "model": model}


def to_gemini_contents(messages):
    """OpenAI-style messages -> Gemini `contents`.

    Gemini names the assistant role "model", and takes the system prompt as
    a separate `systemInstruction` field. System messages are extracted
    rather than inlined, because Gemini rejects a system turn inside
    `contents`.
    """
    system_parts = []
    contents = []
    for msg in messages or []:
        role = (msg or {}).get("role")
        text = (msg or {}).get("content") or ""
        if role == "system":
            if text.strip():
                system_parts.append(text)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        contents.append({"role": gemini_role,
                         "parts": [{"text": text}]})
    return contents, system_parts


class GeminiClient:
    """Thin generateContent client for the Gemini API."""

    def __init__(self, config=None):
        self._config = resolve_gemini_config(config)
        self._last_usage = {}

    @property
    def last_usage(self):
        return self._last_usage

    @property
    def model(self):
        return self._config["model"]

    def chat(self, messages, temperature=0.2, json_mode=True,
             max_tokens=DEFAULT_MAX_TOKENS):
        url = (f"{self._config['base']}/models/"
               f"{self._config['model']}:generateContent")
        contents, system_parts = to_gemini_contents(messages)
        generation_config = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if json_mode:
            # Ask for JSON at the protocol level rather than only in the
            # prompt. The Ollama client does the equivalent with
            # response_format, so both providers get the same contract.
            generation_config["responseMimeType"] = "application/json"
        body = {"contents": contents, "generationConfig": generation_config}
        if system_parts:
            body["systemInstruction"] = {"parts":
                                         [{"text": "\n\n".join(system_parts)}]}

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-goog-api-key", self._config["key"])
        try:
            with urllib.request.urlopen(req,
                                        timeout=HTTP_TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                pass
            # The detail is provider error text, never request headers, so
            # it cannot leak the key. Retry-After is read off the RESPONSE
            # headers separately and never interpolated into the message.
            from agents.ollama_client import parse_retry_after
            retry_after = parse_retry_after(getattr(exc, "headers", None))
            raise GeminiApiError(f"HTTP {exc.code}: {detail}",
                                 retry_after=retry_after) from None
        except urllib.error.URLError as exc:
            raise GeminiApiError(f"connection failed: {exc.reason}") from None

        usage = payload.get("usageMetadata") or {}
        if isinstance(usage, dict):
            self._last_usage = {
                "prompt_tokens": usage.get("promptTokenCount"),
                "completion_tokens": usage.get("candidatesTokenCount"),
                "model": self._config["model"],
            }
        else:
            self._last_usage = {}

        candidates = payload.get("candidates") or []
        if not candidates:
            raise GeminiApiError(
                f"no candidates in response: "
                f"{sorted(payload.keys())}")
        # A candidate can be blocked by the provider's own safety filter or
        # finish on MAX_TOKENS with no text. Report the reason rather than
        # returning an empty string, so the caller can distinguish
        # "truncated" from "refused".
        finish = candidates[0].get("finishReason") or ""
        if finish == "SAFETY":
            raise GeminiApiError("response blocked by provider safety filter")
        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        text = "".join(p.get("text") or "" for p in parts)
        if not text.strip():
            raise GeminiApiError(
                f"empty response (finishReason={finish or 'UNKNOWN'})")
        return text

    def complete_json(self, system_prompt, user_payload, retries=1,
                      temperature=0.2):
        """Send system + user JSON payload; parse the model's JSON answer.

        Same contract as `OllamaClient.complete_json`, including one retry
        with an explicit "valid JSON only" reminder. The retry also parses
        the answer, which `OllamaClient.complete_json` does NOT do on its
        retry path - that path returns the raw string, which is the
        behaviour `offpage_drafter.normalize_llm_response` defends against.
        `temperature` rides through for callers that tune sampling.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, default=str)},
        ]
        content = self.chat(messages=messages, temperature=temperature)
        # parse_json_content delegates to the Ollama parser, which signals
        # malformed output with OllamaApiError - NOT GeminiApiError. Catching
        # only the Gemini type here meant a malformed Gemini reply escaped
        # with no retry at all. Transport errors stay uncatchable here so they
        # propagate to the caller for provider fallback.
        from agents.ollama_client import OllamaApiError
        try:
            return parse_json_content(content)
        except OllamaApiError:
            if retries <= 0:
                raise
            retry = self.chat(messages=messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "Your previous answer was cut off "
                 "or invalid JSON. Return ONLY the complete valid JSON "
                 "object, same schema, no truncation."},
            ])
            # Deliberately parsed, unlike the Ollama retry path.
            return parse_json_content(retry)


def parse_json_content(content):
    """Parse the model's content as JSON, tolerating ```json fences.

    Delegates to the Ollama parser so both providers tolerate exactly the
    same malformed-model-output shapes - one parser, one behaviour.
    """
    from agents.ollama_client import parse_json_content as _parse
    return _parse(content)
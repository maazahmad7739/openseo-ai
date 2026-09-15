"""Ollama Cloud HTTP client — hosted LLM provider for the SEO agent.

Auth: Authorization: Bearer <OLLAMA_API_KEY> against OLLAMA_API_BASE
(OpenAI-compatible chat completions endpoint, e.g. https://ollama.com/v1).

Credential rules:
  - OLLAMA_API_KEY and OLLAMA_API_BASE are resolved at client construction
    and raise OllamaConfigError loudly when missing (no silent fallback, no
    offline stub — per roadmap decision the agent always targets the hosted
    API).
  - The key is never logged, echoed, or persisted.
"""

import json
import os
import urllib.request
import urllib.error

OLLAMA_API_BASE_ENV = "OLLAMA_API_BASE"
OLLAMA_API_KEY_ENV = "OLLAMA_API_KEY"
OLLAMA_MODEL_ENV = "OLLAMA_MODEL"
DEFAULT_MODEL = "gpt-oss:120b"
HTTP_TIMEOUT_SECONDS = 300


class OllamaConfigError(Exception):
    pass


class OllamaApiError(Exception):
    pass


def resolve_ollama_config(config=None):
    cfg = config or {}
    base = cfg.get("ollama.api_base") or os.environ.get(OLLAMA_API_BASE_ENV)
    key = cfg.get("ollama.api_key") or os.environ.get(OLLAMA_API_KEY_ENV)
    model = cfg.get("ollama.model") or os.environ.get(OLLAMA_MODEL_ENV) or DEFAULT_MODEL
    missing = [name for name, value in (
        (OLLAMA_API_BASE_ENV, base), (OLLAMA_API_KEY_ENV, key)) if not value]
    if missing:
        raise OllamaConfigError(
            f"Ollama Cloud credentials not configured: set {', '.join(missing)} "
            "(hosted Ollama Cloud API; no offline fallback per roadmap decision). "
            "The agent will not run until these are provided."
        )
    return {"base": base.rstrip("/"), "key": key, "model": model}


class OllamaClient:
    """Thin chat-completions client for Ollama Cloud."""

    def __init__(self, config=None):
        self._config = resolve_ollama_config(config)
        self._last_usage = {}

    @property
    def last_usage(self):
        return self._last_usage

    @property
    def model(self):
        return self._config["model"]

    def chat(self, messages, temperature=0.2, json_mode=True, max_tokens=32768):
        url = f"{self._config['base']}/chat/completions"
        body = {
            "model": self._config["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self._config['key']}")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise OllamaApiError(f"HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise OllamaApiError(f"connection failed: {exc.reason}") from None
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise OllamaApiError(f"unexpected response shape: {list(payload.keys())}") from None
        usage = payload.get("usage") or {}
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens") or usage.get("prompt_eval_count")
            completion_tokens = usage.get("completion_tokens") or usage.get("eval_count")
            self._last_usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "model": self._config["model"],
            }
        else:
            self._last_usage = {}
        content = message.get("content") or ""
        if not content.strip():
            # Some hosted models spend the budget on a reasoning field and
            # return empty content; fall back to reasoning text before failing.
            reasoning = message.get("reasoning") or ""
            if reasoning.strip():
                return reasoning
        return content

    def complete_json(self, system_prompt, user_payload, retries=1):
        """Send system + user JSON payload; parse the model's JSON answer.

        One retry with an explicit "valid JSON only" reminder when the model
        returns truncated/malformed JSON (hit the token ceiling, etc.).
        """
        content = self.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, default=str)},
            ],
        )
        try:
            return parse_json_content(content)
        except OllamaApiError:
            if retries <= 0:
                raise
            return self.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, default=str)},
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "Your previous answer was cut off or invalid JSON. "
                     "Return ONLY the complete valid JSON object, same schema, no truncation."},
                ],
            )


def parse_json_content(content):
    """Parse the model's content as JSON, tolerating ```json fences."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except ValueError as exc:
        raise OllamaApiError(f"model returned non-JSON content: {str(exc)}") from None
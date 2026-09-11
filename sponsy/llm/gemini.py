"""Gemini, over REST.

Written against the REST API rather than the SDK on purpose: it is ~200 lines,
has no version drift, and makes the request/response shape visible when a
tool-call round trip misbehaves.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from datetime import date
from typing import Any

from .base import BaseLLM, LLMError, LLMResponse, QuotaExhausted, ToolCall, mask

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
RETRY_STATUS = {429, 500, 502, 503, 504}

# proto enum names, uppercased on the way out
_TYPES = {"string": "STRING", "number": "NUMBER", "integer": "INTEGER",
          "boolean": "BOOLEAN", "array": "ARRAY", "object": "OBJECT"}


def _upper_types(schema: Any) -> Any:
    if isinstance(schema, dict):
        out = {}
        for key, val in schema.items():
            if key == "type" and isinstance(val, str):
                out[key] = _TYPES.get(val.lower(), val.upper())
            elif key in ("additionalProperties", "$schema"):
                continue                    # not in Gemini's schema subset
            else:
                out[key] = _upper_types(val)
        return out
    if isinstance(schema, list):
        return [_upper_types(item) for item in schema]
    return schema


def retry_delay_of(body: str) -> float | None:
    """Google returns a RetryInfo detail with retryDelay like "27s". Obeying it
    is the difference between working and not working on a free-tier key."""
    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s?"', body or "")
    return float(match.group(1)) if match else None


def is_daily_quota(body: str) -> bool:
    return "PerDay" in (body or "")


class GeminiLLM(BaseLLM):
    name = "gemini"

    # Limits are per key, so both the pacing window and the exhausted set are
    # tracked per key and shared across instances (default and cheap models).
    _next_slot: dict[str, float] = {}
    _exhausted: dict[str, str] = {}          # key -> ISO date it ran out

    def __init__(self, cfg, model: str | None = None, max_attempts: int = 6,
                 db=None):
        self.cfg = cfg
        default, _cheap = cfg.resolved_models()
        self.model = model or default
        self.max_attempts = max_attempts
        self.min_interval = float(getattr(cfg, "min_interval_s", 0) or 0)
        self.keys = list(cfg.api_keys) or [""]
        self.db = db
        # Models that refuse to have thinking switched off, learned from the
        # first 400 rather than hardcoded, so a new model does not need a code
        # change to work here.
        self._no_thinking: set[str] = set()
        self._load_exhausted()

    # -- key rotation ------------------------------------------------------
    @staticmethod
    def _key_id(key: str) -> str:
        """Identify a key in storage without storing the key itself."""
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def _load_exhausted(self) -> None:
        if self.db is None:
            return
        try:
            stored = json.loads(self.db.memory_get("llm", "exhausted") or "{}")
        except (ValueError, TypeError):
            return
        today = self._today()
        for key in self.keys:
            if stored.get(self._key_id(key)) == today:
                GeminiLLM._exhausted[key] = today

    def current_key(self) -> str | None:
        today = self._today()
        for key in self.keys:
            if GeminiLLM._exhausted.get(key) != today:
                return key
        return None

    def retire_key(self, key: str) -> None:
        today = self._today()
        GeminiLLM._exhausted[key] = today
        if self.db is None:
            return
        try:
            stored = json.loads(self.db.memory_get("llm", "exhausted") or "{}")
        except (ValueError, TypeError):
            stored = {}
        stored = {k: v for k, v in stored.items() if v == today}
        stored[self._key_id(key)] = today
        self.db.memory_set("llm", "exhausted", json.dumps(stored))

    def _throttle(self, key: str) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        wait = GeminiLLM._next_slot.get(key, 0.0) - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        GeminiLLM._next_slot[key] = now + self.min_interval

    # -- message conversion ------------------------------------------------
    @staticmethod
    def _to_contents(messages: list[dict]) -> list[dict]:
        contents: list[dict] = []
        for msg in messages:
            role = msg["role"]
            if role == "tool":
                response = msg.get("response")
                if not isinstance(response, dict):
                    response = {"result": response}
                contents.append({"role": "user", "parts": [{"functionResponse": {
                    "name": msg["name"], "response": response}}]})
                continue
            parts: list[dict] = list(msg.get("parts") or [])
            if not parts:
                if msg.get("text"):
                    parts.append({"text": msg["text"]})
                for call in msg.get("tool_calls", []):
                    part = {"functionCall": {"name": call.name, "args": call.args}}
                    if getattr(call, "thought_signature", ""):
                        part["thoughtSignature"] = call.thought_signature
                    parts.append(part)
            if not parts:
                parts = [{"text": ""}]
            contents.append({"role": "model" if role == "model" else "user",
                             "parts": parts})
        return contents

    def _cost(self, prompt_tokens: int, output_tokens: int, model: str) -> float:
        in_price, out_price = self.cfg.price(model)
        return (prompt_tokens * in_price + output_tokens * out_price) / 1_000_000

    # -- call --------------------------------------------------------------
    def generate(self, *, system: str, messages: list[dict],
                 tools: list[dict] | None = None,
                 response_json: bool = False,
                 temperature: float = 0.3,
                 model: str | None = None) -> LLMResponse:
        import requests  # noqa: PLC0415

        model = model or self.model
        gen_config: dict[str, Any] = {"temperature": temperature}
        if self.cfg.thinking_budget is not None and model not in self._no_thinking:
            gen_config["thinkingConfig"] = {"thinkingBudget": self.cfg.thinking_budget}
        if response_json and not tools:
            # Gemini rejects responseMimeType together with function declarations.
            gen_config["responseMimeType"] = "application/json"

        body: dict[str, Any] = {"contents": self._to_contents(messages),
                                "generationConfig": gen_config}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t["name"], "description": t["description"],
                 "parameters": _upper_types(t["parameters"])} for t in tools]}]
            body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        url = "{}/{}:generateContent".format(API_ROOT, model)
        last_error = ""
        attempt = 0
        while attempt < self.max_attempts:
            key = self.current_key()
            if key is None:
                raise QuotaExhausted(
                    "every configured Gemini key has hit its daily cap for {} "
                    "({} key(s)). Quotas reset on Google's clock, not yours."
                    .format(model, len(self.keys)))
            self._throttle(key)
            wait = min(2 ** attempt, 8)
            try:
                resp = requests.post(url, json=body, timeout=90,
                                     headers={"x-goog-api-key": key,
                                              "Content-Type": "application/json"})
            except Exception as exc:                       # network flake
                last_error = "{}: {}".format(type(exc).__name__, exc)
            else:
                if resp.status_code == 200:
                    return self._parse(resp.json(), model)
                last_error = "HTTP {}: {}".format(resp.status_code, resp.text[:300])
                if (resp.status_code == 400 and "thinkingConfig" in gen_config
                        and "INVALID_ARGUMENT" in resp.text):
                    # Not every model lets thinking be switched off. The lite
                    # models reject thinkingBudget:0 outright with a bare
                    # "invalid argument", which silently broke every cheap-model
                    # call -- grounding, reply classification, sponsor
                    # extraction -- while the main model accepted the same
                    # setting. Drop the budget and try once more rather than
                    # making the operator work that out from a 400.
                    dropped = gen_config.pop("thinkingConfig")
                    self._no_thinking.add(model)     # do not re-pay this 400
                    print("[llm] {} rejected thinkingBudget={}; retrying without it"
                          .format(model, dropped.get("thinkingBudget")))
                    continue
                if resp.status_code not in RETRY_STATUS:
                    raise LLMError(last_error)
                if resp.status_code == 429:
                    if is_daily_quota(resp.text):
                        self.retire_key(key)
                        remaining = self.current_key()
                        print("[llm] key {} is out of daily quota for {}; {}".format(
                            mask(key), model,
                            "rotating to " + mask(remaining) if remaining
                            else "no keys left"))
                        if remaining is not None:
                            continue
                        raise QuotaExhausted(
                            "daily quota exhausted on all {} key(s) for {}. Enable "
                            "billing, or use a different LLM_MODEL (the quota is "
                            "per model).".format(len(self.keys), model))
                    wait = retry_delay_of(resp.text) or min(8 * 2 ** attempt, 65)
                    print("[llm] key {} rate limited, waiting {:.0f}s (attempt {}/{})"
                          .format(mask(key), wait, attempt + 1, self.max_attempts))
            attempt += 1
            time.sleep(wait + random.uniform(0, 0.75))
        raise LLMError("gemini failed after {} attempts: {}".format(
            self.max_attempts, last_error))

    def _parse(self, data: dict, model: str) -> LLMResponse:
        usage = data.get("usageMetadata", {})
        prompt_tokens = int(usage.get("promptTokenCount", 0))
        output_tokens = (int(usage.get("candidatesTokenCount", 0))
                         + int(usage.get("thoughtsTokenCount", 0)))
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError("no candidates (blocked?): " + json.dumps(data)[:300])
        cand = candidates[0]
        raw_parts = cand.get("content", {}).get("parts", []) or []
        text_parts, calls = [], []
        for part in raw_parts:
            if part.get("thought"):
                continue
            if "text" in part:
                text_parts.append(part["text"])
            if "functionCall" in part:
                fc = part["functionCall"]
                calls.append(ToolCall(name=fc.get("name", ""), args=fc.get("args") or {},
                                      thought_signature=part.get("thoughtSignature", "")))
        return LLMResponse(
            text="".join(text_parts).strip(), tool_calls=calls,
            prompt_tokens=prompt_tokens, output_tokens=output_tokens,
            cost_usd=self._cost(prompt_tokens, output_tokens, model),
            finish_reason=cand.get("finishReason", ""), raw=data, parts=raw_parts)

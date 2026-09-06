"""Any OpenAI-compatible /chat/completions endpoint.

One client covers OpenAI, NVIDIA NIM (build.nvidia.com), Groq, OpenRouter and
Featherless: set OPENAI_BASE_URL and OPENAI_API_KEY. Tool calling uses the
standard `tools=[{"type":"function",...}]` shape.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

from .base import BaseLLM, LLMError, LLMResponse, QuotaExhausted, ToolCall

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}
# Models that reject a non-default temperature. Sending none is always safe.
NO_TEMPERATURE_PREFIXES = ("gpt-5", "o1", "o3", "o4")


class OpenAILLM(BaseLLM):
    name = "openai"

    def __init__(self, cfg, model: str | None = None, max_attempts: int = 5,
                 db=None):
        self.cfg = cfg
        default, _cheap = cfg.resolved_models()
        self.model = model or default
        self.base_url = (cfg.openai_base_url or "https://api.openai.com/v1").rstrip("/")
        self.api_key = cfg.openai_api_key
        self.max_attempts = max_attempts
        self.db = db

    # -- message conversion ------------------------------------------------
    @staticmethod
    def _to_messages(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for i, msg in enumerate(messages):
            role = msg["role"]
            if role == "user":
                out.append({"role": "user", "content": msg.get("text", "")})
            elif role == "model":
                entry: dict[str, Any] = {"role": "assistant",
                                         "content": msg.get("text") or None}
                calls = msg.get("tool_calls") or []
                if calls:
                    entry["tool_calls"] = [{
                        "id": c.id or "call_{}_{}".format(i, j), "type": "function",
                        "function": {"name": c.name,
                                     "arguments": json.dumps(c.args, ensure_ascii=False)}}
                        for j, c in enumerate(calls)]
                out.append(entry)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": msg.get("id") or "",
                            "content": json.dumps(msg.get("response"), ensure_ascii=False,
                                                  default=str)})
        return out

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
        body: dict[str, Any] = {"model": model,
                                "messages": self._to_messages(system, messages)}
        if not model.lower().startswith(NO_TEMPERATURE_PREFIXES):
            body["temperature"] = temperature
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["parameters"]}} for t in tools]
            body["tool_choice"] = "auto"
        elif response_json:
            body["response_format"] = {"type": "json_object"}

        url = self.base_url + "/chat/completions"
        headers = {"Authorization": "Bearer " + self.api_key,
                   "Content-Type": "application/json"}
        last_error = ""
        for attempt in range(self.max_attempts):
            try:
                resp = requests.post(url, json=body, headers=headers, timeout=120)
            except Exception as exc:
                last_error = "{}: {}".format(type(exc).__name__, exc)
            else:
                if resp.status_code == 200:
                    return self._parse(resp.json(), model)
                last_error = "HTTP {}: {}".format(resp.status_code, resp.text[:300])
                if resp.status_code == 429 and "quota" in resp.text.lower() \
                        and "billing" in resp.text.lower():
                    raise QuotaExhausted(last_error)
                if resp.status_code == 400 and "response_format" in resp.text \
                        and "response_format" in body:
                    body.pop("response_format")        # endpoint lacks JSON mode
                    continue
                if resp.status_code not in RETRY_STATUS:
                    raise LLMError(last_error)
                retry_after = resp.headers.get("retry-after")
                wait = float(retry_after) if retry_after and retry_after.isdigit() \
                    else min(2 ** attempt, 20)
                print("[llm] {} retrying in {:.0f}s (attempt {}/{})".format(
                    last_error[:80], wait, attempt + 1, self.max_attempts))
                time.sleep(wait + random.uniform(0, 0.5))
                continue
            time.sleep(min(2 ** attempt, 8))
        raise LLMError("openai-compatible endpoint failed after {} attempts: {}"
                       .format(self.max_attempts, last_error))

    def _parse(self, data: dict, model: str) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("no choices in response: " + json.dumps(data)[:300])
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):                       # some proxies return parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {"value": args}
            calls.append(ToolCall(name=fn.get("name", ""), args=args, id=tc.get("id", "")))
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        return LLMResponse(
            text=text.strip(), tool_calls=calls, prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            cost_usd=self._cost(prompt_tokens, output_tokens, model),
            finish_reason=choice.get("finish_reason", "") or "", raw=data)

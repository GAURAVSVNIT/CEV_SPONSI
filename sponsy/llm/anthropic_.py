"""Anthropic models through the official SDK (`pip install anthropic`).

Defaults to claude-opus-5 for research and drafting and claude-haiku-4-5 for
the cheap calls. Opus 5 runs adaptive thinking by default and rejects sampling
parameters, so no temperature is sent to any Anthropic model; the prompts do
the constraining. Server-side refusal fallbacks are on by default
(ANTHROPIC_FALLBACKS=0 turns them off).
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

from .base import BaseLLM, LLMError, LLMResponse, ToolCall

MAX_TOKENS = 16000


class AnthropicLLM(BaseLLM):
    name = "anthropic"

    def __init__(self, cfg, model: str | None = None, max_attempts: int = 4,
                 db=None):
        self.cfg = cfg
        default, _cheap = cfg.resolved_models()
        self.model = model or default
        self.max_attempts = max_attempts
        self.db = db
        self._client = None
        self._fallbacks_ok = bool(getattr(cfg, "anthropic_fallbacks", True))

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic  # noqa: PLC0415
            except ImportError as exc:
                raise LLMError("LLM_PROVIDER=anthropic needs `pip install anthropic`") \
                    from exc
            self._client = anthropic.Anthropic(api_key=self.cfg.anthropic_api_key,
                                               max_retries=2)
        return self._client

    # -- message conversion ------------------------------------------------
    @staticmethod
    def _to_messages(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for msg in messages:
            role = msg["role"]
            if role == "user":
                out.append({"role": "user", "content": msg.get("text", "")})
            elif role == "model":
                parts = msg.get("parts") or []
                if parts:
                    # Replay the turn as returned: thinking blocks must go back
                    # unchanged on the same model.
                    out.append({"role": "assistant", "content": parts})
                    continue
                content: list[dict] = []
                if msg.get("text"):
                    content.append({"type": "text", "text": msg["text"]})
                for call in msg.get("tool_calls") or []:
                    content.append({"type": "tool_use", "id": call.id,
                                    "name": call.name, "input": call.args})
                out.append({"role": "assistant", "content": content or
                            [{"type": "text", "text": ""}]})
            elif role == "tool":
                block = {"type": "tool_result", "tool_use_id": msg.get("id") or "",
                         "content": json.dumps(msg.get("response"), ensure_ascii=False,
                                               default=str)}
                if isinstance(msg.get("response"), dict) and "error" in msg["response"]:
                    block["is_error"] = True
                # every tool_result for one assistant turn goes in ONE user message
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
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
        import anthropic  # noqa: PLC0415

        model = model or self.model
        kwargs: dict[str, Any] = {"model": model, "max_tokens": MAX_TOKENS,
                                  "messages": self._to_messages(messages)}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [{"name": t["name"], "description": t["description"],
                                "input_schema": t["parameters"]} for t in tools]

        last_error = ""
        for attempt in range(self.max_attempts):
            try:
                response = self._create(kwargs)
                return self._parse(response, model)
            except anthropic.RateLimitError as exc:
                last_error = "rate limited: {}".format(exc)
                retry_after = exc.response.headers.get("retry-after") if exc.response else None
                wait = float(retry_after) if retry_after and retry_after.isdigit() \
                    else min(4 * 2 ** attempt, 60)
            except anthropic.APIStatusError as exc:
                last_error = "HTTP {}: {}".format(exc.status_code, exc.message)
                if exc.status_code < 500:
                    raise LLMError(last_error) from exc
                wait = min(2 ** attempt, 20)
            except anthropic.APIConnectionError as exc:
                last_error = "connection error: {}".format(exc)
                wait = min(2 ** attempt, 20)
            print("[llm] anthropic {} (attempt {}/{}), waiting {:.0f}s".format(
                last_error[:80], attempt + 1, self.max_attempts, wait))
            time.sleep(wait + random.uniform(0, 0.5))
        raise LLMError("anthropic failed after {} attempts: {}".format(
            self.max_attempts, last_error))

    def _create(self, kwargs: dict):
        """Refusal fallbacks first; a plain create when the SDK or model
        rejects the parameter."""
        if self._fallbacks_ok and kwargs["model"].startswith("claude-opus-5"):
            try:
                return self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default", **kwargs)
            except TypeError:
                self._fallbacks_ok = False          # older SDK: no such kwarg
            except Exception as exc:                # unknown beta / bad request
                name = type(exc).__name__
                if "BadRequest" not in name and "NotFound" not in name:
                    raise
                self._fallbacks_ok = False
        return self.client.messages.create(**kwargs)

    def _parse(self, response, model: str) -> LLMResponse:
        if getattr(response, "stop_reason", "") == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMError("model refused: {}".format(
                getattr(details, "explanation", "") or "no explanation"))
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(name=block.name, args=args, id=block.id))
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        return LLMResponse(
            text="".join(text_parts).strip(), tool_calls=calls,
            prompt_tokens=prompt_tokens, output_tokens=output_tokens,
            cost_usd=self._cost(prompt_tokens, output_tokens, model),
            finish_reason=getattr(response, "stop_reason", "") or "",
            raw=response, parts=list(response.content))

"""Shared model-client types. Provider clients live next to this file."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

FENCE = "`" * 3


class LLMError(RuntimeError):
    pass


class QuotaExhausted(LLMError):
    """A per-DAY quota, not a per-minute one. Waiting will not help today."""


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = ""                      # OpenAI / Anthropic call id; Gemini has none
    thought_signature: str = ""       # Gemini 3.x: must be echoed back verbatim


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    finish_reason: str = ""
    raw: Any = None
    # The model turn exactly as the provider returned it, so it can be replayed
    # verbatim (Gemini thought signatures, Anthropic thinking blocks).
    parts: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# lenient JSON parsing -- models wrap JSON in prose and code fences
# ---------------------------------------------------------------------------

def parse_json(text: str) -> Any:
    """Best-effort extraction of a JSON object from model output."""
    if not text:
        raise LLMError("empty model response")
    cleaned = text.strip()
    fence = re.search(FENCE + r"(?:json)?\s*(.+?)" + FENCE, cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        blob = cleaned[start:end + 1]
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            repaired = re.sub(r",\s*([}\]])", r"\1", blob)   # trailing commas
            try:
                return json.loads(repaired)
            except json.JSONDecodeError as exc:
                raise LLMError("unparseable JSON: " + str(exc)) from exc
    raise LLMError("no JSON object in response: " + repr(text[:200]))


def mask(key: str) -> str:
    return "..." + key[-4:] if len(key) > 4 else "(unset)"


class BaseLLM:
    name = "base"
    model: str = ""

    def generate(self, *, system: str, messages: list[dict],
                 tools: list[dict] | None = None,
                 response_json: bool = False,
                 temperature: float = 0.3,
                 model: str | None = None) -> LLMResponse:
        raise NotImplementedError

    def ping(self) -> str:
        """One tiny call; returns the model's text. Used by `doctor`."""
        resp = self.generate(system="", messages=[{"role": "user", "text": "Say OK."}],
                             temperature=0.0)
        return resp.text


# ---------------------------------------------------------------------------
# fake-model registry. Prompts start their system message with "TASK: <name>";
# the fake dispatches on it. Handlers are registered by sponsy.fakes.
# ---------------------------------------------------------------------------

FakeHandler = Callable[[str, list], Any]
FAKE_HANDLERS: dict[str, FakeHandler] = {}


def register_fake(task: str) -> Callable[[FakeHandler], FakeHandler]:
    def deco(fn: FakeHandler) -> FakeHandler:
        FAKE_HANDLERS[task] = fn
        return fn
    return deco


def task_of(system: str) -> str:
    match = re.match(r"\s*TASK:\s*(\S+)", system or "")
    return match.group(1) if match else ""

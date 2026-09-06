"""Fake model for tests, evals and offline demos.

This is a stub, not a simulator: it exists so the pipeline, the verifier, the
ladder and the approval loop can be exercised end to end without a key or a
network, and so a run against frozen sources is byte-stable.
"""

from __future__ import annotations

import json

from .base import FAKE_HANDLERS, BaseLLM, LLMError, LLMResponse, task_of


class FakeLLM(BaseLLM):
    name = "fake"
    model = "fake"

    def __init__(self, cfg=None, scripted: list[LLMResponse] | None = None):
        self.cfg = cfg
        self.scripted = list(scripted or [])
        self.calls: list[dict] = []

    def generate(self, *, system: str, messages: list[dict],
                 tools: list[dict] | None = None,
                 response_json: bool = False,
                 temperature: float = 0.3,
                 model: str | None = None) -> LLMResponse:
        self.calls.append({"system": system, "messages": messages, "tools": tools,
                           "model": model})
        if self.scripted:
            return self.scripted.pop(0)
        task = task_of(system)
        if not task and not tools:
            return LLMResponse(text="OK", prompt_tokens=3, output_tokens=1)
        handler = FAKE_HANDLERS.get(task)
        if handler is None:
            raise LLMError(
                "FakeLLM has no handler for task {!r}. Register one with "
                "@register_fake or script a response.".format(task))
        out = handler(system, messages)
        if isinstance(out, LLMResponse):
            return out
        if isinstance(out, (dict, list)):
            out = json.dumps(out, ensure_ascii=False)
        return LLMResponse(text=str(out), prompt_tokens=400, output_tokens=180,
                           cost_usd=0.0)

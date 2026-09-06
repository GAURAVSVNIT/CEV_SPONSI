"""Model access. One interface, four backends: Gemini, OpenAI-compatible,
Anthropic, and a fake for offline runs.

    llm = build_llm(cfg, db=db)
    resp = llm.generate(system=..., messages=[{"role": "user", "text": ...}])

Message format shared by every backend:
    {"role": "user",  "text": str}
    {"role": "model", "text": str, "tool_calls": [ToolCall], "parts": [raw]}
    {"role": "tool",  "name": str, "id": str, "response": dict}
"""

from __future__ import annotations

import os

from .base import (FAKE_HANDLERS, BaseLLM, LLMError, LLMResponse, QuotaExhausted,
                   ToolCall, mask, parse_json, register_fake, task_of)
from .fake import FakeLLM

__all__ = ["BaseLLM", "LLMError", "LLMResponse", "QuotaExhausted", "ToolCall",
           "FakeLLM", "FAKE_HANDLERS", "register_fake", "parse_json", "mask",
           "task_of", "build_llm"]


def build_llm(cfg, model: str | None = None, db=None) -> BaseLLM:
    provider = cfg.resolved_llm_provider()
    if provider == "fake" or os.environ.get("SPONSY_FAKE_LLM") == "1":
        from .. import fakes  # noqa: F401, PLC0415  (registers the handlers)
        return FakeLLM(cfg)
    if provider == "gemini":
        from .gemini import GeminiLLM  # noqa: PLC0415
        return GeminiLLM(cfg, model=model, db=db)
    if provider == "openai":
        from .openai_compat import OpenAILLM  # noqa: PLC0415
        return OpenAILLM(cfg, model=model, db=db)
    if provider == "anthropic":
        from .anthropic_ import AnthropicLLM  # noqa: PLC0415
        return AnthropicLLM(cfg, model=model, db=db)
    raise LLMError("unknown LLM provider: " + provider)

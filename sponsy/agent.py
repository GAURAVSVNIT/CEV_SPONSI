"""The bounded tool-calling loop.

Two entry points:

  run_agent()  -- multi-step, the model picks tools. Used where the model
                  genuinely has a choice to make (which pages to fetch).
  structured() -- one shot, JSON out, no tools. Used everywhere else:
                  drafting, humanizing, reply classification, groundedness.

Every tool call is logged to `actions` with its arguments, including the ones
that failed. The trace of what the agent tried is the interesting part.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .llm import BaseLLM, LLMError, LLMResponse, parse_json
from .tools import ToolRegistry


class BudgetExceeded(RuntimeError):
    def __init__(self, steps: int, cost: float, partial: str = ""):
        super().__init__("budget exhausted: steps={} cost=${:.4f}".format(steps, cost))
        self.steps, self.cost, self.partial = steps, cost, partial


@dataclass
class AgentRun:
    text: str = ""
    cost_usd: float = 0.0
    steps: int = 0
    elapsed_s: float = 0.0
    tool_calls: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    hit_budget: bool = False

    def json(self) -> dict:
        return parse_json(self.text)


def run_agent(llm: BaseLLM, *, system: str, user: str, registry: ToolRegistry,
              max_steps: int = 8, max_cost: float = 0.05,
              db=None, run_id: int | None = None, target: str = "",
              temperature: float = 0.2, raise_on_budget: bool = True) -> AgentRun:
    started = time.time()
    messages: list[dict] = [{"role": "user", "text": user}]
    run = AgentRun(messages=messages)

    while run.steps < max_steps and run.cost_usd < max_cost:
        resp: LLMResponse = llm.generate(
            system=system, messages=messages,
            tools=registry.declarations() or None,
            temperature=temperature)
        run.steps += 1
        run.cost_usd += resp.cost_usd

        if not resp.tool_calls:
            run.text = resp.text
            run.elapsed_s = time.time() - started
            return run

        messages.append({"role": "model", "text": resp.text,
                         "tool_calls": resp.tool_calls, "parts": resp.parts})
        for call in resp.tool_calls:
            result = registry.call(call.name, call.args)
            record = {"tool": call.name, "args": call.args,
                      "ok": "error" not in result, "result": _truncate(result)}
            run.tool_calls.append(record)
            if db is not None:
                db.log_action(run_id, "tool:" + call.name, target, record)
            messages.append({"role": "tool", "name": call.name, "id": call.id,
                             "response": result})

    run.elapsed_s = time.time() - started
    run.hit_budget = True
    if raise_on_budget:
        raise BudgetExceeded(run.steps, run.cost_usd, run.text)
    return run


def structured(llm: BaseLLM, *, system: str, user: str, model: str | None = None,
               temperature: float = 0.2, retries: int = 1,
               db=None, run_id: int | None = None, kind: str = "",
               target: str = "") -> tuple[dict, float]:
    """One JSON call, with one reprompt if the model returns junk."""
    messages = [{"role": "user", "text": user}]
    cost = 0.0
    last_error: Exception | None = None
    for _attempt in range(retries + 1):
        resp = llm.generate(system=system, messages=messages, response_json=True,
                            temperature=temperature, model=model)
        cost += resp.cost_usd
        try:
            data = parse_json(resp.text)
        except LLMError as exc:
            last_error = exc
            messages = [
                {"role": "user", "text": user},
                {"role": "model", "text": resp.text[:1500]},
                {"role": "user", "text": "That was not valid JSON. Return ONLY the "
                                         "JSON object, no prose, no code fence."},
            ]
            continue
        if not isinstance(data, dict):
            data = {"result": data}
        if db is not None and kind:
            db.log_action(run_id, kind, target, _truncate(data),
                          confidence=_confidence_of(data))
        return data, cost
    raise LLMError("structured call failed after {} attempts: {}".format(
        retries + 1, last_error))


def _confidence_of(data: Any) -> float | None:
    if isinstance(data, dict):
        val = data.get("confidence")
        if isinstance(val, (int, float)):
            return float(val)
    return None


def _truncate(obj: Any, limit: int = 1200) -> Any:
    blob = json.dumps(obj, default=str, ensure_ascii=False)
    if len(blob) <= limit:
        return obj
    return {"truncated": blob[:limit] + "..."}

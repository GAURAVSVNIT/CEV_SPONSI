"""Tool registry.

Tools are plain Python callables plus a JSON-schema description. The registry
hands out provider-neutral declarations ({name, description, parameters}) --
each LLM client converts them to its own wire shape -- and dispatches calls by
name, turning exceptions into a value the model can read and recover from.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable


class ToolError(RuntimeError):
    """Raised by a tool when the model asked for something it may not have."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]

    def declaration(self) -> dict:
        return {"name": self.name, "description": self.description.strip(),
                "parameters": self.parameters}


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, name: str, description: str, parameters: dict,
            fn: Callable[..., Any]) -> Tool:
        tool = Tool(name=name, description=description, parameters=parameters, fn=fn)
        self.tools[name] = tool
        return tool

    def tool(self, name: str, description: str, parameters: dict):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.add(name, description, parameters, fn)
            return fn
        return deco

    def declarations(self) -> list[dict]:
        return [t.declaration() for t in self.tools.values()]

    def call(self, name: str, args: dict) -> dict:
        """Dispatch. Always returns a dict -- every provider wants an object."""
        tool = self.tools.get(name)
        if tool is None:
            return {"error": "unknown tool {!r}; available: {}".format(
                name, ", ".join(self.tools))}
        try:
            sig = inspect.signature(tool.fn)
            accepted = {k: v for k, v in (args or {}).items() if k in sig.parameters}
            result = tool.fn(**accepted)
        except ToolError as exc:
            return {"error": str(exc)}
        except Exception as exc:                            # keep the loop alive
            return {"error": "{}: {}".format(type(exc).__name__, exc)}
        if isinstance(result, dict):
            return result
        return {"result": result}

    def __contains__(self, name: str) -> bool:
        return name in self.tools

    def __len__(self) -> int:
        return len(self.tools)

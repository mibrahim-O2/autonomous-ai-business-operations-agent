"""Deterministic offline model.

`ScriptedLLM` lets the whole platform run and be tested without an API key or
network access. It implements the same protocol as a hosted model, but instead
of generating text it dispatches on a JSON "envelope" that agents place in the
user message:

    {"kind": "act", "action": "qualify_lead", "inputs": {...}}

Each envelope kind and action maps to a registered Python handler that returns
either tool calls or a final JSON answer. The handlers live with the workflow
definitions (see `bizops.workflows`). This is a rule-based stand-in for model
reasoning: it exercises planning, tool calling, and recovery paths exactly as a
real model would drive them, with reproducible results.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..core.models import ToolCall
from .base import LLMMessage, LLMResponse, ToolSchema, estimate_tokens, price_usage

# A handler receives the envelope and the tool results so far, and returns
# either a list of tool calls to make next or a dict that is the final answer.
Handler = Callable[[dict[str, Any], list[dict[str, Any]]], "list[ToolCall] | dict[str, Any]"]


class ScriptedLLM:
    name = "scripted"

    def __init__(self) -> None:
        self.model = "scripted-v1"
        self._handlers: dict[str, Handler] = {}
        self.calls: list[dict[str, Any]] = []  # kept for tests and debugging

    def register(self, key: str, handler: Handler) -> None:
        """Register a handler under 'kind' or 'kind:action' (the more specific key wins)."""
        self._handlers[key] = handler

    @staticmethod
    def _envelope(messages: list[LLMMessage]) -> dict[str, Any]:
        for message in messages:
            if message.role == "user":
                try:
                    return json.loads(message.content)
                except json.JSONDecodeError:
                    return {"kind": "chat", "text": message.content}
        return {"kind": "chat", "text": ""}

    async def complete(self, messages: list[LLMMessage], tools: list[ToolSchema] | None = None) -> LLMResponse:
        envelope = self._envelope(messages)
        results = []
        for message in messages:
            if message.role == "tool":
                try:
                    result = json.loads(message.content)
                    # Untrusted output arrives in a labelled envelope; the playbooks work on its content.
                    if isinstance(result, dict) and set(result) == {"untrusted_content", "handling"}:
                        result = result["untrusted_content"]
                    results.append({"tool": message.name, "result": result})
                except json.JSONDecodeError:
                    results.append({"tool": message.name, "result": message.content})
        key = f"{envelope.get('kind')}:{envelope.get('action')}"
        handler = self._handlers.get(key) or self._handlers.get(str(envelope.get("kind")))
        self.calls.append({"key": key, "tool_results": len(results)})
        if handler is None:
            outcome: list[ToolCall] | dict[str, Any] = {
                "ok": False,
                "summary": f"No scripted behaviour for '{key}'",
                "error": "unsupported_action",
            }
        else:
            outcome = handler(envelope, results)

        # A scripted handler may only call tools the agent was actually offered,
        # mirroring the constraint a hosted model operates under.
        offered = {tool.name for tool in tools or []}
        prompt_tokens = sum(estimate_tokens(m.content) for m in messages)
        if isinstance(outcome, list):
            calls = [call for call in outcome if call.tool in offered]
            if len(calls) != len(outcome):
                missing = sorted({c.tool for c in outcome} - offered)
                text = json.dumps(
                    {
                        "ok": False,
                        "summary": f"Tools not available to this agent: {missing}",
                        "error": "tool_not_offered",
                    }
                )
                return LLMResponse(
                    text=text, model=self.model, usage=price_usage(self.model, prompt_tokens, estimate_tokens(text))
                )
            completion = sum(estimate_tokens(json.dumps(c.arguments)) for c in calls)
            return LLMResponse(
                tool_calls=calls, model=self.model, usage=price_usage(self.model, prompt_tokens, completion)
            )
        text = json.dumps(outcome, default=str)
        return LLMResponse(
            text=text, model=self.model, usage=price_usage(self.model, prompt_tokens, estimate_tokens(text))
        )

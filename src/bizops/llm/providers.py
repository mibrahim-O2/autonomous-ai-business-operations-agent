"""Hosted model providers.

Both adapters translate the neutral message format to the vendor SDK and back.
The SDKs are optional dependencies and are imported lazily. These adapters were
written against the vendors' published SDK interfaces but have NOT been run
against live APIs in this repository's test suite, which is fully offline.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.errors import ConfigurationError
from ..core.models import ToolCall
from .base import LLMMessage, LLMResponse, ToolSchema, price_usage


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-sonnet-5-5", max_tokens: int = 2048):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise ConfigurationError("Install the 'anthropic' extra to use this provider") from exc
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model, self.max_tokens = model, max_tokens

    async def complete(self, messages: list[LLMMessage], tools: list[ToolSchema] | None = None) -> LLMResponse:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        converted: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "user":
                converted.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                blocks: list[dict[str, Any]] = [{"type": "text", "text": m.content}] if m.content else []
                blocks += [
                    {"type": "tool_use", "id": c.call_id, "name": c.tool, "input": c.arguments} for c in m.tool_calls
                ]
                converted.append({"role": "assistant", "content": blocks})
            elif m.role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}],
                    }
                )
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "messages": converted}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [t.model_dump() for t in tools]
        response = await self._client.messages.create(**kwargs)
        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [
            ToolCall(call_id=b.id, tool=b.name, arguments=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]
        usage = price_usage(self.model, response.usage.input_tokens, response.usage.output_tokens)
        return LLMResponse(text=text, tool_calls=calls, usage=usage, model=self.model)


class OpenAICompatibleProvider:
    """Works with the OpenAI API and with servers that expose the same chat-completions interface."""

    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise ConfigurationError("Install the 'openai' extra to use this provider") from exc
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    async def complete(self, messages: list[LLMMessage], tools: list[ToolSchema] | None = None) -> LLMResponse:
        converted: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "assistant" and m.tool_calls:
                converted.append(
                    {
                        "role": "assistant",
                        "content": m.content or None,
                        "tool_calls": [
                            {
                                "id": c.call_id,
                                "type": "function",
                                "function": {"name": c.tool, "arguments": json.dumps(c.arguments)},
                            }
                            for c in m.tool_calls
                        ],
                    }
                )
            elif m.role == "tool":
                converted.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
            else:
                converted.append({"role": m.role, "content": m.content})
        kwargs: dict[str, Any] = {"model": self.model, "messages": converted}
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
                }
                for t in tools
            ]
        response = await self._client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        calls = [
            ToolCall(call_id=c.id, tool=c.function.name, arguments=json.loads(c.function.arguments or "{}"))
            for c in (message.tool_calls or [])
        ]
        usage = price_usage(self.model, response.usage.prompt_tokens, response.usage.completion_tokens)
        return LLMResponse(text=message.content or "", tool_calls=calls, usage=usage, model=self.model)

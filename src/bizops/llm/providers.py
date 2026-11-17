"""Hosted model providers.

Both adapters translate the neutral message format to the vendor SDK and back.
The SDKs are optional dependencies and are imported lazily. These adapters were
written against the vendors' published SDK interfaces. The automated tests run
them against stand-in clients that return SDK-shaped objects; they have NOT
been run against the live APIs in this repository's test suite, which is fully
offline. docs/LIVE-MODELS.md describes how to verify them with your own key.

Responses are validated before use: a response with no choice, a refusal by the
content filter, a tool call whose arguments are not a JSON object, or an output
cut off mid tool call is an `LLMError`, not a silently wrong action.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.errors import ConfigurationError, LLMError
from ..core.models import ToolCall
from .base import LLMMessage, LLMResponse, ToolSchema, estimate_tokens, price_usage


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-5-5",
        max_tokens: int = 2048,
        timeout: float = 60.0,
        client: Any = None,
    ):
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on optional install
                raise ConfigurationError("Install the 'anthropic' extra to use this provider") from exc
            # Retries are handled by ResilientLLM, in one place, so the SDK's own are switched off.
            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=0)
        self._client = client
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
        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        if tool_blocks and getattr(response, "stop_reason", None) == "max_tokens":
            raise LLMError("anthropic: output was cut off in the middle of a tool call", retryable=True)
        if any(not isinstance(b.input, dict) for b in tool_blocks):
            raise LLMError("anthropic: tool call arguments were not an object", retryable=True)
        calls = [ToolCall(call_id=b.id, tool=b.name, arguments=dict(b.input)) for b in tool_blocks]
        usage = price_usage(self.model, response.usage.input_tokens, response.usage.output_tokens)
        return LLMResponse(text=text, tool_calls=calls, usage=usage, model=self.model)


class OpenAICompatibleProvider:
    """Works with the OpenAI API and with servers that expose the same chat-completions interface."""

    name = "openai"

    def __init__(
        self, api_key: str, model: str, base_url: str | None = None, timeout: float = 60.0, client: Any = None
    ):
        if client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - depends on optional install
                raise ConfigurationError("Install the 'openai' extra to use this provider") from exc
            # Retries are handled by ResilientLLM, in one place, so the SDK's own are switched off.
            client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
        self._client = client
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
        return self._validated(response, converted)

    def _validated(self, response: Any, sent: list[dict[str, Any]]) -> LLMResponse:
        """Check the response before anything acts on it."""
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise LLMError("openai: response contained no choices", retryable=True)
        choice = choices[0]
        message, finish = choice.message, getattr(choice, "finish_reason", None)
        if finish == "content_filter":
            raise LLMError("openai: the response was withheld by the provider's content filter")
        if getattr(message, "refusal", None):
            raise LLMError("openai: the model refused the request")
        raw_calls = message.tool_calls or []
        if raw_calls and finish == "length":
            raise LLMError("openai: output was cut off in the middle of a tool call", retryable=True)
        calls = []
        for raw in raw_calls:
            try:
                arguments = json.loads(raw.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise LLMError(
                    f"openai: tool call '{raw.function.name}' had arguments that are not valid JSON", retryable=True
                ) from exc
            if not isinstance(arguments, dict):
                raise LLMError(f"openai: tool call '{raw.function.name}' arguments were not an object", retryable=True)
            calls.append(ToolCall(call_id=raw.id, tool=raw.function.name, arguments=arguments))
        text = message.content or ""
        if not calls and not text.strip():
            raise LLMError("openai: response was empty", retryable=True)
        reported = getattr(response, "usage", None)
        if reported is not None:
            usage = price_usage(self.model, reported.prompt_tokens, reported.completion_tokens)
        else:
            # Some compatible servers omit usage. Estimate so that budgets still apply.
            prompt = sum(estimate_tokens(json.dumps(m, default=str)) for m in sent)
            usage = price_usage(self.model, prompt, estimate_tokens(text + json.dumps([c.arguments for c in calls])))
        return LLMResponse(
            text=text, tool_calls=calls, usage=usage, model=getattr(response, "model", None) or self.model
        )

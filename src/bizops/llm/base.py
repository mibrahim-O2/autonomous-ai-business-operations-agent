"""Provider-neutral chat and tool-calling contract.

Agents build `LLMMessage` lists and pass tool schemas; a provider returns either
text or tool calls. Hosted providers and the offline scripted model implement
the same protocol, so agents cannot tell them apart.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from ..core.models import ToolCall, Usage


class ToolSchema(BaseModel):
    """What the model is told about a tool: name, purpose, and JSON Schema of its arguments."""

    name: str
    description: str
    input_schema: dict[str, Any]


class LLMMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)  # assistant turns
    tool_call_id: str | None = None  # tool turns
    name: str | None = None  # tool name on tool turns


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str = ""


class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(self, messages: list[LLMMessage], tools: list[ToolSchema] | None = None) -> LLMResponse: ...


def estimate_tokens(text: str) -> int:
    """Rough token count (about four characters per token). Used for budgets, not billing."""
    return max(1, len(text) // 4)


# USD per million tokens (input, output). Used only to estimate run cost; verify
# against the provider's price list before relying on the numbers.
PRICE_TABLE: dict[str, tuple[float, float]] = {"scripted": (0.0, 0.0), "default": (3.0, 15.0)}


def price_usage(model: str, input_tokens: int, output_tokens: int) -> Usage:
    rate_in, rate_out = PRICE_TABLE.get(model, PRICE_TABLE["scripted" if model.startswith("scripted") else "default"])
    cost = (input_tokens * rate_in + output_tokens * rate_out) / 1_000_000
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=round(cost, 6), calls=1)

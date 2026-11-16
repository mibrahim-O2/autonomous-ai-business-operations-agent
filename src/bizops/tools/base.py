"""Tool contracts.

A `ToolSpec` describes a tool to three audiences: the model (name, description,
schema), the validator (pydantic model or JSON Schema), and the control layer
(risk level, whether it returns untrusted content). A `ToolExecutor` is any
source of tools: in-process functions here, MCP servers in the integration layer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from ..core.models import RiskLevel
from ..llm.base import ToolSchema
from ..storage import Database


@dataclass(frozen=True)
class ToolContext:
    """Ambient information a tool may need. Tools never receive the model's messages."""

    tenant_id: str
    run_id: str
    db: Database
    actor: str = "agent"
    # Stable per (run, step, call). Tools with side effects pass it on so that a
    # repeated call is recognised by the target system and not performed twice.
    idempotency_key: str = ""
    # The run's claim generation. Target systems refuse a token lower than one they have
    # already seen for the run, which stops a superseded worker from acting.
    fencing_token: int = 0


@dataclass
class ToolSpec:
    name: str
    description: str
    risk: RiskLevel
    input_model: type[BaseModel] | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    # True when the tool returns text authored outside the organisation.
    returns_untrusted: bool = False
    handler: Callable[[Any, ToolContext], Awaitable[Any]] | None = None
    source: str = "local"  # local | mcp:<server>

    @property
    def side_effect(self) -> bool:
        return self.risk != RiskLevel.READ

    def schema(self) -> ToolSchema:
        schema = self.input_model.model_json_schema() if self.input_model else self.input_schema
        return ToolSchema(name=self.name, description=self.description, input_schema=schema)


class ToolExecutor(Protocol):
    def specs(self) -> list[ToolSpec]: ...

    async def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any: ...

"""MCP client side: discover tools on an MCP server and execute them for agents.

Discovery turns each MCP tool into a `ToolSpec`, reading the risk level and the
untrusted-content flag from the tool's metadata. Two parameters are handled by
the platform and hidden from the model:

  * `tenant_id`        injected from the run, so a model cannot address another tenant
  * `idempotency_key`  injected for side-effecting tools, derived from the run,
                       step, and call fingerprint, so a retried call is not repeated
  * `fencing_token`    the run's claim generation, so the target refuses a worker
                       that has been superseded

A session is opened per operation. For an in-process server that costs almost
nothing and avoids holding a connection across event loops; for a stdio or HTTP
server, wrap the executor in a long-lived session if call volume makes the
handshake significant.
"""

from __future__ import annotations

from typing import Any

from mcp import Client

from ..core.errors import StaleLease, ToolError
from ..core.models import RiskLevel
from .base import ToolContext, ToolSpec

HIDDEN_PARAMS = ("tenant_id", "idempotency_key", "fencing_token")


class MCPToolExecutor:
    def __init__(self, name: str, target: Any, read_timeout_seconds: float = 30.0):
        """`target` is anything `mcp.Client` accepts: a server object, stdio parameters, or a URL."""
        self.name, self.target, self.read_timeout_seconds = name, target, read_timeout_seconds
        self._specs: dict[str, ToolSpec] = {}
        self._accepts: dict[str, set[str]] = {}
        self.server_info: dict[str, Any] = {}

    def _client(self) -> Client:
        return Client(self.target, read_timeout_seconds=self.read_timeout_seconds)

    async def discover(self) -> list[ToolSpec]:
        """List the server's tools and convert them to specs. Call again to pick up changes."""
        async with self._client() as client:
            listing = await client.list_tools()
            prompts = await client.list_prompts()
            templates = await client.list_resource_templates()
        specs: dict[str, ToolSpec] = {}
        for tool in listing.tools:
            meta = tool.meta or {}
            schema = dict(tool.input_schema or {})
            properties = dict(schema.get("properties", {}))
            self._accepts[tool.name] = set(properties)
            # Hide platform-controlled parameters from the model's view of the tool.
            schema["properties"] = {k: v for k, v in properties.items() if k not in HIDDEN_PARAMS}
            schema["required"] = [k for k in schema.get("required", []) if k not in HIDDEN_PARAMS]
            try:
                risk = RiskLevel(meta.get("risk", "high"))
            except ValueError:
                risk = RiskLevel.HIGH  # unknown or missing risk is treated as the most restrictive
            specs[tool.name] = ToolSpec(
                name=tool.name,
                description=tool.description or "",
                risk=risk,
                input_schema=schema,
                returns_untrusted=bool(meta.get("returns_untrusted")),
                source=f"mcp:{self.name}",
            )
        self._specs = specs
        self.server_info = {
            "tools": len(specs),
            "prompts": [p.name for p in prompts.prompts],
            "resource_templates": [t.uri_template for t in templates.resource_templates],
        }
        return list(specs.values())

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    async def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        spec = self._specs[name]
        accepted = self._accepts.get(name, set())
        payload = {k: v for k, v in arguments.items() if k not in HIDDEN_PARAMS}
        if "tenant_id" in accepted:
            payload["tenant_id"] = ctx.tenant_id
        if spec.side_effect and "idempotency_key" in accepted and ctx.idempotency_key:
            payload["idempotency_key"] = ctx.idempotency_key
            if "fencing_token" in accepted and ctx.fencing_token:
                payload["fencing_token"] = ctx.fencing_token
        try:
            async with self._client() as client:
                result = await client.call_tool(name, payload)
        except Exception as exc:
            raise ToolError(f"MCP transport error calling {name}: {type(exc).__name__}: {exc}", retryable=True) from exc
        if result.is_error:
            text = result.content[0].text if result.content else "unknown error"
            raise ToolError(f"{name} failed: {text}")
        envelope = result.structured_content or {}
        if envelope.get("stale"):
            raise StaleLease(str(envelope.get("error")))
        if not envelope.get("ok", False):
            raise ToolError(
                str(envelope.get("error", "tool returned no result")), retryable=bool(envelope.get("retryable"))
            )
        return envelope.get("data")

    async def read_resource(self, uri: str) -> str:
        async with self._client() as client:
            result = await client.read_resource(uri)
        return result.contents[0].text

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> str:
        async with self._client() as client:
            result = await client.get_prompt(name, arguments or {})
        return "\n".join(m.content.text for m in result.messages)

"""Composition root: builds every component once and wires them together.

Nothing else in the package constructs its own dependencies, so tests and
alternative deployments can swap any part (model, gate, tools, integrations,
storage) here.

Construction is synchronous. `await runtime.start()` then performs the steps
that need the event loop: discovering tools on MCP servers and seeding demo data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agents import AgentContext, AgentRegistry, PlannerAgent
from .approvals import ApprovalService, PolicyGate
from .core.config import Settings, get_settings
from .integrations import Integrations, build_mock_integrations, seed_demo_data
from .integrations.mock import MockControl
from .jobs import JobQueue, Worker
from .knowledge import KnowledgeBase
from .llm import LLMProvider, ScriptedLLM, build_llm
from .mcp_server import create_business_server
from .memory import ContextBuilder, MemoryStore
from .observability import Tracer, configure_logging
from .orchestrator import Orchestrator
from .security import ApiKeyStore, AuditLog
from .storage import Database
from .tools import ApprovalGate, LocalToolExecutor, ToolExecutor, ToolGateway
from .tools.mcp_executor import MCPToolExecutor
from .workflows import WorkflowRegistry, default_workflows

DEMO_KNOWLEDGE = Path(__file__).resolve().parents[2] / "data" / "demo" / "knowledge"


@dataclass
class Runtime:
    settings: Settings
    db: Database
    tracer: Tracer
    memory: MemoryStore
    knowledge: KnowledgeBase
    integrations: Integrations
    mock_control: MockControl
    llm: LLMProvider
    gateway: ToolGateway
    mcp: MCPToolExecutor
    registry: AgentRegistry
    workflows: WorkflowRegistry
    orchestrator: Orchestrator
    queue: JobQueue
    worker: Worker
    audit: AuditLog
    approvals: ApprovalService
    keys: ApiKeyStore
    _started: bool = field(default=False, repr=False)

    async def start(self) -> Runtime:
        """Discover MCP tools and load demo data. Safe to call more than once."""
        if not self._started:
            await self.mcp.discover()
            self.gateway.refresh()
            if self.settings.seed_demo_data:
                self.seed(self.settings.default_tenant)
            self._started = True
        return self

    def seed(self, tenant_id: str) -> dict[str, Any]:
        """Load the fictional business records and policy documents for a tenant."""
        seeded = seed_demo_data(self.db, tenant_id)
        documents = self.knowledge.ingest_path(tenant_id, DEMO_KNOWLEDGE) if DEMO_KNOWLEDGE.exists() else []
        return {"records_seeded": seeded, "documents": documents}

    def close(self) -> None:
        self.db.close()


def build_runtime(
    settings: Settings | None = None,
    *,
    llm: LLMProvider | None = None,
    gate: ApprovalGate | None = None,
    extra_executors: list[ToolExecutor] | None = None,
    db: Database | None = None,
    integrations: Integrations | None = None,
) -> Runtime:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    db = db or Database(settings.database_path)
    tracer = Tracer(db)
    memory = MemoryStore(db)
    knowledge = KnowledgeBase(db)
    mock_integrations, mock_control = build_mock_integrations(db)
    integrations = integrations or mock_integrations

    workflows = default_workflows()
    llm = llm or build_llm(settings)
    if isinstance(llm, ScriptedLLM):
        workflows.install(llm)  # the offline model executes the workflow playbooks directly

    # The business systems are reached only through MCP, in-process by default.
    mcp = MCPToolExecutor("business", create_business_server(integrations, db, mock_control))
    # Human-in-the-loop control is on by default: the tenant's approval policy decides which tool
    # calls wait for a person. Passing `gate` replaces it (tests use this to isolate other behaviour).
    audit = AuditLog(db)
    approvals = ApprovalService(db, audit, settings.approval_ttl_seconds)
    gateway = ToolGateway(
        [LocalToolExecutor(memory, knowledge), mcp, *(extra_executors or [])],
        db,
        tracer,
        gate or PolicyGate(db, approvals),
        audit=audit,
        tool_timeout_seconds=settings.tool_timeout_seconds,
    )
    registry = AgentRegistry()
    planner = PlannerAgent(registry, workflows)
    agent_ctx = AgentContext(
        llm=llm,
        gateway=gateway,
        context_builder=ContextBuilder(memory, settings.context_token_budget),
        tracer=tracer,
        settings=settings,
    )
    orchestrator = Orchestrator(
        settings=settings,
        db=db,
        tracer=tracer,
        memory=memory,
        registry=registry,
        workflows=workflows,
        planner=planner,
        agent_ctx=agent_ctx,
    )
    orchestrator.approvals = approvals
    queue = JobQueue(db)
    return Runtime(
        settings,
        db,
        tracer,
        memory,
        knowledge,
        integrations,
        mock_control,
        llm,
        gateway,
        mcp,
        registry,
        workflows,
        orchestrator,
        queue,
        Worker(queue, orchestrator),
        audit,
        approvals,
        ApiKeyStore(db),
    )

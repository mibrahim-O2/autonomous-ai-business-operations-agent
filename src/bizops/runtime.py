"""Composition root: builds every component once and wires them together.

Nothing else in the package constructs its own dependencies, so tests and
alternative deployments can swap any part (model, gate, tools, storage) here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .agents import AgentContext, AgentRegistry, PlannerAgent
from .core.config import Settings, get_settings
from .llm import LLMProvider, ScriptedLLM, build_llm
from .memory import ContextBuilder, MemoryStore
from .observability import Tracer, configure_logging
from .orchestrator import Orchestrator
from .storage import Database
from .tools import ApprovalGate, LocalToolExecutor, ToolExecutor, ToolGateway
from .workflows import WorkflowRegistry, default_workflows


@dataclass
class Runtime:
    settings: Settings
    db: Database
    tracer: Tracer
    memory: MemoryStore
    llm: LLMProvider
    gateway: ToolGateway
    registry: AgentRegistry
    workflows: WorkflowRegistry
    orchestrator: Orchestrator

    def close(self) -> None:
        self.db.close()


def build_runtime(
    settings: Settings | None = None,
    *,
    llm: LLMProvider | None = None,
    gate: ApprovalGate | None = None,
    extra_executors: list[ToolExecutor] | None = None,
    db: Database | None = None,
) -> Runtime:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    db = db or Database(settings.database_path)
    tracer = Tracer(db)
    memory = MemoryStore(db)
    workflows = default_workflows()
    llm = llm or build_llm(settings)
    if isinstance(llm, ScriptedLLM):
        workflows.install(llm)  # the offline model executes the workflow playbooks directly
    gateway = ToolGateway([LocalToolExecutor(memory), *(extra_executors or [])], db, tracer, gate)
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
    return Runtime(settings, db, tracer, memory, llm, gateway, registry, workflows, orchestrator)

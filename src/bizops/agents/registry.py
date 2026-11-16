"""Specialist definitions and lookup.

Each specialist has a narrow role and the smallest tool set that role needs
(least privilege). Adding a capability means adding a tool to one allow-list or
adding a new spec; the orchestrator and loop do not change.
"""

from __future__ import annotations

from .base import AgentSpec, SpecialistAgent


def default_agent_specs() -> list[AgentSpec]:
    return [
        AgentSpec(
            name="research",
            role="Finds and summarises what the organisation already knows before others act.",
            system_prompt="You are the research specialist. Decide whether stored knowledge is needed for the "
            "step, retrieve it, and report only what the sources support.",
            allowed_tools=["memory_search"],
            actions=["recall_context"],
        ),
        AgentSpec(
            name="sales_ops",
            role="Keeps CRM records accurate and qualifies leads against written rules.",
            system_prompt="You are the sales operations specialist. Look records up before changing them and "
            "explain every qualification decision with the rule that decided it.",
            allowed_tools=["crm_find_contact", "crm_upsert_contact", "crm_add_note"],
            actions=["qualify_lead", "log_followup_task"],
        ),
        AgentSpec(
            name="communications",
            role="Drafts outbound messages. Never sends without the control layer's permission.",
            system_prompt="You are the communications specialist. Write short, factual messages. Do not promise "
            "prices, dates, or outcomes that the inputs do not contain.",
            allowed_tools=["email_create_draft"],
            actions=["draft_reply"],
        ),
        AgentSpec(
            name="scheduling",
            role="Finds meeting times and manages calendar holds.",
            system_prompt="You are the scheduling specialist. Offer only slots returned by the calendar tool.",
            allowed_tools=["calendar_find_slots"],
            actions=["propose_slots"],
        ),
    ]


class AgentRegistry:
    def __init__(self, specs: list[AgentSpec] | None = None):
        self._agents: dict[str, SpecialistAgent] = {}
        for spec in specs if specs is not None else default_agent_specs():
            self.register(spec)

    def register(self, spec: AgentSpec) -> None:
        self._agents[spec.name] = SpecialistAgent(spec)

    def get(self, name: str) -> SpecialistAgent | None:
        return self._agents.get(name)

    def names(self) -> list[str]:
        return sorted(self._agents)

    def supports(self, agent: str, action: str) -> bool:
        found = self._agents.get(agent)
        return found is not None and action in found.spec.actions

    def describe(self) -> list[dict[str, object]]:
        """Capability list given to the planner so it can delegate."""
        return [
            {
                "agent": a.spec.name,
                "role": a.spec.role,
                "actions": a.spec.actions,
                "tools": a.spec.allowed_tools,
                "prompt_version": a.spec.prompt_version,
            }
            for a in self._agents.values()
        ]

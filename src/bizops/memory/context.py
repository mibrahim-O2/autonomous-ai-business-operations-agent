"""Context management.

An agent's context is assembled from prioritised sections and cut to a token
budget. Lower-priority sections are dropped or truncated first, so the step
instruction and its direct inputs always survive.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.models import PlanStep, RunState
from ..llm.base import estimate_tokens
from .store import MemoryStore


class ContextBuilder:
    def __init__(self, memory: MemoryStore, token_budget: int):
        self.memory, self.token_budget = memory, token_budget

    def build(self, state: RunState, step: PlanStep, resolved_inputs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Return (context text, stats). Sections are listed most important first."""
        dependencies = {
            dep.action: state.working_memory.get(dep.action)
            for dep in (state.plan.steps if state.plan else [])
            if dep.step_id in step.depends_on
        }
        query = f"{state.task.objective} {step.description} {json.dumps(resolved_inputs, default=str)}"
        memories = self.memory.search(state.tenant_id, query, limit=5)
        sections: list[tuple[str, str]] = [
            ("objective", state.task.objective),
            ("step", f"{step.action}: {step.description}"),
            ("inputs", json.dumps(resolved_inputs, default=str)),
            ("dependency_results", json.dumps(dependencies, default=str)),
            ("memory", "\n".join(f"- [{m.kind}] {m.text}" for m in memories)),
        ]
        remaining, parts, dropped, truncated = self.token_budget, [], [], []
        for name, text in sections:
            if not text or text in ("{}", "[]"):
                continue
            cost = estimate_tokens(text)
            if cost > remaining:
                if remaining < 50:
                    dropped.append(name)
                    continue
                text = text[: remaining * 4] + " ...[truncated]"
                truncated.append(name)
                cost = remaining
            parts.append(f"## {name}\n{text}")
            remaining -= cost
        stats = {
            "tokens_used": self.token_budget - remaining,
            "budget": self.token_budget,
            "dropped": dropped,
            "truncated": truncated,
            "memories": len(memories),
        }
        return "\n\n".join(parts), stats

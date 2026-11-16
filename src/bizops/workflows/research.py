"""Agentic retrieval behaviour shared by workflows.

`research_policy` is where the research agent makes its retrieval decisions:

  1. Is retrieval needed? No question means no search.
  2. Which source and filter? Policies are searched in trusted policy documents first.
  3. Was the result good enough? A weak top score triggers one reformulated,
     broader search.
  4. What do the passages say? Named facts are extracted with patterns supplied
     by the workflow, so later steps receive numbers, not prose.
  5. If nothing grounded is found, the step fails with `no_grounded_policy`.
     The planner then replans around the gap instead of letting agents guess.
"""

from __future__ import annotations

import re
from typing import Any

from ..core.models import ToolCall
from .base import call

MIN_SCORE = 0.3


def research_policy(envelope: dict[str, Any], results: list[dict[str, Any]]) -> list[ToolCall] | dict[str, Any]:
    inputs = envelope["inputs"]
    question = str(inputs.get("question") or "").strip()
    if not question:
        return {
            "ok": True,
            "summary": "No question posed; retrieval skipped.",
            "data": {"grounded": False, "retrieval_used": False, "facts": {}, "citations": []},
        }

    searches = [r["result"] for r in results if r["tool"] == "kb_search"]
    if not searches:
        return [call("kb_search", query=question, doc_types=inputs.get("doc_types") or [], trusted_only=True, limit=4)]

    def strong(search: dict[str, Any]) -> list[dict[str, Any]]:
        return [hit for hit in search.get("results", []) if hit["score"] >= MIN_SCORE]

    hits = strong(searches[-1])
    if not hits and len(searches) == 1:
        # Reformulate: add the workflow's alternative phrasing and drop the document-type filter.
        broader = f"{question} {inputs.get('fallback_query', '')}".strip()
        return [call("kb_search", query=broader, doc_types=[], trusted_only=True, limit=6)]
    if not hits:
        if inputs.get("optional"):
            # The caller has a safe default, so a missing document is reported, not treated as a failure.
            return {
                "ok": True,
                "summary": "No supporting document found; the caller's default applies.",
                "data": {
                    "grounded": False,
                    "retrieval_used": True,
                    "facts": {},
                    "citations": [],
                    "searches": len(searches),
                },
            }
        return {
            "ok": False,
            "summary": "No policy document answers this question.",
            "error": "no_grounded_policy",
            "data": {"grounded": False, "searches": len(searches)},
        }

    text = " ".join(hit["text"] for hit in hits)
    facts: dict[str, Any] = {}
    for name, pattern in (inputs.get("extract") or {}).items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = match.group(1)
            facts[name] = float(value) if re.fullmatch(r"\d+(\.\d+)?", value) else value
    missing = [name for name in (inputs.get("extract") or {}) if name not in facts]
    if missing and inputs.get("optional"):
        missing = []
    if missing:
        return {
            "ok": False,
            "summary": f"Policy found but it does not state: {missing}.",
            "error": "no_grounded_policy",
            "data": {"grounded": False, "missing": missing},
        }
    return {
        "ok": True,
        "summary": f"Policy retrieved from {len(hits)} passages after {len(searches)} search(es).",
        "data": {
            "grounded": True,
            "retrieval_used": True,
            "facts": facts,
            "searches": len(searches),
            "citations": sorted({hit["citation"] for hit in hits}),
            "passages": [hit["text"] for hit in hits[:2]],
        },
    }

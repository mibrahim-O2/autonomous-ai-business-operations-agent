"""Prompt-injection defences: detection and provenance.

Detection is a heuristic and is treated as one. It is not what keeps the system
safe; a determined attacker can phrase an instruction that no pattern matches.
The controls that hold regardless of wording are structural:

  1. Untrusted text reaches the model only as tool output, wrapped and labelled
     as data (see agents/base.py).
  2. A run that has read untrusted content is marked tainted, and the approval
     policy tightens for it.
  3. High-risk actions need a human decision whatever the model was told.
  4. Agents can call only the tools on their allow-list.

What detection adds is visibility: a suspected attempt is recorded in the trace,
shown on the run, and shown to the approver next to any request from that run.
"""

from __future__ import annotations

import re
from typing import Any

PATTERNS: dict[str, re.Pattern[str]] = {
    "override_instructions": re.compile(
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|your)\b[^.\n]{0,30}"
        r"\b(instruction|rule|prompt|polic|direction)",
        re.I,
    ),
    "role_reassignment": re.compile(r"\b(you are now|act as|pretend to be|new role|from now on you)\b", re.I),
    "system_prompt_probe": re.compile(
        r"\b(system prompt|developer message|hidden instruction|reveal your (prompt|instruction))", re.I
    ),
    "addressed_to_assistant": re.compile(
        r"\b(ai|assistant|agent|bot|system)\s*(note|instruction|directive|override|message)?\s*:", re.I
    ),
    "tool_directive": re.compile(
        r"\b(call|use|invoke|run|execute)\b[^.\n]{0,25}\b(tool|function|[a-z]+_[a-z_]+)\b"
        r"|\b(send|forward|wire|transfer|refund|export|delete)\b[^.\n]{0,40}\b(all|every|immediately|urgent)",
        re.I,
    ),
    # Attempts to keep people out of the loop: "do not tell anyone", "without asking for approval".
    "secrecy": re.compile(
        r"\b(do not|don't|never|without)\b[^.\n]{0,25}\b(tell|mention|inform|alert|log|ask|notify|approv\w*)\b",
        re.I,
    ),
}

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL = re.compile(r"https?://[^\s\"'<>)]+", re.I)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list | tuple):
        return [s for item in value for s in _strings(item)]
    return []


def scan_for_injection(value: Any) -> list[dict[str, str]]:
    """Return the suspicious patterns found in any string inside `value`, with a short excerpt each."""
    findings: list[dict[str, str]] = []
    seen: set[str] = set()
    for text in _strings(value):
        for name, pattern in PATTERNS.items():
            match = pattern.search(text)
            if match and name not in seen:
                seen.add(name)
                # Start and end the excerpt on word boundaries so it reads as text, not a fragment.
                start = max(0, match.start() - 20)
                if start:
                    space = text.find(" ", start)
                    start = space + 1 if 0 <= space < match.start() else match.start()
                end = min(len(text), match.end() + 40)
                if end < len(text):
                    space = text.rfind(" ", match.end(), end)
                    end = space if space > match.end() else end
                findings.append({"pattern": name, "excerpt": text[start:end].strip()[:160]})
    return findings


def extract_refs(value: Any) -> set[str]:
    """Email addresses and links found anywhere inside `value`, lower-cased."""
    refs: set[str] = set()
    for text in _strings(value):
        refs.update(m.lower() for m in EMAIL.findall(text))
        refs.update(m.lower().rstrip(".,;") for m in URL.findall(text))
    return refs

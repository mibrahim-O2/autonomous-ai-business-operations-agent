"""Document parsing and chunking.

Chunking is structure-aware: a Markdown document is split at headings first, so
a chunk never straddles two policy sections, and long sections are then split
into overlapping windows of sentences. Each chunk records its section heading,
which becomes part of the citation.
"""

from __future__ import annotations

import re

from ..llm.base import estimate_tokens

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split optional '---' front matter (simple key: value lines) from the body."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip().lower()] = value.strip()
    return meta, text[end + 4 :].lstrip("\n")


def chunk_markdown(body: str, max_tokens: int = 120, overlap_sentences: int = 1) -> list[tuple[str, str]]:
    """Return (section, text) chunks."""
    sections: list[tuple[str, list[str]]] = [("Introduction", [])]
    for line in body.splitlines():
        match = _HEADING.match(line)
        if match:
            sections.append((match.group(2).strip(), []))
        elif line.strip():
            sections[-1][1].append(line.strip())

    chunks: list[tuple[str, str]] = []
    for section, lines in sections:
        sentences = [s for s in _SENTENCE.split(" ".join(lines)) if s]
        window: list[str] = []
        for sentence in sentences:
            if window and estimate_tokens(" ".join(window + [sentence])) > max_tokens:
                chunks.append((section, " ".join(window)))
                window = window[-overlap_sentences:] if overlap_sentences else []
            window.append(sentence)
        if window:
            chunks.append((section, " ".join(window)))
    return chunks

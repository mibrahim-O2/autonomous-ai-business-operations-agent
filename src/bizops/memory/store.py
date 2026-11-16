"""Long-term memory.

Two kinds of item share one table:
  * semantic: durable facts ("Harborline Realty prefers morning meetings")
  * episodic: summaries of past runs ("lead_intake for X completed, tier hot")

Retrieval here is lexical (BM25). The knowledge layer adds embeddings and hybrid
search over documents; memory keeps to BM25 so it has no extra dependencies and
its ranking is easy to reason about in tests.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ..core.models import new_id
from ..storage import Database

SEMANTIC, EPISODIC = "semantic", "episodic"
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an and are as at be by for from has in is it of on or that the to was with".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


def bm25_scores(query: str, documents: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Okapi BM25 over a small in-memory corpus. Returns one score per document."""
    tokenized = [tokenize(doc) for doc in documents]
    if not tokenized:
        return []
    avg_len = sum(len(doc) for doc in tokenized) / len(tokenized) or 1.0
    doc_freq: Counter[str] = Counter(term for doc in tokenized for term in set(doc))
    scores = []
    for doc in tokenized:
        counts, score = Counter(doc), 0.0
        for term in set(tokenize(query)):
            if term not in counts:
                continue
            idf = math.log(1 + (len(tokenized) - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            tf = counts[term]
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(doc) / avg_len))
        scores.append(round(score, 4))
    return scores


@dataclass(frozen=True)
class MemoryHit:
    item_id: str
    kind: str
    text: str
    score: float
    metadata: dict[str, Any]


class MemoryStore:
    def __init__(self, db: Database):
        self.db = db

    def add_fact(self, tenant_id: str, text: str, **metadata: Any) -> str:
        """Store a semantic fact. Identical text for a tenant is stored once."""
        for item in self.db.list_memory(tenant_id, SEMANTIC):
            if item["text"].strip().lower() == text.strip().lower():
                return item["item_id"]
        item_id = new_id("fact")
        self.db.add_memory(item_id, tenant_id, SEMANTIC, text.strip(), metadata)
        return item_id

    def add_episode(self, tenant_id: str, run_id: str, summary: str, **metadata: Any) -> str:
        item_id = f"episode_{run_id}"
        self.db.add_memory(item_id, tenant_id, EPISODIC, summary, {"run_id": run_id, **metadata})
        return item_id

    def search(self, tenant_id: str, query: str, kind: str | None = None, limit: int = 5) -> list[MemoryHit]:
        items = self.db.list_memory(tenant_id, kind)
        scores = bm25_scores(query, [item["text"] for item in items])
        ranked = sorted(zip(items, scores, strict=True), key=lambda pair: -pair[1])
        return [
            MemoryHit(item["item_id"], item["kind"], item["text"], score, item["metadata"])
            for item, score in ranked[:limit]
            if score > 0
        ]

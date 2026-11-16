"""Knowledge base with hybrid retrieval.

Search pipeline:
  1. filter chunks by tenant and metadata (document type, tags, trust)
  2. rank by BM25 (exact terms) and by vector similarity (embedding)
  3. fuse the two rankings with reciprocal rank fusion, which needs no score calibration
  4. rerank the fused candidates by query-term coverage, phrase match, and heading match
  5. return the top results with citations and the component scores

Documents carry a `trusted` flag. Internal policies are trusted; material that
originated outside the organisation is not, and callers can exclude it or treat
its text as untrusted data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..core.models import new_id
from ..memory.store import bm25_scores, tokenize
from ..storage import Database
from .embeddings import Embedder, HashingEmbedder, cosine, pack, unpack
from .ingest import chunk_markdown, parse_front_matter

RRF_K = 60


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    doc_id: str
    title: str
    section: str
    text: str
    doc_type: str
    trusted: bool
    score: float
    scores: dict[str, float]

    @property
    def citation(self) -> str:
        return f"{self.title} > {self.section}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"citation": self.citation}


class KnowledgeBase:
    def __init__(self, db: Database, embedder: Embedder | None = None):
        self.db = db
        self.embedder = embedder or HashingEmbedder()

    # Ingestion ---------------------------------------------------------------
    def ingest_text(
        self,
        tenant_id: str,
        title: str,
        text: str,
        *,
        doc_type: str = "document",
        source: str = "manual",
        trusted: bool = True,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Parse, chunk, embed, and store a document. Re-ingesting unchanged content is a no-op."""
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        existing = self.db.query(
            "SELECT doc_id, content_hash FROM documents WHERE tenant_id=? AND source=?", (tenant_id, source)
        )
        if existing and existing[0]["content_hash"] == content_hash:
            return {"doc_id": existing[0]["doc_id"], "chunks": 0, "status": "unchanged"}
        chunks = chunk_markdown(text)
        vectors = self.embedder.embed([f"{title}. {section}. {body}" for section, body in chunks])
        doc_id = existing[0]["doc_id"] if existing else new_id("doc")
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))  # chunks cascade
            conn.execute(
                "INSERT INTO documents(doc_id, tenant_id, title, doc_type, source, trusted, content_hash, "
                "metadata_json, created_at) VALUES(?,?,?,?,?,?,?,?,datetime('now'))",
                (
                    doc_id,
                    tenant_id,
                    title,
                    doc_type,
                    source,
                    int(trusted),
                    content_hash,
                    json.dumps({"tags": tags or [], "embedder": self.embedder.name}),
                ),
            )
            for position, ((section, body), vector) in enumerate(zip(chunks, vectors, strict=True)):
                conn.execute(
                    "INSERT INTO chunks(chunk_id, doc_id, tenant_id, position, section, text, embedding) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (f"{doc_id}:{position}", doc_id, tenant_id, position, section, body, pack(vector)),
                )
        return {"doc_id": doc_id, "chunks": len(chunks), "status": "replaced" if existing else "created"}

    def ingest_path(self, tenant_id: str, path: Path) -> list[dict[str, Any]]:
        """Ingest one Markdown or text file, or every such file in a directory."""
        files = sorted(p for p in path.rglob("*") if p.suffix in (".md", ".txt")) if path.is_dir() else [path]
        results = []
        for file in files:
            meta, body = parse_front_matter(file.read_text(encoding="utf-8"))
            trusted = meta.get("trusted", "true").lower() != "false"
            tags = [t.strip() for t in meta.get("tags", "").split(",") if t.strip()]
            results.append(
                self.ingest_text(
                    tenant_id,
                    meta.get("title", file.stem),
                    body,
                    doc_type=meta.get("doc_type", "document"),
                    source=f"file:{file.name}",
                    trusted=trusted,
                    tags=tags,
                )
                | {"file": file.name}
            )
        return results

    def list_documents(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT doc_id, title, doc_type, source, trusted FROM documents WHERE tenant_id=? ORDER BY title",
            (tenant_id,),
        )
        return [dict(row) | {"trusted": bool(row["trusted"])} for row in rows]

    # Retrieval ---------------------------------------------------------------
    def search(
        self,
        tenant_id: str,
        query: str,
        *,
        limit: int = 5,
        doc_types: list[str] | None = None,
        tags: list[str] | None = None,
        trusted_only: bool = False,
        mode: str = "hybrid",
    ) -> list[Hit]:
        rows = self.db.query(
            "SELECT c.chunk_id, c.doc_id, c.section, c.text, c.embedding, d.title, d.doc_type, d.trusted, "
            "d.metadata_json "
            "FROM chunks c JOIN documents d ON d.doc_id = c.doc_id WHERE c.tenant_id=? "
            "ORDER BY d.title, d.source, c.position",
            (tenant_id,),
        )
        candidates = []
        for row in rows:
            if doc_types and row["doc_type"] not in doc_types:
                continue
            if trusted_only and not row["trusted"]:
                continue
            if tags and not set(tags) & set(json.loads(row["metadata_json"]).get("tags", [])):
                continue
            candidates.append(row)
        if not candidates or not tokenize(query):
            return []

        texts = [f"{r['title']} {r['section']} {r['text']}" for r in candidates]
        lexical = bm25_scores(query, texts)
        query_vector = self.embedder.embed([query])[0]
        vector = [cosine(query_vector, unpack(r["embedding"])) for r in candidates]

        def ranks(scores: list[float]) -> dict[int, int]:
            order = sorted(range(len(scores)), key=lambda i: -scores[i])
            return {index: rank for rank, index in enumerate(order, start=1) if scores[index] > 0}

        lexical_rank, vector_rank = ranks(lexical), ranks(vector)
        fused: dict[int, float] = {}
        for index in range(len(candidates)):
            score = 0.0
            if mode in ("hybrid", "lexical") and index in lexical_rank:
                score += 1 / (RRF_K + lexical_rank[index])
            if mode in ("hybrid", "vector") and index in vector_rank:
                score += 1 / (RRF_K + vector_rank[index])
            if score:
                fused[index] = score
        shortlist = sorted(fused, key=lambda i: -fused[i])[: max(limit * 4, 10)]

        # Rerank: what share of the query's terms does the chunk actually contain, and does it
        # contain the query's word pairs in order? Cheap stand-in for a cross-encoder.
        terms = tokenize(query)
        pairs = set(zip(terms, terms[1:], strict=False))
        best_fused = max(fused.values()) if fused else 1.0
        hits = []
        for index in shortlist:
            chunk_terms = tokenize(texts[index])
            coverage = len(set(terms) & set(chunk_terms)) / len(set(terms))
            phrase = len(pairs & set(zip(chunk_terms, chunk_terms[1:], strict=False))) / len(pairs) if pairs else 0.0
            heading_terms = set(tokenize(f"{candidates[index]['title']} {candidates[index]['section']}"))
            heading = len(set(terms) & heading_terms) / len(set(terms))
            final = round(0.35 * fused[index] / best_fused + 0.4 * coverage + 0.1 * phrase + 0.15 * heading, 4)
            row = candidates[index]
            hits.append(
                Hit(
                    row["chunk_id"],
                    row["doc_id"],
                    row["title"],
                    row["section"],
                    row["text"],
                    row["doc_type"],
                    bool(row["trusted"]),
                    final,
                    {
                        "bm25": lexical[index],
                        "vector": round(vector[index], 4),
                        "rrf": round(fused[index], 5),
                        "coverage": round(coverage, 3),
                        "heading": round(heading, 3),
                        "phrase": round(phrase, 3),
                    },
                )
            )
        return sorted(hits, key=lambda h: -h.score)[:limit]

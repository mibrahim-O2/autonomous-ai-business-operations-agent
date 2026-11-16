"""Knowledge base: document ingestion and hybrid retrieval with citations."""

from .base import Hit, KnowledgeBase
from .embeddings import Embedder, HashingEmbedder
from .ingest import chunk_markdown, parse_front_matter

__all__ = ["Embedder", "HashingEmbedder", "Hit", "KnowledgeBase", "chunk_markdown", "parse_front_matter"]

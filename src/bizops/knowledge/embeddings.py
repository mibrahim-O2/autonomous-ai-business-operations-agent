"""Embeddings.

`HashingEmbedder` is a dependency-free local embedding: unigrams and bigrams
are hashed into a fixed number of dimensions (the "hashing trick") and the
vector is L2-normalised. It captures lexical overlap including word order in
bigrams, but not meaning: "refund" and "reimbursement" are unrelated to it.
That is a deliberate trade-off for an offline demo. A hosted or local neural
embedding model can be substituted by implementing `Embedder`; nothing else
changes, because vectors are only ever compared with each other.
"""

from __future__ import annotations

import hashlib
import math
from array import array
from typing import Protocol

from ..memory.store import tokenize


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    name = "hashing-v1"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        tokens = tokenize(text)
        features = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:], strict=False)]
        vector = [0.0] * self.dim
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0  # signed hashing reduces collision bias
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]


def pack(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def unpack(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    return values.tolist()


def cosine(a: list[float], b: list[float]) -> float:
    """Dot product; equal to cosine similarity because stored vectors are normalised."""
    return sum(x * y for x, y in zip(a, b, strict=False))

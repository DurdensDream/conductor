"""Pluggable embedder. `fake` (default) is a deterministic feature-hashing embedder so
the dedup tool is exercised offline in CI with *real* cosine similarity (overlapping
text -> similar vectors -> a genuine near-duplicate signal, not a tautology). `bge`
uses sentence-transformers bge-small for the demo (CPU torch)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache

from ..settings import get_settings


class Embedder(ABC):
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one unit-normalized vector per input text."""


@lru_cache
def get_embedder() -> Embedder:
    s = get_settings()
    if s.embedder == "bge":
        from .bge import BgeEmbedder

        return BgeEmbedder(s.embedding_model, s.embedding_device, s.embedding_dim)
    from .fake import HashEmbedder

    return HashEmbedder(s.embedding_dim)

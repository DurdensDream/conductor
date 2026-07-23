"""The duplicate-index seam. `search_duplicates` depends on this Protocol, not on a
concrete store, so the same tool runs against an in-memory index (tests / the isolated
agent runner) and against pgvector (the real worker) with zero code change.

Cosine similarity is used throughout; embedders return unit-normalized vectors, so a
dot product *is* cosine."""

from __future__ import annotations

from typing import Protocol

from ..models import DuplicateHit


class DuplicateIndex(Protocol):
    def search(self, external_id: str, vector: list[float], k: int, threshold: float) -> list[DuplicateHit]:
        """Return existing postings with cosine >= threshold, excluding external_id itself."""

    def add(self, external_id: str, title: str, vector: list[float]) -> None:
        """Add/refresh a posting in the corpus (idempotent by external_id)."""


class InMemoryIndex(DuplicateIndex):
    """Deterministic, dependency-free index for tests and the isolated agent runner."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, list[float]]] = {}

    def add(self, external_id: str, title: str, vector: list[float]) -> None:
        self._rows[external_id] = (title, vector)

    def search(self, external_id: str, vector: list[float], k: int, threshold: float) -> list[DuplicateHit]:
        hits = []
        for ext_id, (title, vec) in self._rows.items():
            if ext_id == external_id:
                continue
            score = sum(a * b for a, b in zip(vector, vec, strict=False))
            if score >= threshold:
                hits.append(DuplicateHit(posting_id=ext_id, score=round(min(1.0, max(0.0, score)), 4), title=title))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

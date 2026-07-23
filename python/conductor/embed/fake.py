"""Deterministic feature-hashing embedder (the CI default).

Bag-of-words hashed into `dim` buckets with a signed hash, then L2-normalized. No model,
no network, fully deterministic (Python's hash randomization is bypassed with a fixed
seed via blake2b). Crucially it preserves *semantics-by-overlap*: two postings that share
most tokens land close in cosine space, so `search_duplicates` produces a real
near-duplicate signal in tests — unlike a random or purely-positional hash."""

from __future__ import annotations

import hashlib
import math
import re

from .base import Embedder

_TOKEN = re.compile(r"[a-z0-9]+")


def _hash(token: str) -> int:
    # blake2b for a stable hash across processes (Python's built-in hash() is salted).
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")


class HashEmbedder(Embedder):
    def __init__(self, dim: int = 384):
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _TOKEN.findall(text.lower()):
            h = _hash(tok)
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0  # signed hashing reduces collisions
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

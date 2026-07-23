"""sentence-transformers bge-small embedder for the demo. Lazy import so CI (fake
embedder) never pulls torch. Install the demo extra: `pip install '.[demo]'` with the
CPU torch index (see the Dockerfile / README)."""

from __future__ import annotations

from .base import Embedder


class BgeEmbedder(Embedder):
    def __init__(self, model_name: str, device: str = "cpu", dim: int = 384):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name, device=device)
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = self._model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        return [v.tolist() for v in vecs]

"""search_duplicates — the vector-search / DB-lookup tool.

Embeds the posting (title + description) and queries the duplicate index for existing
postings above a cosine threshold. Reuses StreamRAG's embed-in-app pattern: the app
produces the vector, the store just does kNN. Returns the hits; the workflow decides
what a hit *means* (a high-scoring duplicate lowers confidence -> human review)."""

from __future__ import annotations

from ..embed.base import Embedder
from ..models import DuplicateHit, RawPosting
from .dedup_index import DuplicateIndex


def _doc(posting: RawPosting) -> str:
    return f"{posting.title}\n{posting.description}".strip()


def search_duplicates(
    posting: RawPosting, embedder: Embedder, index: DuplicateIndex, k: int = 5, threshold: float = 0.90
) -> list[DuplicateHit]:
    vector = embedder.embed([_doc(posting)])[0]
    return index.search(posting.external_id, vector, k=k, threshold=threshold)

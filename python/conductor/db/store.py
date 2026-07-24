"""The domain-store seam. Activities depend on this Protocol, not on Postgres, so the
same activity code runs against an in-memory store (time-skipping unit tests) and against
pgvector (the real worker + the chaos test). A `Store` also satisfies `DuplicateIndex`
(search/add), so one object serves the dedup tool and the audit/record writes.

Idempotency lives here: `append_audit` dedupes on (workflow_id, idempotency_key) and
returns whether the row was newly inserted — the caller logs a replay when it wasn't."""

from __future__ import annotations

from typing import Protocol

from ..models import DuplicateHit


class Store(Protocol):
    # ---- dedup corpus (satisfies tools.dedup_index.DuplicateIndex) ----
    def search(self, external_id: str, vector: list[float], k: int, threshold: float) -> list[DuplicateHit]: ...
    def add(self, external_id: str, title: str, vector: list[float]) -> None: ...

    # ---- append-only audit log (idempotent) ----
    def append_audit(
        self, workflow_id: str, external_id: str, idempotency_key: str, seq: int,
        step: str, status: str, detail: dict,
    ) -> bool:
        """Insert one audit event. Returns True if newly inserted, False if the key already
        existed (a retried/replayed activity — no duplicate side effect)."""

    # ---- final record (upsert) ----
    def upsert_record(self, external_id: str, record: dict, status: str, confidence: float) -> None: ...

    # ---- reads (API + tests) ----
    def get_record(self, external_id: str) -> dict | None: ...
    def audit_trail(self, workflow_id: str) -> list[dict]: ...


class InMemoryStore(Store):
    """Deterministic, dependency-free store for unit/time-skipping tests."""

    def __init__(self) -> None:
        self._vecs: dict[str, tuple[str, list[float]]] = {}
        self._audit: list[dict] = []
        self._audit_keys: set[tuple[str, str]] = set()
        self._records: dict[str, dict] = {}

    # dedup
    def add(self, external_id: str, title: str, vector: list[float]) -> None:
        self._vecs[external_id] = (title, vector)

    def search(self, external_id: str, vector: list[float], k: int, threshold: float) -> list[DuplicateHit]:
        hits = []
        for ext_id, (title, vec) in self._vecs.items():
            if ext_id == external_id:
                continue
            score = sum(a * b for a, b in zip(vector, vec, strict=False))
            if score >= threshold:
                hits.append(DuplicateHit(posting_id=ext_id, score=round(min(1.0, max(0.0, score)), 4), title=title))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    # audit
    def append_audit(self, workflow_id, external_id, idempotency_key, seq, step, status, detail) -> bool:
        key = (workflow_id, idempotency_key)
        if key in self._audit_keys:
            # Replay/retry: refresh detail+status to the latest attempt (mirrors the pg
            # DO UPDATE), but report it as not-newly-inserted so the caller dedups correctly.
            for e in self._audit:
                if e["workflow_id"] == workflow_id and e["idempotency_key"] == idempotency_key:
                    e["detail"], e["status"] = detail, status
                    break
            return False
        self._audit_keys.add(key)
        self._audit.append({
            "workflow_id": workflow_id, "external_id": external_id, "idempotency_key": idempotency_key,
            "seq": seq, "step": step, "status": status, "detail": detail,
        })
        return True

    # record
    def upsert_record(self, external_id, record, status, confidence) -> None:
        self._records[external_id] = {"record": record, "status": status, "confidence": confidence}

    # reads
    def get_record(self, external_id: str) -> dict | None:
        return self._records.get(external_id)

    def audit_trail(self, workflow_id: str) -> list[dict]:
        return [e for e in self._audit if e["workflow_id"] == workflow_id]

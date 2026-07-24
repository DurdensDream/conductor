"""pgvector-backed Store (the real worker + chaos test). Synchronous psycopg3 over a
connection pool — the tool/activity layer is sync (LLM clients + embedder are sync), and
Temporal runs sync activities in a thread pool, so a sync DB fits cleanly and never blocks
the workflow event loop.

Cosine similarity via the `<=>` operator on a unit-normalized vector column: score =
1 - cosine_distance."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import psycopg
from pgvector import Vector
from psycopg_pool import ConnectionPool

from ..models import DuplicateHit

log = logging.getLogger(__name__)

_SCHEMA = Path(__file__).with_name("schema.sql")


def init_schema(dsn: str) -> None:
    """Apply schema.sql (idempotent — all CREATE ... IF NOT EXISTS). Compose auto-runs it
    via docker-entrypoint-initdb.d; tests and the chaos harness call this directly."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(_SCHEMA.read_text())


class PgStore:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 8):
        # Register the pgvector adapter on every pooled connection so list[float] <-> vector
        # round-trips without manual encoding.
        def _configure(conn):
            from pgvector.psycopg import register_vector

            register_vector(conn)

        self._pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, configure=_configure, open=True)

    def close(self) -> None:
        self._pool.close()

    # ---- dedup ----
    def add(self, external_id: str, title: str, vector: list[float]) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO posting_embeddings (external_id, title, embedding) VALUES (%s, %s, %s) "
                "ON CONFLICT (external_id) DO UPDATE SET title = EXCLUDED.title, embedding = EXCLUDED.embedding",
                (external_id, title, Vector(vector)),
            )

    def search(self, external_id: str, vector: list[float], k: int, threshold: float) -> list[DuplicateHit]:
        # Wrap in Vector so the param serializes as pgvector's `vector` type, not float8[].
        vec = Vector(vector)
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT external_id, title, 1 - (embedding <=> %s) AS score "
                "FROM posting_embeddings WHERE external_id <> %s "
                "ORDER BY embedding <=> %s LIMIT %s",
                (vec, external_id, vec, k),
            ).fetchall()
        hits = [
            DuplicateHit(posting_id=r[0], score=round(min(1.0, max(0.0, float(r[2]))), 4), title=r[1] or "")
            for r in rows
            if r[2] is not None and float(r[2]) >= threshold
        ]
        return hits

    # ---- audit (idempotent) ----
    def append_audit(self, workflow_id, external_id, idempotency_key, seq, step, status, detail) -> bool:
        # DO UPDATE (not DO NOTHING) so a retried non-deterministic activity refreshes the
        # recorded detail/status to the attempt Temporal actually committed (the last one to
        # run wins). `RETURNING (xmax = 0)` still tells insert (True) from update (False), so
        # the caller's idempotency/replay signal — and the run-count metric — stay correct.
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO audit_log (workflow_id, external_id, idempotency_key, seq, step, status, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (workflow_id, idempotency_key) "
                "DO UPDATE SET detail = EXCLUDED.detail, status = EXCLUDED.status "
                "RETURNING (xmax = 0) AS inserted",
                (workflow_id, external_id, idempotency_key, seq, step, status, json.dumps(detail)),
            ).fetchone()
            return bool(row[0])  # True => newly inserted; False => row already existed (replay/retry)

    # ---- record (upsert) ----
    def upsert_record(self, external_id, record, status, confidence) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO postings (external_id, record, status, confidence) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (external_id) DO UPDATE SET record = EXCLUDED.record, "
                "status = EXCLUDED.status, confidence = EXCLUDED.confidence, updated_at = now()",
                (external_id, json.dumps(record), status, confidence),
            )

    # ---- reads ----
    def get_record(self, external_id: str) -> dict | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT record, status, confidence FROM postings WHERE external_id = %s", (external_id,)
            ).fetchone()
        if not row:
            return None
        return {"record": row[0], "status": row[1], "confidence": row[2]}

    def audit_trail(self, workflow_id: str) -> list[dict]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT seq, step, status, detail, ts FROM audit_log WHERE workflow_id = %s ORDER BY seq, id",
                (workflow_id,),
            ).fetchall()
        return [{"seq": r[0], "step": r[1], "status": r[2], "detail": r[3], "ts": r[4].isoformat()} for r in rows]

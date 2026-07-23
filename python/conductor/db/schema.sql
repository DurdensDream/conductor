-- Conductor app database (domain state). Temporal owns *workflow* state in its own
-- Postgres; this store owns *domain* state: final records, the append-only audit log,
-- and the pgvector dedup corpus.

CREATE EXTENSION IF NOT EXISTS vector;

-- Final clean records. Upserted on external_id, so a re-run/replay never duplicates.
CREATE TABLE IF NOT EXISTS postings (
    external_id TEXT PRIMARY KEY,
    record      JSONB            NOT NULL,
    status      TEXT             NOT NULL,
    confidence  DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ      NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ      NOT NULL DEFAULT now()
);

-- Append-only step history. The UNIQUE(workflow_id, idempotency_key) constraint is the
-- idempotency guarantee: an activity that physically re-executes after a crash (the
-- window between its side effect and Temporal recording completion) inserts with the
-- same key and is a no-op — so "no duplicate side effects" is enforced by the schema,
-- not hoped for. The chaos test asserts exactly one row per logical step.
CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    workflow_id     TEXT        NOT NULL,
    external_id     TEXT        NOT NULL,
    idempotency_key TEXT        NOT NULL,
    seq             INT         NOT NULL,
    step            TEXT        NOT NULL,
    status          TEXT        NOT NULL,
    detail          JSONB       NOT NULL DEFAULT '{}',
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workflow_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS audit_log_wf ON audit_log (workflow_id, seq);

-- Dedup corpus. Embeddings produced in-app (StreamRAG pattern); Postgres just does kNN.
CREATE TABLE IF NOT EXISTS posting_embeddings (
    external_id TEXT PRIMARY KEY,
    title       TEXT        NOT NULL DEFAULT '',
    embedding   vector(384) NOT NULL
);
-- HNSW cosine index: no training step (unlike ivfflat), good for a small/medium corpus.
CREATE INDEX IF NOT EXISTS posting_embeddings_hnsw
    ON posting_embeddings USING hnsw (embedding vector_cosine_ops);

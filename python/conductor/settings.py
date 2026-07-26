"""Central, env-driven configuration shared by the worker, the API and the eval
harness — one source of truth for hosts, models, thresholds and the pluggable
provider selection.

The provider defaults to `fake` so tests and CI are hermetic (no network, no model,
no GPU). Local demos set `LLM_PROVIDER=ollama`; a cloud upgrade sets `anthropic`.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- Temporal ----
    temporal_address: str = "temporal:7233"
    temporal_namespace: str = "default"
    # The task queue is NOT here: worker and API must agree on it, so it is a shared constant
    # (`temporal/shared.py`), and the activity retry/timeout knobs live in
    # `temporal/constants.py` because the workflow sandbox can't read env. Keeping an
    # env-settable copy of either here would give two sources of truth, one of them dead.
    #
    # Bound the worker's wait for its two dependencies (Temporal + Postgres) at boot.
    startup_connect_timeout_seconds: float = 60.0

    # ---- App Postgres (domain state: records, audit log, dedup corpus) ----
    postgres_user: str = "conductor"
    postgres_password: str = "conductor"
    postgres_db: str = "conductor"
    postgres_host: str = "app-db"
    postgres_port: int = 5432

    # ---- LLM (pluggable) ----
    llm_provider: str = "fake"  # fake | ollama | anthropic
    ollama_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "llama3.2:3b"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"  # bare alias — never date-suffixed

    # ---- Eval judge (falls back to the primary LLM when unset) ----
    judge_provider: str = ""
    judge_model: str = ""

    # ---- Embeddings (pluggable: fake hash embedder for CI, bge-small for demo) ----
    embedder: str = "fake"  # fake | bge
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    embedding_device: str = "cpu"

    # ---- Agent / workflow behavior ----
    max_agent_turns: int = 8
    confidence_threshold: float = 0.70  # below this (or policy BLOCK) -> human review
    dedup_score_threshold: float = 0.90  # cosine >= this counts as a near-duplicate
    review_sla_seconds: int = 86_400  # 24h; no decision -> REVIEW_TIMED_OUT

    # ---- Observability ----
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    otel_traces_endpoint: str = "http://jaeger:4317"  # Jaeger v2 ingests OTLP directly
    otel_service_name: str = "conductor"
    otel_enabled: bool = True

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def effective_judge_provider(self) -> str:
        return self.judge_provider or self.llm_provider

    @property
    def effective_judge_model(self) -> str:
        if self.judge_model:
            return self.judge_model
        return self.anthropic_model if self.effective_judge_provider == "anthropic" else self.ollama_model


@lru_cache
def get_settings() -> Settings:
    return Settings()

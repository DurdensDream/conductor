"""Hermetic defaults for the test session: telemetry off and fake providers, set BEFORE
`get_settings()` is first called (and thus cached). Keeps the API lifespan from spinning up
real OTLP exporters to jaeger/otel-collector during unit tests, and keeps every test on the
deterministic fake LLM/embedder unless a test overrides it explicitly."""

from __future__ import annotations

import os

os.environ.setdefault("OTEL_ENABLED", "false")
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("EMBEDDER", "fake")

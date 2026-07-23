"""Wraps an LLM to emit OTel metrics (latency + tokens + cost) on every call. The worker
wraps its generation LLM in this, so token/cost show up in Grafana with zero changes to
the agent or tools. No-ops safely when telemetry is disabled (T's record methods do)."""

from __future__ import annotations

import time

from ..obs.telemetry import T
from .base import LLMClient, LLMResult


class MeteredLLM(LLMClient):
    def __init__(self, inner: LLMClient):
        self.inner = inner
        self.model = inner.model
        self.provider = inner.provider

    def generate(self, system: str, user: str, max_tokens: int = 1024) -> LLMResult:
        t0 = time.perf_counter()
        r = self.inner.generate(system, user, max_tokens)
        T.record_llm(time.perf_counter() - t0, r.tokens_in, r.tokens_out, r.cost_usd, r.model)
        return r

    def generate_json(self, system: str, user: str, schema: dict, max_tokens: int = 512):
        t0 = time.perf_counter()
        parsed, r = self.inner.generate_json(system, user, schema, max_tokens)
        T.record_llm(time.perf_counter() - t0, r.tokens_in, r.tokens_out, r.cost_usd, r.model)
        return parsed, r

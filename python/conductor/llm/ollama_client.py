"""Local Ollama backend (the demo default). Free, offline, on the host at :11434.

Uses the typed python client (>=0.6): `resp.message.content`, and native structured
output via `format=<json schema>` — which is the reliable path on small models
(llama3.2:3b's native function-calling is not dependable; JSON-schema-constrained
output is). For a higher-quality local demo, point `OLLAMA_MODEL` at a larger
tool-tuned model (e.g. qwen3.5:9b)."""

from __future__ import annotations

import ollama

from .base import LLMClient, LLMResult, extract_json


class OllamaClient(LLMClient):
    provider = "ollama"

    def __init__(self, model: str, host: str, num_ctx: int = 8192, timeout: float = 120.0):
        self.model = model
        # A timeout is mandatory: a wedged Ollama with no deadline would hang an activity
        # until Temporal's start_to_close fires — the timeout makes the failure fast and
        # local so the RetryPolicy can do its job. ollama.Client forwards to httpx.
        self._client = ollama.Client(host=host, timeout=timeout)
        self._options = {"temperature": 0, "num_ctx": num_ctx}

    def _messages(self, system: str, user: str) -> list[dict]:
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def generate(self, system: str, user: str, max_tokens: int = 1024) -> LLMResult:
        resp = self._client.chat(
            model=self.model,
            messages=self._messages(system, user),
            options={**self._options, "num_predict": max_tokens},
        )
        return LLMResult(
            text=resp.message.content or "",
            model=self.model,
            tokens_in=resp.prompt_eval_count or 0,
            tokens_out=resp.eval_count or 0,
            cost_usd=0.0,  # local
        )

    def generate_json(
        self, system: str, user: str, schema: dict, max_tokens: int = 512
    ) -> tuple[dict | None, LLMResult]:
        # Tell it twice: native format= AND a prompt instruction (small models comply better).
        resp = self._client.chat(
            model=self.model,
            messages=self._messages(system + "\nRespond ONLY with a single JSON object.", user),
            format=schema,
            options={**self._options, "num_predict": max_tokens},
        )
        result = LLMResult(
            text=resp.message.content or "",
            model=self.model,
            tokens_in=resp.prompt_eval_count or 0,
            tokens_out=resp.eval_count or 0,
        )
        return extract_json(result.text), result

"""LLM provider contract. One `generate` / `generate_json` interface so the agent's
decision node and the eval judge don't care whether the backend is a deterministic
fake (CI), local Ollama (demo) or Anthropic Claude (quality).

`generate_json` is the workhorse: the agent picks tools and assembles the final record
via structured output, which is far more reliable on small local models than native
function-calling (verified for llama3.2:3b)."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResult:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class LLMClient(ABC):
    model: str
    provider: str

    @abstractmethod
    def generate(self, system: str, user: str, max_tokens: int = 1024) -> LLMResult:
        """Free-text generation."""

    @abstractmethod
    def generate_json(
        self, system: str, user: str, schema: dict, max_tokens: int = 512
    ) -> tuple[dict | None, LLMResult]:
        """Structured generation. Returns (parsed_or_None, raw_result)."""


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model response (handles ```json fences and
    leading prose from small local models)."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None

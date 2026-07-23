"""Pick the LLM backend from config. Provider imports are lazy so a fake/Ollama-only
run doesn't require the anthropic SDK (and vice versa). `fake` is the default so tests
and CI never touch the network."""

from __future__ import annotations

from functools import lru_cache

from ..settings import get_settings
from .base import LLMClient


def build_llm(provider: str, model: str) -> LLMClient:
    s = get_settings()
    if provider == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(model, api_key=s.anthropic_api_key)
    if provider == "ollama":
        from .ollama_client import OllamaClient

        return OllamaClient(model, s.ollama_url)
    from .fake import FakeLLM

    return FakeLLM()


@lru_cache
def get_generation_llm() -> LLMClient:
    s = get_settings()
    model = s.anthropic_model if s.llm_provider == "anthropic" else s.ollama_model
    return build_llm(s.llm_provider, model)


@lru_cache
def get_judge_llm() -> LLMClient:
    s = get_settings()
    return build_llm(s.effective_judge_provider, s.effective_judge_model)

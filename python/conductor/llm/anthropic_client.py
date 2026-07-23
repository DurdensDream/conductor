"""Anthropic Claude backend (optional quality upgrade).

Reads ANTHROPIC_API_KEY from Settings. Notes baked in from the current API:
  * temperature is accepted ONLY on Haiku 4.5; Sonnet 5 / Opus reject it (400).
  * never append a date suffix to an alias (`claude-haiku-4-5`, not `...-20xxxxxx`).
  * read text by joining text blocks, never `content[0].text` blindly.
"""

from __future__ import annotations

import anthropic

from .base import LLMClient, LLMResult, extract_json

# (input, output) USD per million tokens.
_PRICING = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
}


def _cost(model: str, tin: int, tout: int) -> float:
    pin, pout = next((v for k, v in _PRICING.items() if model.startswith(k)), (1.0, 5.0))
    return tin / 1e6 * pin + tout / 1e6 * pout


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, model: str, api_key: str = "", timeout: float = 60.0):
        self.model = model
        # Pass the key explicitly: pydantic-settings loads .env into Settings but does NOT
        # export it to os.environ, so a bare Anthropic() (env-only) never sees a key set in
        # .env. Bounded timeout + one retry so the sync endpoint can't hang an activity.
        self._client = anthropic.Anthropic(api_key=api_key or None, timeout=timeout, max_retries=1)
        self._temp_ok = "haiku" in model  # only Haiku 4.5 accepts temperature

    def _create(self, system: str, user: str, max_tokens: int):
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self._temp_ok:
            kwargs["temperature"] = 0
        return self._client.messages.create(**kwargs)

    @staticmethod
    def _text(resp) -> str:
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    def _result(self, resp) -> LLMResult:
        tin = resp.usage.input_tokens
        tout = resp.usage.output_tokens
        return LLMResult(
            text=self._text(resp), model=self.model, tokens_in=tin, tokens_out=tout,
            cost_usd=_cost(self.model, tin, tout),
        )

    def generate(self, system: str, user: str, max_tokens: int = 1024) -> LLMResult:
        return self._result(self._create(system, user, max_tokens))

    def generate_json(
        self, system: str, user: str, schema: dict, max_tokens: int = 512
    ) -> tuple[dict | None, LLMResult]:
        sys = f"{system}\nRespond ONLY with a single JSON object matching this schema: {schema}"
        result = self._result(self._create(sys, user, max_tokens))
        return extract_json(result.text), result

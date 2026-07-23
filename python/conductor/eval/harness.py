"""Drives the gold set through the agent (the same decision graph + tools + finalize +
routing the durable workflow uses — durability itself is proven by the workflow + chaos
tests, so the eval focuses on agent *quality*). Fresh context per case isolates the dedup
corpus; a token-counting LLM wrapper yields real cost/run numbers."""

from __future__ import annotations

from ..agent.runner import run_agent
from ..embed.base import get_embedder
from ..llm.base import LLMClient, LLMResult
from ..llm.factory import get_generation_llm
from ..models import RawPosting
from ..tools.dedup_index import InMemoryIndex
from ..tools.registry import ToolContext
from .gold import GOLD, GoldCase
from .metrics import validate_record


class _CountingLLM(LLMClient):
    """Wraps any LLM to accumulate token/cost usage for the cost/run metric."""

    def __init__(self, inner: LLMClient):
        self.inner = inner
        self.model = inner.model
        self.provider = inner.provider
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0

    def _acc(self, r: LLMResult) -> None:
        self.tokens_in += r.tokens_in
        self.tokens_out += r.tokens_out
        self.cost_usd += r.cost_usd

    def generate(self, system, user, max_tokens=1024) -> LLMResult:
        r = self.inner.generate(system, user, max_tokens)
        self._acc(r)
        return r

    def generate_json(self, system, user, schema, max_tokens=512):
        parsed, r = self.inner.generate_json(system, user, schema, max_tokens)
        self._acc(r)
        return parsed, r


def run_case(case: GoldCase) -> dict:
    embedder = get_embedder()
    index = InMemoryIndex()
    if case.seed:
        doc = f"{case.seed['title']}\n{case.seed.get('description', '')}"
        index.add(case.seed["external_id"], case.seed["title"], embedder.embed([doc])[0])
    llm = _CountingLLM(get_generation_llm())
    ctx = ToolContext(llm=llm, embedder=embedder, index=index, dedup_threshold=0.90)

    res = run_agent(RawPosting(**case.raw), ctx)
    record = res.record.model_dump()
    return {
        "tag": case.tag,
        "record": record,
        "trajectory": res.trajectory,
        "expected_trajectory": case.expected_trajectory,
        "expect": case.expect,
        "needs_review": res.needs_review,
        "review_reason": res.review_reason,
        "valid": validate_record(record),
        "tokens_in": llm.tokens_in,
        "tokens_out": llm.tokens_out,
        "cost_usd": llm.cost_usd,
    }


def run_gold(cases: list[GoldCase] | None = None) -> list[dict]:
    return [run_case(c) for c in (cases or GOLD)]

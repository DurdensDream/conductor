"""The tool registry + dispatcher.

The registry is the single source of truth for the tool menu: name, human/LLM-facing
description, and output model. The agent reads the menu to decide; the workflow calls
`run_tool(name, ...)` to execute the chosen tool as an activity. `observations` carries
prior tool outputs (raw dicts) so a tool that needs an earlier result — policy needs the
parsed salary — can read it without the workflow hand-wiring every dependency."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from ..embed.base import Embedder
from ..llm.base import LLMClient
from ..models import Category, DuplicateHit, Location, PolicyVerdict, RawPosting, SalaryRange
from .category import classify_category
from .dedup import search_duplicates
from .dedup_index import DuplicateIndex
from .location import normalize_location
from .policy import check_policy
from .salary import parse_salary


@dataclass
class ToolContext:
    """Dependencies a tool may need. Built once per worker (real: pgvector index, live
    LLM/embedder) or per test (in-memory index, fake LLM/embedder)."""

    llm: LLMClient
    embedder: Embedder
    index: DuplicateIndex
    dedup_k: int = 5
    dedup_threshold: float = 0.90


@dataclass
class ToolSpec:
    name: str
    description: str
    output_model: type[BaseModel]
    # (posting, ctx, observations) -> output model instance
    fn: Callable[[RawPosting, ToolContext, dict], BaseModel]


class DedupResult(BaseModel):
    """Uniform model wrapper so every tool returns a BaseModel (search_duplicates would
    otherwise return a bare list)."""

    hits: list[DuplicateHit] = []


def _t_location(p: RawPosting, ctx: ToolContext, obs: dict) -> Location:
    return normalize_location(p)


def _t_salary(p: RawPosting, ctx: ToolContext, obs: dict) -> SalaryRange | None:
    return parse_salary(p)


def _t_category(p: RawPosting, ctx: ToolContext, obs: dict) -> Category:
    return classify_category(p, ctx.llm)


def _t_dedup(p: RawPosting, ctx: ToolContext, obs: dict) -> DedupResult:
    hits = search_duplicates(p, ctx.embedder, ctx.index, k=ctx.dedup_k, threshold=ctx.dedup_threshold)
    return DedupResult(hits=hits)


def _t_policy(p: RawPosting, ctx: ToolContext, obs: dict) -> PolicyVerdict:
    salary = None
    raw = obs.get("parse_salary")
    if raw:
        try:
            salary = SalaryRange.model_validate(raw)
        except Exception:
            salary = None
    return check_policy(p, salary)


TOOLS: dict[str, ToolSpec] = {
    "normalize_location": ToolSpec(
        "normalize_location",
        "Parse the location string and resolve city/state/country + remote/hybrid/onsite. "
        "Use when location_raw is present or remoteness is unclear.",
        Location,
        _t_location,
    ),
    "parse_salary": ToolSpec(
        "parse_salary",
        "Parse the salary string into min/max/currency/period. Use only when salary_raw is present.",
        SalaryRange,
        _t_salary,
    ),
    "classify_category": ToolSpec(
        "classify_category",
        "Classify the posting into an O*NET occupation code + label. Always useful.",
        Category,
        _t_category,
    ),
    "search_duplicates": ToolSpec(
        "search_duplicates",
        "Vector-search existing postings for near-duplicates. Always run to catch re-posts.",
        DedupResult,
        _t_dedup,
    ),
    "check_policy": ToolSpec(
        "check_policy",
        "Flag discriminatory language / banned terms / salary sanity. Enforced as a guardrail.",
        PolicyVerdict,
        _t_policy,
    ),
}

# Tools the agent may select turn-by-turn. check_policy is NOT here: it is a mandatory
# guardrail the workflow always runs, so it can't be skipped by a mis-planning agent.
AGENT_TOOLS = ["normalize_location", "parse_salary", "classify_category", "search_duplicates"]


def run_tool(name: str, posting: RawPosting, ctx: ToolContext, observations: dict | None = None) -> BaseModel:
    spec = TOOLS.get(name)
    if spec is None:
        raise KeyError(f"unknown tool: {name!r}")
    return spec.fn(posting, ctx, observations or {})

"""In-process agent runner: drives the full plan->act->observe->finalize loop WITHOUT
Temporal. This proves the agent logic in isolation (FR-2) and is reused by the eval
harness's fast path. The durable Temporal workflow mirrors this exact loop, but executes
each step as an activity — the shared pure functions (decide, run_tool, assemble_record)
keep the two from drifting."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import EnrichedPosting, RawPosting
from ..settings import get_settings
from ..tools.registry import ToolContext, run_tool
from .finalize import assemble_record, needs_human_review
from .graph import build_agent_graph, decide


@dataclass
class RunResult:
    record: EnrichedPosting
    trajectory: list[str]
    observations: dict
    needs_review: bool
    review_reason: str
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    events: list[str] = field(default_factory=list)


def run_agent(posting: RawPosting, ctx: ToolContext, max_turns: int | None = None) -> RunResult:
    s = get_settings()
    max_turns = max_turns or s.max_agent_turns
    graph = build_agent_graph(ctx.llm)

    posting_d = posting.model_dump()
    tools_run: list[str] = []
    observations: dict = {}
    trajectory: list[str] = []
    events: list[str] = ["input_validated"]

    for _ in range(max_turns):
        decision = decide(graph, posting_d, tools_run, observations)
        if decision.action == "finish":
            break
        name = decision.tool
        output = run_tool(name, posting, ctx, observations)
        observations[name] = output.model_dump()
        tools_run.append(name)
        trajectory.append(name)
        events.append(f"tool:{name}")

    # check_policy is a mandatory guardrail step (not agent-selectable) — always run it.
    policy = run_tool("check_policy", posting, ctx, observations)
    observations["check_policy"] = policy.model_dump()
    events.append("policy_checked")

    review, reason = needs_human_review(posting, observations, s.confidence_threshold)
    record = assemble_record(posting, observations, trajectory)
    events.append("record_assembled")
    return RunResult(
        record=record,
        trajectory=trajectory,
        observations=observations,
        needs_review=review,
        review_reason=reason,
        events=events,
    )

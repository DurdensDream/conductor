"""Agent-loop tests (FR-2): the in-process runner produces a valid EnrichedPosting with a
sensible, content-aware trajectory, and routes low-confidence/policy/duplicate cases to
review. Deterministic via the FakeLLM."""

from __future__ import annotations

from conductor.agent.finalize import compute_confidence, needs_human_review
from conductor.agent.graph import _valid
from conductor.agent.runner import run_agent
from conductor.embed.fake import HashEmbedder
from conductor.llm.fake import FakeLLM
from conductor.models import AgentAction, AgentDecision, EnrichedPosting, RawPosting
from conductor.tools.dedup_index import InMemoryIndex
from conductor.tools.registry import ToolContext


def _ctx() -> ToolContext:
    return ToolContext(llm=FakeLLM(), embedder=HashEmbedder(384), index=InMemoryIndex())


def test_clean_posting_auto_resolves():
    p = RawPosting(source="li", external_id="c1", title="Senior Python Engineer",
                   description="Backend Kafka Postgres. Hybrid.", location_raw="Bangalore, India",
                   salary_raw="$120k-$150k/yr")
    r = run_agent(p, _ctx())
    EnrichedPosting.model_validate(r.record.model_dump())  # output guardrail
    assert r.trajectory == ["normalize_location", "parse_salary", "classify_category", "search_duplicates"]
    assert not r.needs_review
    assert r.record.confidence >= 0.7


def test_no_salary_skips_salary_tool():
    # A content-aware agent must NOT call parse_salary when there's no salary text —
    # this is what makes tool-selection-accuracy a real metric, not a tautology.
    p = RawPosting(source="li", external_id="c2", title="Registered Nurse ICU",
                   description="Patient care.", location_raw="London")
    r = run_agent(p, _ctx())
    assert "parse_salary" not in r.trajectory
    assert "classify_category" in r.trajectory


def test_policy_block_forces_review():
    p = RawPosting(source="x", external_id="c3", title="Packer - men only",
                   description="no women. forklift.", location_raw="Austin")
    r = run_agent(p, _ctx())
    assert r.needs_review
    assert r.review_reason.startswith("policy_block")


def test_near_duplicate_forces_review():
    ctx = _ctx()
    doc = "Senior Python Engineer\nBackend Kafka Postgres Hybrid"
    ctx.index.add("seed", "Senior Python Engineer", ctx.embedder.embed([doc])[0])
    p = RawPosting(source="li", external_id="c4", title="Senior Python Engineer",
                   description="Backend Kafka Postgres Hybrid", location_raw="Bangalore, India")
    r = run_agent(p, ctx)
    assert r.needs_review
    assert r.review_reason.startswith("near_duplicate")


def test_confidence_penalizes_unresolved_location():
    p = RawPosting(source="x", external_id="c5", title="Mystery Role", description="unclear")
    obs = {"normalize_location": {"resolved": False, "remote_type": "UNKNOWN"},
           "classify_category": {"onet_code": "00-0000.00", "label": "General", "confidence": 0.3}}
    low = compute_confidence(p, obs)
    review, _ = needs_human_review(p, obs, 0.7)
    assert low < 0.7 and review


def test_valid_rejects_tool_whose_input_is_absent():
    # Regression for the parse_salary-None crash: _valid must not accept a tool whose only
    # input field is missing, even if a (real) LLM proposes it. Otherwise run_tool would
    # return None and the activity would crash on .model_dump().
    no_salary = {"title": "Backend Dev", "salary_raw": "", "location_raw": "London"}
    assert not _valid(AgentDecision(action=AgentAction.CALL_TOOL, tool="parse_salary"), [], no_salary)
    assert _valid(AgentDecision(action=AgentAction.CALL_TOOL, tool="classify_category"), [], no_salary)
    no_loc = {"title": "Backend Dev", "salary_raw": "$90k", "location_raw": ""}
    assert not _valid(AgentDecision(action=AgentAction.CALL_TOOL, tool="normalize_location"), [], no_loc)
    assert _valid(AgentDecision(action=AgentAction.CALL_TOOL, tool="parse_salary"), [], no_loc)
    # finish is always valid; an already-run tool is not.
    assert _valid(AgentDecision(action=AgentAction.FINISH), ["classify_category"], no_salary)
    assert not _valid(AgentDecision(action=AgentAction.CALL_TOOL, tool="classify_category"),
                      ["classify_category"], no_salary)

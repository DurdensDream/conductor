"""The inner agent's per-turn decision graph (LangGraph).

One invocation = one decision: given the posting + what's been done so far, pick the next
tool or finish. Two nodes — `propose` (LLM structured output) and `repair` (a bounded
deterministic fallback) — joined by one conditional edge whose `route` function validates the
proposal (is the choice a real, not-yet-run, runnable tool?) and either ENDs, re-proposes
while attempts remain, or drops to `repair`. Temporal drives the loop *across* turns, so each
tool call is its own durable activity — that's why this graph runs to completion inside a
single activity with no checkpointer (durability is Temporal's job).

We use structured-output routing (LLM emits an AgentDecision) rather than native
function-calling because it is far more reliable on small local models (llama3.2:3b)."""

from __future__ import annotations

import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ..llm.base import LLMClient
from ..models import AgentAction, AgentDecision
from ..tools.registry import AGENT_TOOLS, TOOLS

_SYSTEM = (
    "You are a job-posting intake agent. Decide the SINGLE next step. You may call one tool "
    "or finish. Prefer calling a tool that has not run yet and is relevant to this posting; "
    "finish once the needed enrichment tools have run. Respond as JSON: "
    '{"action": "call_tool"|"finish", "tool": <name or null>, "tool_args": {}, "reasoning": <short>}.'
)


class DecisionState(TypedDict, total=False):
    posting: dict          # RawPosting.model_dump()
    tools_run: list[str]   # names already executed this run
    observations: dict     # name -> tool output dict
    decision: dict         # AgentDecision.model_dump()
    attempts: int          # propose attempts (bounds the repair loop)


def _tool_menu() -> str:
    return "\n".join(f"- {n}: {TOOLS[n].description}" for n in AGENT_TOOLS)


def _present_fields(posting: dict) -> list[str]:
    fields = []
    if posting.get("location_raw", "").strip():
        fields.append("location_raw")
    if posting.get("salary_raw", "").strip():
        fields.append("salary_raw")
    fields.append("title")
    return fields


def build_prompt(state: DecisionState) -> str:
    posting = state["posting"]
    tools_run = state.get("tools_run", [])
    # The compact STATE_JSON block is what the deterministic FakeLLM keys on; real models
    # also see it and it only sharpens the decision.
    state_json = json.dumps({"present_fields": _present_fields(posting), "tools_run": tools_run})
    return (
        f"POSTING:\n title: {posting.get('title', '')}\n"
        f" location_raw: {posting.get('location_raw', '')!r}\n"
        f" salary_raw: {posting.get('salary_raw', '')!r}\n"
        f" description: {posting.get('description', '')[:600]}\n\n"
        f"AVAILABLE TOOLS:\n{_tool_menu()}\n\n"
        f"TOOLS ALREADY RUN: {tools_run}\n"
        f"STATE_JSON: {state_json}\n\n"
        "Pick the next step."
    )


def _valid(decision: AgentDecision, tools_run: list[str], posting: dict) -> bool:
    if decision.action == AgentAction.FINISH:
        return True
    if decision.tool not in AGENT_TOOLS or decision.tool in tools_run:
        return False
    # Field-presence gate (mirrors _fallback): a tool whose only input is absent must not be
    # selectable — otherwise a real LLM picking parse_salary on a posting with no salary_raw
    # would run a tool that returns nothing. Keeps the agent's choices executable.
    present = set(_present_fields(posting))
    if decision.tool == "parse_salary" and "salary_raw" not in present:
        return False
    if decision.tool == "normalize_location" and "location_raw" not in present:
        return False
    return True


def _fallback(tools_run: list[str], posting: dict) -> AgentDecision:
    """Deterministic next-tool policy used when the LLM's choice is unusable. Mirrors a
    competent agent: run each relevant, not-yet-run tool, then finish."""
    present = set(_present_fields(posting))
    for tool in AGENT_TOOLS:
        if tool in tools_run:
            continue
        if tool == "parse_salary" and "salary_raw" not in present:
            continue
        if tool == "normalize_location" and "location_raw" not in present:
            continue
        return AgentDecision(action=AgentAction.CALL_TOOL, tool=tool, reasoning="fallback: next needed tool")
    return AgentDecision(action=AgentAction.FINISH, reasoning="fallback: nothing left to do")


def build_agent_graph(llm: LLMClient):
    """Compile the decision graph bound to an LLM. Cheap to build; the worker builds it once."""

    def propose(state: DecisionState) -> DecisionState:
        prompt = build_prompt(state)
        parsed, _ = llm.generate_json(_SYSTEM, prompt, AgentDecision.model_json_schema(), max_tokens=200)
        attempts = state.get("attempts", 0) + 1
        try:
            decision = AgentDecision.model_validate(parsed) if parsed else _fallback(
                state.get("tools_run", []), state["posting"]
            )
        except Exception:
            decision = _fallback(state.get("tools_run", []), state["posting"])
        return {"decision": decision.model_dump(), "attempts": attempts}

    def repair(state: DecisionState) -> DecisionState:
        # The LLM proposed something unusable (unknown/duplicate tool) and we're out of
        # retries: substitute the deterministic fallback so the run always makes progress.
        decision = _fallback(state.get("tools_run", []), state["posting"])
        return {"decision": decision.model_dump()}

    def route(state: DecisionState) -> str:
        decision = AgentDecision.model_validate(state["decision"])
        if _valid(decision, state.get("tools_run", []), state["posting"]):
            return "ok"
        return "retry" if state.get("attempts", 0) < 2 else "repair"

    g = StateGraph(DecisionState)
    g.add_node("propose", propose)
    g.add_node("repair", repair)
    g.add_edge(START, "propose")
    g.add_conditional_edges("propose", route, {"ok": END, "retry": "propose", "repair": "repair"})
    g.add_edge("repair", END)
    return g.compile()


def decide(graph, posting: dict, tools_run: list[str], observations: dict) -> AgentDecision:
    """Run one decision turn. Pure w.r.t. inputs (Temporal owns durability across turns)."""
    out = graph.invoke(
        {"posting": posting, "tools_run": tools_run, "observations": observations, "attempts": 0}
    )
    return AgentDecision.model_validate(out["decision"])

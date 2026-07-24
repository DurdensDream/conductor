"""The activities — the ONLY place non-determinism lives (LLM, IO, embeddings). Each is a
sync function (Temporal runs them in a thread pool) holding shared deps built once per
worker. Every side-effecting activity appends exactly one audit row keyed by the workflow
id + a deterministic seq, so a physical re-execution after a crash is deduped by the DB —
that is the idempotency guarantee the chaos test verifies.

Activities are plain functions here (registered from the worker) that close over a single
`Deps` object; this keeps registration explicit and testable."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from temporalio import activity
from temporalio.exceptions import ApplicationError

from ..agent.finalize import assemble_record, needs_human_review
from ..agent.graph import build_agent_graph, decide
from ..db.store import Store
from ..embed.base import Embedder
from ..llm.base import LLMClient
from ..models import RawPosting
from ..obs.telemetry import T
from ..settings import get_settings
from ..tools.registry import ToolContext, run_tool
from . import shared

_TERMINAL = {"COMPLETED", "REJECTED", "CANCELLED", "REVIEW_TIMED_OUT", "FAILED_VALIDATION"}

log = logging.getLogger("conductor.activity")


@dataclass
class Deps:
    store: Store
    llm: LLMClient
    embedder: Embedder
    dedup_k: int = 5
    dedup_threshold: float = 0.90

    @property
    def ctx(self) -> ToolContext:
        return ToolContext(
            llm=self.llm, embedder=self.embedder, index=self.store,
            dedup_k=self.dedup_k, dedup_threshold=self.dedup_threshold,
        )


def _key(seq: int, step: str) -> str:
    return f"{seq:03d}:{step}"


def build_activities(deps: Deps) -> list:
    """Return the list of @activity.defn callables bound to `deps`, for Worker registration."""
    graph = build_agent_graph(deps.llm)

    def _audit(external_id: str, seq: int, step: str, status: str, detail: dict) -> bool:
        # Scope the idempotency key to the *run*, not just the workflow: re-submitting the
        # same external_id starts a new run under the same workflow_id, and a workflow-only
        # key would collide with the prior run's rows and silently drop the new run's audit.
        # workflow_run_id is stable across a worker crash/replay, so per-step dedup still holds.
        info = activity.info()
        key = f"{info.workflow_run_id}:{_key(seq, step)}"
        newly = deps.store.append_audit(info.workflow_id, external_id, key, seq, step, status, detail)
        if not newly:
            log.info("idempotent_replay", extra={"workflow_id": info.workflow_id, "step": step, "seq": seq})
        return newly

    @activity.defn(name=shared.ACT_VALIDATE_INPUT)
    def validate_input(raw: dict, seq: int) -> dict:
        # Input guardrail. A malformed payload is a permanent error — do NOT retry it.
        try:
            posting = RawPosting.model_validate(raw)
        except Exception as err:
            raise ApplicationError(f"invalid posting: {err}", type="ValidationError", non_retryable=True) from err
        _audit(posting.external_id, seq, "input_validated", "ok", {"source": posting.source})
        return posting.model_dump()

    @activity.defn(name=shared.ACT_AGENT_DECIDE)
    def agent_decide(posting: dict, tools_run: list, observations: dict, seq: int) -> dict:
        d = decide(graph, posting, tools_run, observations)
        _audit(posting["external_id"], seq, "agent_decide", "ok",
               {"action": d.action, "tool": d.tool, "reasoning": d.reasoning[:200]})
        return d.model_dump()

    @activity.defn(name=shared.ACT_RUN_TOOL)
    def run_tool_activity(name: str, posting: dict, observations: dict, seq: int) -> dict:
        p = RawPosting.model_validate(posting)
        t0 = time.perf_counter()
        out = run_tool(name, p, deps.ctx, observations)
        T.record_tool(name, time.perf_counter() - t0)
        # A tool may legitimately produce no observation (e.g. parse_salary on empty
        # salary_raw returns None). Treat that as an empty observation rather than crashing
        # on .model_dump(); finalize reads a falsy observation as "not applicable".
        result = out.model_dump() if out is not None else {}
        _audit(p.external_id, seq, f"tool:{name}", "ok", {"tool": name})
        return result

    @activity.defn(name=shared.ACT_CHECK_POLICY)
    def check_policy_activity(posting: dict, observations: dict, seq: int) -> dict:
        p = RawPosting.model_validate(posting)
        out = run_tool("check_policy", p, deps.ctx, observations)
        result = out.model_dump()
        _audit(p.external_id, seq, "check_policy", "ok",
               {"severity": result.get("severity"), "flags": result.get("flags", [])[:5]})
        return result

    @activity.defn(name=shared.ACT_FINALIZE_RECORD)
    def finalize_record(posting: dict, observations: dict, trajectory: list, seq: int) -> dict:
        # Output guardrail: assemble + validate the final record. A shape error is permanent
        # (bad tool output), so it is non-retryable and the workflow ends FAILED_VALIDATION.
        p = RawPosting.model_validate(posting)
        try:
            record = assemble_record(p, observations, trajectory)
        except Exception as err:
            raise ApplicationError(f"record failed validation: {err}", type="ValidationError",
                                   non_retryable=True) from err
        review, reason = needs_human_review(p, observations, get_settings().confidence_threshold)
        _audit(p.external_id, seq, "record_assembled", "ok",
               {"confidence": record.confidence, "needs_review": review, "reason": reason})
        return {"record": record.model_dump(), "needs_review": review, "review_reason": reason}

    @activity.defn(name=shared.ACT_PERSIST_RECORD)
    def persist_record(record: dict, status: str, seq: int) -> None:
        ext = record["external_id"]
        deps.store.upsert_record(ext, record, status, float(record.get("confidence", 0.0)))
        # Add to the dedup corpus so future postings dedup against this one.
        doc = f"{record.get('title', '')}\n{record.get('notes', '')}".strip()
        vec = deps.embedder.embed([doc])[0]
        deps.store.add(ext, record.get("title", ""), vec)
        _audit(ext, seq, "persist_record", status, {"confidence": record.get("confidence")})

    @activity.defn(name=shared.ACT_RECORD_EVENT)
    def record_event(external_id: str, seq: int, step: str, status: str, detail: dict) -> None:
        # Count the run outcome only when the audit row was *newly* inserted. On an activity
        # retry (e.g. the result was lost after the DB write committed) the audit insert is a
        # dedup no-op, and counting again would inflate conductor_runs_total.
        newly = _audit(external_id, seq, step, status, detail or {})
        if newly and status in _TERMINAL:
            T.count_run(status)

    return [validate_input, agent_decide, run_tool_activity, check_policy_activity,
            finalize_record, persist_record, record_event]

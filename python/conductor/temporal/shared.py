"""Types + constants shared by the workflow and the activities. Payloads are plain dicts
(RawPosting/EnrichedPosting .model_dump()) so the default Temporal data converter handles
them without a custom Pydantic converter; only the top-level input/result are dataclasses.

Activity names are string constants referenced by both sides — the workflow schedules by
name, the worker registers by name — so a rename can't silently split them."""

from __future__ import annotations

from dataclasses import dataclass, field

TASK_QUEUE = "conductor-intake"

# Activity names
ACT_VALIDATE_INPUT = "validate_input"
ACT_AGENT_DECIDE = "agent_decide"
ACT_RUN_TOOL = "run_tool"
ACT_CHECK_POLICY = "check_policy"
ACT_FINALIZE_RECORD = "finalize_record"
ACT_PERSIST_RECORD = "persist_record"
ACT_RECORD_EVENT = "record_event"


@dataclass
class IntakeInput:
    """The work item. `raw` is a RawPosting.model_dump() (validated inside the workflow's
    first activity, not trusted at the boundary). Runtime knobs travel here so they're
    captured deterministically in workflow history."""

    raw: dict
    max_turns: int = 8
    review_sla_seconds: int = 86_400


@dataclass
class IntakeResult:
    external_id: str
    status: str                       # RunStatus value
    record: dict | None = None        # EnrichedPosting.model_dump() when a record was produced
    confidence: float = 0.0
    trajectory: list[str] = field(default_factory=list)
    review_reason: str = ""
    reviewer: str = ""

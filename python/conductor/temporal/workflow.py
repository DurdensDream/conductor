"""JobIntakeWorkflow — the durable outer loop.

This is the ONLY orchestration authority: it drives the agent turn-by-turn, and because
each LLM decision and each tool call is a separate activity, Temporal event-sources every
result. A worker crash mid-run resumes here from the last completed activity — finished
steps are replayed from history, never re-executed, so no LLM cost is re-paid and no side
effect is duplicated. That is what makes the chaos test meaningful.

Determinism rules (NFR-1): nothing here does IO, reads the clock via anything but
workflow.now(), or uses randomness. All of that lives in activities. The pure agent
helpers are deliberately NOT imported — routing/assembly happen in the finalize activity —
so this module stays trivially replay-safe."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from . import constants, shared


def _is_validation_error(err: ActivityError) -> bool:
    """True only for the *intended* permanent failure: an activity that raised
    ApplicationError(type="ValidationError"). Temporal also wraps retry-exhausted transient
    failures (a flaky DB/LLM) in ActivityError, so a bare `except ActivityError` would
    mislabel a transient outage as a permanent validation rejection. Inspect the cause."""
    cause = err.cause
    return isinstance(cause, ApplicationError) and cause.type == "ValidationError"


def _retry() -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=constants.RETRY_INITIAL_INTERVAL_SECONDS),
        backoff_coefficient=constants.RETRY_BACKOFF_COEFFICIENT,
        maximum_interval=timedelta(seconds=constants.RETRY_MAX_INTERVAL_SECONDS),
        maximum_attempts=constants.ACTIVITY_MAX_ATTEMPTS,
        non_retryable_error_types=constants.NON_RETRYABLE_ERRORS,
    )


_ACT_TIMEOUT = timedelta(seconds=constants.ACTIVITY_START_TO_CLOSE_SECONDS)
_LLM_TIMEOUT = timedelta(seconds=constants.LLM_ACTIVITY_START_TO_CLOSE_SECONDS)
_RETRY = _retry()


@workflow.defn
class JobIntakeWorkflow:
    def __init__(self) -> None:
        self._external_id = ""
        self._status = "RUNNING"
        self._current_step = "created"
        self._trajectory: list[str] = []
        self._record: dict | None = None
        self._review_reason = ""
        self._review_decision: dict | None = None
        self._cancel_requested = False
        self._updated_at: str | None = None
        self._seq = 0

    # ---- signals (HITL) ----
    @workflow.signal(name="approve")
    def approve(self, reviewer: str = "unknown", note: str = "") -> None:
        # Only honor a decision made *while the gate is armed*. A signal delivered before the
        # workflow reaches PENDING_REVIEW (early client retry, operator racing the run) must
        # not pre-satisfy the gate — that would auto-approve a policy-blocked record no human
        # ever saw. The reviewer decides against the assembled record, not ahead of it.
        if self._status == "PENDING_REVIEW" and self._review_decision is None:
            self._review_decision = {"approved": True, "reviewer": reviewer, "note": note}
            self._note("approve_received")

    @workflow.signal(name="reject")
    def reject(self, reviewer: str = "unknown", reason: str = "") -> None:
        if self._status == "PENDING_REVIEW" and self._review_decision is None:
            self._review_decision = {"approved": False, "reviewer": reviewer, "note": reason}
            self._note("reject_received")

    @workflow.signal(name="cancel")
    def cancel(self) -> None:
        self._cancel_requested = True
        self._note("cancel_received")

    # ---- query (approval UI reads this) ----
    @workflow.query(name="state")
    def state(self) -> dict:
        return {
            "external_id": self._external_id,
            "status": self._status,
            "current_step": self._current_step,
            "trajectory": self._trajectory,
            "record": self._record,
            "review_reason": self._review_reason,
            "updated_at": self._updated_at,
        }

    # ---- helpers ----
    def _note(self, step: str) -> None:
        self._current_step = step
        self._updated_at = workflow.now().isoformat()
        workflow.logger.info("intake_step", extra={"external_id": self._external_id, "step": step})

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    async def _event(self, step: str, status: str, detail: dict) -> None:
        await workflow.execute_activity(
            shared.ACT_RECORD_EVENT, args=[self._external_id, self._next(), step, status, detail],
            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=_RETRY,
        )

    async def _terminal(self, status: str, step: str, detail: dict | None = None) -> shared.IntakeResult:
        self._status = status
        self._note(step)
        await self._event(step, status, detail or {})
        return self._result()

    def _result(self) -> shared.IntakeResult:
        reviewer = (self._review_decision or {}).get("reviewer", "")
        return shared.IntakeResult(
            external_id=self._external_id, status=self._status, record=self._record,
            confidence=float((self._record or {}).get("confidence", 0.0)),
            trajectory=self._trajectory, review_reason=self._review_reason, reviewer=reviewer,
        )

    @workflow.run
    async def run(self, inp: shared.IntakeInput) -> shared.IntakeResult:
        self._external_id = str(inp.raw.get("external_id", ""))
        self._note("started")

        # ---- input guardrail (non-retryable on invalid) ----
        try:
            posting = await workflow.execute_activity(
                shared.ACT_VALIDATE_INPUT, args=[inp.raw, self._next()],
                start_to_close_timeout=_ACT_TIMEOUT, retry_policy=_RETRY,
            )
        except ActivityError as err:
            if _is_validation_error(err):
                return await self._terminal("FAILED_VALIDATION", "input_rejected")
            raise  # transient/infra failure — fail loudly & retryably, don't mislabel as invalid
        self._external_id = posting["external_id"]

        # ---- agentic loop: decide -> run tool -> observe (each an activity) ----
        observations: dict = {}
        tools_run: list[str] = []
        for _ in range(max(1, inp.max_turns)):
            if self._cancel_requested:
                return await self._terminal("CANCELLED", "cancelled")
            decision = await workflow.execute_activity(
                shared.ACT_AGENT_DECIDE, args=[posting, tools_run, observations, self._next()],
                start_to_close_timeout=_LLM_TIMEOUT, retry_policy=_RETRY,
            )
            if decision.get("action") == "finish":
                break
            tool = decision.get("tool")
            if not tool or tool in tools_run:
                break  # defensive: graph guarantees validity, but never loop forever
            out = await workflow.execute_activity(
                shared.ACT_RUN_TOOL, args=[tool, posting, observations, self._next()],
                start_to_close_timeout=_LLM_TIMEOUT, retry_policy=_RETRY,
            )
            observations[tool] = out
            tools_run.append(tool)
            self._trajectory.append(tool)
            self._note(f"tool:{tool}")

        # ---- mandatory guardrail: policy check (agent cannot skip it) ----
        observations["check_policy"] = await workflow.execute_activity(
            shared.ACT_CHECK_POLICY, args=[posting, observations, self._next()],
            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=_RETRY,
        )

        # ---- output guardrail: assemble + validate the record ----
        try:
            fin = await workflow.execute_activity(
                shared.ACT_FINALIZE_RECORD, args=[posting, observations, self._trajectory, self._next()],
                start_to_close_timeout=_ACT_TIMEOUT, retry_policy=_RETRY,
            )
        except ActivityError as err:
            if _is_validation_error(err):
                return await self._terminal("FAILED_VALIDATION", "record_invalid")
            raise  # transient/infra failure — fail loudly & retryably, don't mislabel as invalid
        self._record = fin["record"]
        self._review_reason = fin["review_reason"]

        if self._cancel_requested:
            return await self._terminal("CANCELLED", "cancelled")

        # ---- HITL gate: low-confidence / policy-block / duplicate -> wait for a human ----
        if fin["needs_review"]:
            self._status = "PENDING_REVIEW"
            self._note("awaiting_review")
            await self._event("pending_review", "PENDING_REVIEW", {"reason": self._review_reason})
            try:
                await workflow.wait_condition(
                    lambda: self._review_decision is not None or self._cancel_requested,
                    timeout=timedelta(seconds=inp.review_sla_seconds),
                )
            except TimeoutError:
                return await self._terminal("REVIEW_TIMED_OUT", "review_timed_out")
            if self._cancel_requested:
                return await self._terminal("CANCELLED", "cancelled")
            if not self._review_decision["approved"]:
                return await self._terminal(
                    "REJECTED", "rejected", {"reviewer": self._review_decision["reviewer"]}
                )
            self._note("approved")

        # ---- commit: persist the clean record + add to dedup corpus ----
        await workflow.execute_activity(
            shared.ACT_PERSIST_RECORD, args=[self._record, "COMPLETED", self._next()],
            start_to_close_timeout=_ACT_TIMEOUT, retry_policy=_RETRY,
        )
        return await self._terminal("COMPLETED", "completed", {"confidence": self._record.get("confidence")})

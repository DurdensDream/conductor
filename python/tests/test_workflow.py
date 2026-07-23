"""Time-skipping workflow tests (FR-3): exercise the durable orchestration + all terminal
states with an in-memory store + FakeLLM — no Docker, milliseconds per test. The
time-skipping env fast-forwards the review SLA so the timeout path is instant.

These validate the workflow is deterministic (sandbox-safe) and that the HITL gate,
guardrails, and idempotent audit all behave, before we ever stand up real Temporal."""

from __future__ import annotations

import contextlib
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from conductor.db.store import InMemoryStore
from conductor.embed.fake import HashEmbedder
from conductor.llm.fake import FakeLLM
from conductor.temporal.activities import Deps, build_activities
from conductor.temporal.shared import TASK_QUEUE, IntakeInput
from conductor.temporal.workflow import JobIntakeWorkflow


def _deps() -> Deps:
    return Deps(store=InMemoryStore(), llm=FakeLLM(), embedder=HashEmbedder(384))


@contextlib.asynccontextmanager
async def _running(env: WorkflowEnvironment, deps: Deps):
    with ThreadPoolExecutor(max_workers=8) as executor:
        worker = Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[JobIntakeWorkflow],
            activities=build_activities(deps), activity_executor=executor,
        )
        async with worker:
            yield


def _raw(**kw) -> dict:
    base = dict(source="test", external_id=f"x-{uuid.uuid4().hex[:8]}", title="Senior Python Engineer",
                description="Backend Kafka Postgres. Hybrid.", location_raw="Bangalore, India",
                salary_raw="$120k-$150k/yr")
    base.update(kw)
    return base


async def _start(env, raw, sla=86400):
    return await env.client.start_workflow(
        JobIntakeWorkflow.run, IntakeInput(raw=raw, review_sla_seconds=sla),
        id=f"intake-{raw['external_id']}", task_queue=TASK_QUEUE,
    )


@pytest.mark.asyncio
async def test_clean_posting_completes_and_persists():
    deps = _deps()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _running(env, deps):
            raw = _raw()
            handle = await _start(env, raw)
            result = await handle.result()
    assert result.status == "COMPLETED"
    assert result.trajectory == ["normalize_location", "parse_salary", "classify_category", "search_duplicates"]
    assert result.confidence >= 0.7
    stored = deps.store.get_record(raw["external_id"])
    assert stored and stored["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_policy_block_routes_to_review_then_approve_completes():
    deps = _deps()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _running(env, deps):
            raw = _raw(title="Packer - men only", description="no women. forklift.", salary_raw="")
            handle = await _start(env, raw)
            await handle.signal(JobIntakeWorkflow.approve, args=["reviewer@x", "override ok"])
            result = await handle.result()
    assert result.status == "COMPLETED"
    assert result.review_reason.startswith("policy_block")
    assert deps.store.get_record(raw["external_id"])["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_reject_does_not_persist():
    deps = _deps()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _running(env, deps):
            raw = _raw(title="Packer - men only", description="no women.", salary_raw="")
            handle = await _start(env, raw)
            await handle.signal(JobIntakeWorkflow.reject, args=["reviewer@x", "genuinely discriminatory"])
            result = await handle.result()
    assert result.status == "REJECTED"
    assert deps.store.get_record(raw["external_id"]) is None


@pytest.mark.asyncio
async def test_review_sla_times_out():
    deps = _deps()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _running(env, deps):
            raw = _raw(title="Packer - men only", description="no women.", salary_raw="")
            handle = await _start(env, raw, sla=30)  # time-skipping fast-forwards past this
            result = await handle.result()
    assert result.status == "REVIEW_TIMED_OUT"
    assert deps.store.get_record(raw["external_id"]) is None


@pytest.mark.asyncio
async def test_cancel_before_review_cancels():
    deps = _deps()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _running(env, deps):
            raw = _raw(title="Packer - men only", description="no women.", salary_raw="")
            handle = await _start(env, raw)
            await handle.signal(JobIntakeWorkflow.cancel)
            result = await handle.result()
    assert result.status == "CANCELLED"
    assert deps.store.get_record(raw["external_id"]) is None


@pytest.mark.asyncio
async def test_invalid_input_fails_validation():
    deps = _deps()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _running(env, deps):
            raw = _raw(title="")  # empty title violates RawPosting -> non-retryable
            handle = await _start(env, raw)
            result = await handle.result()
    assert result.status == "FAILED_VALIDATION"


@pytest.mark.asyncio
async def test_audit_has_no_duplicate_side_effects():
    deps = _deps()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _running(env, deps):
            raw = _raw()
            handle = await _start(env, raw)
            await handle.result()
    trail = deps.store.audit_trail(f"intake-{raw['external_id']}")
    keys = [e["idempotency_key"] for e in trail]
    assert len(keys) == len(set(keys)), "duplicate audit rows — idempotency broken"
    assert any(e["step"] == "persist_record" for e in trail)
    assert any(e["step"] == "input_validated" for e in trail)

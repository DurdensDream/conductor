"""The reliability proof (FR-4 / spec §2.9) — the wow moment.

Runs against a REAL Temporal server + REAL pgvector Postgres (not the time-skipping mock).
It: starts a workflow, lets the agent run every enrichment step and park in the durable
HITL review gate, then SIGKILLs the worker process. A brand-new worker (sharing zero memory
with the dead one) is started; it reconstructs the ENTIRE accumulated state — every tool
observation, the assembled record, the full trajectory — purely by replaying Temporal event
history. We approve, the workflow completes with byte-identical output, and the audit log
shows no duplicate side effects.

Skipped unless CONDUCTOR_CHAOS=1 (needs live services); the CI chaos job sets it after
`docker compose up temporal app-db`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from temporalio.client import Client

from conductor.temporal.shared import TASK_QUEUE, IntakeInput
from conductor.temporal.workflow import JobIntakeWorkflow

pytestmark = pytest.mark.skipif(
    os.getenv("CONDUCTOR_CHAOS") != "1", reason="chaos test needs live Temporal + Postgres (set CONDUCTOR_CHAOS=1)"
)

PKG = str(Path(__file__).resolve().parents[1])  # the python/ dir
TEMPORAL_ADDR = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT = os.getenv("POSTGRES_PORT", "5433")
PG_DSN = f"postgresql://conductor:conductor@{PG_HOST}:{PG_PORT}/conductor"

_WORKER_ENV = {
    **os.environ,
    "TEMPORAL_ADDRESS": TEMPORAL_ADDR,
    "POSTGRES_HOST": PG_HOST,
    "POSTGRES_PORT": PG_PORT,
    "LLM_PROVIDER": "fake",
    "EMBEDDER": "fake",
    "OTEL_ENABLED": "false",
}


def _spawn_worker() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "conductor.temporal.worker"], cwd=PKG, env=_WORKER_ENV)


async def _wait_status(handle, target: str, timeout: float = 60.0) -> dict:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last = {}
    while loop.time() < deadline:
        last = await handle.query("state")
        if last.get("status") == target:
            return last
        await asyncio.sleep(0.5)
    raise AssertionError(f"workflow never reached {target}; last={last}")


def _audit_keys(workflow_id: str) -> list[str]:
    with psycopg.connect(PG_DSN) as conn:
        rows = conn.execute(
            "SELECT idempotency_key FROM audit_log WHERE workflow_id = %s ORDER BY seq, id", (workflow_id,)
        ).fetchall()
    return [r[0] for r in rows]


@pytest.mark.asyncio
async def test_worker_crash_mid_run_resumes_with_identical_output():
    client = await Client.connect(TEMPORAL_ADDR)
    ext = f"chaos-{uuid.uuid4().hex[:8]}"
    wf_id = f"intake-{ext}"
    # A policy-block posting parks in the durable review gate — a deterministic point to crash.
    raw = dict(source="test", external_id=ext, title="Warehouse packer - men only",
               description="no women. forklift experience required.", location_raw="Austin",
               salary_raw="$18/hr")

    worker1 = _spawn_worker()
    try:
        handle = await client.start_workflow(
            JobIntakeWorkflow.run, IntakeInput(raw=raw, review_sla_seconds=3600),
            id=wf_id, task_queue=TASK_QUEUE,
        )
        # Let the agent run everything and park awaiting review.
        parked = await _wait_status(handle, "PENDING_REVIEW")
        pre_crash_keys = _audit_keys(wf_id)
        pre_trajectory = parked["trajectory"]
        pre_record = parked["record"]
        assert pre_record is not None and pre_trajectory, "state not built before crash"
    finally:
        # HARD kill — no graceful shutdown, no chance to flush anything.
        worker1.send_signal(signal.SIGKILL)
        worker1.wait(timeout=10)
    assert worker1.returncode is not None, "worker did not die"

    # Fresh process. It has never seen this workflow; everything it knows it must replay.
    worker2 = _spawn_worker()
    try:
        # Before approving, prove the new worker reconstructed the full state from history.
        resumed = await _wait_status(handle, "PENDING_REVIEW", timeout=30)
        assert resumed["trajectory"] == pre_trajectory, "trajectory not reconstructed after crash"
        assert resumed["record"] == pre_record, "proposed record not reconstructed after crash"

        await handle.signal(JobIntakeWorkflow.approve, args=["chaos@test", "approved after crash"])
        result = await asyncio.wait_for(handle.result(), timeout=60)
    finally:
        worker2.send_signal(signal.SIGTERM)
        worker2.wait(timeout=10)

    # 1) resumed and completed
    assert result.status == "COMPLETED", result.status
    # 2) identical enrichment output (durable state survived the kill)
    assert result.trajectory == pre_trajectory
    assert result.record["confidence"] == pre_record["confidence"]
    assert result.record["category"] == pre_record["category"]
    # 3) no duplicate side effects — every audit key unique, pre-crash keys all still present
    post_keys = _audit_keys(wf_id)
    assert len(post_keys) == len(set(post_keys)), "duplicate audit rows — idempotency broken"
    assert set(pre_crash_keys) <= set(post_keys), "pre-crash audit rows lost"
    assert any(k.endswith(":persist_record") for k in post_keys), "record was never persisted"

    # 4) the record is actually in the domain store
    with psycopg.connect(PG_DSN) as conn:
        row = conn.execute("SELECT status FROM postings WHERE external_id = %s", (ext,)).fetchone()
    assert row and row[0] == "COMPLETED"

"""Control-plane API (FR-6). Thin: it starts workflows, lists the ones parked for review
(Temporal-native: list running workflows of this type + read each one's `state` query),
routes approve/reject/cancel to signals, and reads persisted records/audit from the store.
No business logic — the workflow owns all of that.

Kept intentionally boring so the durability story isn't diluted: the API is a remote
control for workflows, nothing more."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from ..db.pg import PgStore, init_schema
from ..models import RawPosting
from ..obs.telemetry import T, instrument_fastapi
from ..settings import get_settings
from ..temporal.client import connect_ready
from ..temporal.shared import TASK_QUEUE, IntakeInput
from ..temporal.workflow import JobIntakeWorkflow

log = logging.getLogger("conductor.api")

# How many Running workflows /pending will scan before giving up (status isn't a Temporal
# Search Attribute yet — see README "What I'd do next"). Bounds work per request.
_PENDING_MAX_SCAN = 200

_state: dict = {}


def _wf_id(external_id: str) -> str:
    return f"intake-{external_id}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    T.init("conductor-api")
    init_schema(s.postgres_dsn)
    _state["store"] = PgStore(s.postgres_dsn)
    _state["client"] = await connect_ready()
    log.info("api ready; temporal=%s", s.temporal_address)
    yield
    _state["store"].close()


app = FastAPI(title="Conductor", version="0.1.0", lifespan=lifespan)
# MUST instrument at import time (Starlette caches the middleware stack on first request).
instrument_fastapi(app)


class SubmitReq(BaseModel):
    source: str = Field(default="api")
    external_id: str
    title: str
    description: str = ""
    location_raw: str = ""
    salary_raw: str = ""
    employment_type_raw: str = ""
    company: str = ""


class DecisionReq(BaseModel):
    reviewer: str = "ui"
    note: str = ""


def _client() -> Client:
    return _state["client"]


def _store() -> PgStore:
    return _state["store"]


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/postings")
async def submit(req: SubmitReq) -> dict:
    # Validate at the edge too (defense in depth); the workflow re-validates authoritatively.
    try:
        raw = RawPosting.model_validate(req.model_dump()).model_dump()
    except Exception as err:
        raise HTTPException(status_code=422, detail=f"invalid posting: {err}") from err
    s = get_settings()
    try:
        handle = await _client().start_workflow(
            JobIntakeWorkflow.run,
            IntakeInput(raw=raw, max_turns=s.max_agent_turns, review_sla_seconds=s.review_sla_seconds),
            id=_wf_id(req.external_id), task_queue=TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError as err:
        # An at-least-once upstream re-submitted this external_id while its run is open. Say so
        # explicitly (409) instead of a bare 500, and point at the existing workflow.
        raise HTTPException(
            status_code=409,
            detail=f"posting {req.external_id!r} already has an in-flight intake ({_wf_id(req.external_id)})",
        ) from err
    T.count_run("submitted")
    return {"external_id": req.external_id, "workflow_id": handle.id, "run_id": handle.result_run_id}


@app.get("/postings/{external_id}")
async def get_posting(external_id: str) -> dict:
    # Prefer the live workflow state; fall back to the persisted record for finished runs.
    try:
        handle = _client().get_workflow_handle(_wf_id(external_id))
        return await handle.query("state")
    except RPCError as err:
        # NOT_FOUND => the run is closed/GC'd (or never existed): read the durable record.
        # Any other RPC status (UNAVAILABLE, DEADLINE_EXCEEDED) is a Temporal outage — say 503
        # rather than lying "404 unknown" about a posting that may well exist.
        if err.status != RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=503, detail=f"temporal unavailable: {err.status.name}") from err
        stored = await asyncio.to_thread(_store().get_record, external_id)
        if stored:
            return {"external_id": external_id, "status": stored["status"], "record": stored["record"]}
        raise HTTPException(status_code=404, detail="unknown posting") from err


@app.get("/postings/{external_id}/audit")
async def get_audit(external_id: str) -> dict:
    # PgStore is synchronous psycopg; run it off the event loop so a slow query can't stall
    # every other request served by this async worker.
    trail = await asyncio.to_thread(_store().audit_trail, _wf_id(external_id))
    return {"external_id": external_id, "trail": trail}


@app.get("/pending")
async def pending(limit: int = 50) -> dict:
    """Workflows parked for human review. Temporal-native: enumerate running workflows of
    this type, read each one's state query, keep the PENDING_REVIEW ones. Bounded to
    _PENDING_MAX_SCAN running workflows per call and queried concurrently; at production
    scale you'd promote status to a Temporal Search Attribute and filter server-side."""
    query = f"WorkflowType = '{JobIntakeWorkflow.__name__}' AND ExecutionStatus = 'Running'"
    wf_ids: list[str] = []
    async for wf in _client().list_workflows(query=query):
        wf_ids.append(wf.id)
        if len(wf_ids) >= _PENDING_MAX_SCAN:
            break

    async def _state_of(wf_id: str) -> dict | None:
        try:
            return await _client().get_workflow_handle(wf_id).query("state")
        except RPCError:
            return None  # closed between listing and query, or a transient blip — just skip

    states = await asyncio.gather(*(_state_of(w) for w in wf_ids))
    items = [s for s in states if s and s.get("status") == "PENDING_REVIEW"][:limit]
    return {"count": len(items), "items": items, "scanned": len(wf_ids),
            "scan_capped": len(wf_ids) >= _PENDING_MAX_SCAN}


async def _signal(external_id: str, signal, *args) -> dict:
    handle = _client().get_workflow_handle(_wf_id(external_id))
    try:
        await handle.signal(signal, args=list(args))
    except RPCError as err:
        # Only a genuine NOT_FOUND means the workflow is gone; a Temporal outage must not be
        # reported to the reviewer as "already closed" (they'd think the approval took).
        if err.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail="unknown or closed workflow") from err
        raise HTTPException(status_code=503, detail=f"temporal unavailable: {err.status.name}") from err
    return {"external_id": external_id, "signalled": True}


@app.post("/postings/{external_id}/approve")
async def approve(external_id: str, req: DecisionReq) -> dict:
    return await _signal(external_id, JobIntakeWorkflow.approve, req.reviewer, req.note)


@app.post("/postings/{external_id}/reject")
async def reject(external_id: str, req: DecisionReq) -> dict:
    return await _signal(external_id, JobIntakeWorkflow.reject, req.reviewer, req.note)


@app.post("/postings/{external_id}/cancel")
async def cancel(external_id: str) -> dict:
    return await _signal(external_id, JobIntakeWorkflow.cancel)

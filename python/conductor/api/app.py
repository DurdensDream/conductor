"""Control-plane API (FR-6). Thin: it starts workflows, lists the ones parked for review
(Temporal-native: list running workflows of this type + read each one's `state` query),
routes approve/reject/cancel to signals, and reads persisted records/audit from the store.
No business logic — the workflow owns all of that.

Kept intentionally boring so the durability story isn't diluted: the API is a remote
control for workflows, nothing more."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from temporalio.client import Client
from temporalio.service import RPCError

from ..db.pg import PgStore, init_schema
from ..models import RawPosting
from ..obs.telemetry import T, instrument_fastapi
from ..settings import get_settings
from ..temporal.client import connect_ready
from ..temporal.shared import TASK_QUEUE, IntakeInput
from ..temporal.workflow import JobIntakeWorkflow

log = logging.getLogger("conductor.api")

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
    handle = await _client().start_workflow(
        JobIntakeWorkflow.run,
        IntakeInput(raw=raw, max_turns=s.max_agent_turns, review_sla_seconds=s.review_sla_seconds),
        id=_wf_id(req.external_id), task_queue=TASK_QUEUE,
    )
    T.count_run("submitted")
    return {"external_id": req.external_id, "workflow_id": handle.id, "run_id": handle.result_run_id}


@app.get("/postings/{external_id}")
async def get_posting(external_id: str) -> dict:
    # Prefer the live workflow state; fall back to the persisted record for finished runs.
    try:
        handle = _client().get_workflow_handle(_wf_id(external_id))
        return await handle.query("state")
    except RPCError as err:
        stored = _store().get_record(external_id)
        if stored:
            return {"external_id": external_id, "status": stored["status"], "record": stored["record"]}
        raise HTTPException(status_code=404, detail="unknown posting") from err


@app.get("/postings/{external_id}/audit")
async def get_audit(external_id: str) -> dict:
    return {"external_id": external_id, "trail": _store().audit_trail(_wf_id(external_id))}


@app.get("/pending")
async def pending(limit: int = 50) -> dict:
    """Workflows parked for human review. Temporal-native: enumerate running workflows of
    this type, read each one's state query, keep the PENDING_REVIEW ones. Fine at demo
    scale; at production scale you'd promote status to a Temporal Search Attribute."""
    items = []
    query = f"WorkflowType = '{JobIntakeWorkflow.__name__}' AND ExecutionStatus = 'Running'"
    async for wf in _client().list_workflows(query=query):
        if len(items) >= limit:
            break
        try:
            st = await _client().get_workflow_handle(wf.id).query("state")
        except RPCError:
            continue
        if st.get("status") == "PENDING_REVIEW":
            items.append(st)
    return {"count": len(items), "items": items}


async def _signal(external_id: str, signal, *args) -> dict:
    handle = _client().get_workflow_handle(_wf_id(external_id))
    try:
        await handle.signal(signal, args=list(args))
    except RPCError as err:
        raise HTTPException(status_code=404, detail="unknown or closed workflow") from err
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

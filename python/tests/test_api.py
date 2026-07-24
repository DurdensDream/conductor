"""Control-API tests (FR-6): the endpoints map Temporal/store failures to the RIGHT HTTP
status, with no live Temporal or Postgres — the client and store are faked. Guards the
regressions where a Temporal outage was reported to the client as 404 not-found, and a
duplicate submit surfaced as an opaque 500."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

import conductor.api.app as appmod


def _rpc(status: RPCStatusCode) -> RPCError:
    return RPCError("temporal blip", status, b"")


class FakeHandle:
    def __init__(self) -> None:
        self.query_exc = None
        self.query_val = {"external_id": "x", "status": "RUNNING"}
        self.signal_exc = None
        self.id = "intake-x"
        self.result_run_id = "run-1"

    async def query(self, name: str):
        if self.query_exc:
            raise self.query_exc
        return self.query_val

    async def signal(self, sig, args=None):
        if self.signal_exc:
            raise self.signal_exc


class FakeClient:
    def __init__(self) -> None:
        self.handle = FakeHandle()
        self.start_exc = None

    async def start_workflow(self, *a, **k):
        if self.start_exc:
            raise self.start_exc
        return self.handle

    def get_workflow_handle(self, wf_id):
        return self.handle

    async def list_workflows(self, query=None):
        return
        yield  # pragma: no cover — makes this an (empty) async generator


class FakeStore:
    def __init__(self) -> None:
        self.rec: dict = {}

    def get_record(self, ext):
        return self.rec.get(ext)

    def audit_trail(self, wf):
        return []

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    fake, store = FakeClient(), FakeStore()
    monkeypatch.setattr(appmod, "init_schema", lambda dsn: None)
    monkeypatch.setattr(appmod, "PgStore", lambda dsn: store)

    async def _connect():
        return fake

    monkeypatch.setattr(appmod, "connect_ready", _connect)
    with TestClient(appmod.app) as c:
        c.fake, c.store = fake, store  # type: ignore[attr-defined]
        yield c


def test_get_posting_returns_503_on_temporal_outage(client):
    client.fake.handle.query_exc = _rpc(RPCStatusCode.UNAVAILABLE)
    assert client.get("/postings/x").status_code == 503


def test_get_posting_404_when_not_found_and_no_record(client):
    client.fake.handle.query_exc = _rpc(RPCStatusCode.NOT_FOUND)
    assert client.get("/postings/x").status_code == 404


def test_get_posting_falls_back_to_store_for_closed_run(client):
    client.fake.handle.query_exc = _rpc(RPCStatusCode.NOT_FOUND)
    client.store.rec["x"] = {"status": "COMPLETED", "record": {"external_id": "x"}}
    r = client.get("/postings/x")
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"


def test_signal_returns_503_on_outage(client):
    client.fake.handle.signal_exc = _rpc(RPCStatusCode.UNAVAILABLE)
    assert client.post("/postings/x/approve", json={}).status_code == 503


def test_signal_returns_404_when_workflow_gone(client):
    client.fake.handle.signal_exc = _rpc(RPCStatusCode.NOT_FOUND)
    assert client.post("/postings/x/approve", json={}).status_code == 404


def test_duplicate_submit_returns_409(client):
    client.fake.start_exc = WorkflowAlreadyStartedError("intake-x", "JobIntakeWorkflow", run_id="r0")
    r = client.post("/postings", json={"external_id": "x", "title": "Engineer"})
    assert r.status_code == 409


def test_submit_rejects_invalid_posting_422(client):
    r = client.post("/postings", json={"external_id": "x", "title": ""})
    assert r.status_code == 422

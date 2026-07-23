"""The worker process: hosts JobIntakeWorkflow + the activities. `python -m conductor.temporal.worker`.

Builds dependencies once (Postgres store, LLM, embedder), registers the sync activities
with a thread-pool executor (Temporal runs sync activities off the workflow event loop),
and serves the task queue. The chaos test kills THIS process with SIGKILL mid-run and
starts a fresh one; because all durable state is in Temporal + Postgres, the restarted
worker resumes in-flight workflows with no special handling."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from temporalio.worker import Worker

from ..db.pg import PgStore, init_schema
from ..embed.base import get_embedder
from ..llm.factory import get_generation_llm
from ..obs.telemetry import T
from ..settings import get_settings
from .activities import Deps, build_activities
from .client import connect_ready
from .shared import TASK_QUEUE
from .workflow import JobIntakeWorkflow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("conductor.worker")


async def main() -> None:
    s = get_settings()
    T.init("conductor-worker")

    log.info("initializing app-db schema")
    init_schema(s.postgres_dsn)
    store = PgStore(s.postgres_dsn)

    deps = Deps(
        store=store,
        llm=get_generation_llm(),
        embedder=get_embedder(),
        dedup_k=5,
        dedup_threshold=s.dedup_score_threshold,
    )
    activities = build_activities(deps)

    client = await connect_ready()
    log.info("connected to temporal at %s; serving task queue %s", s.temporal_address, TASK_QUEUE)

    # Sync activities need an executor; the workflow itself runs in the sandboxed runner.
    with ThreadPoolExecutor(max_workers=16) as executor:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[JobIntakeWorkflow],
            activities=activities,
            activity_executor=executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

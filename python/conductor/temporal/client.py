"""Temporal client connection with a bounded readiness wait, so the worker/API can boot
in any Compose order and simply wait for the server rather than crash-looping."""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client

from ..settings import get_settings

log = logging.getLogger("conductor.temporal.client")


async def connect_ready(timeout_seconds: float | None = None) -> Client:
    s = get_settings()
    timeout = s.startup_connect_timeout_seconds if timeout_seconds is None else timeout_seconds
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last: Exception | None = None
    while loop.time() < deadline:
        try:
            client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
            # A real round-trip confirms the namespace is actually serving, not just TCP-up.
            async for _ in client.list_workflows():
                break
            return client
        except Exception as err:  # noqa: BLE001 — retry any connect/handshake failure
            last = err
            log.warning("waiting_for_temporal: %s", err)
            await asyncio.sleep(1.0)
    raise RuntimeError(f"temporal not ready at {s.temporal_address}: {last}")

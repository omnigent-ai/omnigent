"""Runner lookup retries must honor expiry and authoritative crash reports."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx

from omnigent.runner.routing import RunnerRouter
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.host_registry import RunnerExitReports
from omnigent.server.routes.sessions import _wait_for_runner_client


async def test_expired_connect_wait_does_not_start_another_lookup() -> None:
    registry = AsyncMock(spec=TunnelRegistry)
    registry.wait_for_runner.return_value = object()
    calls: list[float] = []
    timeout_s = 0.05
    async with httpx.AsyncClient(base_url="http://runner") as client:

        def lookup(_session_id: str) -> SimpleNamespace:
            calls.append(time.monotonic() - started)
            if len(calls) == 1:
                raise LookupError("binding has not settled")
            time.sleep(0.05)
            return SimpleNamespace(client=client)

        router = cast(RunnerRouter, SimpleNamespace(client_for_session_resources=lookup))
        started = time.monotonic()
        result = await _wait_for_runner_client(
            "conv_expired",
            router,
            registry,
            runner_id="runner_expired",
            timeout_s=timeout_s,
        )
        elapsed = time.monotonic() - started
        assert result is None, (
            f"Accepted a client after expiry: timeout={timeout_s}s, "
            f"elapsed={elapsed:.3f}s, lookup start times={calls}"
        )
        assert len(calls) == 1


async def test_crash_during_retry_pause_prevents_another_lookup() -> None:
    registry = AsyncMock(spec=TunnelRegistry)
    registry.wait_for_runner.return_value = object()
    reports = RunnerExitReports()
    lookup_missed = asyncio.Event()
    calls = 0
    async with httpx.AsyncClient(base_url="http://runner") as client:

        def lookup(_session_id: str) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            if calls == 1:
                lookup_missed.set()
                raise LookupError("binding has not settled")
            return SimpleNamespace(client=client)

        async def report_crash() -> None:
            await lookup_missed.wait()
            await asyncio.sleep(0.05)
            reports.record("runner_dead", "runner exited with code 1", owner=None)

        report = asyncio.create_task(report_crash())
        router = cast(RunnerRouter, SimpleNamespace(client_for_session_resources=lookup))
        started = time.monotonic()
        try:
            result = await _wait_for_runner_client(
                "conv_dead",
                router,
                registry,
                runner_id="runner_dead",
                timeout_s=10.0,
                runner_exit_reports=reports,
            )
        finally:
            await report
        elapsed = time.monotonic() - started
        assert result is None, (
            f"Accepted a client despite a crash report: calls={calls}, elapsed={elapsed:.3f}s"
        )
        assert calls == 1

"""Host reconnect grace uses tenant-local liveness and remains cancellable."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.host.frames import HostHelloFrame
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._sessions.orchestration import _wait_for_host_reconnect
from tests.server.test_host_registry import FakeWebSocket

pytestmark = pytest.mark.asyncio


async def test_reconnect_wait_ignores_same_host_in_another_workspace() -> None:
    """Another tenant's registration cannot release a send on this tenant."""
    hosts = HostRegistry()
    hello = HostHelloFrame(version="test", frame_protocol_version=1, name="laptop")
    with workspace_scope(41):
        waiting = asyncio.create_task(
            _wait_for_host_reconnect("host", hosts, None, runner_id=None, timeout_s=5)
        )
        try:
            hosts.register("host", FakeWebSocket(), hello, owner=None, workspace_id=42)
            await asyncio.sleep(0.15)
            assert not waiting.done()
            connection = hosts.register("host", FakeWebSocket(), hello, owner=None)
            assert await asyncio.wait_for(waiting, timeout=1) is connection
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


async def test_surviving_runner_ends_host_grace_early() -> None:
    """A tunnel flap need not hold a live runner behind an absent daemon."""
    hosts = HostRegistry()
    runners = TunnelRegistry()
    waiting = asyncio.create_task(
        _wait_for_host_reconnect("host", hosts, runners, runner_id="runner", timeout_s=5)
    )
    try:
        await asyncio.sleep(0)
        assert not waiting.done()
        runners.register(
            "runner",
            FakeWebSocket(),
            HelloFrame(runner_version="test", frame_protocol_version=1, harnesses=[], envs=[]),
        )
        assert await asyncio.wait_for(waiting, timeout=1) is None
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_host_grace_can_be_cancelled() -> None:
    """An abandoned send cannot keep a reconnect wait alive."""
    waiting = asyncio.create_task(
        _wait_for_host_reconnect("host", HostRegistry(), None, runner_id=None, timeout_s=30)
    )
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

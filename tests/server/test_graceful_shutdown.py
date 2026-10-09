"""Tests for the shared graceful-shutdown Server subclass.

``ShutdownSignalingServer`` is the single launcher behaviour that ``omnigent
server`` and the deploy entrypoints share. These guard its contract directly
(fast, no port bind), separate from the slow Docker end-to-end test.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
import uvicorn
import uvicorn.server

from omnigent.runtime import session_stream
from omnigent.server import graceful_shutdown, shutdown_state
from omnigent.server.graceful_shutdown import (
    SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S,
    SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S_DEFAULT,
    ShutdownSignalingServer,
)


async def _dummy_app(scope: object, receive: object, send: object) -> None:
    """Minimal ASGI callable so uvicorn.Config has something to hold."""


def _server() -> ShutdownSignalingServer:
    return ShutdownSignalingServer(uvicorn.Config(_dummy_app))


def test_shutdown_drains_sse_and_marks_shutdown_before_the_graceful_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE drain and the shutdown mark must both run before uvicorn's base
    graceful wait: afterwards the drain is too late (streams get force-cancelled)
    and the mark is too late (runner-disconnect reconciliation misreads the
    self-inflicted tunnel loss as the runners dying).
    """
    calls: list[str] = []
    monkeypatch.setattr(shutdown_state, "mark_server_shutting_down", lambda: calls.append("mark"))
    monkeypatch.setattr(session_stream, "shutdown_all", lambda: calls.append("drain"))

    captured: dict[str, object] = {}

    async def _base_shutdown(self: uvicorn.server.Server, sockets: object = None) -> None:
        calls.append("super")
        captured["sockets"] = sockets

    monkeypatch.setattr(uvicorn.server.Server, "shutdown", _base_shutdown)

    sentinel = object()
    asyncio.run(_server().shutdown(sockets=sentinel))  # type: ignore[arg-type]

    assert calls == ["mark", "drain", "super"]
    assert captured["sockets"] is sentinel


def test_graceful_timeout_defaults_to_five_seconds() -> None:
    assert SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S_DEFAULT == 5
    assert isinstance(SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S, int)
    assert SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S > 0


def test_graceful_timeout_honours_the_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators can widen the drain window via OMNIGENT_SERVER_SHUTDOWN_TIMEOUT_S."""
    monkeypatch.setenv("OMNIGENT_SERVER_SHUTDOWN_TIMEOUT_S", "17")
    try:
        reloaded = importlib.reload(graceful_shutdown)
        assert reloaded.SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S == 17
    finally:
        monkeypatch.delenv("OMNIGENT_SERVER_SHUTDOWN_TIMEOUT_S", raising=False)
        importlib.reload(graceful_shutdown)

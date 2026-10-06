"""Planned renewal keeps the old tunnel usable until credentials are ready."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from websockets.asyncio.server import ServerConnection, serve

from omnigent.runner.transports.ws_tunnel import serve as serve_module
from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    PingFrame,
    PongFrame,
    decode_frame,
    encode_frame,
)


@dataclass
class _AcceptedTunnel:
    socket: ServerConnection
    hello: HelloFrame
    messages: asyncio.Queue[str | bytes] = field(default_factory=asyncio.Queue)


@asynccontextmanager
async def _running_tunnel(
    factory: Callable[[], str | None],
    *,
    shutdown: asyncio.Event | None = None,
) -> AsyncIterator[tuple[asyncio.Queue[_AcceptedTunnel], asyncio.Task[None]]]:
    accepted: asyncio.Queue[_AcceptedTunnel] = asyncio.Queue()

    async def handle(socket: ServerConnection) -> None:
        raw = await socket.recv()
        hello = decode_frame(raw)
        assert isinstance(hello, HelloFrame)
        tunnel = _AcceptedTunnel(socket, hello)
        await accepted.put(tunnel)
        async for message in socket:
            await tunnel.messages.put(message)

    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(
            serve_module.serve_tunnel(
                FastAPI(),
                server_url=f"http://127.0.0.1:{port}",
                runner_id="renewal-test-runner",
                runner_version="test",
                auth_token_factory=factory,
                shutdown_event=shutdown,
            )
        )
        try:
            yield accepted, task
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.fixture
def short_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_TUNNEL_RENEWAL_S", "0.05")
    monkeypatch.setattr(serve_module, "_TUNNEL_RENEWAL_RETRY_S", 0.02, raising=False)
    monkeypatch.setattr(serve_module, "_INITIAL_RECONNECT_DELAY_S", 0.01)
    monkeypatch.setattr(serve_module, "_RECYCLE_RECONNECT_MIN_S", 0.01)
    monkeypatch.setattr(serve_module, "_RECYCLE_RECONNECT_MAX_S", 0.02)


async def test_renewal_uses_prepared_credential_for_the_next_handshake(
    short_renewal: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only the socket changes; the provider is not called again in the gap."""

    class Credential:
        calls = 0
        renewals = 0

        def __call__(self) -> str:
            self.calls += 1
            return "bootstrap"

        def refresh(self) -> str:
            self.renewals += 1
            return "renewed"

    credential = Credential()

    with caplog.at_level("INFO", logger=serve_module.__name__):
        async with _running_tunnel(credential) as (accepted, _task):
            first = await asyncio.wait_for(accepted.get(), timeout=2)
            second = await asyncio.wait_for(accepted.get(), timeout=2)
            assert first.socket.request.headers["Authorization"] == "Bearer bootstrap"
            assert second.socket.request.headers["Authorization"] == "Bearer renewed"
            assert first.hello.connection_id != second.hello.connection_id
            assert first.socket.close_code == 1001
            assert first.socket.close_reason == "scheduled tunnel renewal"
            assert credential.calls == 1
            assert credential.renewals == 1
            assert any(
                getattr(record, "event_name", None) == "runner_tunnel_renewal_started"
                for record in caplog.records
            )


@pytest.mark.parametrize("failure", ["error", "missing"])
async def test_failed_refresh_keeps_the_old_socket_responsive(
    short_renewal: None,
    failure: str,
) -> None:
    """The credential service is unavailable; renewal must leave traffic flowing."""
    attempted = asyncio.Event()
    loop = asyncio.get_running_loop()
    allow_refresh = threading.Event()
    calls = 0

    def credential() -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "bootstrap"
        loop.call_soon_threadsafe(attempted.set)
        if allow_refresh.is_set():
            return "renewed"
        if failure == "error":
            raise OSError("credential provider unavailable")
        return None

    async with _running_tunnel(credential) as (accepted, _task):
        first = await asyncio.wait_for(accepted.get(), timeout=2)
        await asyncio.wait_for(attempted.wait(), timeout=2)
        await first.socket.send(encode_frame(PingFrame(ts=123)))
        response = decode_frame(await asyncio.wait_for(first.messages.get(), timeout=2))
        assert isinstance(response, PongFrame)
        assert response.ts == 123
        assert first.socket.close_code is None
        assert accepted.empty()
        allow_refresh.set()
        second = await asyncio.wait_for(accepted.get(), timeout=2)
        assert second.socket.request.headers["Authorization"] == "Bearer renewed"


async def test_shutdown_during_refresh_does_not_start_another_connection(
    short_renewal: None,
) -> None:
    """A slow provider does not block traffic or tunnel coroutine shutdown."""
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    calls = 0

    def credential() -> str:
        nonlocal calls
        calls += 1
        if calls > 1:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5)
        return "token"

    try:
        async with _running_tunnel(credential, shutdown=shutdown) as (accepted, task):
            first = await asyncio.wait_for(accepted.get(), timeout=2)
            await asyncio.wait_for(started.wait(), timeout=2)
            await first.socket.send(encode_frame(PingFrame(ts=123)))
            response = decode_frame(await asyncio.wait_for(first.messages.get(), timeout=2))
            assert isinstance(response, PongFrame)
            shutdown.set()
            await asyncio.wait_for(task, timeout=2)
            assert first.socket.close_code == 1000
            assert accepted.empty()
    finally:
        release.set()


async def test_peer_close_during_refresh_reconnects_without_the_old_renewal(
    short_renewal: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old connection's credential worker must not keep its timer alive."""
    monkeypatch.setenv("OMNIGENT_RUNNER_TUNNEL_RENEWAL_S", "0.2")
    started = asyncio.Event()
    release = threading.Event()
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    calls = 0

    def credential() -> str:
        nonlocal calls
        calls += 1
        call = calls
        if call == 2:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5)
            loop.call_soon_threadsafe(finished.set)
            return "old-connection-renewal"
        return f"token-{call}"

    try:
        async with _running_tunnel(credential) as (accepted, _task):
            first = await asyncio.wait_for(accepted.get(), timeout=2)
            await asyncio.wait_for(started.wait(), timeout=2)
            await first.socket.close(code=1001, reason="peer recycle")
            second = await asyncio.wait_for(accepted.get(), timeout=2)
            assert second.socket.request.headers["Authorization"] == "Bearer token-3"
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=2)
            await second.socket.send(encode_frame(PingFrame(ts=123)))
            response = decode_frame(await asyncio.wait_for(second.messages.get(), timeout=2))
            assert isinstance(response, PongFrame)
            assert second.socket.close_code is None
            assert accepted.empty()
    finally:
        release.set()


async def test_zero_interval_disables_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_TUNNEL_RENEWAL_S", "0")
    calls = 0

    def credential() -> str:
        nonlocal calls
        calls += 1
        return "token"

    async with _running_tunnel(credential) as (accepted, _task):
        first = await asyncio.wait_for(accepted.get(), timeout=2)
        # Span multiple short-renewal test intervals while keeping real traffic.
        await asyncio.sleep(0.15)
        await first.socket.send(encode_frame(PingFrame(ts=1)))
        assert isinstance(decode_frame(await first.messages.get()), PongFrame)
        assert accepted.empty()
        assert calls == 1


@pytest.mark.parametrize("interval", ["-1", "nan", "inf", "invalid"])
def test_invalid_renewal_interval_keeps_a_finite_default(
    monkeypatch: pytest.MonkeyPatch, interval: str
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_TUNNEL_RENEWAL_S", interval)
    assert serve_module._tunnel_renewal_interval_s() == 85800.0

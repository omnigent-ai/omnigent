"""The handshake owns its receive task and preserves received frames and cancellation."""

import asyncio
import logging

import pytest
from starlette.websockets import WebSocketDisconnect

from omnigent.server import tunnel_handshake


class _Socket:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.receiving = asyncio.Event()
        self.receive_ended = False
        self.closes = []

    async def receive_text(self) -> str:
        self.receiving.set()
        try:
            if self.text is not None:
                return self.text
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        finally:
            self.receive_ended = True

    async def close(self, **kwargs: object) -> None:
        self.closes.append(kwargs)


class _DroppedSocket(_Socket):
    """A peer that disconnects while the timeout close is being sent."""

    async def close(self, **kwargs: object) -> None:
        await super().close(**kwargs)
        raise WebSocketDisconnect(code=1006)


class _FailingReceiveSocket(_Socket):
    """A transport whose own read times out before the handshake deadline."""

    async def receive_text(self) -> str:
        self.receive_ended = True
        raise TimeoutError("transport read timed out")


class _ScriptedClock:
    """Stand-in for the module's ``time`` that returns fixed monotonic readings."""

    def __init__(self, *readings: float) -> None:
        self._readings = iter(readings)

    def monotonic(self) -> float:
        return next(self._readings)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["host", "runner"])
async def test_timeout_cancels_owned_receive_and_closes(
    kind: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(tunnel_handshake, "HELLO_TIMEOUT_S", 0.01)
    monkeypatch.setattr(tunnel_handshake, "time", _ScriptedClock(100.0, 100.25))
    socket = _Socket()
    with caplog.at_level(logging.WARNING):
        result = await tunnel_handshake.receive_tunnel_hello(
            socket,
            peer_kind=kind,
            peer_id="synthetic-peer",  # type: ignore[arg-type]
        )
    assert result is None
    assert socket.receive_ended
    assert socket.closes == [{"code": 1013, "reason": "hello timeout"}]
    rows = [r for r in caplog.records if getattr(r, "event_name", None) == "tunnel_hello_timeout"]
    assert len(rows) == 1
    assert rows[0].attributes["peer_kind"] == kind
    assert rows[0].attributes["peer_id"] == "synthetic-peer"
    assert rows[0].attributes["timeout_s"] == 0.01
    assert rows[0].attributes["elapsed_ms"] == 250


@pytest.mark.asyncio
async def test_timeout_ignores_close_failure_after_peer_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tunnel_handshake, "HELLO_TIMEOUT_S", 0.01)
    socket = _DroppedSocket()
    result = await tunnel_handshake.receive_tunnel_hello(
        socket,
        peer_kind="runner",
        peer_id="synthetic-peer",  # type: ignore[arg-type]
    )
    assert result is None
    assert socket.receive_ended
    assert socket.closes == [{"code": 1013, "reason": "hello timeout"}]


@pytest.mark.asyncio
async def test_receive_timeout_error_is_preserved() -> None:
    socket = _FailingReceiveSocket()
    with pytest.raises(TimeoutError, match="transport read timed out"):
        await tunnel_handshake.receive_tunnel_hello(
            socket,
            peer_kind="host",
            peer_id="synthetic-peer",  # type: ignore[arg-type]
        )
    assert socket.closes == []


@pytest.mark.asyncio
async def test_initial_frame_is_unchanged() -> None:
    socket = _Socket('{"type":"hello","private":"synthetic"}')
    result = await tunnel_handshake.receive_tunnel_hello(
        socket,
        peer_kind="runner",
        peer_id="synthetic-peer",  # type: ignore[arg-type]
    )
    assert result == socket.text
    assert socket.closes == []


@pytest.mark.asyncio
async def test_owner_cancellation_is_preserved() -> None:
    socket = _Socket()
    task = asyncio.create_task(
        tunnel_handshake.receive_tunnel_hello(
            socket,
            peer_kind="host",
            peer_id="synthetic-peer",  # type: ignore[arg-type]
        )
    )
    await socket.receiving.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.receive_ended
    assert socket.closes == []

"""Extended unit tests for TunnelRegistry — covers owner, timing,
WS channels, send_text, and observability methods not exercised
by the core lifecycle tests in test_registry.py.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading

import pytest

from omnigent.runner.transports.ws_tunnel.diagnostics import TunnelDiagnostics
from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    ResponseHeadFrame,
    WSCloseFrame,
    WSFrame,
)
from omnigent.runner.transports.ws_tunnel.registry import (
    TunnelRegistry,
    WSChannelState,
)
from tests.budgets import budget


class _NoopWS:
    """Minimal WebSocket fake."""

    async def send_text(self, data: str) -> None:
        pass

    async def receive_text(self) -> str:
        return await asyncio.Future()


class _RecordingWS:
    """WebSocket fake that records sent text."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        return await asyncio.Future()


def _hello() -> HelloFrame:
    return HelloFrame(runner_version="0.1.0", frame_protocol_version=1, harnesses=[], envs=[])


# ── runner_owner ────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_owner_returns_owner_when_set() -> None:
    """Registration with an owner surfaces it via runner_owner."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello(), owner="alice@example.com")
    assert reg.runner_owner("r1") == "alice@example.com"


@pytest.mark.asyncio
async def test_runner_owner_returns_none_when_no_owner() -> None:
    """Registration without an owner returns None."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    assert reg.runner_owner("r1") is None


def test_runner_owner_returns_none_for_unknown_runner() -> None:
    """An unknown runner_id yields None."""
    reg = TunnelRegistry()
    assert reg.runner_owner("ghost") is None


# ── mark_frame_seen / seconds_since_last_frame ──────────


@pytest.mark.asyncio
async def test_mark_frame_seen_updates_timestamp() -> None:
    """mark_frame_seen returns True and updates the timestamp."""
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    old_ts = session.last_frame_at
    # Small sleep to ensure time difference.
    await asyncio.sleep(0.01)
    assert reg.mark_frame_seen(session) is True
    assert session.last_frame_at > old_ts


@pytest.mark.asyncio
async def test_mark_frame_seen_returns_false_for_stale_session() -> None:
    """A replaced session is no longer current."""
    reg = TunnelRegistry()
    old_session = reg.register("r1", _NoopWS(), _hello())
    reg.register("r1", _NoopWS(), _hello())  # Replace
    assert reg.mark_frame_seen(old_session) is False


@pytest.mark.asyncio
async def test_seconds_since_last_frame_returns_positive_float() -> None:
    """Active session reports a small idle time."""
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    await asyncio.sleep(0.01)
    idle = reg.seconds_since_last_frame(session)
    assert idle is not None
    assert idle >= 0.01


@pytest.mark.asyncio
async def test_seconds_since_last_frame_returns_none_for_stale() -> None:
    """A stale session returns None."""
    reg = TunnelRegistry()
    old_session = reg.register("r1", _NoopWS(), _hello())
    reg.register("r1", _NoopWS(), _hello())
    assert reg.seconds_since_last_frame(old_session) is None


# ── request_is_open ─────────────────────────────────────


@pytest.mark.asyncio
async def test_request_is_open_true_while_open() -> None:
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    reg.open_request("r1", "req1")
    assert reg.request_is_open(session, "req1") is True


@pytest.mark.asyncio
async def test_request_is_open_false_after_close() -> None:
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    reg.open_request("r1", "req1")
    reg.close_request("r1", "req1")
    assert reg.request_is_open(session, "req1") is False


# ── __len__ / __contains__ ──────────────────────────────


@pytest.mark.asyncio
async def test_len_tracks_session_count() -> None:
    reg = TunnelRegistry()
    assert len(reg) == 0
    reg.register("r1", _NoopWS(), _hello())
    assert len(reg) == 1
    reg.register("r2", _NoopWS(), _hello())
    assert len(reg) == 2
    reg.deregister("r1")
    assert len(reg) == 1


@pytest.mark.asyncio
async def test_contains_checks_runner_presence() -> None:
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    assert "r1" in reg
    assert "ghost" not in reg


# ── WS channel lifecycle ───────────────────────────────


@pytest.mark.asyncio
async def test_open_ws_channel_creates_state() -> None:
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    ch_state = reg.open_ws_channel("r1", "ch01")
    assert isinstance(ch_state, WSChannelState)
    assert "ch01" in session.ws_channels


@pytest.mark.asyncio
async def test_open_ws_channel_duplicate_raises_valueerror() -> None:
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    reg.open_ws_channel("r1", "ch01")
    with pytest.raises(ValueError, match="already open"):
        reg.open_ws_channel("r1", "ch01")


@pytest.mark.asyncio
async def test_open_ws_channel_unknown_runner_raises_keyerror() -> None:
    reg = TunnelRegistry()
    with pytest.raises(KeyError):
        reg.open_ws_channel("ghost", "ch01")


@pytest.mark.asyncio
async def test_open_ws_channel_stale_session_raises_keyerror() -> None:
    """Session guard prevents allocating on an old generation."""
    reg = TunnelRegistry()
    old_session = reg.register("r1", _NoopWS(), _hello())
    reg.register("r1", _NoopWS(), _hello())  # Replace
    with pytest.raises(KeyError):
        reg.open_ws_channel("r1", "ch01", session=old_session)


@pytest.mark.asyncio
async def test_close_ws_channel_removes_state() -> None:
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    reg.open_ws_channel("r1", "ch01")
    reg.close_ws_channel("r1", "ch01")
    assert "ch01" not in session.ws_channels


@pytest.mark.asyncio
async def test_close_ws_channel_idempotent() -> None:
    """Closing an unknown channel is a no-op."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    reg.close_ws_channel("r1", "nonexistent")  # Should not raise.


@pytest.mark.asyncio
async def test_close_ws_channel_unknown_runner_is_noop() -> None:
    reg = TunnelRegistry()
    reg.close_ws_channel("ghost", "ch01")  # Should not raise.


# ── route_ws_inbound ────────────────────────────────────


@pytest.mark.asyncio
async def test_route_ws_inbound_text_frame() -> None:
    """A utf-8 ws.frame is delivered as a ('text', str) item."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    ch_state = reg.open_ws_channel("r1", "ch01")

    frame = WSFrame(ch_id="ch01", data="hello", encoding="utf-8")
    assert reg.route_ws_inbound("r1", frame) is True

    item = ch_state.inbound_queue.get_nowait()
    assert item == ("text", "hello")


@pytest.mark.asyncio
async def test_route_ws_inbound_base64_frame() -> None:
    """A base64 ws.frame is decoded and delivered as ('data', bytes)."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    ch_state = reg.open_ws_channel("r1", "ch01")

    raw_bytes = b"\x00\x01\x02"
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    frame = WSFrame(ch_id="ch01", data=encoded, encoding="base64")
    assert reg.route_ws_inbound("r1", frame) is True

    item = ch_state.inbound_queue.get_nowait()
    assert item == ("data", raw_bytes)


@pytest.mark.asyncio
async def test_route_ws_inbound_close_frame() -> None:
    """A ws.close is delivered as a ('close', (code, reason)) item."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    ch_state = reg.open_ws_channel("r1", "ch01")

    frame = WSCloseFrame(ch_id="ch01", code=1000, reason="done")
    assert reg.route_ws_inbound("r1", frame) is True

    item = ch_state.inbound_queue.get_nowait()
    assert item == ("close", (1000, "done"))


@pytest.mark.asyncio
async def test_route_ws_inbound_unknown_channel_returns_false() -> None:
    """Frames for an unregistered channel are silently dropped."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    frame = WSFrame(ch_id="unknown", data="hi", encoding="utf-8")
    assert reg.route_ws_inbound("r1", frame) is False


@pytest.mark.asyncio
async def test_route_ws_inbound_unknown_runner_returns_false() -> None:
    reg = TunnelRegistry()
    frame = WSFrame(ch_id="ch01", data="hi", encoding="utf-8")
    assert reg.route_ws_inbound("ghost", frame) is False


@pytest.mark.asyncio
async def test_route_ws_inbound_malformed_base64_returns_false() -> None:
    """Malformed base64 data is dropped."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    reg.open_ws_channel("r1", "ch01")

    frame = WSFrame(ch_id="ch01", data="not-valid-base64!!!", encoding="base64")
    assert reg.route_ws_inbound("r1", frame) is False


@pytest.mark.asyncio
async def test_route_ws_inbound_unknown_encoding_returns_false() -> None:
    """Unknown encoding is dropped."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    reg.open_ws_channel("r1", "ch01")

    frame = WSFrame(ch_id="ch01", data="data", encoding="utf-16")
    assert reg.route_ws_inbound("r1", frame) is False


@pytest.mark.asyncio
async def test_route_ws_inbound_non_ws_frame_returns_false() -> None:
    """Non-WS frame types (e.g. ResponseHead) are rejected."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    reg.open_ws_channel("r1", "ch01")

    frame = ResponseHeadFrame(id="req1", status=200)
    assert reg.route_ws_inbound("r1", frame) is False


@pytest.mark.asyncio
async def test_route_ws_inbound_stale_session_returns_false() -> None:
    """Frames from a stale session guard are rejected."""
    reg = TunnelRegistry()
    old_session = reg.register("r1", _NoopWS(), _hello())
    reg.register("r1", _NoopWS(), _hello())  # Replace

    frame = WSFrame(ch_id="ch01", data="hi", encoding="utf-8")
    assert reg.route_ws_inbound("r1", frame, session=old_session) is False


# ── send_text ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_text_enqueues_on_outbound_queue() -> None:
    """send_text puts data onto the session's outbound queue."""
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())

    await reg.send_text(session, '{"kind":"ping"}')

    item = session.outbound_queue.get_nowait()
    assert item is not None
    assert item.data == '{"kind":"ping"}'


async def test_send_diagnostics_follow_owner_loop_and_connection_generation() -> None:
    registry = TunnelRegistry()
    first = registry.register("r1", _NoopWS(), _hello())
    owner_thread = threading.get_ident()
    owner_now = 102.0
    # The caller requests at 100; handoff reaches the socket loop at 102.
    first.diagnostics = TunnelDiagnostics(
        clock=lambda: owner_now if threading.get_ident() == owner_thread else 100.0
    )
    await asyncio.to_thread(
        lambda: asyncio.run(
            asyncio.wait_for(
                registry.send_text(first, "heartbeat", app_ping_ts=123), timeout=budget(1)
            )
        )
    )
    queued = first.outbound_queue.get_nowait()
    assert queued is not None
    assert queued.data == "heartbeat"
    assert queued.app_ping_ts == 123
    assert queued.queued_at == 102.0
    snapshot = first.diagnostics.snapshot()
    assert snapshot["app_pings_queued"] == 1
    assert snapshot["outbound_queue_depth"] == 1
    assert snapshot["enqueue_delay_s"] == 2.0
    owner_now = 107.0
    first.diagnostics.dequeued(queued)
    assert first.diagnostics.snapshot()["queue_wait_s"] == 5.0

    second = registry.register("r1", _NoopWS(), _hello())
    with pytest.raises(ConnectionError, match="replaced"):
        await registry.send_text(first, "stale", app_ping_ts=456)
    assert second.diagnostics.snapshot()["app_pings_queued"] == 0
    assert second.diagnostics.snapshot()["last_app_ping_queued_age_s"] is None
    assert first.diagnostics.snapshot()["app_pings_queued"] == 0


async def test_send_text_propagates_owner_loop_timestamp_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = TunnelRegistry()
    session = registry.register("r1", _NoopWS(), _hello())
    owner_thread = threading.get_ident()
    error = RuntimeError("queue timing failed")

    def fail_on_owner_loop() -> float:
        if threading.get_ident() == owner_thread:
            raise error
        return 100.0

    monkeypatch.setattr(session.diagnostics, "timestamp", fail_on_owner_loop)

    async def send_from_other_loop() -> None:
        await asyncio.wait_for(registry.send_text(session, "payload"), timeout=budget(1))

    with pytest.raises(RuntimeError) as raised:
        await asyncio.to_thread(lambda: asyncio.run(send_from_other_loop()))
    assert raised.value is error
    assert session.outbound_queue.empty()


@pytest.mark.parametrize("cross_loop", [False, True], ids=["same-loop", "cross-loop"])
async def test_send_text_accepts_queued_frame_when_diagnostics_fail(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, cross_loop: bool
) -> None:
    registry = TunnelRegistry()
    session = registry.register("r1", _NoopWS(), _hello())
    await registry.send_text(session, "earlier frame")
    first = session.outbound_queue.get_nowait()
    assert first is not None
    session.diagnostics.dequeued(first)
    error = RuntimeError("queue diagnostics failed")
    caplog.set_level(logging.DEBUG, logger="omnigent.runner.transports.ws_tunnel.registry")

    def fail_recording(*_args: object) -> None:
        raise error

    monkeypatch.setattr(session.diagnostics, "enqueued", fail_recording)

    async def send() -> None:
        await asyncio.wait_for(
            registry.send_text(session, "heartbeat", app_ping_ts=123), timeout=budget(1)
        )

    if cross_loop:
        await asyncio.to_thread(lambda: asyncio.run(send()))
    else:
        await send()

    frame = session.outbound_queue.get_nowait()
    assert frame is not None
    assert frame.data == "heartbeat"
    assert frame.app_ping_ts == 123
    assert session.outbound_queue.empty()
    session.diagnostics.dequeued(frame)
    snapshot = session.diagnostics.snapshot()
    assert snapshot["outbound_queue_depth"] == 0
    assert snapshot["app_pings_queued"] == 0
    recorded = next(r for r in caplog.records if "outbound queue diagnostics failed" in r.message)
    assert recorded.exc_info is not None
    assert recorded.exc_info[1] is error


@pytest.mark.asyncio
async def test_send_text_raises_on_stale_session() -> None:
    """send_text rejects a session that was replaced."""
    reg = TunnelRegistry()
    old_session = reg.register("r1", _NoopWS(), _hello())
    reg.register("r1", _NoopWS(), _hello())  # Replace

    with pytest.raises(ConnectionError, match="replaced"):
        await reg.send_text(old_session, "data")


# ── Deregister aborts WS channels ──────────────────────


@pytest.mark.asyncio
async def test_deregister_aborts_ws_channels() -> None:
    """Deregistration sends None sentinel to all open WS channels."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    ch_state = reg.open_ws_channel("r1", "ch01")

    reg.deregister("r1")

    item = ch_state.inbound_queue.get_nowait()
    assert item is None


# ── wait_for_runner with zero timeout ───────────────────


@pytest.mark.asyncio
async def test_wait_for_runner_zero_timeout_just_checks() -> None:
    """timeout_s <= 0 falls through to a plain get()."""
    reg = TunnelRegistry()
    assert await reg.wait_for_runner("r1", timeout_s=0) is None
    session = reg.register("r1", _NoopWS(), _hello())
    assert await reg.wait_for_runner("r1", timeout_s=-1) is session

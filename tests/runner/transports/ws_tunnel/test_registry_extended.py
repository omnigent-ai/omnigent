"""Extended unit tests for TunnelRegistry — covers owner, timing,
WS channels, send_text, and observability methods not exercised
by the core lifecycle tests in test_registry.py.
"""

from __future__ import annotations

import asyncio
import base64
import threading

import pytest

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
    assert item == '{"kind":"ping"}'


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


# ── Outbound queue backpressure ─────────────────────────


@pytest.mark.asyncio
async def test_send_text_waits_out_a_full_queue_instead_of_failing() -> None:
    """A momentarily full outbound queue is backpressure, not an error.

    A burst can fill the queue before the sender task wakes; the send must
    wait for drain (never dropping or failing the frame) and complete once
    the sender frees a slot.
    """
    from omnigent.runner.transports.ws_tunnel.registry import _OUTBOUND_QUEUE_MAX_FRAMES

    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    for _ in range(_OUTBOUND_QUEUE_MAX_FRAMES):
        session.outbound_queue.put_nowait("frame")

    send = asyncio.create_task(reg.send_text(session, "tail-frame"))
    await asyncio.sleep(0.01)
    assert not send.done(), "send failed instead of waiting for the sender to drain"

    session.outbound_queue.get_nowait()  # the sender drains one slot
    await asyncio.wait_for(send, timeout=2.0)  # frame accepted, never dropped


@pytest.mark.asyncio
async def test_send_text_fails_loud_when_sender_stalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sender that frees no room within the deadline fails the send loudly.

    Protocol frames must never be dropped silently — a lost frame strands its
    RPC — so a truly stalled socket surfaces ConnectionError to the caller.
    """
    import omnigent.runner.transports.ws_tunnel.registry as registry_mod
    from omnigent.runner.transports.ws_tunnel.registry import _OUTBOUND_QUEUE_MAX_FRAMES

    monkeypatch.setattr(registry_mod, "_OUTBOUND_SEND_STALL_S", 0.05)
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    for _ in range(_OUTBOUND_QUEUE_MAX_FRAMES):
        session.outbound_queue.put_nowait("frame")

    with pytest.raises(ConnectionError, match="stalled"):
        await reg.send_text(session, "frame-behind-stalled-sender")


@pytest.mark.asyncio
async def test_send_text_reports_replaced_when_session_swapped_mid_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A send parked on a full queue resolves as 'replaced' after a swap.

    Replacing the session retires the old queue (stop sentinel keeps it
    full), so the parked put can't complete — the send must still resolve
    within the stall deadline and name the replacement, not a stall.
    """
    import omnigent.runner.transports.ws_tunnel.registry as registry_mod
    from omnigent.runner.transports.ws_tunnel.registry import _OUTBOUND_QUEUE_MAX_FRAMES

    monkeypatch.setattr(registry_mod, "_OUTBOUND_SEND_STALL_S", 0.2)
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    for _ in range(_OUTBOUND_QUEUE_MAX_FRAMES):
        session.outbound_queue.put_nowait("frame")

    send = asyncio.create_task(reg.send_text(session, "parked-frame"))
    await asyncio.sleep(0.01)
    assert not send.done(), "send resolved before the sender freed any room"

    reg.register("r1", _NoopWS(), _hello())  # replaces + retires the old session
    with pytest.raises(ConnectionError, match="replaced"):
        await asyncio.wait_for(send, timeout=2.0)


@pytest.mark.asyncio
async def test_send_text_resolves_when_enqueue_task_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the owner-loop enqueue task must never orphan the caller.

    Route teardown cancels pending tasks on the session loop; a send parked
    in the bounded-wait put must surface ConnectionError, not hang forever
    on an ack nothing will ever settle.
    """
    import omnigent.runner.transports.ws_tunnel.registry as registry_mod
    from omnigent.runner.transports.ws_tunnel.registry import _OUTBOUND_QUEUE_MAX_FRAMES

    monkeypatch.setattr(registry_mod, "_OUTBOUND_SEND_STALL_S", 30.0)
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    for _ in range(_OUTBOUND_QUEUE_MAX_FRAMES):
        session.outbound_queue.put_nowait("frame")

    before = asyncio.all_tasks()
    send = asyncio.create_task(reg.send_text(session, "parked-frame"))
    await asyncio.sleep(0.01)
    enqueue_tasks = asyncio.all_tasks() - before - {send}
    assert enqueue_tasks, "expected the owner-loop enqueue task to be running"
    for task in enqueue_tasks:
        task.cancel()

    with pytest.raises(ConnectionError):
        await asyncio.wait_for(send, timeout=2.0)


@pytest.mark.asyncio
async def test_send_text_resolves_when_enqueue_task_cancelled_before_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task cancelled before its coroutine's FIRST step must settle the ack.

    Such a cancel never enters the coroutine body, so its try/finally cannot
    run — only the task-done backstop keeps the caller from hanging forever.
    """
    import omnigent.runner.transports.ws_tunnel.registry as registry_mod
    from omnigent.runner.transports.ws_tunnel.registry import _OUTBOUND_QUEUE_MAX_FRAMES

    monkeypatch.setattr(registry_mod, "_OUTBOUND_SEND_STALL_S", 30.0)
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    for _ in range(_OUTBOUND_QUEUE_MAX_FRAMES):
        session.outbound_queue.put_nowait("frame")

    before = asyncio.all_tasks()
    send = asyncio.create_task(reg.send_text(session, "parked-frame"))
    # ONE bare yield: send_text has created the enqueue task, but the loop
    # has not stepped it yet — the cancel below lands pre-start.
    await asyncio.sleep(0)
    enqueue_tasks = asyncio.all_tasks() - before - {send}
    assert enqueue_tasks, "expected the enqueue task to exist before its first step"
    for task in enqueue_tasks:
        task.cancel()

    with pytest.raises(ConnectionError):
        await asyncio.wait_for(send, timeout=2.0)


@pytest.mark.asyncio
async def test_send_text_fails_loud_when_owner_loop_stopped() -> None:
    """A stopped owner loop never runs the enqueue callback — fail loud.

    In-process the owner loop is the server loop and stops only at shutdown
    (the stopped-loop hang is not reachable in normal operation), but the
    guard is cheap and turns a would-be forever-await into ConnectionError.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        reg = TunnelRegistry()

        async def _register() -> object:
            return reg.register("r1", _NoopWS(), _hello())

        session = asyncio.run_coroutine_threadsafe(_register(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        assert not loop.is_running()

        with pytest.raises(ConnectionError, match="not running"):
            await asyncio.wait_for(reg.send_text(session, "frame"), timeout=2.0)  # type: ignore[arg-type]
    finally:
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
        loop.close()


@pytest.mark.asyncio
async def test_retire_delivers_stop_sentinel_through_full_queue() -> None:
    """Retiring a session with a full outbound queue still stops its sender.

    The stop sentinel makes room by evicting one dead frame (the session's
    in-flight requests are already aborted) instead of overflowing — so the
    queue stays at its bound and the sender task still sees None and exits.
    """
    from omnigent.runner.transports.ws_tunnel.registry import _OUTBOUND_QUEUE_MAX_FRAMES

    reg = TunnelRegistry()
    old = reg.register("r1", _NoopWS(), _hello())
    for _ in range(_OUTBOUND_QUEUE_MAX_FRAMES):
        old.outbound_queue.put_nowait("frame")

    reg.register("r1", _NoopWS(), _hello())  # newest-wins triggers retire of old
    await asyncio.sleep(0)  # let the retire callback run on this loop

    assert old.outbound_queue.qsize() == _OUTBOUND_QUEUE_MAX_FRAMES
    drained = [old.outbound_queue.get_nowait() for _ in range(old.outbound_queue.qsize())]
    assert drained[-1] is None

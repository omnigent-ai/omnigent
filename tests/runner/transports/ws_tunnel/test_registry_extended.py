"""Extended unit tests for TunnelRegistry — covers owner, timing,
WS channels, send_text, and observability methods not exercised
by the core lifecycle tests in test_registry.py.
"""

from __future__ import annotations

import asyncio
import base64
import threading

import pytest

import omnigent.runner.transports.ws_tunnel.registry as registry_mod
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


def _fill_outbound(session: registry_mod.RunnerSession) -> None:
    for _ in range(registry_mod._OUTBOUND_QUEUE_MAX_FRAMES):
        session.outbound_queue.put_nowait("frame")


@pytest.mark.asyncio
async def test_registered_outbound_queue_is_bounded() -> None:
    """Each runner generation has a fixed frame budget."""
    session = TunnelRegistry().register("r1", _NoopWS(), _hello())
    assert session.outbound_queue.maxsize == registry_mod._OUTBOUND_QUEUE_MAX_FRAMES
    _fill_outbound(session)
    with pytest.raises(asyncio.QueueFull):
        session.outbound_queue.put_nowait("overflow")


@pytest.mark.asyncio
async def test_send_text_waits_for_a_full_queue_to_drain() -> None:
    """A temporary burst waits for room without dropping its tail frame."""
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    _fill_outbound(session)

    send = asyncio.create_task(reg.send_text(session, "tail-frame"))
    await asyncio.sleep(0.01)
    assert not send.done()
    session.outbound_queue.get_nowait()
    await asyncio.wait_for(send, timeout=2.0)

    drained = [session.outbound_queue.get_nowait() for _ in range(session.outbound_queue.qsize())]
    assert drained[-1] == "tail-frame"


@pytest.mark.asyncio
async def test_send_text_fails_when_a_full_queue_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sender that frees no room surfaces a bounded ConnectionError."""
    monkeypatch.setattr(registry_mod, "_OUTBOUND_SEND_STALL_S", 0.05)
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    _fill_outbound(session)

    with pytest.raises(ConnectionError, match="stalled"):
        await reg.send_text(session, "blocked-frame")


@pytest.mark.asyncio
async def test_replacement_releases_waiter_without_enqueueing_on_retired_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generation replacement wins atomically over a waiting enqueue."""
    monkeypatch.setattr(registry_mod, "_OUTBOUND_SEND_STALL_S", 30.0)
    reg = TunnelRegistry()
    old = reg.register("r1", _NoopWS(), _hello())
    _fill_outbound(old)
    send = asyncio.create_task(reg.send_text(old, "sneaky-frame"))
    await asyncio.sleep(0.05)
    assert not send.done()

    reg.register("r1", _NoopWS(), _hello())
    await asyncio.sleep(0)
    old.outbound_queue.get_nowait()
    with pytest.raises(ConnectionError, match="replaced"):
        await asyncio.wait_for(send, timeout=2.0)

    drained = [old.outbound_queue.get_nowait() for _ in range(old.outbound_queue.qsize())]
    assert "sneaky-frame" not in drained


@pytest.mark.parametrize("cancel_before_start", [False, True])
@pytest.mark.asyncio
async def test_cancelled_enqueue_task_resolves_waiting_sender(
    monkeypatch: pytest.MonkeyPatch,
    cancel_before_start: bool,
) -> None:
    """Owner-loop cancellation cannot orphan the cross-loop acknowledgement."""
    monkeypatch.setattr(registry_mod, "_OUTBOUND_SEND_STALL_S", 30.0)
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    _fill_outbound(session)

    before = asyncio.all_tasks()
    send = asyncio.create_task(reg.send_text(session, "parked-frame"))
    await asyncio.sleep(0 if cancel_before_start else 0.01)
    enqueue_tasks = asyncio.all_tasks() - before - {send}
    assert enqueue_tasks
    for task in enqueue_tasks:
        task.cancel()

    with pytest.raises(ConnectionError):
        await asyncio.wait_for(send, timeout=2.0)


@pytest.mark.asyncio
async def test_cancelled_send_text_never_enqueues_after_room_frees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller cancellation prevents a parked owner-loop enqueue from landing."""
    monkeypatch.setattr(registry_mod, "_OUTBOUND_SEND_STALL_S", 30.0)
    monkeypatch.setattr(registry_mod, "_OUTBOUND_SEND_POLL_S", 0.01)
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    _fill_outbound(session)

    send = asyncio.create_task(reg.send_text(session, "cancelled-frame"))
    await asyncio.sleep(0)
    send.cancel()
    with pytest.raises(asyncio.CancelledError):
        await send

    session.outbound_queue.get_nowait()
    await asyncio.sleep(0.05)
    drained = [session.outbound_queue.get_nowait() for _ in range(session.outbound_queue.qsize())]
    assert "cancelled-frame" not in drained


@pytest.mark.asyncio
async def test_send_text_cancellation_is_atomic_across_event_loop_threads() -> None:
    """Cancellation cannot be reported after the owner loop commits the frame."""
    entered_put = threading.Event()
    release_put = threading.Event()
    inserted = threading.Event()

    class _PausingQueue(asyncio.Queue[str | None]):
        def put_nowait(self, item: str | None) -> None:
            if item == "cancelled-frame":
                entered_put.set()
                release_put.wait(timeout=2.0)
            super().put_nowait(item)
            if item == "cancelled-frame":
                inserted.set()

    owner_loop = asyncio.new_event_loop()
    owner_thread = threading.Thread(target=owner_loop.run_forever, daemon=True)
    owner_thread.start()
    timer: threading.Timer | None = None
    try:
        reg = TunnelRegistry()

        async def _register() -> registry_mod.RunnerSession:
            session = reg.register("r1", _NoopWS(), _hello())
            session.outbound_queue = _PausingQueue(maxsize=registry_mod._OUTBOUND_QUEUE_MAX_FRAMES)
            return session

        session = await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(_register(), owner_loop)
        )
        send = asyncio.create_task(reg.send_text(session, "cancelled-frame"))
        assert await asyncio.wait_for(asyncio.to_thread(entered_put.wait, 2.0), timeout=3.0)

        # The owner loop has passed the cancellation guard but is paused inside
        # put_nowait. Let cancellation run on this separate caller loop before
        # releasing the insertion window.
        timer = threading.Timer(0.1, release_put.set)
        timer.start()
        send.cancel()
        await send

        assert await asyncio.wait_for(asyncio.to_thread(inserted.wait, 2.0), timeout=3.0)

        async def _drain() -> list[str | None]:
            return [
                session.outbound_queue.get_nowait() for _ in range(session.outbound_queue.qsize())
            ]

        drained = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_drain(), owner_loop))
        assert drained == ["cancelled-frame"]
    finally:
        release_put.set()
        if timer is not None:
            timer.join(timeout=2.0)
        owner_loop.call_soon_threadsafe(owner_loop.stop)
        owner_thread.join(timeout=5.0)
        owner_loop.close()


@pytest.mark.asyncio
async def test_send_text_fails_when_owner_loop_is_stopped() -> None:
    """A stopped owner loop fails the send instead of awaiting forever."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        reg = TunnelRegistry()

        async def _register() -> registry_mod.RunnerSession:
            return reg.register("r1", _NoopWS(), _hello())

        session = asyncio.run_coroutine_threadsafe(_register(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        assert not loop.is_running()
        with pytest.raises(ConnectionError, match="not running"):
            await asyncio.wait_for(reg.send_text(session, "frame"), timeout=2.0)
    finally:
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
        loop.close()


@pytest.mark.asyncio
async def test_retire_places_stop_sentinel_in_a_full_queue() -> None:
    """Retiring a saturated generation still wakes and stops its writer."""
    reg = TunnelRegistry()
    old = reg.register("r1", _NoopWS(), _hello())
    _fill_outbound(old)

    reg.register("r1", _NoopWS(), _hello())
    await asyncio.sleep(0)

    assert old.outbound_queue.qsize() == registry_mod._OUTBOUND_QUEUE_MAX_FRAMES
    drained = [old.outbound_queue.get_nowait() for _ in range(old.outbound_queue.qsize())]
    assert drained[-1] is None

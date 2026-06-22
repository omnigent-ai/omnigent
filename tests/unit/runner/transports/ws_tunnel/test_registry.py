"""Tests for TunnelRegistry session lifecycle + newest-wins + abort semantics.

Pinning:
- register/deregister maintains the dict.
- "newest wins" — re-registering aborts old session's in-flight.
- deregister aborts in-flight requests.
- route_response_frame routes to right req_id.
- close_request cleans up.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

import pytest

from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
)
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry


class _NoopWS:
    """Minimal WebSocket fake for tests that don't actually send."""

    async def send_text(self, data: str) -> None:
        pass

    async def receive_text(self) -> str:
        # Block forever — tests that need to read drive their own
        # path; this fake exists so the registry has something to
        # hold.
        return await asyncio.Future()


def _hello() -> HelloFrame:
    return HelloFrame(runner_version="0.1.0", frame_protocol_version=1, harnesses=[], envs=[])


async def _wait_until(predicate: Callable[[], object], *, timeout_s: float = 1.0) -> None:
    """Wait until a synchronous predicate returns true.

    :param predicate: Zero-argument callable returning a truthy value
        when the wait should stop.
    :param timeout_s: Maximum seconds to wait, e.g. ``1.0``.
    :returns: None.
    :raises AssertionError: If the predicate never becomes true.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("predicate did not become true before timeout")


@pytest.mark.asyncio
async def test_register_then_get_returns_session() -> None:
    reg = TunnelRegistry()
    ws = _NoopWS()
    session = reg.register("r1", ws, _hello())
    fetched = reg.get("r1")
    assert fetched is session
    assert fetched.ws is ws


def test_get_returns_none_for_unknown() -> None:
    reg = TunnelRegistry()
    assert reg.get("ghost") is None


@pytest.mark.asyncio
async def test_wait_for_runner_returns_existing_session_immediately() -> None:
    """Registered runners resolve without creating waiter state."""
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())

    resolved = await reg.wait_for_runner("r1", timeout_s=1.0)

    assert resolved is session
    assert reg.connect_waiter_count("r1") == 0
    assert reg.connect_wait_started_at("r1") is None


@pytest.mark.asyncio
async def test_wait_for_runner_resolves_when_runner_registers() -> None:
    """A waiter is event-woken by register() and cleaned up."""
    reg = TunnelRegistry()
    task = asyncio.create_task(reg.wait_for_runner("r1", timeout_s=1.0))
    await _wait_until(lambda: reg.connect_waiter_count("r1") == 1)
    started_at = reg.connect_wait_started_at("r1")

    session = reg.register("r1", _NoopWS(), _hello())
    resolved = await task

    assert resolved is session
    assert started_at is not None and started_at <= time.time()
    assert reg.connect_waiter_count("r1") == 0
    assert reg.connect_wait_started_at("r1") is None


@pytest.mark.asyncio
async def test_wait_for_runner_timeout_removes_waiter() -> None:
    """Timed-out waiters do not leave stale registry state."""
    reg = TunnelRegistry()

    resolved = await reg.wait_for_runner("missing", timeout_s=0.01)

    assert resolved is None
    assert reg.connect_waiter_count("missing") == 0
    assert reg.connect_wait_started_at("missing") is None


@pytest.mark.asyncio
async def test_wait_for_runner_cancel_removes_waiter() -> None:
    """Cancelled request tasks clean their waiter in the finally block."""
    reg = TunnelRegistry()
    task = asyncio.create_task(reg.wait_for_runner("r1", timeout_s=10.0))
    await _wait_until(lambda: reg.connect_waiter_count("r1") == 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert reg.connect_waiter_count("r1") == 0
    assert reg.connect_wait_started_at("r1") is None


@pytest.mark.asyncio
async def test_wait_for_runner_resolves_multiple_waiters() -> None:
    """One registration wakes every active waiter for that runner id."""
    reg = TunnelRegistry()
    tasks = [asyncio.create_task(reg.wait_for_runner("r1", timeout_s=1.0)) for _ in range(3)]
    await _wait_until(lambda: reg.connect_waiter_count("r1") == 3)

    session = reg.register("r1", _NoopWS(), _hello())
    resolved = await asyncio.gather(*tasks)

    assert resolved == [session, session, session]
    assert reg.connect_waiter_count("r1") == 0


@pytest.mark.asyncio
async def test_wait_for_runner_cap_bounds_waiter_growth() -> None:
    """The per-runner cap prevents unbounded waiter accumulation."""
    reg = TunnelRegistry(max_connect_waiters_per_runner=1)
    first = asyncio.create_task(reg.wait_for_runner("r1", timeout_s=0.2))
    await _wait_until(lambda: reg.connect_waiter_count("r1") == 1)

    second = asyncio.create_task(reg.wait_for_runner("r1", timeout_s=0.01))
    await asyncio.sleep(0)
    assert reg.connect_waiter_count("r1") == 1

    assert await second is None
    assert reg.connect_waiter_count("r1") == 1
    assert await first is None
    assert reg.connect_waiter_count("r1") == 0


@pytest.mark.asyncio
async def test_wait_for_runner_global_cap_bounds_waiter_growth() -> None:
    """The global cap prevents unbounded waiter accumulation across runner ids."""
    reg = TunnelRegistry(max_connect_waiters_total=1)
    first = asyncio.create_task(reg.wait_for_runner("r1", timeout_s=0.2))
    await _wait_until(lambda: reg.connect_waiter_count() == 1)

    second = asyncio.create_task(reg.wait_for_runner("r2", timeout_s=0.01))
    await asyncio.sleep(0)
    assert reg.connect_waiter_count() == 1
    assert reg.connect_waiter_count("r2") == 0
    assert reg.connect_wait_started_at("r2") is None

    assert await second is None
    assert reg.connect_waiter_count() == 1
    assert await first is None
    assert reg.connect_waiter_count() == 0


def test_wait_for_runner_rejects_invalid_waiter_caps() -> None:
    """Waiter caps must be positive finite counters."""
    with pytest.raises(ValueError, match="per_runner"):
        TunnelRegistry(max_connect_waiters_per_runner=0)
    with pytest.raises(ValueError, match="total"):
        TunnelRegistry(max_connect_waiters_total=0)


@pytest.mark.asyncio
async def test_deregister_removes_session() -> None:
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    assert "r1" in reg
    reg.deregister("r1")
    assert reg.get("r1") is None
    assert "r1" not in reg


def test_deregister_unknown_runner_is_noop() -> None:
    reg = TunnelRegistry()
    # Should not raise.
    assert reg.deregister("ghost") is None


@pytest.mark.asyncio
async def test_register_replacing_existing_session_aborts_inflight() -> None:
    """Newest-wins (RUNNER.md §2): old session's in-flight requests get
    a ConnectionError so awaiters don't hang."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    state = reg.open_request("r1", "req1")

    # Re-register with a different WS — old session should be aborted.
    reg.register("r1", _NoopWS(), _hello())

    # The old in-flight request's head_future got an exception.
    assert state.head_future.done()
    with pytest.raises(ConnectionError, match="newest-wins"):
        state.head_future.result()


@pytest.mark.asyncio
async def test_deregister_aborts_inflight() -> None:
    """Tunnel close → in-flight requests fail with ConnectionError, not hang."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    state = reg.open_request("r1", "req1")

    reg.deregister("r1")

    assert state.head_future.done()
    with pytest.raises(ConnectionError, match="tunnel closed"):
        state.head_future.result()


@pytest.mark.asyncio
async def test_stale_deregister_does_not_remove_newest_session() -> None:
    """Cleanup from an old route handler must not remove a newer tunnel."""
    reg = TunnelRegistry()
    old_session = reg.register("r1", _NoopWS(), _hello())
    new_session = reg.register("r1", _NoopWS(), _hello())

    assert reg.deregister("r1", old_session) is None
    assert reg.get("r1") is new_session

    assert reg.deregister("r1", new_session) is new_session
    assert reg.get("r1") is None


@pytest.mark.asyncio
async def test_open_request_for_unknown_runner_raises_keyerror() -> None:
    reg = TunnelRegistry()
    with pytest.raises(KeyError):
        reg.open_request("ghost", "req1")


@pytest.mark.asyncio
async def test_open_request_with_duplicate_id_raises() -> None:
    """req_ids are per-session unique; reusing one is a programming bug."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    reg.open_request("r1", "req1")
    with pytest.raises(ValueError, match="already in flight"):
        reg.open_request("r1", "req1")


@pytest.mark.asyncio
async def test_route_response_head_resolves_future() -> None:
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    state = reg.open_request("r1", "req1")
    routed = reg.route_response_frame(
        "r1",
        ResponseHeadFrame(id="req1", status=200, headers=[["content-type", "text/plain"]]),
    )
    assert routed is True
    head = await state.head_future
    assert head.status == 200


@pytest.mark.asyncio
async def test_route_response_head_from_other_thread_wakes_future_promptly() -> None:
    """Response frames routed from the WS loop wake a DBOS-loop waiter."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    state = reg.open_request("r1", "req1")
    route_now = threading.Event()

    def _route_from_thread() -> None:
        """Route the head frame from a non-asyncio worker thread."""
        assert route_now.wait(timeout=1.0), "test did not release routing thread"
        reg.route_response_frame("r1", ResponseHeadFrame(id="req1", status=200))

    thread = threading.Thread(target=_route_from_thread, name="route-response-frame")
    started = time.monotonic()
    thread.start()
    try:
        asyncio.get_running_loop().call_later(0.02, route_now.set)
        head = await asyncio.wait_for(state.head_future, timeout=1.0)
    finally:
        thread.join(timeout=1.0)

    assert head.status == 200
    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_route_body_then_end_drains_queue() -> None:
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    state = reg.open_request("r1", "req1")
    reg.route_response_frame("r1", ResponseHeadFrame(id="req1", status=200))
    reg.route_response_frame("r1", ResponseBodyFrame(id="req1", body="chunk1", encoding="utf-8"))
    reg.route_response_frame("r1", ResponseEndFrame(id="req1"))
    # Body queue: chunk1 then a None sentinel from the end-event.
    first = await state.body_queue.get()
    assert isinstance(first, ResponseBodyFrame)
    assert first.body == "chunk1"
    second = await state.body_queue.get()
    assert second is None  # sentinel for end-of-stream
    assert state.end_event.is_set()


@pytest.mark.asyncio
async def test_route_response_drops_stale_request_when_owner_loop_closed() -> None:
    """Late frames for a closed request loop do not tear down the tunnel."""
    reg = TunnelRegistry()
    session = reg.register("r1", _NoopWS(), _hello())
    state = reg.open_request("r1", "req1")
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    state.loop = closed_loop

    routed = reg.route_response_frame("r1", ResponseHeadFrame(id="req1", status=200))

    assert routed is False
    assert reg.get("r1") is session
    assert "req1" not in session.in_flight


def test_route_response_for_unknown_runner_returns_false() -> None:
    """Late frame for a runner that's no longer registered: silent drop, return False."""
    reg = TunnelRegistry()
    routed = reg.route_response_frame("ghost", ResponseHeadFrame(id="req1", status=200))
    assert routed is False


@pytest.mark.asyncio
async def test_route_response_for_unknown_req_id_returns_false() -> None:
    """Late frame for a req_id we already closed: silent drop."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    routed = reg.route_response_frame("r1", ResponseHeadFrame(id="never_opened", status=200))
    assert routed is False


@pytest.mark.asyncio
async def test_close_request_removes_state() -> None:
    # Async because ``open_request`` allocates an asyncio.Future via
    # the running loop; without a loop in scope it raises
    # "There is no current event loop in thread 'MainThread'" once
    # earlier tests have closed/cleared the default loop.
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    reg.open_request("r1", "req1")
    reg.close_request("r1", "req1")
    # The req_id is no longer trackable.
    routed = reg.route_response_frame("r1", ResponseHeadFrame(id="req1", status=200))
    assert routed is False


def test_close_request_unknown_runner_is_noop() -> None:
    reg = TunnelRegistry()
    # Should not raise.
    reg.close_request("ghost", "req1")


@pytest.mark.asyncio
async def test_online_runner_ids_returns_insertion_order() -> None:
    """Insertion-order iteration gives routing layer a deterministic round-robin."""
    reg = TunnelRegistry()
    reg.register("r1", _NoopWS(), _hello())
    reg.register("r2", _NoopWS(), _hello())
    reg.register("r3", _NoopWS(), _hello())
    assert reg.online_runner_ids() == ["r1", "r2", "r3"]
    reg.deregister("r2")
    assert reg.online_runner_ids() == ["r1", "r3"]

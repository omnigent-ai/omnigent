"""
Tests for the controllable mock tool helper.

These tests verify the queue/blocking/release semantics that
Phase 2+ concurrency tests rely on. If these tests pass, callers
can trust that:

* queued calls are consumed in FIFO order;
* ``call_event`` fires before the body blocks;
* blocked calls don't return until ``release()`` runs;
* exceptions propagate from the call site, not from ``add_call``;
* ``release_all()`` cleans up forgotten releases in teardown.

Without trustworthy mock semantics the broader concurrency suite
becomes fire-and-hope and silently rots. See the testing skill,
"Concurrency test requirements".
"""

from __future__ import annotations

import asyncio

import pytest

from tests.unit.server.integration.mock_tool import (
    ControllableMockTool,
    MockToolCall,
)

# ─── basic queue semantics ───────────────────────────────────


@pytest.mark.asyncio
async def test_calls_consumed_in_fifo_order() -> None:
    """Queued calls fire in the same order they were added."""
    tool = ControllableMockTool()
    tool.add_call(result="first")
    tool.add_call(result="second")
    tool.add_call(result="third")

    # 3 invocations because we added 3 — if the order were wrong
    # the assert would surface "second" or "third" first.
    assert await tool() == "first"
    assert await tool() == "second"
    assert await tool() == "third"


@pytest.mark.asyncio
async def test_unscripted_call_uses_default_so_tests_dont_deadlock() -> None:
    """Calls past the queued count fall back to a default MockToolCall."""
    tool = ControllableMockTool()
    # No add_call — production code would call us anyway.
    result = await tool()
    # The default marker — proves the mock fired (rather than
    # the production tool) without the test having to script.
    assert result == "mock-tool-result"


@pytest.mark.asyncio
async def test_received_calls_records_invocations_in_order() -> None:
    """``received_calls`` lets tests assert on exactly-N invocations."""
    tool = ControllableMockTool()
    call_a = tool.add_call(result="a")
    call_b = tool.add_call(result="b")

    await tool(arg="alpha")
    await tool(arg="beta")

    # Exactly 2 invocations recorded; if the production code
    # accidentally double-fires, this would be 3+.
    assert tool.received_calls == [call_a, call_b]
    assert call_a.received_arguments == {"arg": "alpha"}
    assert call_b.received_arguments == {"arg": "beta"}


# ─── exception path ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_exception_propagates_from_call_site() -> None:
    """An ``exception=`` config raises at ``__call__`` time, not at ``add_call``."""
    tool = ControllableMockTool()
    sentinel = ValueError("boom from mock")
    tool.add_call(exception=sentinel)

    with pytest.raises(ValueError, match="boom from mock"):
        await tool()
    # add_call must not raise — production code triggers the
    # exception, not the test setup.


# ─── blocking + release semantics ────────────────────────────


@pytest.mark.asyncio
async def test_blocked_call_does_not_return_until_release() -> None:
    """``block=True`` holds the body until ``release()`` runs."""
    tool = ControllableMockTool()
    call = tool.add_call(result="unblocked", block=True)

    invoke_task = asyncio.create_task(tool())
    # Wait until the body has signalled entry, then prove it's
    # still suspended — if it weren't blocking, the task would
    # already be done by the time we ask.
    await call.wait_called(timeout=1.0)
    assert not invoke_task.done(), (
        "Blocked call returned before release() was called — "
        "block_before_response is not actually gating the body."
    )

    call.release()
    # release() lets the body proceed — task should finish promptly.
    result = await asyncio.wait_for(invoke_task, timeout=1.0)
    assert result == "unblocked"


@pytest.mark.asyncio
async def test_call_event_fires_before_block_so_test_can_synchronize() -> None:
    """``call_event`` is set BEFORE the body waits on the block event.

    This is the deterministic-race-window guarantee the testing skill
    requires for concurrency tests: if the event fired AFTER the wait,
    the test would block forever waiting for an event that depends on
    the test releasing the mock. The test would deadlock.
    """
    tool = ControllableMockTool()
    call = tool.add_call(result="done", block=True)

    invoke_task = asyncio.create_task(tool())
    # If call_event were set after block_before_response.wait(),
    # this wait_for would time out — the body would be parked on
    # the block event forever and never set call_event.
    await call.wait_called(timeout=1.0)

    call.release()
    await invoke_task


@pytest.mark.asyncio
async def test_release_all_unblocks_every_pending_call() -> None:
    """Teardown helper unblocks both queued and consumed calls."""
    tool = ControllableMockTool()
    call_consumed = tool.add_call(result="consumed", block=True)
    call_queued = tool.add_call(result="queued", block=True)

    # Start the first invocation so it sits in the consumed list,
    # blocked.
    invoke_task = asyncio.create_task(tool())
    await call_consumed.wait_called(timeout=1.0)

    # release_all should unblock both: the in-flight one (so the
    # task finishes) AND the still-queued one (so a future invoke
    # wouldn't hang either).
    tool.release_all()

    # The blocked, in-flight call returns.
    assert await asyncio.wait_for(invoke_task, timeout=1.0) == "consumed"

    # The still-queued call is also pre-released — its body
    # completes immediately when invoked.
    assert await asyncio.wait_for(tool(), timeout=1.0) == "queued"
    # Both events should be set (release_all idempotently sets both).
    assert call_consumed.block_before_response is not None
    assert call_consumed.block_before_response.is_set()
    assert call_queued.block_before_response is not None
    assert call_queued.block_before_response.is_set()


# ─── default MockToolCall shape ──────────────────────────────


def test_default_mock_call_has_no_block_event() -> None:
    """A non-blocking call has ``block_before_response=None``."""
    import threading

    call = MockToolCall()
    # If block_before_response were always created, every call
    # would have a release() side-effect — release_all wouldn't
    # be able to distinguish blocking from non-blocking calls.
    assert call.block_before_response is None
    # call_event always exists — tests can wait on it whether
    # the call blocks or not. threading.Event (not asyncio.Event)
    # so cross-loop set()/wait() works between the test loop and
    # DBOS's background workflow loop.
    assert isinstance(call.call_event, threading.Event)

"""The snapshot backstop that ends a turn whose terminal stream event was missed."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from omnigent_client import OmnigentClient

from omnigent.repl import _repl as repl_mod


def _adapter(get: AsyncMock) -> repl_mod._SessionsChatReplAdapter:
    client = Mock(spec=OmnigentClient)
    client.sessions = SimpleNamespace(get=get)
    adapter = repl_mod._SessionsChatReplAdapter(client=client, agent_name="test-agent")
    adapter._turn_done = asyncio.Event()  # send() creates it per turn
    return adapter


async def test_missed_terminal_event_is_caught_with_a_growing_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream never reports the end, so the snapshot does, checked less and less often."""
    timeouts: list[float] = []

    async def no_event(awaitable: Awaitable[object], timeout: float) -> object:
        timeouts.append(timeout)
        awaitable.close()  # type: ignore[attr-defined]
        raise TimeoutError

    monkeypatch.setattr(repl_mod.asyncio, "wait_for", no_event)
    statuses = ["running", "running", "running", "running", "idle"]
    get = AsyncMock(side_effect=[SimpleNamespace(status=s) for s in statuses])
    adapter = _adapter(get)

    await adapter._wait_for_turn_done("conv_a")

    assert adapter._turn_done.is_set()
    assert timeouts == [1.0, 2.0, 4.0, 5.0, 5.0]
    # Each check reads the snapshot without items, liveness or usage.
    for call in get.await_args_list:
        assert call.args == ("conv_a",)
        assert call.kwargs == {
            "include_items": False,
            "include_liveness": False,
            "include_usage": False,
        }


async def test_a_failed_turn_also_ends_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_event(awaitable: Awaitable[object], timeout: float) -> object:
        awaitable.close()  # type: ignore[attr-defined]
        raise TimeoutError

    monkeypatch.setattr(repl_mod.asyncio, "wait_for", no_event)
    get = AsyncMock(return_value=SimpleNamespace(status="failed"))
    adapter = _adapter(get)

    await adapter._wait_for_turn_done("conv_a")

    assert get.await_count == 1


async def test_the_stream_event_ends_the_wait_without_a_snapshot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repl_mod, "_TURN_BACKSTOP_FIRST_SECONDS", 30.0)
    get = AsyncMock()
    adapter = _adapter(get)

    waiter = asyncio.create_task(adapter._wait_for_turn_done("conv_a"))
    await asyncio.sleep(0)
    adapter._turn_done.set()
    await asyncio.wait_for(waiter, timeout=5)

    get.assert_not_awaited()


async def test_cancelling_the_wait_leaves_no_task_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repl_mod, "_TURN_BACKSTOP_FIRST_SECONDS", 30.0)
    adapter = _adapter(AsyncMock())
    before = len(asyncio.all_tasks())

    waiter = asyncio.create_task(adapter._wait_for_turn_done("conv_a"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert len(asyncio.all_tasks()) == before

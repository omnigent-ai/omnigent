"""Unit tests for :mod:`omnigent.runtime.user_session_stream`.

The per-user discovery fan-out is workspace-namespaced: the same user key can
belong to several workspaces on one pod, so a ``session_added`` (or hosts /
projects changed) event published in one tenant's workspace must not reach that
user's stream in another's. Regression for OMNI-7361.
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.runtime import user_session_stream


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    user_session_stream._subscribers.clear()
    yield
    user_session_stream._subscribers.clear()


async def _collect_one(user_key: str) -> dict:
    gen = user_session_stream.subscribe(user_key)
    try:
        async for event in gen:
            return event
        return {}
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_discovery_events_isolated_across_workspaces() -> None:
    """A publish for a user key in one workspace never reaches another's."""
    user = "alice@example.com"
    with workspace_scope(1):
        task = asyncio.create_task(_collect_one(user))
        await asyncio.sleep(0)  # register the subscriber under workspace 1
    with workspace_scope(2):
        # Same user key, different workspace: this publish reaches no one here.
        user_session_stream.publish(user, {"type": "session_added", "session_id": "ws2"})
    with workspace_scope(1):
        user_session_stream.publish(user, {"type": "session_added", "session_id": "ws1"})
    event = await asyncio.wait_for(task, timeout=2.0)
    assert event == {"type": "session_added", "session_id": "ws1"}


@pytest.mark.parametrize("event_type", ["session_added", "hosts_changed", "projects_changed"])
@pytest.mark.parametrize("threaded", [False, True])
async def test_closed_subscriber_does_not_break_discovery_delivery(
    event_type: str, threaded: bool
) -> None:
    user = "alice@example.com"
    stale_loop = asyncio.new_event_loop()
    stale_loop.close()
    stale_queue: asyncio.Queue[dict] = asyncio.Queue()
    stale = (stale_queue, stale_loop)
    user_session_stream._subscribers.setdefault(user, set()).add(stale)
    live = asyncio.create_task(_collect_one(user))
    await asyncio.sleep(0)
    event = {"type": event_type, "session_id": "session-test"}
    try:
        if threaded:
            await asyncio.to_thread(user_session_stream.publish, user, event)
        else:
            user_session_stream.publish(user, event)
        assert stale not in user_session_stream._subscribers.get(user, set())
        assert await asyncio.wait_for(live, timeout=2.0) == event
        assert stale_queue.empty()
    finally:
        live.cancel()
        await asyncio.gather(live, return_exceptions=True)
    assert user not in user_session_stream._subscribers


async def test_publish_removes_last_closed_subscriber_only_in_current_workspace() -> None:
    user = "alice@example.com"
    loop = asyncio.new_event_loop()
    loop.close()
    stale = (asyncio.Queue(), loop)
    for workspace_id in (1, 2):
        with workspace_scope(workspace_id):
            user_session_stream._subscribers.setdefault(user, set()).add(stale)
    with workspace_scope(1):
        user_session_stream.publish(user, {"type": "hosts_changed"})
        assert user not in user_session_stream._subscribers
        user_session_stream.publish(user, {"type": "hosts_changed"})
    with workspace_scope(2):
        assert stale in user_session_stream._subscribers[user]
        user_session_stream.publish(user, {"type": "hosts_changed"})
        assert user not in user_session_stream._subscribers

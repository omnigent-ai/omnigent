"""Tests for the runner-launch single-flight's binding read.

Conversation reads may be served from a cache that lags a binding written a
moment ago, so the single-flight decides from ``get_runner_ids``.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.entities import Conversation
from omnigent.server.routes._sessions import helpers
from omnigent.server.routes._sessions.helpers import (
    _HostLaunchAttempt,
    _launch_runner_on_host_impl,
)

_SESSION_ID = "conv_cached_binding"


def _conversation(runner_id: str | None) -> Conversation:
    return Conversation(
        id=_SESSION_ID,
        created_at=0,
        updated_at=0,
        root_conversation_id=_SESSION_ID,
        host_id="host_1",
        runner_id=runner_id,
    )


class _CachedReadStore:
    """Serves conversation rows from a stale cache and bindings from the database."""

    def __init__(self, *, cached_runner_id: str | None, bound_runner_id: str | None) -> None:
        self.cached_runner_id = cached_runner_id
        self.bound_runner_id = bound_runner_id

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return _conversation(self.cached_runner_id)

    def get_runner_ids(self, conversation_ids: list[str]) -> dict[str, str | None]:
        return dict.fromkeys(conversation_ids, self.bound_runner_id)


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[Conversation]:
    """Record the rows handed to the real host launch instead of spawning."""
    calls: list[Conversation] = []

    async def _record(conv: Conversation, *_args: Any) -> _HostLaunchAttempt:
        calls.append(conv)
        return _HostLaunchAttempt(runner_id="runner_new")

    monkeypatch.setattr(helpers, "_launch_runner_on_host_locked", _record)
    return calls


@pytest.mark.asyncio
async def test_rides_binding_the_cached_row_has_not_seen(launched: list[Conversation]) -> None:
    """A runner bound a moment ago is ridden even while the cached row shows none."""
    store = _CachedReadStore(cached_runner_id=None, bound_runner_id="runner_just_bound")

    attempt = await _launch_runner_on_host_impl(
        _conversation(None),
        store,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert attempt.runner_id == "runner_just_bound"
    assert launched == []


@pytest.mark.asyncio
async def test_relaunch_supersedes_the_authoritative_binding(
    launched: list[Conversation],
) -> None:
    """A real relaunch replaces the bound runner, not the cache's stale view of it."""
    store = _CachedReadStore(cached_runner_id=None, bound_runner_id="runner_dead")

    attempt = await _launch_runner_on_host_impl(
        _conversation("runner_dead"),
        store,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert attempt.runner_id == "runner_new"
    assert [conv.runner_id for conv in launched] == ["runner_dead"]

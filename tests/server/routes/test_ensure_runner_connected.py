"""Unit tests for ``ensure_runner_connected`` — the out-of-band runner-wake
ladder shared by resource routes (e.g. shell create).

The full message-dispatch relaunch is exercised by the integration suites; here
we pin the branch logic in isolation by patching the primitives the helper
composes, so the three outcomes (already-connected fast path, wakeable →
relaunch, non-wakeable → ``None``) can't silently regress.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities.conversation import Conversation
from omnigent.server.routes._sessions import orchestration


def _conv(**kwargs: Any) -> Conversation:
    base = {
        "id": "conv_1",
        "created_at": 1,
        "updated_at": 1,
        "root_conversation_id": "conv_1",
        "agent_id": "agent_1",
    }
    base.update(kwargs)
    return Conversation(**base)


class _Store:
    """Conversation store stub whose ``get_conversation`` returns a fixed row."""

    def __init__(self, conv: Conversation) -> None:
        self._conv = conv

    def get_conversation(self, conversation_id: str) -> Conversation:
        del conversation_id
        return self._conv


@pytest.mark.asyncio
async def test_returns_existing_client_without_waking(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live runner short-circuits — no wake/relaunch primitive is touched."""
    sentinel = object()
    monkeypatch.setattr(
        orchestration,
        "_get_runner_client",
        _async_return(sentinel),
    )

    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("wake/relaunch must not run when a runner is live")

    monkeypatch.setattr(orchestration, "_maybe_wake_stale_resumable_managed_sandbox", _boom)
    monkeypatch.setattr(orchestration, "_launch_runner_on_host", _boom)

    conv = _conv(runner_id="runner_1")
    client, out_conv = await orchestration.ensure_runner_connected(
        session_id="conv_1",
        conv=conv,
        app_state=SimpleNamespace(),
        conversation_store=_Store(conv),
        runner_router=None,
    )

    assert client is sentinel
    assert out_conv is conv


@pytest.mark.asyncio
async def test_launches_runner_on_live_host_then_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host-bound, host online, runner dead → launch on host, then connect."""
    # No client on the first resolve; the connect wait then returns one.
    monkeypatch.setattr(orchestration, "_get_runner_client", _async_return(None))
    monkeypatch.setattr(
        orchestration,
        "_maybe_wake_stale_resumable_managed_sandbox",
        _async_return(False),
    )
    launch_attempt = SimpleNamespace(runner_id="runner_new", error_code=None, error=None)
    launched: dict[str, Any] = {}

    async def _launch(conv: Conversation, *_a: Any, **_k: Any) -> Any:
        launched["called"] = True
        return launch_attempt

    monkeypatch.setattr(orchestration, "_launch_runner_on_host", _launch)
    # Pinned runner_old is truly gone: the connect grace yields nothing, so
    # the helper relaunches rather than reusing it.
    monkeypatch.setattr(orchestration, "_wait_for_host_bound_runner_client", _async_return(None))
    connected = object()
    waited: dict[str, Any] = {}

    async def _wait(*_a: Any, runner_id: str | None = None, **_k: Any) -> Any:
        waited["runner_id"] = runner_id
        return connected

    monkeypatch.setattr(orchestration, "_wait_for_runner_client", _wait)

    conv = _conv(host_id="host_1", runner_id="runner_old", workspace="/w")
    host_registry = SimpleNamespace(get=lambda _hid: object())
    app_state = SimpleNamespace(host_registry=host_registry, tunnel_registry=None)

    client, _ = await orchestration.ensure_runner_connected(
        session_id="conv_1",
        conv=conv,
        app_state=app_state,
        conversation_store=_Store(conv),
        runner_router=None,
    )

    assert launched.get("called") is True
    # The connect wait must target the freshly-launched runner id.
    assert waited["runner_id"] == "runner_new"
    assert client is connected


@pytest.mark.asyncio
async def test_reuses_booting_runner_within_connect_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host online + pinned runner still booting → wait the grace, don't relaunch.

    Mirrors post_event's connect grace: a session whose runner_id is set but
    whose tunnel hasn't registered yet must be given the grace to connect,
    rather than eagerly spawning a second runner and orphaning the booting one.
    """
    monkeypatch.setattr(orchestration, "_get_runner_client", _async_return(None))
    monkeypatch.setattr(
        orchestration,
        "_maybe_wake_stale_resumable_managed_sandbox",
        _async_return(False),
    )
    # The booting runner connects within the grace window.
    connected = object()
    monkeypatch.setattr(
        orchestration, "_wait_for_host_bound_runner_client", _async_return(connected)
    )

    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("must not relaunch while the pinned runner is still booting")

    monkeypatch.setattr(orchestration, "_launch_runner_on_host", _boom)
    monkeypatch.setattr(orchestration, "_wait_for_runner_client", _boom)

    conv = _conv(host_id="host_1", runner_id="runner_booting", workspace="/w")
    host_registry = SimpleNamespace(get=lambda _hid: object())
    app_state = SimpleNamespace(host_registry=host_registry, tunnel_registry=None)

    client, _ = await orchestration.ensure_runner_connected(
        session_id="conv_1",
        conv=conv,
        app_state=app_state,
        conversation_store=_Store(conv),
        runner_router=None,
    )

    assert client is connected


@pytest.mark.asyncio
async def test_non_wakeable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not host-bound, runner dead → nothing to wake; returns ``None``."""
    monkeypatch.setattr(orchestration, "_get_runner_client", _async_return(None))

    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("no host to launch/relaunch on for a stranded session")

    monkeypatch.setattr(orchestration, "_launch_runner_on_host", _boom)

    conv = _conv(host_id=None, runner_id="runner_dead")
    app_state = SimpleNamespace(host_registry=None, tunnel_registry=None)

    client, out_conv = await orchestration.ensure_runner_connected(
        session_id="conv_1",
        conv=conv,
        app_state=app_state,
        conversation_store=_Store(conv),
        runner_router=None,
    )

    assert client is None
    assert out_conv is conv


def _async_return(value: Any):
    """Build an async callable that ignores its args and returns ``value``."""

    async def _fn(*_a: Any, **_k: Any) -> Any:
        return value

    return _fn

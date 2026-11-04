"""Runner failures settle only after fresh profile and conversation affinity rollback."""

from __future__ import annotations

import asyncio
import threading
import uuid
from unittest.mock import AsyncMock

import pytest

from omnigent.entities import Conversation
from omnigent.host.frames import HostHelloFrame
from omnigent.server.managed_hosts import ManagedHostLaunch
from omnigent.server.routes import sessions as sessions_routes
from omnigent.server.routes._sessions.orchestration import _bind_and_launch_managed_runner
from tests.server.integration._sandbox_profile_support import (
    _OWNER,
    _Env,
)
from tests.server.integration._sandbox_profile_support import (
    env as env,
)


@pytest.mark.parametrize("failure", ["profile", "runner", "tunnel"])
@pytest.mark.parametrize("relaunch", [False, True])
async def test_failed_runner_preserves_only_persistent_host_affinity(
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    relaunch: bool,
) -> None:
    session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    source = env.conversations.get_conversation(session_id)
    assert source is not None and source.host_id is not None and source.workspace is not None
    host_id = source.host_id
    host = env.hosts.get_host(host_id)
    assert host is not None
    snapshot = env.catalog.get_session_snapshot(session_id)
    registry = env.app.state.profile_test_hosts
    registry.register(
        host_id=host_id,
        ws=AsyncMock(),
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="profile-host"),
        owner=_OWNER,
    )
    tracker = env.app.state.managed_launches
    tracker.begin(session_id)
    bind_host = env.conversations.bind_profiled_host
    clear_host = env.conversations.clear_host_binding
    clear_started, release_clear = threading.Event(), threading.Event()

    def failed_bind(*args, **kwargs) -> None:
        bind_host(*args, **kwargs)
        raise RuntimeError("Profile bind committed before storage failed")

    def delayed_clear(*args, **kwargs):
        clear_started.set()
        assert release_clear.wait(timeout=10)
        return clear_host(*args, **kwargs)

    async def launch_runner(conv: Conversation, *_args: object) -> object:
        runner_id = uuid.uuid4().hex
        env.conversations.replace_runner_id(conv.id, runner_id)
        return sessions_routes._HostLaunchAttempt(
            runner_id=runner_id,
            error_code="harness_not_configured" if failure == "runner" else None,
            error="harness is unavailable" if failure == "runner" else None,
        )

    if failure == "profile":
        monkeypatch.setattr(env.conversations, "bind_profiled_host", failed_bind)
    monkeypatch.setattr(env.conversations, "clear_host_binding", delayed_clear)
    monkeypatch.setattr(sessions_routes, "_launch_runner_on_host", launch_runner)
    monkeypatch.setattr(sessions_routes, "_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S", 0.01)
    task = asyncio.create_task(
        _bind_and_launch_managed_runner(
            session_id=session_id,
            managed=ManagedHostLaunch(host_id=host_id, workspace=source.workspace),
            sandbox_config=env.app.state.sandbox_config,
            tracker=tracker,
            conversation_store=env.conversations,
            host_store=env.hosts,
            host_registry=registry,
            tunnel_registry=env.app.state.profile_test_tunnels,
            relaunch_host=host if relaunch else None,
            sandbox_profiles=env.catalog,
        )
    )
    launch = tracker.get(session_id)
    assert launch is not None
    if relaunch:
        await asyncio.wait_for(task, timeout=10)
        assert not clear_started.is_set()
    else:
        try:
            assert await asyncio.to_thread(clear_started.wait, 10)
            assert not launch.settled.is_set()
            assert env.hosts.get_host(host_id) is not None
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release_clear.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)

    assert launch.settled.is_set() and launch.error is not None
    assert env.catalog.get_session_snapshot(session_id) == snapshot
    session = env.conversations.get_conversation(session_id)
    assert session is not None
    if relaunch:
        assert session.host_id == host_id
        assert env.catalog._store.get_session_host_id(session_id) == host_id
        assert env.hosts.get_host(host_id) is not None
        assert env.recording.terminations == []
    else:
        assert session.host_id is None and session.workspace is None
        assert session.runner_id is None
        assert env.catalog._store.get_session_host_id(session_id) is None
        assert env.catalog.get_host_snapshot(host_id) is None
        assert env.hosts.get_host(host_id) is None
        assert env.recording.terminations == ["profile-sandbox-1"]

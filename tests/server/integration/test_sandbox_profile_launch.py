"""Accepted sandbox profiles reach real create, fork, wake, and replacement flows."""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient, Response

from omnigent.host.frames import HostHelloFrame
from omnigent.onboarding.sandboxes.profiles import (
    SandboxProfileAccessDenied,
    SandboxProfileContext,
)
from omnigent.server.routes import sessions as sessions_routes
from omnigent.server.routes._sessions.orchestration import _run_managed_launch
from omnigent.server.routes._workspace_validation import WorkspaceValidationError
from omnigent.server.sandbox_profiles import SandboxProfileCatalog
from omnigent.server.scheduled.fire import (
    _STAGED_LAUNCH_ERRORS,
    FireDeps,
    _make_managed_sandbox_start,
)
from tests.server.helpers import (
    HostStartInvocation,
    register_test_runner,
)
from tests.server.integration._sandbox_profile_support import (
    _OTHER_USER,
    _OWNER,
    _PROVIDER_CONFIG,
    _PROVIDER_PROFILE,
    _Env,
    _Launcher,
    _ProviderLauncher,
)
from tests.server.integration._sandbox_profile_support import env as env

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("mode", ["profile", "ordinary", "flag-off"])
@pytest.mark.parametrize("launch_fails", [False, True])
async def test_daemon_clear_then_launch_recovers_offline_runner(
    env, monkeypatch, mode, launch_fails
):
    from omnigent.host.frames import HostLaunchRunnerFrame, decode_host_frame

    if mode == "profile":
        session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
        host_id = env.conversations.get_conversation(session_id).host_id
    else:
        if mode == "flag-off":
            env.app.state.sandbox_profiles = None
        host_id = uuid.uuid4().hex
        env.hosts.upsert_on_connect(host_id, "ordinary-host", _OWNER)
        response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
        env.conversations.set_host_id(session_id, host_id, "/workspace")
        monkeypatch.setattr(
            env.conversations,
            "bind_profiled_host",
            Mock(side_effect=AssertionError("Ordinary recovery must use the legacy binding")),
        )
    env.conversations.replace_runner_id(session_id, "offline-runner")
    registry = env.app.state.profile_test_hosts
    registry.register(
        host_id=host_id,
        ws=AsyncMock(),
        owner=_OWNER,
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="host"),
    )

    async def valid_workspace(**kwargs):
        return kwargs["workspace"]

    frames = []

    def launch(conn, raw):
        frame = decode_host_frame(raw)
        assert isinstance(frame, HostLaunchRunnerFrame)
        frames.append(frame)
        conn.pending_launches[frame.request_id].set_result(
            {"status": "failed", "error": "spawn failed"}
            if launch_fails
            else {"status": "started"}
        )

    monkeypatch.setattr(
        "omnigent.server.routes._workspace_validation.validate_workspace", valid_workspace
    )
    monkeypatch.setattr(registry, "send_text", launch)
    cleared = await env.client.patch(f"/v1/sessions/{session_id}", json={"runner_id": ""})
    assert cleared.status_code == (400 if mode == "profile" else 200), cleared.text
    response = await env.client.post(
        f"/v1/hosts/{host_id}/runners",
        json={"session_id": session_id, "workspace": "/workspace"},
    )
    assert response.status_code == (502 if launch_fails else 200), response.text
    [frame] = frames
    assert frame.superseded_runner_ids == (("offline-runner",) if mode == "profile" else ())
    current = env.conversations.get_conversation(session_id)
    if launch_fails:
        assert current.runner_id == ("offline-runner" if mode == "profile" else None)
        assert current.host_id == (host_id if mode == "profile" else None)
    else:
        assert current.runner_id == response.json()["runner_id"] != "offline-runner"
        assert current.host_id == host_id
    assert env.app.state.sandbox_profiles is None or env.catalog.get_session_snapshot(
        session_id
    ) == (env.profile if mode == "profile" else None)


@pytest.mark.parametrize("race", ["local-live", "fresh-heartbeat", "new-binding"])
async def test_profile_runner_replacement_rejects_live_or_changed_binding(env, monkeypatch, race):
    from omnigent.db.utils import now_epoch

    session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    env.conversations.replace_runner_id(session_id, "old-runner")
    before = env.conversations.get_profile_host_binding(session_id)
    registry = env.app.state.profile_test_hosts
    registry.register(
        host_id=before.host_id,
        ws=AsyncMock(),
        owner=_OWNER,
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="host"),
    )
    if race == "local-live":
        env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
        register_test_runner(env.app, "old-runner", harnesses=["codex"], owner=_OWNER)
    else:
        bind = env.conversations.bind_profiled_host

        def raced_binding(*args, **kwargs):
            if kwargs.get("runner_id") is not None:
                if race == "fresh-heartbeat":
                    env.conversations.touch_runner_liveness(["old-runner"], now=now_epoch())
                else:
                    env.conversations.replace_runner_id(session_id, "concurrent-runner")
            return bind(*args, **kwargs)

        monkeypatch.setattr(env.conversations, "bind_profiled_host", raced_binding)

    async def valid_workspace(**kwargs):
        return kwargs["workspace"]

    monkeypatch.setattr(
        "omnigent.server.routes._workspace_validation.validate_workspace", valid_workspace
    )
    send = Mock(side_effect=AssertionError("No launch after stale/live binding rejection"))
    monkeypatch.setattr(registry, "send_text", send)
    response = await env.client.post(
        f"/v1/hosts/{before.host_id}/runners",
        json={"session_id": session_id, "workspace": "/workspace"},
    )
    assert response.status_code == 409, response.text
    expected = (
        dataclasses.replace(before, runner_id="concurrent-runner")
        if race == "new-binding"
        else before
    )
    assert env.conversations.get_profile_host_binding(session_id) == expected
    send.assert_not_called()


@pytest.mark.parametrize("conflicting_host", [False, True])
async def test_ordinary_runner_replacement_with_large_history_in_mixed_deployment(
    env, monkeypatch, conflicting_host
):
    from sqlalchemy import insert

    from omnigent.db.db_models import SqlConversationMetadata

    host_id = uuid.uuid4().hex
    env.hosts.upsert_on_connect(host_id, "ordinary-host", _OWNER)
    runner_id = "ordinary-runner"
    env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
    register_test_runner(env.app, runner_id, harnesses=["codex"], owner=_OWNER)
    monkeypatch.setattr(sessions_routes, "_get_runner_client", AsyncMock(return_value=None))
    rows = [
        {
            "id": f"{index:032x}",
            "host_id": host_id,
            "workspace": "/workspace",
            "runner_id": runner_id,
        }
        for index in range(1, 202)
    ]
    if conflicting_host:
        rows.append({**rows[0], "id": "f" * 32, "host_id": uuid.uuid4().hex})
    with env.conversations._session_immediate("seed_large_runner_history") as transaction:
        transaction.execute(insert(SqlConversationMetadata), rows)
    response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    env.recording.allowed_users.clear()
    response = await env.client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id})
    assert response.status_code == (400 if conflicting_host else 200), response.text
    if conflicting_host:
        assert "multiple hosts" in response.text
    assert env.conversations.get_conversation(session_id).runner_id == (
        None if conflicting_host else runner_id
    )
    assert env.catalog.get_session_snapshot(session_id) is None
    assert env.recording.selections == []


@pytest.mark.parametrize("env", ["controller"], indirect=True)
async def test_missing_sandbox_uses_bound_reference_with_large_host_history(env, monkeypatch):
    from sqlalchemy import insert

    from omnigent.db.db_models import SqlConversationMetadata

    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    host_id = env.conversations.get_conversation(source_id).host_id
    with env.conversations._session_immediate("seed_large_host_history") as transaction:
        transaction.execute(
            insert(SqlConversationMetadata),
            [
                {"id": uuid.uuid4().hex, "host_id": host_id, "workspace": "/workspace"}
                for _ in range(250)
            ],
        )
    monkeypatch.setattr(
        env.conversations,
        "list_host_conversation_ids",
        Mock(side_effect=AssertionError("Resume must use its known session reference")),
    )
    env.recording.gone = True
    await env.wake(source_id)
    launch = env.app.state.managed_launches.get(source_id)
    assert launch is None or launch.error is None
    assert env.catalog.get_session_snapshot(source_id) == env.profile
    assert len(env.recording.allocations) == 2


@pytest.mark.parametrize("transport", ["json", "multipart"])
async def test_explicit_managed_child_inherits_profile_and_parent_binding(
    env, monkeypatch, transport
):
    parent_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
    register_test_runner(env.app, "parent-runner", harnesses=["codex"], owner=_OWNER)
    env.conversations.replace_runner_id(parent_id, "parent-runner")
    monkeypatch.setattr(sessions_routes, "_get_runner_client", AsyncMock(return_value=None))
    response = await env.create(transport, parent_session_id=parent_id, host_type="managed")
    assert response.status_code == 201, response.text
    child = env.conversations.get_conversation(
        response.json().get("id", response.json().get("session_id"))
    )
    parent = env.conversations.get_conversation(parent_id)
    assert child.parent_conversation_id == parent_id
    assert child.runner_id == parent.runner_id
    from omnigent.runner.routing import routing_host_id

    assert routing_host_id(child, env.conversations) == parent.host_id
    assert env.catalog.get_session_snapshot(child.id) == env.profile
    assert len(env.recording.allocations) == 1


@pytest.mark.parametrize("wake", [False, True])
async def test_foreign_saved_identity_is_rejected_before_credentials_or_provider(
    env, monkeypatch, wake
):
    from omnigent.db.db_models import SqlConversationLabel, current_workspace_id
    from omnigent.server import sandbox_profiles

    session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    with env.conversations._conv_session_immediate("seed_legacy_foreign_identity") as transaction:
        row = transaction.get(
            SqlConversationLabel, (current_workspace_id(), session_id, "sandbox_profile:user_id")
        )
        row.value = _OTHER_USER
    credential_lookup = Mock(side_effect=AssertionError("Must reject before owner credentials"))
    monkeypatch.setattr(sandbox_profiles, "build_owner_launcher", credential_lookup)
    env.recording.selections.clear()
    if wake:
        await env.wake(session_id)
    else:
        env.app.state.managed_launches.begin(session_id)
        await _run_managed_launch(
            session_id=session_id,
            owner=_OWNER,
            sandbox_config=env.app.state.sandbox_config,
            repos=[],
            tracker=env.app.state.managed_launches,
            conversation_store=env.conversations,
            host_store=env.hosts,
            host_registry=None,
            tunnel_registry=None,
            sandbox_profiles=env.catalog,
        )
    assert "identity mismatch" in env.app.state.managed_launches.get(session_id).error
    credential_lookup.assert_not_called()
    assert env.recording.selections == []
    assert len(env.recording.allocations) == 1


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("legacy_key", ["sandbox_profile:team", "sandbox_profile:id"])
async def test_malformed_legacy_profile_labels_remain_readable(env, enabled, legacy_key):
    from omnigent.db.db_models import SqlConversationLabel

    response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    with env.conversations._conv_session_immediate("seed_legacy_profile_label") as transaction:
        transaction.add(
            SqlConversationLabel(
                conversation_id=session_id, key=legacy_key, value="build", updated_at=0
            )
        )
    if not enabled:
        env.app.state.sandbox_profiles = None
    response = await env.client.get(f"/v1/sessions/{session_id}")
    assert response.status_code == 200, response.text
    assert response.json()["sandbox_profile"] is None
    assert env.recording.selections == []
    if enabled:
        if legacy_key == "sandbox_profile:id":
            with pytest.raises(HTTPException, match="Incomplete saved sandbox profile"):
                env.catalog.get_session_snapshot(session_id)
        else:
            assert env.catalog.get_session_snapshot(session_id) is None


@pytest.mark.parametrize("transport", ["json", "multipart"])
@pytest.mark.parametrize("concurrent_rebind", [False, True])
async def test_caller_host_create_clears_runner_when_post_bind_profile_check_fails(
    env, monkeypatch, transport, concurrent_rebind
):
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    host_id = env.conversations.get_conversation(source_id).host_id
    registry = env.app.state.profile_test_hosts
    env.app.state.host_registry = registry
    registry.register(
        host_id=host_id,
        ws=AsyncMock(),
        owner=_OWNER,
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="profile-host"),
    )

    async def valid_workspace(**kwargs):
        return kwargs["workspace"]

    monkeypatch.setattr(
        "omnigent.server.routes._workspace_validation.validate_workspace", valid_workspace
    )
    require_host = env.catalog.require_session_host
    failed_sessions = []

    def reject_after_bind(session_id, host_id, **kwargs):
        if env.conversations.get_conversation(session_id).runner_id:
            failed_sessions.append(session_id)
            if concurrent_rebind:
                env.conversations.replace_runner_id(session_id, "replacement-runner")
            raise HTTPException(status_code=403, detail="Profile access revoked after runner bind")
        return require_host(session_id, host_id, **kwargs)

    monkeypatch.setattr(env.catalog, "require_session_host", reject_after_bind)
    response = await env.create(
        transport,
        host_type="external",
        sandbox_provider=None,
        host_id=host_id,
        workspace="/workspace",
    )
    assert len(failed_sessions) == 1, response.text
    assert env.conversations.get_conversation(failed_sessions[0]).runner_id == (
        "replacement-runner" if concurrent_rebind else None
    )
    assert len(env.recording.allocations) == 1


@pytest.mark.parametrize("transport", ["json", "multipart"])
async def test_profile_provider_with_flag_off_rejects_before_persistence(env, transport):
    env.app.state.sandbox_profiles = None
    before = env.persisted()
    response = await env.create(transport)
    assert response.status_code == 400, response.text
    assert "enable the sandbox_profiles feature" in response.text
    assert env.persisted() == before
    assert env.recording.allocations == []
    assert env.recording.selections == []


@pytest.mark.parametrize("transport", ["json", "multipart"])
@pytest.mark.parametrize("invalid", ["unknown", "stale", "disabled"])
async def test_rejected_selection_does_not_persist_or_allocate(
    env: _Env, transport: str, invalid: str
) -> None:
    profile_id = "unknown" if invalid == "unknown" else "build"
    revision = "stale-revision" if invalid == "stale" else env.profile["revision"]
    if invalid == "disabled":
        env.recording.allowed_users.clear()
    before = env.persisted()
    response = await env.create(
        transport, sandbox_profile_id=profile_id, sandbox_profile_revision=revision
    )
    assert response.status_code == (409 if invalid == "stale" else 403), response.text
    assert env.persisted() == before
    assert env.recording.allocations == []


@pytest.mark.parametrize("caller,outage", [(_OWNER, True), (_OTHER_USER, False)])
async def test_implicit_fork_can_launch_locally_without_provider_access(
    env, monkeypatch, caller, outage
):
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    env.client.headers["X-Forwarded-Email"] = caller
    env.recording.selections.clear()
    if outage:
        env.recording.selection_error = RuntimeError("private provider failure")
    else:
        assert caller not in env.recording.allowed_users
    response = await env.client.post(f"/v1/sessions/{source_id}/fork", json={})
    assert response.status_code == 201, response.text
    fork_id = response.json()["id"]
    assert env.catalog.get_session_snapshot(fork_id) is None

    host_id = uuid.uuid4().hex
    env.hosts.upsert_on_connect(host_id, "local-host", caller)
    registry = env.app.state.profile_test_hosts
    registry.register(
        host_id=host_id,
        ws=AsyncMock(),
        owner=caller,
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="local-host"),
    )

    async def valid_workspace(**kwargs):
        return kwargs["workspace"]

    def successful_launch(conn, frame):
        [future] = conn.pending_launches.values()
        future.set_result({"status": "started"})

    monkeypatch.setattr(
        "omnigent.server.routes._workspace_validation.validate_workspace", valid_workspace
    )
    monkeypatch.setattr(registry, "send_text", successful_launch)
    response = await env.client.post(
        f"/v1/hosts/{host_id}/runners",
        json={"session_id": fork_id, "workspace": "/workspace"},
    )
    assert response.status_code == 200, response.text
    fork = env.conversations.get_conversation(fork_id)
    assert fork.host_id == host_id and fork.runner_id is not None
    assert env.catalog.get_session_snapshot(fork_id) is None
    assert env.catalog.get_session_snapshot(source_id) == env.profile
    assert env.recording.selections == []
    assert len(env.recording.allocations) == 1


@pytest.mark.parametrize("accepted_profile", [False, True])
@pytest.mark.parametrize(
    "failure", ["workspace", "rejection", "send", "timeout", "database", "commit"]
)
async def test_failed_host_launch_restores_profile_binding(
    env: _Env, monkeypatch: pytest.MonkeyPatch, accepted_profile: bool, failure: str
) -> None:
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    source = env.conversations.get_conversation(source_id)
    assert source is not None and source.host_id is not None
    host_id = source.host_id
    env.app.state.profile_test_hosts.register(
        host_id=host_id,
        ws=AsyncMock(),
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="profile-host"),
        owner=_OWNER,
    )
    response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    if accepted_profile:
        env.catalog.bind_session(session_id, env.profile)
    before = env.catalog.get_session_snapshot(session_id)

    async def invalid_workspace(**kwargs):
        if failure == "workspace":
            raise WorkspaceValidationError("The selected directory is unavailable")
        return kwargs["workspace"]

    registry = env.app.state.profile_test_hosts

    def send_launch(conn, frame):
        if failure == "send":
            raise ConnectionError("replaced")
        if failure != "timeout":
            [future] = conn.pending_launches.values()
            future.set_result(
                {
                    "status": "failed",
                    "error_code": "harness_not_configured",
                    "error": "not configured",
                }
            )

    monkeypatch.setattr(registry, "send_text", send_launch)
    monkeypatch.setattr("omnigent.server.routes.hosts.resolve_launch_timeout_s", lambda: 0.01)

    monkeypatch.setattr(
        "omnigent.server.routes._workspace_validation.validate_workspace", invalid_workspace
    )
    bind = env.conversations.bind_profiled_host

    def committed_then_failed(*args, **kwargs):
        if failure == "commit":
            bind(*args, **kwargs)
        raise RuntimeError("Database call failed")

    if failure in ("database", "commit"):
        monkeypatch.setattr(env.conversations, "bind_profiled_host", committed_then_failed)
        with pytest.raises(RuntimeError, match="Database call failed"):
            await env.client.post(
                f"/v1/hosts/{host_id}/runners",
                json={"session_id": session_id, "workspace": "/missing"},
            )
        monkeypatch.setattr(env.conversations, "bind_profiled_host", bind)
    else:
        response = await env.client.post(
            f"/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": "/missing"},
        )
        assert (
            response.status_code
            == {"workspace": 400, "rejection": 412, "send": 409, "timeout": 504}[failure]
        ), response.text
    assert env.catalog.get_session_snapshot(session_id) == before
    assert env.catalog._store.get_session_host_id(session_id) is None
    assert env.catalog.get_host_snapshot(host_id) == env.profile
    session = env.conversations.get_conversation(session_id)
    assert session is not None and session.host_id is None
    assert session.runner_id is None

    replacement_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    replacement = env.conversations.get_conversation(replacement_id)
    assert replacement is not None and replacement.host_id is not None
    registry.register(
        host_id=replacement.host_id,
        ws=AsyncMock(),
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="retry"),
        owner=_OWNER,
    )

    async def valid_workspace(**kwargs):
        return kwargs["workspace"]

    def successful_launch(conn, frame):
        [future] = conn.pending_launches.values()
        future.set_result({"status": "started"})

    monkeypatch.setattr(
        "omnigent.server.routes._workspace_validation.validate_workspace", valid_workspace
    )
    monkeypatch.setattr(registry, "send_text", successful_launch)
    response = await env.client.post(
        f"/v1/hosts/{replacement.host_id}/runners",
        json={"session_id": session_id, "workspace": "/retry"},
    )
    assert response.status_code == 200, response.text
    assert env.catalog.get_session_snapshot(session_id) == env.profile
    assert env.catalog._store.get_session_host_id(session_id) == replacement.host_id


@pytest.mark.parametrize("transport", ["json", "multipart"])
async def test_scheduler_profile_database_failure_preserves_owned_session(
    env: _Env, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    attempted_sessions: list[str] = []
    announce = Mock()
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_core._announce_session_added", announce
    )

    def unavailable_database(session_id):
        attempted_sessions.append(session_id)
        raise RuntimeError("Profile storage is unavailable")

    monkeypatch.setattr(env.catalog, "get_session_snapshot", unavailable_database)
    before = env.persisted()
    async with AsyncClient(
        transport=ASGITransport(app=env.app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"X-Forwarded-Email": _OWNER},
    ) as client:
        previous_client = env.client
        env.client = client
        try:
            response = await env.create(transport, sandbox_profile_id="build")
        finally:
            env.client = previous_client
    assert response.status_code == 500, response.text
    assert len(attempted_sessions) == 1
    session_id = attempted_sessions[0]
    conversation = env.conversations.get_conversation(session_id)
    assert conversation is not None
    assert env.app.state.agent_store.get(conversation.agent_id) is not None
    assert env.app.state.managed_launches.get(session_id) is None
    announce.assert_called_once_with(_OWNER, session_id)
    assert env.persisted()[0] == sorted([*before[0], session_id])
    visible = await env.client.get(f"/v1/sessions/{session_id}")
    assert visible.status_code == 200, visible.text
    assert env.recording.allocations == []


async def test_saved_profile_database_failure_settles_initial_launch(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    env.catalog.bind_session(session_id, env.profile)

    def unavailable_database(session_id):
        raise RuntimeError("Profile storage is unavailable")

    monkeypatch.setattr(env.catalog, "get_session_snapshot", unavailable_database)
    env.app.state.managed_launches.begin(session_id)
    await _run_managed_launch(
        session_id=session_id,
        owner=_OWNER,
        sandbox_config=env.app.state.sandbox_config,
        repos=[],
        tracker=env.app.state.managed_launches,
        conversation_store=env.conversations,
        host_store=env.hosts,
        host_registry=None,
        tunnel_registry=None,
        sandbox_profiles=env.catalog,
    )
    launch = env.app.state.managed_launches.get(session_id)
    assert launch is not None
    assert launch.settled.is_set()
    assert launch.error == "failed to load sandbox profile"
    assert env.recording.allocations == []


@pytest.mark.parametrize("commit_before_error", [False, True])
async def test_profile_host_binding_failure_settles_and_removes_fresh_sandbox(
    env: _Env, monkeypatch: pytest.MonkeyPatch, commit_before_error: bool
) -> None:
    bind_host = env.conversations.bind_profiled_host
    terminate = _Launcher.terminate
    attempted_hosts: list[str] = []

    def unavailable_database(session_id: str, host_id: str, *args: Any, **kwargs: Any) -> None:
        attempted_hosts.append(host_id)
        if commit_before_error:
            bind_host(session_id, host_id, *args, **kwargs)
        raise RuntimeError("Profile storage is unavailable")

    def terminate_after_rollback(launcher: _Launcher, sandbox_id: str) -> None:
        session = env.conversations.get_conversation(session_id)
        assert session is not None and session.host_id is None
        assert env.catalog._store.get_session_host_id(session_id) is None
        terminate(launcher, sandbox_id)

    monkeypatch.setattr(env.conversations, "bind_profiled_host", unavailable_database)
    monkeypatch.setattr(_Launcher, "terminate", terminate_after_rollback)
    response = await env.create("json", sandbox_profile_id="build")
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    launch = env.app.state.managed_launches.get(session_id)
    assert launch is not None
    await asyncio.wait_for(launch.settled.wait(), timeout=10)
    assert launch.error == "failed to bind managed host"
    session = env.conversations.get_conversation(session_id)
    assert session is not None and session.host_id is None
    assert session.runner_id is None and session.workspace is None
    assert env.catalog.get_session_snapshot(session_id) == env.profile
    assert env.catalog._store.get_session_host_id(session_id) is None
    [failed_host_id] = attempted_hosts
    assert env.hosts.get_host(failed_host_id) is None
    assert env.recording.terminations == ["profile-sandbox-1"]
    assert env.catalog.get_host_snapshot(failed_host_id) is None

    monkeypatch.setattr(env.conversations, "bind_profiled_host", bind_host)
    env.app.state.managed_launches.begin(session_id)
    await _run_managed_launch(
        session_id=session_id,
        owner=_OWNER,
        sandbox_config=env.app.state.sandbox_config,
        repos=[],
        tracker=env.app.state.managed_launches,
        conversation_store=env.conversations,
        host_store=env.hosts,
        host_registry=None,
        tunnel_registry=None,
        sandbox_profiles=env.catalog,
    )
    assert await env.settled(response) == session_id
    retried = env.conversations.get_conversation(session_id)
    assert retried is not None and retried.host_id != failed_host_id
    assert retried.host_id is not None and env.hosts.get_host(retried.host_id) is not None
    assert env.catalog._store.get_session_host_id(session_id) == retried.host_id
    assert env.catalog.get_session_snapshot(session_id) == env.profile
    assert env.recording.allocations == [_PROVIDER_CONFIG, _PROVIDER_CONFIG]


@pytest.mark.parametrize("commit_before_error", [False, True])
@pytest.mark.parametrize("relaunch", [False, True])
async def test_cancelled_profile_host_binding_drains_before_rollback(
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    commit_before_error: bool,
    relaunch: bool,
) -> None:
    relaunch_host = None
    if relaunch:
        session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
        source = env.conversations.get_conversation(session_id)
        assert source is not None and source.host_id is not None
        host_id = source.host_id
        relaunch_host = env.hosts.get_host(host_id)
        assert relaunch_host is not None
        env.hosts.set_offline(host_id)
    else:
        response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
        env.catalog.bind_session(session_id, env.profile)
        host_id = uuid.uuid4().hex
    snapshot = env.catalog.get_session_snapshot(session_id)
    bind_started = threading.Event()
    release_bind = threading.Event()
    original_bind = env.conversations.bind_profiled_host

    def delayed_bind(*args: Any, **kwargs: Any) -> Any:
        if len(env.recording.allocations) < (2 if relaunch else 1):
            return original_bind(*args, **kwargs)
        bind_started.set()
        assert release_bind.wait(timeout=10)
        result = original_bind(*args, **kwargs)
        if commit_before_error:
            raise RuntimeError("Binding committed before storage failed")
        return result

    monkeypatch.setattr(env.conversations, "bind_profiled_host", delayed_bind)
    env.app.state.managed_launches.begin(session_id)
    task = asyncio.create_task(
        _run_managed_launch(
            session_id=session_id,
            owner=_OWNER,
            sandbox_config=env.app.state.sandbox_config,
            repos=[],
            tracker=env.app.state.managed_launches,
            conversation_store=env.conversations,
            host_store=env.hosts,
            host_registry=None,
            tunnel_registry=None,
            relaunch_host=relaunch_host,
            host_id=host_id,
            sandbox_profiles=env.catalog,
        )
    )
    try:
        assert await asyncio.to_thread(bind_started.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert env.hosts.get_host(host_id) is not None
        assert env.recording.terminations == (["profile-sandbox-1"] if relaunch else [])
    finally:
        release_bind.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

    launch = env.app.state.managed_launches.get(session_id)
    assert launch is not None
    assert launch.settled.is_set() == commit_before_error
    assert launch.error == ("failed to bind managed host" if commit_before_error else None)
    if not commit_before_error:
        assert launch.bound.is_set()
    assert env.catalog.get_session_snapshot(session_id) == snapshot
    session = env.conversations.get_conversation(session_id)
    assert session is not None
    if relaunch or not commit_before_error:
        assert session.host_id == host_id
        assert env.catalog._store.get_session_host_id(session_id) == host_id
        host = env.hosts.get_host(host_id)
        assert host is not None
        assert host.sandbox_id == ("profile-sandbox-2" if relaunch else "profile-sandbox-1")
        assert env.recording.terminations == (["profile-sandbox-1"] if relaunch else [])
    else:
        assert session.host_id is None and session.workspace is None
        assert session.runner_id is None
        assert env.catalog._store.get_session_host_id(session_id) is None
        assert env.hosts.get_host(host_id) is None
        assert env.recording.terminations == ["profile-sandbox-1"]


@pytest.mark.parametrize("relaunch", [False, True])
async def test_profile_disabled_during_provisioning_settles_shared_admission_failure(
    env: _Env, monkeypatch: pytest.MonkeyPatch, relaunch: bool
) -> None:
    relaunch_host = None
    if relaunch:
        session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
        source = env.conversations.get_conversation(session_id)
        assert source is not None and source.host_id is not None
        relaunch_host = env.hosts.get_host(source.host_id)
        assert relaunch_host is not None
        env.hosts.set_offline(source.host_id)
    else:
        response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
        env.catalog.bind_session(session_id, env.profile)

    registry = env.app.state.profile_test_hosts
    register = _Launcher._register
    registered_hosts: list[str] = []
    selections_before_revoke: list[int] = []

    def register_then_disable(launcher: _Launcher, invocation: HostStartInvocation) -> None:
        register(launcher, invocation)
        registered_hosts.append(invocation.host_id)
        registry.register(
            host_id=invocation.host_id,
            ws=AsyncMock(),
            hello=HostHelloFrame(
                version="0.1.0-test", frame_protocol_version=1, name=invocation.host_name
            ),
            owner=_OWNER,
            registered_with_managed_token=True,
        )
        selections_before_revoke.append(len(env.recording.selections))
        env.recording.allowed_users.clear()

    monkeypatch.setattr(_Launcher, "_register", register_then_disable)
    env.app.state.managed_launches.begin(session_id)
    await _run_managed_launch(
        session_id=session_id,
        owner=_OWNER,
        sandbox_config=env.app.state.sandbox_config,
        repos=[],
        tracker=env.app.state.managed_launches,
        conversation_store=env.conversations,
        host_store=env.hosts,
        host_registry=registry,
        tunnel_registry=None,
        relaunch_host=relaunch_host,
        sandbox_profiles=env.catalog,
    )
    launch = env.app.state.managed_launches.get(session_id)
    assert launch is not None and launch.settled.is_set()
    assert launch.error == "Sandbox profile access denied"
    assert len(env.recording.selections) == selections_before_revoke[0] + 1
    session = env.conversations.get_conversation(session_id)
    assert session is not None
    assert session.runner_id is None
    assert env.recording.allocations == [_PROVIDER_CONFIG] * (2 if relaunch else 1)
    assert env.recording.terminations == ["profile-sandbox-1"]
    assert len(registered_hosts) == 1
    host = env.hosts.get_host(registered_hosts[0])
    if relaunch:
        assert host is not None and relaunch_host is not None
        assert host.host_id == relaunch_host.host_id
        assert session.host_id == host.host_id
        assert host.sandbox_id == "profile-sandbox-2"
    else:
        assert host is None
        assert session.host_id is None


async def test_profiled_side_chat_can_bind_only_its_source_runner(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
    register_test_runner(env.app, "source-runner", harnesses=["codex"], owner=_OWNER)
    register_test_runner(env.app, "other-runner", harnesses=["codex"], owner=_OWNER)
    env.conversations.replace_runner_id(source_id, "source-runner")

    async def no_runner_notification(*args, **kwargs):
        return None

    monkeypatch.setattr(sessions_routes, "_get_runner_client", no_runner_notification)
    response = await env.client.post(f"/v1/sessions/{source_id}/fork", json={"side_chat": True})
    assert response.status_code == 201, response.text
    side_id = response.json()["id"]
    assert env.catalog.get_session_snapshot(side_id) == env.profile
    response = await env.client.patch(
        f"/v1/sessions/{side_id}", json={"runner_id": "source-runner"}
    )
    assert response.status_code == 200, response.text
    side = env.conversations.get_conversation(side_id)
    assert side is not None
    assert side.runner_id == "source-runner" and side.host_id is None
    response = await env.client.patch(
        f"/v1/sessions/{side_id}", json={"runner_id": "other-runner"}
    )
    assert response.status_code == 400, response.text
    side = env.conversations.get_conversation(side_id)
    assert side is not None and side.runner_id == "source-runner"
    assert len(env.recording.allocations) == 1


@pytest.mark.parametrize("transport", ["json", "multipart"])
async def test_profile_acceptance_failure_rolls_back_session_and_preserves_reusable_upload(
    env: _Env, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    def profile_disabled_during_create(session_id, snapshot):
        raise HTTPException(status_code=409, detail="The profile was disabled during creation")

    monkeypatch.setattr(env.catalog, "bind_session", profile_disabled_during_create)
    before = env.persisted()
    response = await env.create(transport, sandbox_profile_id="build")
    assert response.status_code == 409, response.text
    after = env.persisted()
    assert after[0] == before[0]
    if transport == "json":
        assert after[1] == before[1]
    else:
        new_bundles = set(after[1]) - set(before[1])
        assert len(new_bundles) == 1
        uploaded_agent_id = next(iter(new_bundles)).split("/")[0]
        uploaded_agent = env.app.state.agent_store.get(uploaded_agent_id)
        assert uploaded_agent is not None
        assert uploaded_agent.bundle_location in new_bundles
    assert env.recording.allocations == []


class TestProviderManagedProfiles:
    async def test_legacy_unprofiled_managed_runner_cannot_hide_or_reuse_its_host(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
        for runner_id in ("legacy-runner", "ordinary-runner"):
            register_test_runner(env.app, runner_id, harnesses=["codex"], owner=_OWNER)
        monkeypatch.setattr(sessions_routes, "_get_runner_client", AsyncMock(return_value=None))
        response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
        assert response.status_code == 201, response.text
        source_id = response.json()["id"]
        host_id = uuid.uuid4().hex
        env.hosts.register_managed_host(
            host_id=host_id,
            name="legacy-managed-host",
            user_id=_OWNER,
            token="legacy-launch-token",
            provider=env.provider,
            sandbox_id="legacy-sandbox",
            token_expires_at=2_000_000_000,
        )
        env.conversations.set_host_id(source_id, host_id, "/workspace")
        env.conversations.replace_runner_id(source_id, "legacy-runner")
        response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
        assert response.status_code == 201, response.text
        target_id = response.json()["id"]
        response = await env.client.patch(
            f"/v1/sessions/{target_id}", json={"runner_id": "legacy-runner"}
        )
        assert response.status_code == 422, response.text
        assert env.conversations.get_conversation(target_id).runner_id is None
        for runner_id in ("", "ordinary-runner"):
            response = await env.client.patch(
                f"/v1/sessions/{source_id}", json={"runner_id": runner_id}
            )
            assert response.status_code == 400, response.text
            assert env.conversations.get_conversation(source_id).runner_id == "legacy-runner"
        assert env.recording.allocations == []
        assert env.recording.selections == []

    async def test_scheduled_launch_uses_catalog_and_requires_selection(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
        conversation = env.conversations.get_conversation(session_id)
        assert conversation is not None
        require_selection = Mock(wraps=env.catalog.require_selection)
        monkeypatch.setattr(env.catalog, "require_selection", require_selection)
        failed = asyncio.Event()
        loop = asyncio.get_running_loop()
        schedules = Mock()
        schedules.get_running_run_by_conversation.return_value = SimpleNamespace(id="profile-run")
        schedules.update_run.side_effect = lambda *a, **kw: loop.call_soon_threadsafe(failed.set)
        deps = FireDeps(
            scheduled_task_store=schedules,
            agent_store=env.app.state.agent_store,
            conversation_store=env.conversations,
            permission_store=None,
            host_store=env.hosts,
            host_registry=None,
            sandbox_config=env.app.state.sandbox_config,
            managed_launches=env.app.state.managed_launches,
            app_state=env.app.state,
        )
        _make_managed_sandbox_start(deps)(
            conversation,
            SimpleNamespace(user_id=_OWNER, agent_id=env.agent_id),
            uuid.uuid4().hex,
            _STAGED_LAUNCH_ERRORS,
        )
        await asyncio.wait_for(failed.wait(), timeout=10)
        assert schedules.update_run.call_args.kwargs["status"] == "failed"
        assert schedules.update_run.call_args.kwargs["error_code"] == "sandbox_create_failed"
        assert "Choose a sandbox profile" in schedules.update_run.call_args.kwargs["error"]
        require_selection.assert_called_once_with(None, user_id=_OWNER)
        launch = env.app.state.managed_launches.get(session_id)
        assert launch is not None and launch.settled.is_set() and launch.error
        assert env.recording.allocations == []
        assert env.recording.selections == []
        assert env.conversations.get_conversation(session_id).host_id is None

    @pytest.mark.parametrize("operation", ["clear", "fork"])
    @pytest.mark.parametrize("revoked", [False, True])
    async def test_patch_rotates_managed_runner_only_after_profile_authorization(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch, operation: str, revoked: bool
    ) -> None:
        source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
        source = env.conversations.get_conversation(source_id)
        assert source is not None
        env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
        register_test_runner(env.app, "profile-runner", harnesses=["codex"], owner=_OWNER)
        env.conversations.replace_runner_id(source_id, "profile-runner")
        notify = AsyncMock(return_value=None)
        monkeypatch.setattr(sessions_routes, "_get_runner_client", notify)
        # Existing native clients create, bind the replacement, then detach the source.
        response = await env.client.post(
            f"/v1/sessions/{source_id}/fork" if operation == "fork" else "/v1/sessions",
            json={} if operation == "fork" else {"agent_id": env.agent_id},
        )
        assert response.status_code == 201, response.text
        target_id = response.json()["id"]
        target_profile = env.catalog.get_session_snapshot(target_id)
        assert target_profile is None
        if revoked:
            env.recording.allowed_users.clear()
        notify.reset_mock()
        monkeypatch.setattr(
            env.conversations,
            "list_conversations_by_runner_id",
            Mock(side_effect=AssertionError("Unbounded runner lookup")),
        )
        response = await env.client.patch(f"/v1/sessions/{source_id}", json={"runner_id": ""})
        assert response.status_code == 400, response.text
        assert env.conversations.get_conversation(source_id).runner_id == "profile-runner"
        response = await env.client.patch(
            f"/v1/sessions/{target_id}", json={"runner_id": "profile-runner"}
        )
        assert response.status_code == (403 if revoked else 200), response.text
        assert "private" not in response.text
        target = env.conversations.get_conversation(target_id)
        assert target is not None
        if revoked:
            assert target.runner_id is None
            assert env.catalog.get_session_snapshot(target_id) == target_profile
            notify.assert_not_awaited()
        else:
            assert target.runner_id == "profile-runner"
            assert (target.host_id, target.workspace, target.git_branch) == (
                source.host_id,
                source.workspace,
                source.git_branch,
            )
            assert env.catalog.get_session_snapshot(target_id) == env.catalog.get_session_snapshot(
                source_id
            )
            response = await env.client.patch(f"/v1/sessions/{source_id}", json={"runner_id": ""})
            assert response.status_code == 200, response.text
            detached = env.conversations.get_conversation(source_id)
            assert detached is not None and detached.runner_id is None
            assert detached.host_id == source.host_id
            assert env.conversations.get_runner_host_ids("profile-runner") == [source.host_id]
            response = await env.client.patch(f"/v1/sessions/{target_id}", json={"runner_id": ""})
            assert response.status_code == 400, response.text
        assert env.recording.allocations == [_PROVIDER_CONFIG]

    @pytest.mark.parametrize("conflict", ["owner", "profile", "ambiguous"])
    async def test_patch_rotation_rejects_untrusted_host_or_profile(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch, conflict: str
    ) -> None:
        source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
        source = env.conversations.get_conversation(source_id)
        assert source is not None and source.host_id
        env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
        register_test_runner(env.app, "profile-runner", harnesses=["codex"], owner=_OWNER)
        env.conversations.replace_runner_id(source_id, "profile-runner")
        monkeypatch.setattr(sessions_routes, "_get_runner_client", AsyncMock(return_value=None))
        response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
        assert response.status_code == 201, response.text
        target_id = response.json()["id"]
        if conflict == "owner":
            get_host = env.hosts.get_host

            def other_owner(host_id):
                host = get_host(host_id)
                return dataclasses.replace(host, user_id="another@example.test") if host else None

            monkeypatch.setattr(env.hosts, "get_host", other_owner)
        elif conflict == "profile":
            snapshot = env.catalog.get_session_snapshot(source_id)
            assert snapshot is not None
            env.catalog._store.bind_session(target_id, {**snapshot, "revision": "other-revision"})
        else:
            other = env.conversations.create_conversation(runner_id="profile-runner")
            env.conversations.set_host_id(other.id, uuid.uuid4().hex, "/different")
        before = env.catalog.get_session_snapshot(target_id)
        response = await env.client.patch(
            f"/v1/sessions/{target_id}", json={"runner_id": "profile-runner"}
        )
        assert (
            response.status_code == {"owner": 403, "profile": 409, "ambiguous": 400}[conflict]
        ), response.text
        target = env.conversations.get_conversation(target_id)
        assert target is not None and target.host_id is None and target.runner_id is None
        assert env.catalog.get_session_snapshot(target_id) == before
        assert env.recording.allocations == [_PROVIDER_CONFIG]

    async def test_patch_reauthorizes_side_chat_but_keeps_ordinary_runner_binding(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
        env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
        register_test_runner(env.app, "profile-runner", harnesses=["codex"], owner=_OWNER)
        register_test_runner(env.app, "ordinary-runner", harnesses=["codex"], owner=_OWNER)
        env.conversations.replace_runner_id(source_id, "profile-runner")
        monkeypatch.setattr(sessions_routes, "_get_runner_client", AsyncMock(return_value=None))
        response = await env.client.post(
            f"/v1/sessions/{source_id}/fork", json={"side_chat": True}
        )
        assert response.status_code == 201, response.text
        side_id = response.json()["id"]
        response = await env.client.patch(
            f"/v1/sessions/{side_id}", json={"runner_id": "profile-runner"}
        )
        assert response.status_code == 200, response.text
        assert env.conversations.get_conversation(side_id).runner_id == "profile-runner"
        assert (
            await env.client.patch(f"/v1/sessions/{side_id}", json={"runner_id": ""})
        ).status_code == 200
        env.recording.allowed_users.clear()
        response = await env.client.patch(
            f"/v1/sessions/{side_id}", json={"runner_id": "profile-runner"}
        )
        assert response.status_code == 403, response.text
        assert env.conversations.get_conversation(side_id).runner_id is None
        response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
        assert response.status_code == 201, response.text
        ordinary_id = response.json()["id"]
        for runner_id in ("ordinary-runner", ""):
            response = await env.client.patch(
                f"/v1/sessions/{ordinary_id}", json={"runner_id": runner_id}
            )
            assert response.status_code == 200, response.text
            assert env.conversations.get_conversation(ordinary_id).runner_id == (runner_id or None)
        assert env.recording.allocations == [_PROVIDER_CONFIG]

    @pytest.mark.parametrize("transport", ["json", "multipart"])
    async def test_create_selects_provider_settings_and_saves_only_reference(
        self, env: _Env, transport: str
    ) -> None:
        session_id = await env.settled(
            await env.create(
                transport,
                sandbox_profile_id="build",
                sandbox_profile_revision=_PROVIDER_PROFILE.revision,
            )
        )
        assert env.recording.allocations == [_PROVIDER_CONFIG]
        snapshot = env.catalog.get_session_snapshot(session_id)
        assert snapshot == {**env.profile, "user_id": _OWNER, "workspace_id": 0}
        assert "config" not in snapshot
        assert env.recording.selections
        assert all(
            selection == (SandboxProfileContext(user_id=_OWNER, workspace_id=0), _PROVIDER_PROFILE)
            for selection in env.recording.selections
        )

    @pytest.mark.parametrize("transport", ["json", "multipart"])
    @pytest.mark.parametrize("empty_catalog", [False, True])
    async def test_missing_selection_fails_before_persistence_or_allocation(
        self, env: _Env, transport: str, empty_catalog: bool
    ) -> None:
        if empty_catalog:
            env.recording.allowed_users.clear()
        before = env.persisted()
        for provider in (env.provider, None):
            response = await env.create(transport, sandbox_provider=provider)
            assert response.status_code == 422, response.text
            assert env.persisted() == before
            assert env.recording.allocations == []
            assert env.recording.selections == []

    @pytest.mark.parametrize("transport", ["json", "multipart"])
    async def test_provider_can_deny_a_listed_profile_at_create(
        self, env: _Env, transport: str
    ) -> None:
        env.recording.selection_error = SandboxProfileAccessDenied("private directory result")
        before = env.persisted()
        response = await env.create(transport, sandbox_profile_id="build")
        assert response.status_code == 403, response.text
        assert "private directory" not in response.text
        assert env.persisted() == before
        assert env.recording.allocations == []
        assert env.recording.selections

    async def test_fork_reauthorizes_and_records_the_new_caller(self, env: _Env) -> None:
        source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
        source_snapshot = env.catalog.get_session_snapshot(source_id)
        env.client.headers["X-Forwarded-Email"] = _OTHER_USER
        before = env.persisted()
        response = await env.client.post(
            f"/v1/sessions/{source_id}/fork",
            json={
                "host_type": "managed",
                "sandbox_profile_id": "build",
                "sandbox_profile_revision": _PROVIDER_PROFILE.revision,
            },
        )
        assert response.status_code == 403, response.text
        assert env.persisted() == before
        assert len(env.recording.allocations) == 1
        env.recording.allowed_users.add(_OTHER_USER)
        fork_id = await env.settled(
            await env.client.post(
                f"/v1/sessions/{source_id}/fork",
                json={
                    "host_type": "managed",
                    "sandbox_profile_id": "build",
                    "sandbox_profile_revision": _PROVIDER_PROFILE.revision,
                },
            )
        )
        assert env.catalog.get_session_snapshot(fork_id) == {
            **env.profile,
            "user_id": _OTHER_USER,
            "workspace_id": 0,
        }
        assert env.catalog.get_session_snapshot(source_id) == source_snapshot
        assert env.recording.allocations == [_PROVIDER_CONFIG, _PROVIDER_CONFIG]

    @pytest.mark.parametrize("gone", [False, True], ids=["resume", "replace"])
    async def test_wake_reauthorizes_saved_identity_on_fresh_launchers(
        self, env: _Env, gone: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event_loop_thread = threading.get_ident()
        selection_threads = []
        select = _ProviderLauncher.select_profile

        def select_off_event_loop(launcher, context, profile):
            selection_threads.append(threading.get_ident())
            select(launcher, context, profile)

        monkeypatch.setattr(_ProviderLauncher, "select_profile", select_off_event_loop)
        session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
        assert selection_threads and event_loop_thread not in selection_threads
        selection_threads.clear()
        before = env.conversations.get_conversation(session_id)
        assert before is not None and before.host_id is not None
        host = env.hosts.get_host(before.host_id)
        assert host is not None
        snapshot = env.catalog.get_session_snapshot(session_id)
        env.app.state.sandbox_profiles = SandboxProfileCatalog(
            env.conversations, env.app.state.sandbox_config, env.hosts
        )
        env.client.headers["X-Forwarded-Email"] = _OTHER_USER
        env.recording.selections.clear()
        env.recording.gone = gone
        await env.wake(session_id)
        assert selection_threads and event_loop_thread not in selection_threads
        assert env.app.state.managed_launches.get(session_id) is None
        assert env.recording.selections
        assert all(
            selection == (SandboxProfileContext(user_id=_OWNER, workspace_id=0), _PROVIDER_PROFILE)
            for selection in env.recording.selections
        )
        assert env.recording.resumes == [(host.sandbox_id, _PROVIDER_CONFIG)]
        assert env.recording.allocations == [_PROVIDER_CONFIG] * (2 if gone else 1)
        assert env.catalog.get_session_snapshot(session_id) == snapshot
        after = env.conversations.get_conversation(session_id)
        assert after is not None and after.host_id == before.host_id
        replacement = env.hosts.get_host(after.host_id)
        assert replacement is not None
        assert (replacement.sandbox_id != host.sandbox_id) is gone

    @pytest.mark.parametrize("failure", ["revoked", "revision-retired", "unavailable"])
    async def test_wake_denial_prevents_resume_and_allocation(
        self, env: _Env, failure: str
    ) -> None:
        session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
        snapshot = env.catalog.get_session_snapshot(session_id)
        if failure == "revoked":
            env.recording.allowed_users.clear()
        elif failure == "revision-retired":
            env.recording.provider_profile = _PROVIDER_PROFILE.model_copy(
                update={"revision": "replacement-revision"}
            )
        else:
            env.recording.selection_error = RuntimeError("private provider credential")
        await env.wake(session_id)
        launch = env.app.state.managed_launches.get(session_id)
        assert launch is not None and launch.settled.is_set()
        assert launch.error and "private" not in launch.error
        assert env.recording.resumes == []
        assert env.recording.allocations == [_PROVIDER_CONFIG]
        assert env.catalog.get_session_snapshot(session_id) == snapshot

    async def test_revocation_after_missing_sandbox_blocks_replacement(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
        env.recording.gone = True
        resume = _ProviderLauncher.resume

        def resume_then_revoke(launcher: _ProviderLauncher, sandbox_id: str) -> None:
            try:
                resume(launcher, sandbox_id)
            finally:
                env.recording.allowed_users.clear()

        monkeypatch.setattr(_ProviderLauncher, "resume", resume_then_revoke)
        await env.wake(session_id)
        launch = env.app.state.managed_launches.get(session_id)
        assert launch is not None and launch.settled.is_set()
        assert launch.error and "private" not in launch.error
        assert len(env.recording.resumes) == 1
        assert env.recording.allocations == [_PROVIDER_CONFIG]


async def test_missing_host_during_wake_reports_host_loss_without_profile_auth(env: _Env) -> None:
    response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    env.conversations.set_host_id(session_id, uuid.uuid4().hex, "/workspace")
    await env.wake(session_id)
    launch = env.app.state.managed_launches.get(session_id)
    assert launch is not None and launch.settled.is_set()
    assert launch.error == "managed host not found"
    assert env.recording.selections == []
    assert env.recording.allocations == []
    assert env.recording.resumes == []


@pytest.mark.parametrize("transport", ["json", "multipart", "fork"])
async def test_revocation_after_acceptance_preserves_owned_failed_session(
    env: _Env, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    source_id = None
    if transport == "fork":
        source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
        env.recording.allowed_users.add(_OTHER_USER)
        env.client.headers["X-Forwarded-Email"] = _OTHER_USER
    before_allocations = len(env.recording.allocations)
    original_bind = env.catalog.bind_session

    def revoke_after_acceptance(session_id, snapshot):
        original_bind(session_id, snapshot)
        env.recording.allowed_users.clear()

    monkeypatch.setattr(env.catalog, "bind_session", revoke_after_acceptance)
    announce = Mock()
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_core._announce_session_added", announce
    )
    response = (
        await env.client.post(
            f"/v1/sessions/{source_id}/fork",
            json={
                "host_type": "managed",
                "sandbox_profile_id": "build",
                "sandbox_profile_revision": _PROVIDER_PROFILE.revision,
            },
        )
        if source_id is not None
        else await env.create(transport, sandbox_profile_id="build")
    )
    assert response.status_code == 201, response.text
    session_id = response.json().get("session_id", response.json().get("id"))
    launch = env.app.state.managed_launches.get(session_id)
    assert launch is not None
    await asyncio.wait_for(launch.settled.wait(), timeout=10)
    assert launch.error == "Sandbox profile access denied"
    conversation = env.conversations.get_conversation(session_id)
    assert conversation is not None and conversation.host_id is None
    assert env.app.state.agent_store.get(conversation.agent_id) is not None
    assert env.catalog.get_session_snapshot(session_id) is not None
    announce.assert_called_once_with(_OTHER_USER if source_id else _OWNER, session_id)
    visible = await env.client.get(f"/v1/sessions/{session_id}")
    assert visible.status_code == 200, visible.text
    assert len(env.recording.allocations) == before_allocations
    if source_id is not None:
        source = env.conversations.get_conversation(source_id)
        assert source is not None and source.agent_id != conversation.agent_id
        assert env.app.state.agent_store.get(source.agent_id) is not None


@pytest.mark.parametrize("caller", [_OWNER, _OTHER_USER])
async def test_rejected_fork_cleans_private_copy_and_preserves_source(
    env: _Env, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    source = env.conversations.get_conversation(source_id)
    assert source is not None
    env.recording.allowed_users.add(caller)
    env.client.headers["X-Forwarded-Email"] = caller
    rejected_agents = []
    announce = Mock()
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_core._announce_session_added", announce
    )

    def revoked_during_fork(session_id, snapshot):
        fork = env.conversations.get_conversation(session_id)
        assert fork is not None
        assert env.app.state.agent_store.get(fork.agent_id) is not None
        rejected_agents.append(fork.agent_id)
        raise HTTPException(status_code=403, detail="Sandbox profile access denied")

    monkeypatch.setattr(env.catalog, "bind_session", revoked_during_fork)
    before = env.persisted()
    response = await env.client.post(
        f"/v1/sessions/{source_id}/fork",
        json={
            "host_type": "managed",
            "sandbox_profile_id": "build",
            "sandbox_profile_revision": _PROVIDER_PROFILE.revision,
        },
    )
    assert response.status_code == 403, response.text
    assert env.persisted() == before
    assert len(rejected_agents) == 1
    announce.assert_not_called()
    if caller == _OTHER_USER:
        assert rejected_agents[0] != source.agent_id
        assert env.app.state.agent_store.get(rejected_agents[0]) is None
    else:
        assert rejected_agents == [source.agent_id]
    assert env.app.state.agent_store.get(source.agent_id) is not None
    assert env.conversations.get_conversation(source_id) == source
    assert len(env.recording.allocations) == 1


@pytest.mark.parametrize("detach_source", [False, True])
async def test_profiled_rotation_init_failure_preserves_last_host_association(
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    detach_source: bool,
) -> None:
    from omnigent.server.routes.sessions import routes_core

    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    source = env.conversations.get_conversation(source_id)
    assert source is not None and source.host_id is not None
    env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
    register_test_runner(env.app, "profile-runner", harnesses=["codex"], owner=_OWNER)
    env.conversations.replace_runner_id(source_id, "profile-runner")
    target = env.conversations.create_conversation(
        agent_id=env.agent_id,
        title="Before failed rotation",
        inference_snapshot={"runtime_config": {}},
    )

    async def reject_initialization(*args, **kwargs):
        # The source may detach while the replacement's initialization is pending.
        if detach_source:
            detached = await env.client.patch(f"/v1/sessions/{source_id}", json={"runner_id": ""})
            assert detached.status_code == 200, detached.text
        return Response(409, json={"error": "configuration rejected"})

    runner_post = AsyncMock(side_effect=reject_initialization)
    monkeypatch.setattr(
        sessions_routes,
        "_get_runner_client",
        AsyncMock(return_value=SimpleNamespace(post=runner_post)),
    )
    relay = AsyncMock()
    monkeypatch.setattr(routes_core, "_ensure_runner_relay_ready", relay)
    response = await env.client.patch(
        f"/v1/sessions/{target.id}",
        json={"runner_id": "profile-runner", "title": "Must not apply"},
    )

    assert response.status_code == 503, response.text
    assert "did not accept" in response.text
    saved = env.conversations.get_conversation(target.id)
    assert saved is not None
    assert saved.runner_id == ("profile-runner" if detach_source else None)
    assert (saved.host_id, saved.workspace, saved.git_branch) == (
        source.host_id,
        source.workspace,
        source.git_branch,
    )
    assert saved.title == target.title
    assert env.conversations.get_runner_host_ids("profile-runner") == [source.host_id]
    assert env.catalog.get_session_snapshot(target.id) == env.catalog.get_session_snapshot(
        source_id
    )
    runner_post.assert_awaited_once()
    assert (
        runner_post.call_args.kwargs["json"]["session_init"]["snapshot"]["inference_config"] == {}
    )
    relay.assert_not_awaited()


@pytest.mark.parametrize("previous_runner", [None, "previous-runner"])
async def test_profiled_side_chat_init_failure_restores_runner(
    env: _Env, monkeypatch: pytest.MonkeyPatch, previous_runner: str | None
) -> None:
    from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY

    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
    register_test_runner(env.app, "profile-runner", harnesses=["codex"], owner=_OWNER)
    env.conversations.replace_runner_id(source_id, "profile-runner")
    target = env.conversations.create_conversation(
        agent_id=env.agent_id,
        inference_snapshot={"runtime_config": {}},
        labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source_id},
        runner_id=previous_runner,
    )
    env.catalog.bind_session(target.id, env.profile)
    monkeypatch.setattr(sessions_routes, "_get_runner_client", AsyncMock(return_value=None))

    response = await env.client.patch(
        f"/v1/sessions/{target.id}", json={"runner_id": "profile-runner"}
    )

    assert response.status_code == 503, response.text
    saved = env.conversations.get_conversation(target.id)
    assert saved is not None and saved.runner_id == previous_runner and saved.host_id is None
    assert env.conversations.get_conversation(source_id).runner_id == "profile-runner"


@pytest.mark.parametrize("previous_runner", [None, "previous-runner"])
@pytest.mark.parametrize("race_window", ["before_reload", "during_init"])
@pytest.mark.parametrize("profiled_rotation", [False, True])
async def test_runner_rotation_init_failure_preserves_newer_binding(
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    previous_runner: str | None,
    race_window: str,
    profiled_rotation: bool,
) -> None:
    from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY

    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    source = env.conversations.get_conversation(source_id)
    assert source is not None
    env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
    register_test_runner(env.app, "profile-runner", harnesses=["codex"], owner=_OWNER)
    register_test_runner(env.app, "ordinary-runner", harnesses=["codex"], owner=_OWNER)
    env.conversations.replace_runner_id(source_id, "profile-runner")
    target = env.conversations.create_conversation(
        agent_id=env.agent_id,
        runner_id=previous_runner,
        inference_snapshot={"runtime_config": {}},
        labels=(
            {SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source_id}
            if previous_runner is not None and profiled_rotation
            else None
        ),
    )
    if previous_runner is not None and profiled_rotation:
        env.catalog.bind_session(target.id, env.profile)

    def replace_binding() -> None:
        if profiled_rotation:
            # A replacement runner may update both sessions during initialization.
            env.conversations.replace_runner_id(source_id, "newer-runner")
            env.conversations.replace_runner_id(target.id, "newer-runner")
        else:
            rotation_args = {
                "conversation_store": env.conversations,
                "host_store": env.hosts,
                "user_id": _OWNER,
            }
            for session_id, runner_id in (
                (target.id, None),
                (target.id, "profile-runner"),
                (source_id, None),
            ):
                current = env.conversations.get_conversation(session_id)
                assert current is not None
                assert env.catalog.reassign_runner(current, runner_id, **rotation_args)

    async def reject_initialization(*args, **kwargs):
        if race_window == "during_init":
            replace_binding()
        return Response(409, json={"error": "configuration rejected"})

    async def get_runner_client(*args, **kwargs):
        if race_window == "before_reload":
            replace_binding()
        return SimpleNamespace(post=AsyncMock(side_effect=reject_initialization))

    monkeypatch.setattr(sessions_routes, "_get_runner_client", get_runner_client)
    response = await env.client.patch(
        f"/v1/sessions/{target.id}",
        json={"runner_id": "profile-runner" if profiled_rotation else "ordinary-runner"},
    )

    assert response.status_code == 503, response.text
    saved = env.conversations.get_conversation(target.id)
    assert saved is not None
    assert saved.runner_id == ("newer-runner" if profiled_rotation else "profile-runner")
    assert saved.host_id == (
        None if previous_runner is not None and profiled_rotation else source.host_id
    )
    if not profiled_rotation:
        assert env.conversations.get_runner_host_ids("profile-runner") == [source.host_id]


@pytest.mark.parametrize("previous_runner", [None, "previous-runner"])
async def test_flag_off_init_rollback_keeps_legacy_store_calls(
    env: _Env, monkeypatch: pytest.MonkeyPatch, previous_runner: str | None
) -> None:
    env.app.state.sandbox_profiles = None
    env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
    register_test_runner(env.app, "ordinary-runner", harnesses=["codex"], owner=_OWNER)
    target = env.conversations.create_conversation(
        agent_id=env.agent_id,
        runner_id=previous_runner,
        inference_snapshot={"runtime_config": {}, "harness": "codex"},
    )
    replace = env.conversations.replace_runner_id
    clear = env.conversations.clear_runner_id

    def legacy_replace(session_id, runner_id):
        return replace(session_id, runner_id)

    def legacy_clear(session_id):
        return clear(session_id)

    monkeypatch.setattr(env.conversations, "replace_runner_id", legacy_replace)
    monkeypatch.setattr(env.conversations, "clear_runner_id", legacy_clear)
    monkeypatch.setattr(
        env.conversations,
        "rollback_runner_reassignment",
        Mock(side_effect=AssertionError("Profile rollback must remain inactive")),
    )
    monkeypatch.setattr(sessions_routes, "_get_runner_client", AsyncMock(return_value=None))
    response = await env.client.patch(
        f"/v1/sessions/{target.id}", json={"runner_id": "ordinary-runner"}
    )

    assert response.status_code == 503, response.text
    assert env.conversations.get_conversation(target.id).runner_id == previous_runner
    cleared = await env.client.patch(f"/v1/sessions/{target.id}", json={"runner_id": ""})
    assert cleared.status_code == 200, cleared.text


async def test_profile_adoption_rejects_already_bound_unprofiled_target(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
    register_test_runner(env.app, "profile-runner", harnesses=["codex"], owner=_OWNER)
    env.conversations.replace_runner_id(source_id, "profile-runner")
    target = env.conversations.create_conversation(
        agent_id=env.agent_id, runner_id="ordinary-runner"
    )
    notify = AsyncMock(return_value=None)
    monkeypatch.setattr(sessions_routes, "_get_runner_client", notify)

    response = await env.client.patch(
        f"/v1/sessions/{target.id}", json={"runner_id": "profile-runner"}
    )

    assert response.status_code == 400, response.text
    assert env.conversations.get_conversation(target.id) == target
    assert env.catalog.get_session_snapshot(target.id) is None
    assert env.catalog._store.get_session_host_id(target.id) is None
    notify.assert_not_awaited()


@pytest.mark.parametrize("operation", ["clear", "replace"])
async def test_stale_ordinary_patch_preserves_managed_rotation(
    env: _Env, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
    register_test_runner(env.app, "profile-runner", harnesses=["codex"], owner=_OWNER)
    register_test_runner(env.app, "ordinary-runner", harnesses=["codex"], owner=_OWNER)
    env.conversations.replace_runner_id(source_id, "profile-runner")
    source = env.conversations.get_conversation(source_id)
    assert source is not None
    target = env.conversations.create_conversation(agent_id=env.agent_id)
    method = "clear_runner_id" if operation == "clear" else "replace_runner_id"
    write = getattr(env.conversations, method)

    def rotate_before_stale_write(*args, **kwargs):
        rotation_args = {
            "conversation_store": env.conversations,
            "host_store": env.hosts,
            "user_id": _OWNER,
        }
        assert env.catalog.reassign_runner(target, "profile-runner", **rotation_args)
        assert env.catalog.reassign_runner(source, None, **rotation_args)
        return write(*args, **kwargs)

    monkeypatch.setattr(env.conversations, method, rotate_before_stale_write)
    notify = AsyncMock(return_value=None)
    monkeypatch.setattr(sessions_routes, "_get_runner_client", notify)
    response = await env.client.patch(
        f"/v1/sessions/{target.id}",
        json={"runner_id": "" if operation == "clear" else "ordinary-runner"},
    )

    assert response.status_code == 400, response.text
    assert "binding changed" in response.text
    saved = env.conversations.get_conversation(target.id)
    assert saved is not None and saved.runner_id == "profile-runner"
    assert saved.host_id == source.host_id
    assert env.conversations.get_conversation(source_id).runner_id is None
    assert env.conversations.get_runner_host_ids("profile-runner") == [source.host_id]
    assert env.catalog.get_session_snapshot(target.id) == env.profile
    notify.assert_not_awaited()


@pytest.mark.parametrize("race", ["target_rebound", "source_detached"])
async def test_rejected_rotation_does_not_commit_profile_affinity(
    env: _Env, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
    register_test_runner(env.app, "profile-runner", harnesses=["codex"], owner=_OWNER)
    env.conversations.replace_runner_id(source_id, "profile-runner")
    target = env.conversations.create_conversation(agent_id=env.agent_id)
    rotate = env.conversations.reassign_runner_with_host

    def change_binding_before_rotation(*args, **kwargs):
        if race == "target_rebound":
            env.conversations.replace_runner_id(target.id, "ordinary-runner")
        else:
            env.conversations.clear_runner_id(source_id)
        return rotate(*args, **kwargs)

    monkeypatch.setattr(
        env.conversations, "reassign_runner_with_host", change_binding_before_rotation
    )
    response = await env.client.patch(
        f"/v1/sessions/{target.id}", json={"runner_id": "profile-runner"}
    )

    assert response.status_code == 400, response.text
    saved = env.conversations.get_conversation(target.id)
    assert saved is not None
    assert saved.runner_id == ("ordinary-runner" if race == "target_rebound" else None)
    assert saved.host_id is None and saved.workspace is None
    assert env.catalog.get_session_snapshot(target.id) is None
    assert env.catalog._store.get_session_host_id(target.id) is None

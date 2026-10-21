"""Accepted sandbox profiles reach real create, fork, wake, and replacement flows."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient, Response

from omnigent.host.frames import HostHelloFrame
from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.onboarding.sandboxes.profiles import (
    ProviderSandboxProfile,
    SandboxProfileAccessDenied,
    SandboxProfileChanged,
    SandboxProfileContext,
    validate_profile_config,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL, UnifiedAuthProvider
from omnigent.server.managed_hosts import (
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
    launch_managed_host,
)
from omnigent.server.routes import sessions as sessions_routes
from omnigent.server.routes._sessions.helpers import cancel_managed_launch_tasks
from omnigent.server.routes._sessions.orchestration import _run_managed_launch, _run_managed_wake
from omnigent.server.routes._workspace_validation import WorkspaceValidationError
from omnigent.server.sandbox_profiles import SandboxProfileCatalog
from omnigent.server.scheduled.fire import FireDeps, _make_managed_sandbox_dispatch
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import (
    FakeSandboxLauncher,
    HostStartInvocation,
    build_agent_bundle,
    create_test_agent,
    register_test_runner,
)

pytestmark = pytest.mark.asyncio

_EXECUTOR = {"type": "omnigent", "config": {"harness": "codex"}}
_PROFILE = {
    "name": "Build sandbox",
    "description": "A customer-defined build environment",
    "provider": "kubernetes",
    "config": {
        "image": "example.test/host:accepted",
        "resources": {"requests": {"cpu": "2", "memory": "4Gi"}},
        "egress": [{"ports": [{"protocol": "TCP", "port": 443}]}],
    },
    "enabled": True,
}
_OWNER = "owner@example.test"
_OTHER_USER = "other@example.test"
_PROVIDER_PROFILE = ProviderSandboxProfile(
    id="build", name="Provider build sandbox", revision="release/2026.10+1"
)
_PROVIDER_CONFIG = {"image": "example.test/host:provider-owned", "egress": []}


@dataclass
class _Recording:
    allocations: list[dict[str, Any] | None] = field(default_factory=list)
    resumes: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)
    terminations: list[str] = field(default_factory=list)
    gone: bool = False
    allowed_users: set[str] = field(default_factory=lambda: {_OWNER})
    provider_profile: ProviderSandboxProfile = _PROVIDER_PROFILE
    selections: list[tuple[SandboxProfileContext, ProviderSandboxProfile]] = field(
        default_factory=list
    )
    selection_error: Exception | None = None


class _Launcher(FakeSandboxLauncher):
    provider: ClassVar[str] = "kubernetes"

    def __init__(
        self, recording: _Recording, host_store: HostStore, *, owner: str = RESERVED_USER_LOCAL
    ) -> None:
        super().__init__(on_host_start=self._register, can_resume=True)
        self._recording = recording
        self._host_store = host_store
        self._profile: dict[str, Any] | None = None
        self._owner = owner

    def apply_profile(self, config: dict[str, object]) -> None:
        self._profile = validate_profile_config(config)

    def provision(self, name: str) -> str:
        super().provision(name)
        self._recording.allocations.append(copy.deepcopy(self._profile))
        return f"profile-sandbox-{len(self._recording.allocations)}"

    def resume(self, sandbox_id: str) -> None:
        self._recording.resumes.append((sandbox_id, copy.deepcopy(self._profile)))
        if self._recording.gone:
            raise SandboxGoneError("The previous sandbox generation is gone")
        super().resume(sandbox_id)

    def terminate(self, sandbox_id: str) -> None:
        self._recording.terminations.append(sandbox_id)
        super().terminate(sandbox_id)

    def _register(self, invocation: HostStartInvocation) -> None:
        self._host_store.upsert_on_connect(
            invocation.host_id,
            invocation.host_name,
            self._owner,
            managed_token=invocation.token,
        )


class _ProviderLauncher(_Launcher):
    provider: ClassVar[str] = "company"
    manages_profiles: ClassVar[bool] = True

    def list_profiles(self, context: SandboxProfileContext) -> list[ProviderSandboxProfile]:
        return (
            [self._recording.provider_profile]
            if context.user_id in self._recording.allowed_users
            else []
        )

    def select_profile(
        self, context: SandboxProfileContext, profile: ProviderSandboxProfile
    ) -> None:
        self._recording.selections.append((context, profile))
        if self._recording.selection_error is not None:
            raise self._recording.selection_error
        if context.user_id not in self._recording.allowed_users:
            raise SandboxProfileAccessDenied("private membership was revoked")
        if profile != self._recording.provider_profile:
            raise SandboxProfileChanged("private profile configuration changed")
        self._owner = context.user_id
        self._profile = copy.deepcopy(_PROVIDER_CONFIG)

    def apply_profile(self, config: dict[str, object]) -> None:
        raise AssertionError("Provider-owned settings must come from select_profile")

    def provision(self, name: str) -> str:
        assert self._profile == _PROVIDER_CONFIG
        return super().provision(name)

    def resume(self, sandbox_id: str) -> None:
        assert self._profile == _PROVIDER_CONFIG
        super().resume(sandbox_id)


@dataclass
class _Env:
    app: FastAPI
    client: AsyncClient
    hosts: HostStore
    conversations: SqlAlchemyConversationStore
    recording: _Recording
    profile: dict[str, Any]
    agent_id: str
    artifact_dir: Path
    provider: str = "kubernetes"

    @property
    def catalog(self) -> SandboxProfileCatalog:
        return self.app.state.sandbox_profiles

    def persisted(self) -> tuple[list[str], list[str]]:
        return (
            sorted(
                row.id for row in self.conversations.list_conversations(limit=100, kind=None).data
            ),
            sorted(
                str(path.relative_to(self.artifact_dir))
                for path in self.artifact_dir.rglob("*")
                if path.is_file()
            ),
        )

    async def create(self, transport: str, **metadata: Any) -> Response:
        fields = {"host_type": "managed", "sandbox_provider": self.provider, **metadata}
        if transport == "json":
            return await self.client.post(
                "/v1/sessions", json={"agent_id": self.agent_id, **fields}
            )
        return await self.client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(fields)},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle("profile-agent", executor=_EXECUTOR, include_llm=False),
                    "application/gzip",
                )
            },
        )

    async def settled(self, response: Response) -> str:
        assert response.status_code == 201, response.text
        body = response.json()
        session_id = body.get("session_id", body.get("id"))
        assert session_id is not None
        launch = self.app.state.managed_launches.get(session_id)
        if launch is not None:
            await asyncio.wait_for(launch.settled.wait(), timeout=10)
            assert launch.error is None, launch.error
        conversation = self.conversations.get_conversation(session_id)
        assert conversation is not None and conversation.host_id is not None
        return session_id

    async def wake(self, session_id: str) -> None:
        conversation = self.conversations.get_conversation(session_id)
        assert conversation is not None and conversation.host_id is not None
        self.hosts.set_offline(conversation.host_id)
        self.app.state.managed_launches.begin(session_id)
        await _run_managed_wake(
            session_id=session_id,
            conv=conversation,
            sandbox_config=self.app.state.sandbox_config,
            tracker=self.app.state.managed_launches,
            conversation_store=self.conversations,
            host_store=self.hosts,
            host_registry=None,
            tunnel_registry=None,
            agent_store=self.app.state.agent_store,
            agent_id=conversation.agent_id,
            sandbox_profiles=self.catalog,
        )


@pytest_asyncio.fixture
async def env(
    runtime_init: None, db_uri: str, tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[_Env]:
    artifact_dir = tmp_path / "artifacts"
    artifacts = LocalArtifactStore(str(artifact_dir))
    hosts = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    recording = _Recording()
    provider_managed = getattr(request, "param", False)
    launcher = _ProviderLauncher if provider_managed else _Launcher
    config = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://managed.example.test",
            token_ttl_s=3600,
            provider=launcher.provider,
            launcher_factory=lambda: launcher(recording, hosts),
        )
    )
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=hosts,
        sandbox_config=config,
        auth_provider=(
            UnifiedAuthProvider(source="header", local_single_user=False)
            if provider_managed
            else None
        ),
    )
    # Exercise allocation and host registration without needing a live runner.
    app.state.profile_test_tunnels = app.state.tunnel_registry
    app.state.profile_test_hosts = app.state.host_registry
    app.state.host_registry = None
    app.state.tunnel_registry = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if provider_managed:
            client.headers["X-Forwarded-Email"] = _OWNER
            profile_response = await client.get("/v1/sandbox-profiles")
            assert profile_response.status_code == 200, profile_response.text
            profile = profile_response.json()["profiles"][0]
        else:
            profile_response = await client.put("/v1/sandbox-profiles/build", json=_PROFILE)
            assert profile_response.status_code == 200, profile_response.text
            profile = profile_response.json()
        agent = await create_test_agent(client, executor=_EXECUTOR, include_llm=False)
        try:
            yield _Env(
                app,
                client,
                hosts,
                conversations,
                recording,
                profile,
                agent["id"],
                artifact_dir,
                launcher.provider,
            )
        finally:
            await cancel_managed_launch_tasks()


@pytest.mark.parametrize("transport", ["json", "multipart"])
async def test_create_forwards_selected_profile_and_keeps_legacy_default(
    env: _Env, transport: str
) -> None:
    response = await env.create(
        transport, sandbox_profile_id="build", sandbox_profile_revision=env.profile["revision"]
    )
    session_id = await env.settled(response)
    assert env.recording.allocations == [env.profile["config"]]
    assert env.catalog.get_session_snapshot(session_id) == env.profile
    conversation = env.conversations.get_conversation(session_id)
    assert conversation is not None and conversation.host_id is not None
    assert env.catalog.get_host_snapshot(conversation.host_id) == env.profile
    default_id = await env.settled(await env.create(transport))
    assert env.recording.allocations[-1] is None
    assert env.catalog.get_session_snapshot(default_id) is None


async def test_fork_inherits_accepted_profile_after_catalog_edit(env: _Env) -> None:
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    edited = await env.client.put(
        "/v1/sandbox-profiles/build",
        json={**_PROFILE, "config": {"image": "example.test/host:edited"}},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] != env.profile["revision"]
    response = await env.client.post(
        f"/v1/sessions/{source_id}/fork", json={"host_type": "managed"}
    )
    fork_id = await env.settled(response)
    assert env.catalog.get_session_snapshot(fork_id) == env.profile
    assert env.recording.allocations == [env.profile["config"], env.profile["config"]]


@pytest.mark.parametrize("gone", [False, True])
async def test_wake_and_replacement_keep_saved_profile_after_catalog_edit(
    env: _Env, gone: bool
) -> None:
    session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    before = env.conversations.get_conversation(session_id)
    assert before is not None and before.host_id is not None
    host = env.hosts.get_host(before.host_id)
    assert host is not None
    response = await env.client.put(
        "/v1/sandbox-profiles/build",
        json={**_PROFILE, "config": {"image": "example.test/host:edited"}},
    )
    assert response.status_code == 200, response.text
    env.app.state.sandbox_profiles = SandboxProfileCatalog(
        env.hosts._engine, env.app.state.sandbox_config
    )
    env.recording.gone = gone
    await env.wake(session_id)
    assert env.app.state.managed_launches.get(session_id) is None
    assert env.recording.resumes == [(host.sandbox_id, env.profile["config"])]
    after = env.conversations.get_conversation(session_id)
    assert after is not None and after.host_id is not None
    assert after.host_id == before.host_id
    assert env.catalog.get_session_snapshot(session_id) == env.profile
    assert env.recording.allocations == [env.profile["config"]] * (2 if gone else 1)
    replacement = env.hosts.get_host(after.host_id)
    assert replacement is not None
    assert (replacement.sandbox_id != host.sandbox_id) is gone


@pytest.mark.parametrize("transport", ["json", "multipart"])
@pytest.mark.parametrize("invalid", ["unknown", "stale", "disabled"])
async def test_rejected_selection_does_not_persist_or_allocate(
    env: _Env, transport: str, invalid: str
) -> None:
    profile_id = "unknown" if invalid == "unknown" else "build"
    revision = "stale-revision" if invalid == "stale" else env.profile["revision"]
    if invalid == "disabled":
        response = await env.client.delete("/v1/sandbox-profiles/build")
        assert response.status_code == 200
    before = env.persisted()
    response = await env.create(
        transport, sandbox_profile_id=profile_id, sandbox_profile_revision=revision
    )
    assert response.status_code == (404 if invalid == "unknown" else 409), response.text
    assert env.persisted() == before
    assert env.recording.allocations == []


@pytest.mark.parametrize("accepted_profile", [False, True])
async def test_failed_host_workspace_validation_does_not_accept_or_bind_profile(
    env: _Env, monkeypatch: pytest.MonkeyPatch, accepted_profile: bool
) -> None:
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    source = env.conversations.get_conversation(source_id)
    assert source is not None and source.host_id is not None
    host_id = source.host_id
    env.app.state.profile_test_hosts.register(
        host_id=host_id,
        ws=AsyncMock(),
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="profile-host"),
        owner=RESERVED_USER_LOCAL,
    )
    response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    if accepted_profile:
        env.catalog.bind_session(session_id, env.profile)
    before = env.catalog.get_session_snapshot(session_id)

    async def invalid_workspace(**kwargs):
        raise WorkspaceValidationError("The selected directory is unavailable")

    monkeypatch.setattr(
        "omnigent.server.routes._workspace_validation.validate_workspace", invalid_workspace
    )
    response = await env.client.post(
        f"/v1/hosts/{host_id}/runners",
        json={"session_id": session_id, "workspace": "/missing"},
    )
    assert response.status_code == 400, response.text
    assert "selected directory" in response.text
    assert env.catalog.get_session_snapshot(session_id) == before
    assert env.catalog._store.get_session_host_id(session_id) is None
    assert env.catalog.get_host_snapshot(host_id) == env.profile
    session = env.conversations.get_conversation(session_id)
    assert session is not None and session.host_id is None


async def test_scheduler_profile_database_failure_removes_created_session(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempted_sessions: list[str] = []

    def unavailable_database(session_id):
        attempted_sessions.append(session_id)
        raise RuntimeError("Profile storage is unavailable")

    monkeypatch.setattr(env.catalog, "get_session_snapshot", unavailable_database)
    before = env.persisted()
    async with AsyncClient(
        transport=ASGITransport(app=env.app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/sessions",
            json={
                "agent_id": env.agent_id,
                "host_type": "managed",
                "sandbox_profile_id": "build",
            },
        )
    assert response.status_code == 500, response.text
    assert len(attempted_sessions) == 1
    session_id = attempted_sessions[0]
    assert env.conversations.get_conversation(session_id) is None
    assert env.app.state.managed_launches.get(session_id) is None
    assert env.persisted() == before
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
        owner=RESERVED_USER_LOCAL,
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


async def test_profile_host_binding_failure_settles_and_removes_fresh_sandbox(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable_database(session_id, host_id):
        raise RuntimeError("Profile storage is unavailable")

    monkeypatch.setattr(env.catalog, "bind_host", unavailable_database)
    response = await env.create("json", sandbox_profile_id="build")
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    launch = env.app.state.managed_launches.get(session_id)
    assert launch is not None
    await asyncio.wait_for(launch.settled.wait(), timeout=10)
    assert launch.error == "failed to bind managed host"
    session = env.conversations.get_conversation(session_id)
    assert session is not None and session.host_id is not None

    async def wait_for_cleanup() -> None:
        while env.hosts.get_host(session.host_id) is not None:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_for_cleanup(), timeout=10)
    assert env.recording.terminations == ["profile-sandbox-1"]
    assert env.catalog.get_host_snapshot(session.host_id) is None


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

    def register_then_disable(launcher: _Launcher, invocation: HostStartInvocation) -> None:
        register(launcher, invocation)
        registered_hosts.append(invocation.host_id)
        registry.register(
            host_id=invocation.host_id,
            ws=AsyncMock(),
            hello=HostHelloFrame(
                version="0.1.0-test", frame_protocol_version=1, name=invocation.host_name
            ),
            owner=RESERVED_USER_LOCAL,
            registered_with_managed_token=True,
        )
        env.catalog.disable("build")

    monkeypatch.setattr(_Launcher, "_register", register_then_disable)
    env.app.state.managed_launches.begin(session_id)
    await _run_managed_launch(
        session_id=session_id,
        owner=RESERVED_USER_LOCAL,
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
    assert launch.error == "The saved sandbox profile is no longer enabled"
    session = env.conversations.get_conversation(session_id)
    assert session is not None
    assert session.runner_id is None
    assert env.recording.allocations == [env.profile["config"]] * (2 if relaunch else 1)
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


async def test_disabled_saved_profile_blocks_wake_before_provider_calls(env: _Env) -> None:
    session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    assert (await env.client.delete("/v1/sandbox-profiles/build")).status_code == 200
    await env.wake(session_id)
    launch = env.app.state.managed_launches.get(session_id)
    assert launch is not None and "enabled" in launch.error
    assert env.recording.resumes == []
    assert len(env.recording.allocations) == 1


async def test_profiled_side_chat_can_bind_only_its_source_runner(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
    register_test_runner(env.app, "source-runner", harnesses=["codex"])
    register_test_runner(env.app, "other-runner", harnesses=["codex"])
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


@pytest.mark.parametrize("env", [True], indirect=True, ids=["provider-managed"])
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
        deps = FireDeps(
            scheduled_task_store=None,
            agent_store=env.app.state.agent_store,
            conversation_store=env.conversations,
            permission_store=None,
            host_store=env.hosts,
            host_registry=None,
            sandbox_config=env.app.state.sandbox_config,
            managed_launches=env.app.state.managed_launches,
            app_state=env.app.state,
        )
        with pytest.raises(RuntimeError, match="Choose a sandbox profile"):
            await _make_managed_sandbox_dispatch(deps)(
                conversation, SimpleNamespace(user_id=_OWNER, agent_id=env.agent_id)
            )
        require_selection.assert_called_once_with(None, user_id=_OWNER)
        launch = env.app.state.managed_launches.get(session_id)
        assert launch is not None and launch.settled.is_set() and launch.error
        assert env.recording.allocations == []
        assert env.recording.selections == []
        assert env.conversations.get_conversation(session_id).host_id is None

    async def test_direct_launch_without_catalog_rejects_missing_selection_before_prepare(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prepare = Mock(side_effect=AssertionError("Unselected provider must not prepare"))
        monkeypatch.setattr(_ProviderLauncher, "prepare", prepare)
        with pytest.raises(HTTPException) as caught:
            await launch_managed_host(
                config=env.app.state.sandbox_config, owner=_OWNER, host_store=env.hosts
            )
        assert caught.value.status_code == 422
        prepare.assert_not_called()
        assert env.recording.allocations == []
        assert env.recording.selections == []

    @pytest.mark.parametrize("revoked", [False, True])
    async def test_patch_cannot_bypass_host_admission_using_a_managed_runner(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch, revoked: bool
    ) -> None:
        source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
        env.app.state.tunnel_registry = env.app.state.profile_test_tunnels
        register_test_runner(env.app, "profile-runner", harnesses=["codex"], owner=_OWNER)
        env.conversations.replace_runner_id(source_id, "profile-runner")
        if revoked:
            env.recording.allowed_users.clear()
        env.recording.selections.clear()
        notify = AsyncMock(return_value=None)
        monkeypatch.setattr(sessions_routes, "_get_runner_client", notify)
        response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
        assert response.status_code == 201, response.text
        target_id = response.json()["id"]
        notify.reset_mock()
        response = await env.client.patch(
            f"/v1/sessions/{target_id}", json={"runner_id": "profile-runner"}
        )
        assert response.status_code == (403 if revoked else 400), response.text
        assert "private" not in response.text
        assert env.recording.selections == [
            (SandboxProfileContext(user_id=_OWNER, workspace_id=0), _PROVIDER_PROFILE)
        ]
        assert env.conversations.get_conversation(target_id).runner_id is None
        assert env.catalog.get_session_snapshot(target_id) is None
        notify.assert_not_awaited()

        # Clearing the source first must not erase the runner's trusted host binding.
        response = await env.client.patch(f"/v1/sessions/{source_id}", json={"runner_id": ""})
        assert response.status_code == 400, response.text
        assert env.conversations.get_conversation(source_id).runner_id == "profile-runner"

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

    async def test_revoked_failed_start_terminates_without_reselecting_profile(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
        snapshot = await asyncio.to_thread(env.catalog.resolve, "build", user_id=_OWNER)
        env.catalog.bind_session(session_id, snapshot)
        registry = env.app.state.profile_test_hosts
        register = _Launcher._register
        registered_hosts = []
        selections_before_revoke = []

        def register_then_revoke(launcher, invocation):
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

        monkeypatch.setattr(_Launcher, "_register", register_then_revoke)
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
            sandbox_profiles=env.catalog,
        )
        launch = env.app.state.managed_launches.get(session_id)
        assert launch is not None and launch.settled.is_set()
        assert launch.error and "private" not in launch.error
        assert env.recording.allocations == [_PROVIDER_CONFIG]
        assert env.recording.terminations == ["profile-sandbox-1"]
        assert len(registered_hosts) == 1
        assert env.hosts.get_host(registered_hosts[0]) is None
        assert len(env.recording.selections) == selections_before_revoke[0] + 1
        conversation = env.conversations.get_conversation(session_id)
        assert conversation is not None and conversation.runner_id is None
        assert conversation.host_id is None

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
        assert snapshot["config"] == {}
        assert env.recording.selections
        assert all(
            selection == (SandboxProfileContext(user_id=_OWNER, workspace_id=0), _PROVIDER_PROFILE)
            for selection in env.recording.selections
        )
        assert env.catalog._store.list_profiles(include_disabled=True) == ([], None)

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
            f"/v1/sessions/{source_id}/fork", json={"host_type": "managed"}
        )
        assert response.status_code == 403, response.text
        assert env.persisted() == before
        assert len(env.recording.allocations) == 1
        env.recording.allowed_users.add(_OTHER_USER)
        fork_id = await env.settled(
            await env.client.post(f"/v1/sessions/{source_id}/fork", json={"host_type": "managed"})
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
            env.hosts._engine, env.app.state.sandbox_config
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

    async def test_background_launch_cannot_allocate_without_saved_selection(
        self, env: _Env
    ) -> None:
        response = await env.client.post("/v1/sessions", json={"agent_id": env.agent_id})
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
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
        assert launch is not None and launch.settled.is_set()
        assert launch.error
        assert env.recording.allocations == []

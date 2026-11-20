"""Shared provider-owned sandbox launch environment."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.onboarding.sandboxes.profiles import (
    ProviderSandboxProfile,
    SandboxProfileAccessDenied,
    SandboxProfileChanged,
    SandboxProfileContext,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL, UnifiedAuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.managed_hosts import (
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
)
from omnigent.server.routes._sessions.helpers import cancel_managed_launch_tasks
from omnigent.server.routes._sessions.orchestration import _run_managed_wake
from omnigent.server.sandbox_profiles import SandboxProfileCatalog
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
)

_EXECUTOR = {"type": "omnigent", "config": {"harness": "codex"}}
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
    assigned: dict[str, ProviderSandboxProfile] = field(default_factory=dict)


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

    def provision(self, name: str) -> str:
        assert self._profile == _PROVIDER_CONFIG
        return super().provision(name)

    def resume(self, sandbox_id: str) -> None:
        assert self._profile == _PROVIDER_CONFIG
        super().resume(sandbox_id)


class _ControllerLauncher(_ProviderLauncher):
    supports_sandbox_profile_lookup: ClassVar[bool] = True

    def list_profiles(self, context: SandboxProfileContext) -> list[ProviderSandboxProfile]:
        if context.user_id not in self._recording.allowed_users:
            raise SandboxProfileAccessDenied("Controller denied access")
        if context.sandbox_id is not None:
            if self._recording.gone or context.sandbox_id not in self._recording.assigned:
                raise SandboxGoneError("Sandbox was deleted")
            return [self._recording.assigned[context.sandbox_id]]
        return super().list_profiles(context)

    def select_profile(
        self, context: SandboxProfileContext, profile: ProviderSandboxProfile
    ) -> None:
        if context.sandbox_id is not None:
            if self.list_profiles(context) != [profile]:
                raise SandboxProfileChanged("Sandbox assignment changed")
        # Historical revisions remain available for surviving sessions.
        if profile in self._recording.assigned.values():
            self._recording.selections.append((context, profile))
            if context.user_id not in self._recording.allowed_users:
                raise SandboxProfileAccessDenied("Controller denied access")
            self._owner, self._profile = context.user_id, copy.deepcopy(_PROVIDER_CONFIG)
        else:
            super().select_profile(context, profile)
        self._selected = profile

    def provision(self, name: str) -> str:
        sandbox_id = super().provision(name)
        self._recording.assigned[sandbox_id] = self._selected
        self._recording.gone = False
        return sandbox_id


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
    launcher = (
        _ControllerLauncher
        if getattr(request, "param", None) == "controller"
        else _ProviderLauncher
    )
    config = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://managed.example.test",
            token_ttl_s=3600,
            provider=launcher.provider,
            launcher_factory=lambda: launcher(recording, hosts),
            manages_profiles=True,
        )
    )
    app = create_app(
        feature_flags=FeatureFlags(frozenset({Feature.SANDBOX_PROFILES})),
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=hosts,
        sandbox_config=config,
        auth_provider=(UnifiedAuthProvider(source="header", local_single_user=False)),
    )
    # Exercise allocation and host registration without needing a live runner.
    app.state.profile_test_tunnels = app.state.tunnel_registry
    app.state.profile_test_hosts = app.state.host_registry
    app.state.host_registry = None
    app.state.tunnel_registry = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.headers["X-Forwarded-Email"] = _OWNER
        profile = app.state.sandbox_profiles.resolve("build", user_id=_OWNER)
        recording.selections.clear()
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

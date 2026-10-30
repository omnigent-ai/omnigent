"""Provider profile hooks must receive the caller's bound, current credentials."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.onboarding.sandboxes.base import OwnerCredentialMissingError
from omnigent.onboarding.sandboxes.profiles import (
    ProviderSandboxProfile,
    SandboxProfileAccessDenied,
    SandboxProfileContext,
)
from omnigent.server.connections_registry import blocking_owner_resolver
from omnigent.server.managed_hosts import (
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
    build_owner_launcher,
    deployment_with_sandbox_profile,
    launch_managed_host,
)
from omnigent.server.sandbox_profiles import SandboxProfileCatalog
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import FakeSandboxLauncher

_PROFILE = ProviderSandboxProfile(id="build", name="Build", revision="v1")


@dataclass
class _Credentials:
    tokens: dict[tuple[str, int], str] = field(
        default_factory=lambda: {
            ("alice", 41): "alice-41-token",
            ("bob", 41): "bob-41-token",
            ("alice", 42): "alice-42-token",
        }
    )
    resolutions: list[tuple[str, int, int]] = field(default_factory=list)
    hooks: list[tuple[str, SandboxProfileContext]] = field(default_factory=list)
    authorizations: list[Mapping[str, object]] = field(default_factory=list)
    launchers: list[_CredentialProvider] = field(default_factory=list)


class _CredentialProvider(FakeSandboxLauncher):
    provider: ClassVar[str] = "company"
    manages_profiles: ClassVar[bool] = True

    def __init__(self, credentials: _Credentials) -> None:
        super().__init__()
        self.credentials = credentials
        self.resolve: Callable[[], Mapping[str, object] | None] | None = None
        self.selected: ProviderSandboxProfile | None = None
        credentials.launchers.append(self)

    @property
    def owner_credential_provider(self) -> str:
        return "databricks"

    def bind_owner_credential(self, resolve: Callable[[], Mapping[str, object] | None]) -> None:
        self.resolve = resolve

    def _authorize(self, context: SandboxProfileContext) -> None:
        if self.resolve is None:
            raise OwnerCredentialMissingError("Owner resolver was not bound")
        payload = self.resolve()
        if payload is None:
            raise OwnerCredentialMissingError("Missing private-provider credential")
        if (payload["user"], payload["workspace"]) != (
            context.user_id,
            context.workspace_id,
        ):
            raise SandboxProfileAccessDenied("No current credential for this caller")
        self.credentials.authorizations.append(payload)

    def list_profiles(self, context: SandboxProfileContext) -> list[ProviderSandboxProfile]:
        self.credentials.hooks.append(("list", context))
        self._authorize(context)
        return [_PROFILE]

    def select_profile(
        self, context: SandboxProfileContext, profile: ProviderSandboxProfile
    ) -> None:
        self.credentials.hooks.append(("select", context))
        self._authorize(context)
        self.selected = profile


@dataclass
class _Harness:
    credentials: _Credentials
    deployment: ManagedSandboxDeployment
    catalog: SandboxProfileCatalog
    hosts: HostStore


@pytest.fixture
def harness(db_uri: str) -> _Harness:
    credentials = _Credentials()

    async def resolve(user_id: str, *, store: object, client: object) -> dict[str, Any] | None:
        del store, client
        workspace_id = current_workspace_id()
        credentials.resolutions.append((user_id, workspace_id, threading.get_ident()))
        token = credentials.tokens.get((user_id, workspace_id))
        if token is None:
            return None
        return {"token": token, "user": user_id, "workspace": workspace_id}

    deployment = ManagedSandboxDeployment(
        configs=(
            ManagedSandboxConfig(
                server_url="https://managed.example.test",
                token_ttl_s=100,
                provider="company",
                launcher_factory=lambda: _CredentialProvider(credentials),
                manages_profiles=True,
            ),
        ),
        owner_credentials={
            "databricks": blocking_owner_resolver(resolve, store=None, client=None)
        },
    )
    hosts = HostStore(db_uri)
    return _Harness(
        credentials,
        deployment,
        SandboxProfileCatalog(SqlAlchemyConversationStore(db_uri), deployment, hosts),
        hosts,
    )


def _snapshot(user_id: str = "alice", workspace_id: int = 41) -> dict[str, Any]:
    return {
        **_PROFILE.model_dump(),
        "provider": "company",
        "user_id": user_id,
        "workspace_id": workspace_id,
    }


async def test_existing_owner_binding_supports_profile_io_and_credential_refresh(
    harness: _Harness,
) -> None:
    with workspace_scope(41):
        launcher = build_owner_launcher(harness.deployment.default, harness.deployment, "alice")
        context = SandboxProfileContext("alice", 41)
        assert await asyncio.to_thread(launcher.list_profiles, context) == [_PROFILE]
        harness.credentials.tokens[("alice", 41)] = "refreshed-token"
        await asyncio.to_thread(launcher.select_profile, context, _PROFILE)
    assert [payload["token"] for payload in harness.credentials.authorizations] == [
        "alice-41-token",
        "refreshed-token",
    ]
    assert all(
        thread_id != threading.get_ident() for _, _, thread_id in harness.credentials.resolutions
    )


async def test_catalog_binds_each_callers_credential_with_workspace_scope(
    harness: _Harness,
) -> None:
    for user_id, workspace_id in (("alice", 41), ("bob", 41), ("alice", 42)):
        with workspace_scope(workspace_id):
            rows, cursor = await asyncio.to_thread(harness.catalog.list_profiles, user_id=user_id)
        assert cursor is None
        assert [row["id"] for row in rows] == ["build"]
        assert "config" not in rows[0]
        assert harness.credentials.authorizations[-1] == {
            "user": user_id,
            "workspace": workspace_id,
            "token": harness.credentials.tokens[(user_id, workspace_id)],
        }
        assert not {"token", "user_id", "workspace_id"} & rows[0].keys()


@pytest.mark.parametrize("inherited", [False, True], ids=["saved-owner", "fork-owner"])
async def test_snapshot_selection_binds_the_effective_owner(
    harness: _Harness, inherited: bool
) -> None:
    with workspace_scope(41):
        if inherited:
            selected = await asyncio.to_thread(
                harness.catalog.inherit_snapshot, _snapshot(), "bob"
            )
        else:
            selected = await asyncio.to_thread(
                harness.catalog.validate_snapshot, _snapshot(), user_id="alice"
            )
    expected_user = "bob" if inherited else "alice"
    assert selected["user_id"] == expected_user
    assert harness.credentials.authorizations[-1]["user"] == expected_user
    assert harness.credentials.authorizations[-1]["workspace"] == 41


async def test_profile_selection_precedes_launch_with_bound_owner_credentials(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared_profiles: list[ProviderSandboxProfile | None] = []

    def prepare(launcher: _CredentialProvider) -> None:
        prepared_profiles.append(launcher.selected)
        FakeSandboxLauncher.prepare(launcher)

    started = AsyncMock(return_value="/workspace")
    monkeypatch.setattr("omnigent.server.managed_hosts._register_and_start_host", started)
    monkeypatch.setattr(_CredentialProvider, "prepare", prepare)
    with workspace_scope(41):
        config = deployment_with_sandbox_profile(harness.deployment, _snapshot())
        await launch_managed_host(config=config, owner="alice", host_store=harness.hosts)
    started.assert_awaited_once()
    launcher = started.await_args.kwargs["launcher"]
    assert launcher.selected == _PROFILE
    assert launcher.prepared
    assert prepared_profiles == [_PROFILE]
    assert harness.credentials.authorizations[-1]["user"] == "alice"
    assert harness.credentials.resolutions
    assert all(
        thread_id != threading.get_ident() for _, _, thread_id in harness.credentials.resolutions
    )


async def test_launch_rejects_a_profile_accepted_for_another_owner(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = AsyncMock()
    monkeypatch.setattr("omnigent.server.managed_hosts._register_and_start_host", started)
    with workspace_scope(41):
        config = deployment_with_sandbox_profile(harness.deployment, _snapshot("bob"))
        with pytest.raises(HTTPException) as error:
            await launch_managed_host(config=config, owner="alice", host_store=harness.hosts)
    assert error.value.status_code == 403
    assert harness.credentials.hooks == []
    assert harness.credentials.authorizations == []
    started.assert_not_awaited()


@pytest.mark.parametrize("operation", ["list", "select"])
async def test_disconnected_caller_never_uses_another_users_credential(
    harness: _Harness, operation: str
) -> None:
    del harness.credentials.tokens[("alice", 41)]
    with workspace_scope(41), pytest.raises(HTTPException) as error:
        if operation == "list":
            await asyncio.to_thread(harness.catalog.list_profiles, user_id="alice")
        else:
            await asyncio.to_thread(
                harness.catalog.validate_snapshot, _snapshot(), user_id="alice"
            )
    assert error.value.status_code == 409
    assert error.value.detail == "Connect your sandbox provider account before choosing a profile"
    assert harness.credentials.authorizations == []
    assert [(user, workspace) for user, workspace, _ in harness.credentials.resolutions] == [
        ("alice", 41)
    ]

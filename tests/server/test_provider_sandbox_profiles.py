"""Provider catalogs remain scoped, read-only, and authoritative at admission."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import ClassVar

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from omnigent.db.db_models import workspace_scope
from omnigent.errors import OmnigentError
from omnigent.onboarding.sandboxes.base import OwnerCredentialMissingError
from omnigent.onboarding.sandboxes.profiles import (
    ProviderSandboxProfile,
    SandboxProfileAccessDenied,
    SandboxProfileChanged,
    SandboxProfileContext,
)
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.server.routes.sandbox_profiles import create_sandbox_profiles_router
from omnigent.server.sandbox_profiles import SandboxProfileCatalog
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import FakeSandboxLauncher

_ALICE = "alice@example.test"
_BOB = "bob@example.test"
_SECRET = "private-provider-token=do-not-expose"
_BUILD = ProviderSandboxProfile(id="build", name="Build", revision="release/2026.10+1")
_RESEARCH = ProviderSandboxProfile(id="research", name="Research", revision="revision-two")
_WRITE = {"name": "Local override", "provider": "company", "config": {}}


@dataclass
class _Profiles:
    allowed: dict[tuple[str, int], list[ProviderSandboxProfile]] = field(
        default_factory=lambda: {
            (_ALICE, 41): [_BUILD],
            (_BOB, 41): [_RESEARCH],
            (_ALICE, 42): [_RESEARCH],
        }
    )
    listings: list[SandboxProfileContext] = field(default_factory=list)
    selections: list[tuple[SandboxProfileContext, ProviderSandboxProfile]] = field(
        default_factory=list
    )
    list_error: Exception | None = None
    select_error: Exception | None = None


class _Provider(FakeSandboxLauncher):
    provider: ClassVar[str] = "company"
    manages_profiles: ClassVar[bool] = True

    def __init__(self, profiles: _Profiles) -> None:
        super().__init__()
        self.profiles = profiles

    def list_profiles(self, context: SandboxProfileContext) -> list[ProviderSandboxProfile]:
        self.profiles.listings.append(context)
        if self.profiles.list_error is not None:
            raise self.profiles.list_error
        return self.profiles.allowed.get((context.user_id, context.workspace_id), [])

    def select_profile(
        self, context: SandboxProfileContext, profile: ProviderSandboxProfile
    ) -> None:
        self.profiles.selections.append((context, profile))
        if self.profiles.select_error is not None:
            raise self.profiles.select_error
        current = next(
            (
                row
                for row in self.profiles.allowed.get((context.user_id, context.workspace_id), [])
                if row.id == profile.id
            ),
            None,
        )
        if current is None:
            raise SandboxProfileAccessDenied(_SECRET)
        if current.revision != profile.revision:
            raise SandboxProfileChanged(_SECRET)


@pytest.fixture
def profiles() -> _Profiles:
    return _Profiles()


def _accept(catalog, session_id, snapshot):
    store = catalog._store.conversations
    if store.get_conversation(session_id) is None:
        store.create_conversation(conversation_id=session_id)
    catalog.bind_session(session_id, snapshot)


def _bind(catalog, session_id, host_id):
    from omnigent.db.db_models import uuid_to_bytes

    host_id = uuid_to_bytes(host_id).hex()
    profile = catalog.get_session_snapshot(session_id)
    host = catalog._host_store.get_host(host_id)
    if host is None:
        catalog._host_store.register_managed_host(
            host_id=host_id,
            name="profile-host",
            user_id=profile["user_id"],
            token="test-token",
            provider=profile["provider"],
            sandbox_id="test-sandbox",
            token_expires_at=2_000_000_000,
        )
    elif host.sandbox_provider is None:
        catalog._host_store.bind_sandbox(
            host_id, provider=profile["provider"], sandbox_id="test-sandbox"
        )
    catalog._store.conversations.bind_profiled_host(session_id, host_id, "/workspace", profile)


def _require(catalog, session_id, host_id, **kwargs):
    from omnigent.db.db_models import uuid_to_bytes

    host_id = uuid_to_bytes(host_id).hex()
    store = catalog._store.conversations
    if store.get_conversation(session_id) is None:
        store.create_conversation(
            conversation_id=session_id,
            host_id=host_id if kwargs.get("bind", True) else None,
            workspace="/workspace" if kwargs.get("bind", True) else None,
        )
    catalog.require_session_host(session_id, host_id, **kwargs)
    if kwargs.get("bind", True):
        _bind(catalog, session_id, host_id)


@pytest.fixture
def catalog(db_uri: str, profiles: _Profiles) -> SandboxProfileCatalog:
    hosts = HostStore(db_uri)
    deployment = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://managed.example.test",
            token_ttl_s=100,
            provider="company",
            launcher_factory=lambda: _Provider(profiles),
            manages_profiles=True,
        )
    )
    return SandboxProfileCatalog(SqlAlchemyConversationStore(db_uri), deployment, hosts)


@pytest_asyncio.fixture
async def client(catalog: SandboxProfileCatalog) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.state.sandbox_profiles = catalog
    app.include_router(
        create_sandbox_profiles_router(
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def error_handler(request, exc):
        return JSONResponse(status_code=exc.http_status, content={"detail": str(exc)})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def test_discovery_is_per_user_and_workspace_without_local_persistence(
    catalog: SandboxProfileCatalog, profiles: _Profiles
) -> None:
    assert catalog.managed_providers() == ["company"]
    for user_id, workspace_id, expected in (
        (_ALICE, 41, _BUILD),
        (_BOB, 41, _RESEARCH),
        (_ALICE, 42, _RESEARCH),
        (_ALICE, 41, _BUILD),
    ):
        with workspace_scope(workspace_id):
            rows, cursor = catalog.list_profiles(user_id=user_id)
            assert cursor is None
            assert len(rows) == 1
            assert rows[0]["id"] == expected.id
            assert rows[0]["revision"] == expected.revision
            assert set(rows[0]) == {"id", "name", "description", "revision", "provider"}
            assert "user_id" not in rows[0] and "workspace_id" not in rows[0]
            assert profiles.listings[-1] == SandboxProfileContext(
                user_id=user_id, workspace_id=workspace_id
            )


def test_empty_catalog_is_distinct_from_provider_outage(
    catalog: SandboxProfileCatalog, profiles: _Profiles
) -> None:
    with workspace_scope(41):
        assert catalog.list_profiles(user_id="outsider@example.test") == ([], None)
        profiles.list_error = RuntimeError(_SECRET)
        with pytest.raises(HTTPException) as failed:
            catalog.list_profiles(user_id=_ALICE)
    assert failed.value.status_code == 503
    assert _SECRET not in str(failed.value.detail)
    assert catalog.managed_providers() == ["company"]


@pytest.mark.parametrize("provider", [None, "", " "])
def test_managed_profiles_reject_unnamed_embedding_configuration(
    catalog: SandboxProfileCatalog, profiles: _Profiles, provider: str | None
) -> None:
    deployment = catalog._deployment
    assert deployment is not None
    catalog._deployment = ManagedSandboxDeployment.single(
        replace(deployment.default, provider=provider)
    )
    with workspace_scope(41):
        for operation in (
            catalog.managed_providers,
            lambda: catalog.list_profiles(user_id=_ALICE),
            lambda: catalog.require_selection(None, user_id=_ALICE),
            lambda: catalog.resolve("build", user_id=_ALICE),
        ):
            with pytest.raises(HTTPException) as invalid:
                operation()
            assert invalid.value.status_code == 422
            assert "provider name" in invalid.value.detail
    assert profiles.listings == []
    assert profiles.selections == []


def test_unnamed_legacy_provider_keeps_default_launches(catalog: SandboxProfileCatalog) -> None:
    deployment = catalog._deployment
    assert deployment is not None
    catalog._deployment = ManagedSandboxDeployment.single(
        replace(
            deployment.default,
            provider=None,
            launcher_factory=FakeSandboxLauncher,
            manages_profiles=False,
        )
    )
    assert catalog.managed_providers() == []
    catalog.require_selection(None)


@pytest.mark.parametrize("method", ["list_profiles", "select_profile"])
@pytest.mark.parametrize(
    ("error_type", "status"),
    [
        (SandboxProfileAccessDenied, 403),
        (SandboxProfileChanged, 409),
        (OwnerCredentialMissingError, 409),
        (RuntimeError, 503),
        (TimeoutError, 503),
    ],
)
def test_provider_failures_are_translated_and_sanitized(
    catalog: SandboxProfileCatalog,
    profiles: _Profiles,
    method: str,
    error_type: type[Exception],
    status: int,
) -> None:
    setattr(
        profiles,
        "list_error" if method == "list_profiles" else "select_error",
        error_type(_SECRET),
    )
    with workspace_scope(41), pytest.raises(HTTPException) as failed:
        if method == "list_profiles":
            catalog.list_profiles(user_id=_ALICE)
        else:
            catalog.resolve("build", provider="company", user_id=_ALICE)
    assert failed.value.status_code == status
    assert _SECRET not in str(failed.value.detail)


@pytest.mark.parametrize(
    "rows",
    [
        None,
        [object()],
        [_BUILD, _BUILD],
        [_BUILD, {"id": "secret", "token": _SECRET}],
        [_BUILD.model_copy(update={"id": f"profile-{index}"}) for index in range(1001)],
    ],
    ids=["not-a-list", "malformed-row", "duplicate-id", "invalid-after-valid", "oversized"],
)
def test_malformed_provider_catalog_never_returns_partial_results(
    catalog: SandboxProfileCatalog, monkeypatch: pytest.MonkeyPatch, rows: object
) -> None:
    monkeypatch.setattr(_Provider, "list_profiles", lambda self, context: rows)
    with workspace_scope(41), pytest.raises(HTTPException) as failed:
        catalog.list_profiles(user_id=_ALICE)
    assert failed.value.status_code == 503
    assert _SECRET not in str(failed.value.detail)


def test_selection_requires_identity_and_reauthorizes_the_exact_revision(
    catalog: SandboxProfileCatalog, profiles: _Profiles
) -> None:
    with workspace_scope(41):
        with pytest.raises(HTTPException) as unauthenticated:
            catalog.resolve("build", provider="company")
        assert unauthenticated.value.status_code == 401
        assert profiles.selections == []
        snapshot = catalog.resolve("build", _BUILD.revision, provider="company", user_id=_ALICE)
        assert profiles.selections[-1] == (
            SandboxProfileContext(user_id=_ALICE, workspace_id=41),
            _BUILD,
        )
        assert "config" not in snapshot
        assert snapshot["user_id"] == _ALICE
        assert snapshot["workspace_id"] == 41
        with pytest.raises(HTTPException) as stale:
            catalog.resolve("build", "retired-revision", provider="company", user_id=_ALICE)
        assert stale.value.status_code == 409
        for profile_id in ("build", "guessed"):
            with pytest.raises(HTTPException) as forbidden:
                catalog.resolve(profile_id, provider="company", user_id=_BOB)
            assert forbidden.value.status_code == 403


def test_saved_reference_reauthorizes_after_restart_and_revocation(
    catalog: SandboxProfileCatalog, profiles: _Profiles
) -> None:
    session_id, host_id = uuid.uuid4().hex, uuid.uuid4().hex
    with workspace_scope(41):
        snapshot = catalog.resolve("build", provider="company", user_id=_ALICE)
        _accept(catalog, session_id, snapshot)
        _bind(catalog, session_id, host_id)
        restarted = SandboxProfileCatalog(
            catalog._store.conversations, catalog._deployment, catalog._host_store
        )
        profiles.selections.clear()
        profiles.listings.clear()
        profiles.list_error = RuntimeError("Discovery is unavailable; selection is authoritative")
        assert (
            restarted.validate_snapshot(restarted.get_session_snapshot(session_id), user_id=_ALICE)
            == snapshot
        )
        assert profiles.selections == [
            (SandboxProfileContext(user_id=_ALICE, workspace_id=41), _BUILD)
        ]
        assert restarted.get_host_snapshot(host_id) == snapshot
        profiles.allowed[(_ALICE, 41)] = []
        with pytest.raises(HTTPException) as denied:
            restarted.validate_snapshot(snapshot, user_id=_ALICE)
        assert denied.value.status_code == 403
        assert restarted.get_session_snapshot(session_id) == snapshot
        assert profiles.listings == []


def test_inherited_reference_uses_new_caller_without_mutating_source(
    catalog: SandboxProfileCatalog, profiles: _Profiles
) -> None:
    with workspace_scope(41):
        snapshot = catalog.resolve("build", provider="company", user_id=_ALICE)
        with pytest.raises(HTTPException) as denied:
            catalog.inherit_snapshot(snapshot, _BOB)
        assert denied.value.status_code == 403
        profiles.allowed[(_BOB, 41)] = [_BUILD]
        inherited = catalog.inherit_snapshot(snapshot, _BOB)
        assert inherited["user_id"] == _BOB
        assert inherited["revision"] == snapshot["revision"]
        assert snapshot["user_id"] == _ALICE
        assert profiles.selections[-1] == (
            SandboxProfileContext(user_id=_BOB, workspace_id=41),
            _BUILD,
        )


def test_profile_ids_and_pagination_are_scoped_to_the_provider(
    catalog: SandboxProfileCatalog,
) -> None:
    deployment = catalog._deployment
    assert deployment is not None
    catalog._deployment = replace(
        deployment,
        configs=(*deployment.configs, replace(deployment.default, provider="second-company")),
    )
    with workspace_scope(41):
        first, cursor = catalog.list_profiles(user_id=_ALICE, limit=1)
        assert [(row["provider"], row["id"]) for row in first] == [("company", "build")]
        assert cursor is not None
        second, final_cursor = catalog.list_profiles(user_id=_ALICE, limit=1, cursor=cursor)
        assert [(row["provider"], row["id"]) for row in second] == [("second-company", "build")]
        assert final_cursor is None
        assert catalog.resolve("build", user_id=_ALICE)["provider"] == "company"
        assert (
            catalog.resolve("build", provider="second-company", user_id=_ALICE)["provider"]
            == "second-company"
        )
        assert catalog.list_profiles(user_id=_ALICE, provider="unknown") == ([], None)
        for invalid_cursor in ("{", "{}", '["company"]', '["company", 42]'):
            with pytest.raises(HTTPException) as invalid:
                catalog.list_profiles(user_id=_ALICE, cursor=invalid_cursor)
            assert invalid.value.status_code == 422


def test_snapshot_cannot_cross_workspace_boundaries(
    catalog: SandboxProfileCatalog, profiles: _Profiles
) -> None:
    with workspace_scope(41):
        snapshot = catalog.resolve("build", provider="company", user_id=_ALICE)
    profiles.selections.clear()
    with workspace_scope(42), pytest.raises(HTTPException) as denied:
        catalog.validate_snapshot(snapshot, user_id=_ALICE)
    assert denied.value.status_code == 403
    assert profiles.selections == []


@pytest.mark.parametrize("allowed", [False, True])
def test_unprofiled_managed_host_cannot_bypass_required_selection(
    catalog: SandboxProfileCatalog, profiles: _Profiles, allowed: bool
) -> None:
    session_id, host_id = uuid.uuid4().hex, uuid.uuid4().hex
    if not allowed:
        profiles.allowed.clear()
    with workspace_scope(41), pytest.raises(HTTPException) as denied:
        _require(catalog, session_id, host_id, sandbox_provider="company", user_id=_ALICE)
    assert denied.value.status_code == 422
    assert catalog.get_session_snapshot(session_id) is None
    assert catalog.get_host_snapshot(host_id) is None


def test_host_reuse_checks_saved_identity_and_current_access(
    catalog: SandboxProfileCatalog, profiles: _Profiles
) -> None:
    source_id, target_id, host_id = (uuid.uuid4().hex for _ in range(3))
    with workspace_scope(41):
        snapshot = catalog.resolve("build", provider="company", user_id=_ALICE)
        _accept(catalog, source_id, snapshot)
        _bind(catalog, source_id, host_id)
        profiles.allowed[(_BOB, 41)] = [_BUILD]
        with pytest.raises(HTTPException) as mismatched_owner:
            _require(catalog, target_id, host_id, sandbox_provider="company", user_id=_BOB)
        assert mismatched_owner.value.status_code == 403
        assert catalog.get_session_snapshot(target_id) is None
        profiles.allowed[(_ALICE, 41)] = []
        with pytest.raises(HTTPException) as revoked:
            _require(catalog, target_id, host_id, sandbox_provider="company", user_id=_ALICE)
        assert revoked.value.status_code == 403
        assert catalog.get_session_snapshot(target_id) is None
        profiles.allowed[(_ALICE, 41)] = [_BUILD]
        _require(catalog, target_id, host_id, sandbox_provider="company", user_id=_ALICE)
        assert catalog.get_session_snapshot(target_id) == snapshot


async def test_api_uses_authenticated_identity_and_hides_execution_identity(
    client: AsyncClient, profiles: _Profiles
) -> None:
    response = await client.get("/v1/sandbox-profiles?user_id=alice@example.test")
    assert response.status_code == 401
    assert profiles.listings == []
    with workspace_scope(41):
        for user_id, expected_id in ((_ALICE, "build"), (_BOB, "research")):
            response = await client.get(
                "/v1/sandbox-profiles?user_id=outsider&workspace_id=42",
                headers={"X-Forwarded-Email": user_id},
            )
            assert response.status_code == 200, response.text
            assert response.headers["Cache-Control"] == "private, no-store"
            body = response.json()
            assert body["managed_providers"] == ["company"]
            assert "can_manage" not in body
            assert [row["id"] for row in body["profiles"]] == [expected_id]
            assert "user_id" not in response.text and "workspace_id" not in response.text
            assert _ALICE not in response.text and _BOB not in response.text
            assert profiles.listings[-1] == SandboxProfileContext(user_id=user_id, workspace_id=41)

        profiles.allowed.clear()
        response = await client.get("/v1/sandbox-profiles", headers={"X-Forwarded-Email": _ALICE})
        assert response.status_code == 200
        assert response.json()["profiles"] == []
        assert response.json()["managed_providers"] == ["company"]
        profiles.list_error = RuntimeError(_SECRET)
        response = await client.get("/v1/sandbox-profiles", headers={"X-Forwarded-Email": _ALICE})
        assert response.status_code == 503
        assert _SECRET not in response.text


@pytest.mark.parametrize("operation", ["create", "edit", "disable"])
async def test_admin_cannot_mutate_provider_catalog(
    client: AsyncClient, catalog: SandboxProfileCatalog, operation: str
) -> None:
    profile_id = "new" if operation == "create" else "build"
    headers = {"X-Forwarded-Email": _ALICE}
    with workspace_scope(41):
        if operation == "disable":
            response = await client.delete(f"/v1/sandbox-profiles/{profile_id}", headers=headers)
        else:
            response = await client.put(
                f"/v1/sandbox-profiles/{profile_id}", json=_WRITE, headers=headers
            )
        assert response.status_code in (404, 405), response.text
        assert (
            catalog.resolve("build", provider="company", user_id=_ALICE)["revision"]
            == _BUILD.revision
        )


def test_catalog_and_bindings_are_workspace_scoped(
    catalog: SandboxProfileCatalog, profiles: _Profiles
) -> None:
    session_id, host_id = uuid.uuid4().hex, uuid.uuid4().hex
    with workspace_scope(41):
        profile = catalog.resolve("build", user_id=_ALICE)
        _accept(catalog, session_id, profile)
        _bind(catalog, session_id, host_id)
    with workspace_scope(42):
        assert catalog.list_profiles(user_id="outsider") == ([], None)
        assert catalog.get_session_snapshot(session_id) is None
        assert catalog.get_host_snapshot(host_id) is None
        with pytest.raises(HTTPException, match="403"):
            catalog.resolve("build", user_id="outsider")


def test_host_preflight_does_not_create_or_change_bindings(
    catalog: SandboxProfileCatalog,
    profiles: _Profiles,
    db_uri: str,
) -> None:
    profiles.allowed[(_ALICE, 0)] = [_BUILD]
    profile = catalog.resolve("build", user_id=_ALICE)
    source_id, target_id = uuid.uuid4().hex, uuid.uuid4().hex
    host_id, other_host_id = uuid.uuid4().hex, uuid.uuid4().hex
    hosts = HostStore(db_uri)
    for bound_host in (host_id, other_host_id):
        hosts.upsert_on_connect(bound_host, f"profile-{bound_host}", _ALICE)
    _accept(catalog, source_id, profile)
    _bind(catalog, source_id, host_id)
    _require(catalog, target_id, host_id, bind=False)
    assert catalog.get_session_snapshot(target_id) is None
    target_profile = {**profile, "name": "Renamed", "description": "Updated description"}
    _accept(catalog, target_id, target_profile)
    _bind(catalog, target_id, other_host_id)
    with pytest.raises(HTTPException, match="409"):
        _require(catalog, target_id, host_id, bind=False)
    assert catalog.get_session_snapshot(target_id) == target_profile
    assert catalog.get_host_snapshot(other_host_id) == target_profile
    hosts.delete_host(other_host_id)
    _require(catalog, target_id, host_id, bind=False)
    assert catalog._store.get_session_host_id(target_id) is None
    profiles.allowed.clear()
    with pytest.raises(HTTPException, match="403"):
        _require(catalog, target_id, host_id, bind=False)
    assert catalog._store.get_session_host_id(target_id) is None
    profiles.allowed[(_ALICE, 0)] = [_BUILD]
    _require(catalog, target_id, host_id)
    assert catalog._store.get_session_host_id(target_id) == host_id
    assert catalog.get_session_snapshot(target_id) == target_profile


def test_host_binding_cannot_lose_original_host_profile(
    catalog: SandboxProfileCatalog,
    profiles: _Profiles,
    db_uri: str,
) -> None:
    profiles.allowed[(_ALICE, 0)] = [_BUILD]
    profile = catalog.resolve("build", user_id=_ALICE)
    source_id, target_id = uuid.uuid4().hex, uuid.uuid4().hex
    host_id, other_host_id = uuid.uuid4().hex, uuid.uuid4().hex
    hosts = HostStore(db_uri)
    for session_id, bound_host in ((source_id, host_id), (target_id, other_host_id)):
        hosts.upsert_on_connect(bound_host, f"profile-{bound_host}", _ALICE)
        hosts.set_offline(bound_host)
        _accept(catalog, session_id, profile)
        _bind(catalog, session_id, bound_host)
        _bind(catalog, session_id, f"host_{bound_host}")
        for supplied_host in (bound_host, f"host_{bound_host}", str(uuid.UUID(bound_host))):
            catalog.require_session_host(session_id, supplied_host, bind=False, user_id=_ALICE)
    for bind in (False, True):
        with pytest.raises(HTTPException, match="different host"):
            _require(catalog, source_id, other_host_id, bind=bind)
    with pytest.raises(ValueError, match="session binding changed"):
        _bind(catalog, source_id, other_host_id)
    assert catalog.get_host_snapshot(host_id) == profile
    assert catalog.get_host_snapshot(other_host_id) == profile
    profiles.allowed.clear()
    with pytest.raises(HTTPException, match="access denied"):
        _require(catalog, source_id, host_id, bind=False)


def test_ordinary_host_transfer_remains_available_with_profiles_enabled(catalog):
    source_host, target_host = uuid.uuid4().hex, uuid.uuid4().hex
    for host_id in (source_host, target_host):
        catalog._host_store.upsert_on_connect(host_id, "ordinary-host", _ALICE)
    conversation = catalog._store.conversations.create_conversation(
        host_id=source_host, workspace="/workspace"
    )
    catalog.require_session_host(conversation.id, target_host, bind=False, user_id=_ALICE)
    catalog.require_session_host(conversation.id, target_host, user_id=_ALICE)
    assert catalog.get_session_snapshot(conversation.id) is None


def test_ordinary_runner_binding_skips_profile_history_limits(catalog, monkeypatch):
    from unittest.mock import Mock

    deployment = catalog._deployment
    factory = Mock(side_effect=FakeSandboxLauncher)
    catalog._deployment = replace(
        deployment,
        configs=(replace(deployment.default, launcher_factory=factory, manages_profiles=False),),
    )
    store = catalog._store.conversations
    conversation = store.create_conversation()
    discovery = Mock(side_effect=AssertionError("Ordinary runners need no profile lookup"))
    monkeypatch.setattr(store, "get_runner_host_ids", discovery)
    for _ in range(2):
        assert not catalog.reassign_runner(
            conversation,
            "ordinary-runner",
            conversation_store=store,
            host_store=catalog._host_store,
            user_id=_ALICE,
        )
    discovery.assert_not_called()
    factory.assert_not_called()


@pytest.mark.parametrize("managed_host", [False, True])
def test_runner_binding_checks_only_its_host_after_unrelated_factory_failure(
    catalog, managed_host
):
    from unittest.mock import Mock

    deployment = catalog._deployment
    catalog._deployment = replace(
        deployment,
        configs=(replace(deployment.default, launcher_factory=Mock(side_effect=RuntimeError)),),
    )
    host_id = uuid.uuid4().hex
    if managed_host:
        catalog._host_store.register_managed_host(
            host_id=host_id,
            name="managed-host",
            user_id=_ALICE,
            token="test-token",
            token_expires_at=2_000_000_000,
            provider=deployment.default.provider,
            sandbox_id="sandbox",
        )
    else:
        catalog._host_store.upsert_on_connect(host_id, "ordinary-host", _ALICE)
    store = catalog._store.conversations
    store.create_conversation(host_id=host_id, workspace="/workspace", runner_id="runner")
    target = store.create_conversation()
    # Reconnecting uses the saved binding even if a provider cannot construct.
    assert catalog.admit_runner_connection("runner")
    catalog._deployment.default.launcher_factory.assert_not_called()

    def bind():
        return catalog.reassign_runner(
            target,
            "runner",
            conversation_store=store,
            host_store=catalog._host_store,
            user_id=_ALICE,
        )

    if managed_host:
        with pytest.raises(HTTPException) as unavailable:
            bind()
        assert unavailable.value.status_code == 503
    else:
        assert bind() is False


def test_unrelated_provider_outage_does_not_block_default_launches(catalog, profiles):
    deployment = catalog._deployment
    assert deployment is not None
    healthy = _Profiles()
    catalog._deployment = replace(
        deployment,
        configs=(
            *deployment.configs,
            replace(
                deployment.default,
                provider="ordinary",
                launcher_factory=FakeSandboxLauncher,
                manages_profiles=False,
            ),
            replace(
                deployment.default,
                provider="healthy",
                launcher_factory=lambda: _Provider(healthy),
            ),
        ),
    )
    profiles.list_error = RuntimeError(_SECRET)
    assert catalog.list_profiles(provider="ordinary", user_id=_ALICE) == ([], None)
    assert catalog.managed_providers("ordinary") == []
    catalog.require_selection("ordinary", user_id=_ALICE)
    assert profiles.listings == []
    with pytest.raises(HTTPException) as invalid:
        catalog.resolve("build", provider="ordinary", user_id=_ALICE)
    assert invalid.value.status_code == 422
    with workspace_scope(41):
        rows, cursor = catalog.list_profiles(provider="healthy", user_id=_ALICE)
        assert [row["id"] for row in rows] == ["build"]
        assert all(row["provider"] == "healthy" for row in rows)
        assert cursor is None
        assert catalog.managed_providers("healthy") == ["healthy"]
    assert profiles.listings == []


@pytest.mark.parametrize("field", ["config", "users", "credentials"])
def test_saved_references_reject_private_provider_fields(catalog, field):
    with workspace_scope(41):
        snapshot = catalog.resolve("build", user_id=_ALICE)
        with pytest.raises(HTTPException) as invalid:
            _accept(catalog, uuid.uuid4().hex, {**snapshot, field: _SECRET})
        assert invalid.value.status_code == 422
        assert _SECRET not in str(invalid.value.detail)


def test_session_reference_cannot_use_a_different_execution_identity(catalog, profiles):
    source_id, target_id, host_id = (uuid.uuid4().hex for _ in range(3))
    with workspace_scope(41):
        snapshot = catalog.resolve("build", user_id=_ALICE)
        _accept(catalog, source_id, snapshot)
        _bind(catalog, source_id, host_id)
        profiles.allowed[(_BOB, 41)] = [_BUILD]
        _accept(catalog, target_id, catalog.inherit_snapshot(snapshot, _BOB))
        with pytest.raises(HTTPException) as mismatch:
            _require(catalog, target_id, host_id, user_id=_ALICE)
        assert mismatch.value.status_code == 403
        assert catalog._store.get_session_host_id(target_id) is None

"""Provider catalogs remain scoped, read-only, and authoritative at admission."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import ClassVar
from unittest.mock import Mock

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from omnigent.db.db_models import workspace_scope
from omnigent.errors import OmnigentError
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

    def apply_profile(self, config: dict[str, object]) -> None:
        raise AssertionError("Managed profiles must be selected through the provider")


@pytest.fixture
def profiles() -> _Profiles:
    return _Profiles()


@pytest.fixture
def catalog(db_uri: str, profiles: _Profiles) -> SandboxProfileCatalog:
    hosts = HostStore(db_uri)
    deployment = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://managed.example.test",
            token_ttl_s=100,
            provider="company",
            launcher_factory=lambda: _Provider(profiles),
        )
    )
    return SandboxProfileCatalog(hosts._engine, deployment)


@pytest_asyncio.fixture
async def client(catalog: SandboxProfileCatalog) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.state.sandbox_profiles = catalog
    permissions = Mock()
    permissions.is_admin.side_effect = lambda user_id: user_id == _ALICE
    app.include_router(
        create_sandbox_profiles_router(
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
            permission_store=permissions,
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
    assert catalog.providers() == []
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
            assert rows[0]["managed"] is True
            assert rows[0]["config"] == {}
            assert "user_id" not in rows[0] and "workspace_id" not in rows[0]
            assert catalog._store.list_profiles(include_disabled=True) == ([], None)
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
        replace(deployment.default, provider=None, launcher_factory=FakeSandboxLauncher)
    )
    assert catalog.managed_providers() == []
    catalog.require_selection(None)


@pytest.mark.parametrize("method", ["list_profiles", "select_profile"])
@pytest.mark.parametrize(
    ("error_type", "status"),
    [
        (SandboxProfileAccessDenied, 403),
        (SandboxProfileChanged, 409),
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
    assert catalog._store.list_profiles(include_disabled=True) == ([], None)


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
    assert catalog._store.list_profiles(include_disabled=True) == ([], None)


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
        assert snapshot["managed"] is True
        assert snapshot["config"] == {}
        assert snapshot["user_id"] == _ALICE
        assert snapshot["workspace_id"] == 41
        with pytest.raises(HTTPException) as stale:
            catalog.resolve("build", "retired-revision", provider="company", user_id=_ALICE)
        assert stale.value.status_code == 409
        for profile_id in ("build", "guessed"):
            with pytest.raises(HTTPException) as forbidden:
                catalog.resolve(profile_id, provider="company", user_id=_BOB)
            assert forbidden.value.status_code == 403
        assert catalog._store.list_profiles(include_disabled=True) == ([], None)


def test_saved_reference_reauthorizes_after_restart_and_revocation(
    catalog: SandboxProfileCatalog, profiles: _Profiles
) -> None:
    session_id, host_id = uuid.uuid4().hex, uuid.uuid4().hex
    with workspace_scope(41):
        snapshot = catalog.resolve("build", provider="company", user_id=_ALICE)
        catalog.bind_session(session_id, snapshot)
        catalog.bind_host(session_id, host_id)
        restarted = SandboxProfileCatalog(catalog._store._session.engine, catalog._deployment)
        profiles.selections.clear()
        profiles.listings.clear()
        profiles.list_error = RuntimeError("Discovery is unavailable; selection is authoritative")
        assert restarted.validate_snapshot(restarted.get_session_snapshot(session_id)) == snapshot
        assert profiles.selections == [
            (SandboxProfileContext(user_id=_ALICE, workspace_id=41), _BUILD)
        ]
        assert restarted.get_host_snapshot(host_id) == snapshot
        profiles.allowed[(_ALICE, 41)] = []
        with pytest.raises(HTTPException) as denied:
            restarted.validate_snapshot(snapshot)
        assert denied.value.status_code == 403
        assert restarted.get_session_snapshot(session_id) == snapshot
        assert restarted._store.list_profiles(include_disabled=True) == ([], None)
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


def test_snapshot_cannot_cross_workspace_boundaries(
    catalog: SandboxProfileCatalog, profiles: _Profiles
) -> None:
    with workspace_scope(41):
        snapshot = catalog.resolve("build", provider="company", user_id=_ALICE)
    profiles.selections.clear()
    with workspace_scope(42), pytest.raises(HTTPException) as denied:
        catalog.validate_snapshot(snapshot)
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
        catalog.require_session_host(
            session_id, host_id, sandbox_provider="company", user_id=_ALICE
        )
    assert denied.value.status_code == 422
    assert catalog.get_session_snapshot(session_id) is None
    assert catalog.get_host_snapshot(host_id) is None


def test_host_reuse_checks_saved_identity_and_current_access(
    catalog: SandboxProfileCatalog, profiles: _Profiles
) -> None:
    source_id, target_id, host_id = (uuid.uuid4().hex for _ in range(3))
    with workspace_scope(41):
        snapshot = catalog.resolve("build", provider="company", user_id=_ALICE)
        catalog.bind_session(source_id, snapshot)
        catalog.bind_host(source_id, host_id)
        profiles.allowed[(_BOB, 41)] = [_BUILD]
        with pytest.raises(HTTPException) as mismatched_owner:
            catalog.require_session_host(
                target_id, host_id, sandbox_provider="company", user_id=_BOB
            )
        assert mismatched_owner.value.status_code == 403
        assert catalog.get_session_snapshot(target_id) is None
        profiles.allowed[(_ALICE, 41)] = []
        with pytest.raises(HTTPException) as revoked:
            catalog.require_session_host(
                target_id, host_id, sandbox_provider="company", user_id=_ALICE
            )
        assert revoked.value.status_code == 403
        assert catalog.get_session_snapshot(target_id) is None
        profiles.allowed[(_ALICE, 41)] = [_BUILD]
        catalog.require_session_host(
            target_id, host_id, sandbox_provider="company", user_id=_ALICE
        )
        assert catalog.get_session_snapshot(target_id) == snapshot


def test_old_local_row_cannot_override_provider_catalog_or_bypass_denial(
    catalog: SandboxProfileCatalog, profiles: _Profiles
) -> None:
    deployment = catalog._deployment
    assert deployment is not None
    catalog._deployment = ManagedSandboxDeployment.single(
        replace(deployment.default, provider="kubernetes")
    )
    with workspace_scope(41):
        old_local = catalog._store.put(
            {
                "id": "build",
                "name": "Old local build",
                "description": "",
                "provider": "kubernetes",
                "config": {"image": "example.test/host:old-local"},
                "revision": "a" * 64,
                "enabled": True,
            }
        )
        rows, _ = catalog.list_profiles(user_id=_ALICE, include_disabled=True)
        assert len(rows) == 1
        assert rows[0]["managed"] is True and rows[0]["config"] == {}
        assert rows[0]["revision"] == _BUILD.revision
        with pytest.raises(HTTPException) as read_only:
            catalog.disable("build")
        assert read_only.value.status_code == 403
        profiles.allowed.clear()
        assert catalog.list_profiles(user_id=_ALICE) == ([], None)
        with pytest.raises(HTTPException) as denied:
            catalog.resolve("build", provider="kubernetes", user_id=_ALICE)
        assert denied.value.status_code == 403
        profiles.list_error = RuntimeError(_SECRET)
        with pytest.raises(HTTPException) as unavailable:
            catalog.resolve("build", provider="kubernetes", user_id=_ALICE)
        assert unavailable.value.status_code == 503
        assert catalog._store.get("build") == old_local


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
            assert body["can_manage"] is False
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
        assert response.status_code == (404 if operation == "disable" else 403), response.text
        assert catalog._store.list_profiles(include_disabled=True) == ([], None)
        assert (
            catalog.resolve("build", provider="company", user_id=_ALICE)["revision"]
            == _BUILD.revision
        )

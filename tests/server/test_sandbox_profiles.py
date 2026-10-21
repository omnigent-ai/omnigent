"""Profile admission remains authorized, workspace-scoped, and stable across restarts."""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, inspect, text

from omnigent.db.db_models import workspace_scope
from omnigent.errors import OmnigentError
from omnigent.onboarding.sandboxes.agent_sandbox_warm_pool import AgentSandboxWarmPoolLauncher
from omnigent.onboarding.sandboxes.kubernetes import KubernetesSandboxLauncher
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.server.routes.sandbox_profiles import create_sandbox_profiles_router
from omnigent.server.sandbox_profiles import SandboxProfileCatalog

_BODY = {
    "name": "Build sandbox",
    "description": "A larger sandbox for builds",
    "provider": "kubernetes",
    "config": {"resources": {"requests": {"cpu": "2", "memory": "4Gi"}}},
    "enabled": True,
}


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[SandboxProfileCatalog]:
    engine = create_engine(f"sqlite:///{tmp_path / 'profiles.db'}")
    migration = importlib.import_module(
        "omnigent.db.migrations.versions.nn1a2b3c4d5e_add_sandbox_profiles"
    )
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    deployment = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="http://test",
            token_ttl_s=100,
            provider="kubernetes",
            launcher_factory=KubernetesSandboxLauncher,
        )
    )
    yield SandboxProfileCatalog(engine, deployment)
    engine.dispose()


def test_profile_snapshot_survives_restart_and_profile_edits(
    catalog: SandboxProfileCatalog,
) -> None:
    profile = catalog.put("build", _BODY)
    assert profile == catalog.put("build", _BODY)
    session_id, host_id = uuid.uuid4().hex, uuid.uuid4().hex
    catalog.bind_session(session_id, profile)
    catalog.bind_host(session_id, host_id)
    changed = catalog.put("build", {**_BODY, "config": {"image": "example.test/host:new"}})
    assert changed["revision"] != profile["revision"]
    with pytest.raises(HTTPException, match="409"):
        catalog.resolve("build", profile["revision"])
    restarted = SandboxProfileCatalog(catalog._store._session.engine, catalog._deployment)
    assert restarted.get_session_snapshot(session_id) == profile
    assert restarted.get_host_snapshot(host_id) == profile
    assert restarted.validate_snapshot(profile) == profile
    child_id = uuid.uuid4().hex
    restarted.require_session_host(child_id, host_id)
    assert restarted.get_session_snapshot(child_id) == profile
    with pytest.raises(HTTPException, match="409"):
        restarted.require_session_host(session_id, uuid.uuid4().hex)
    restarted.disable("build")
    with pytest.raises(HTTPException, match="409"):
        restarted.validate_snapshot(profile)


def test_catalog_and_bindings_are_workspace_scoped(catalog: SandboxProfileCatalog) -> None:
    session_id, host_id = uuid.uuid4().hex, uuid.uuid4().hex
    with workspace_scope(41):
        profile = catalog.put("build", _BODY)
        catalog.bind_session(session_id, profile)
        catalog.bind_host(session_id, host_id)
    with workspace_scope(42):
        assert catalog.list_profiles() == ([], None)
        assert catalog.get_session_snapshot(session_id) is None
        assert catalog.get_host_snapshot(host_id) is None
        with pytest.raises(HTTPException, match="404"):
            catalog.resolve("build")


def test_host_preflight_does_not_create_or_change_bindings(
    catalog: SandboxProfileCatalog,
) -> None:
    profile = catalog.put("build", _BODY)
    source_id, target_id = uuid.uuid4().hex, uuid.uuid4().hex
    host_id, other_host_id = uuid.uuid4().hex, uuid.uuid4().hex
    catalog.bind_session(source_id, profile)
    catalog.bind_host(source_id, host_id)
    catalog.require_session_host(target_id, host_id, bind=False)
    assert catalog.get_session_snapshot(target_id) is None
    catalog.bind_session(target_id, profile)
    catalog.bind_host(target_id, other_host_id)
    with pytest.raises(HTTPException, match="409"):
        catalog.require_session_host(target_id, host_id, bind=False)
    assert catalog.get_session_snapshot(target_id) == profile
    assert catalog.get_host_snapshot(other_host_id) == profile
    catalog.disable("build")
    with pytest.raises(HTTPException, match="409"):
        catalog.require_session_host(target_id, host_id, bind=False)


def test_host_binding_cannot_lose_original_host_profile(
    catalog: SandboxProfileCatalog,
) -> None:
    profile = catalog.put("build", _BODY)
    source_id, target_id = uuid.uuid4().hex, uuid.uuid4().hex
    host_id, other_host_id = uuid.uuid4().hex, uuid.uuid4().hex
    for session_id, bound_host in ((source_id, host_id), (target_id, other_host_id)):
        catalog.bind_session(session_id, profile)
        catalog.bind_host(session_id, bound_host)
        catalog.bind_host(session_id, f"host_{bound_host}")
        catalog.require_session_host(session_id, f"host_{bound_host}", bind=False)
    for bind in (False, True):
        with pytest.raises(HTTPException, match="different host"):
            catalog.require_session_host(source_id, other_host_id, bind=bind)
    with pytest.raises(HTTPException, match="different host"):
        catalog.bind_host(source_id, other_host_id)
    assert catalog.get_host_snapshot(host_id) == profile
    assert catalog.get_host_snapshot(other_host_id) == profile
    catalog.disable("build")
    with pytest.raises(HTTPException, match="no longer enabled"):
        catalog.require_session_host(source_id, host_id, bind=False)


def test_pagination_uses_indexes_and_disable_preserves_history(
    catalog: SandboxProfileCatalog,
) -> None:
    for profile_id in ("a", "b", "c"):
        catalog.put(profile_id, _BODY)
    catalog.disable("b")
    rows, cursor = catalog.list_profiles(limit=1)
    assert [row["id"] for row in rows] == ["a"]
    assert cursor == "a"
    rows, cursor = catalog.list_profiles(limit=1, cursor=cursor)
    assert [row["id"] for row in rows] == ["c"]
    assert cursor is None
    assert len(catalog.list_profiles(include_disabled=True)[0]) == 3
    engine = catalog._store._session.engine
    with engine.connect() as connection:
        plan = connection.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM sandbox_profiles "
                "WHERE workspace_id=0 AND enabled=1 AND id>'a' ORDER BY id LIMIT 2"
            )
        ).all()
        assert "ix_sandbox_profiles_enabled" in str(plan)
        plan = connection.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT snapshot_json FROM sandbox_profile_bindings "
                "WHERE workspace_id=0 AND host_id=x'00000000000000000000000000000000' "
                "ORDER BY session_id LIMIT 1"
            )
        ).all()
        assert "ix_sandbox_profile_bindings_host" in str(plan)
    assert {column["name"] for column in inspect(engine).get_columns("sandbox_profiles")} == {
        "workspace_id",
        "id",
        "name",
        "description",
        "provider",
        "config_json",
        "revision",
        "enabled",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"config": {"env": {"SECRET": "value"}}},
        {"config": {"resources": {"requests": {"memory": "nonsense"}}}},
        {"provider": "modal"},
        {"name": " "},
        {"config": {"image": "x" * 16384}},
    ],
)
def test_invalid_profiles_never_persist(catalog: SandboxProfileCatalog, changes: dict) -> None:
    with pytest.raises(HTTPException, match="422"):
        catalog.put("invalid", {**_BODY, **changes})
    assert catalog.list_profiles(include_disabled=True) == ([], None)


def test_warm_pool_rejected_before_persistence(catalog: SandboxProfileCatalog) -> None:
    catalog._deployment = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="http://test",
            token_ttl_s=100,
            provider="agent_sandbox",
            launcher_factory=lambda: AgentSandboxWarmPoolLauncher(warm_pool="warm"),
        )
    )
    assert catalog.providers() == []
    with pytest.raises(HTTPException, match="422"):
        catalog.put("pooled", {**_BODY, "provider": "agent_sandbox"})


@pytest.mark.asyncio
@pytest.mark.parametrize("store_available", [False, True])
async def test_api_requires_auth_and_admin_and_returns_paginated_catalog(
    catalog: SandboxProfileCatalog, store_available: bool
) -> None:
    app = FastAPI()
    app.state.sandbox_profiles = catalog
    auth = UnifiedAuthProvider(source="header", local_single_user=False)
    permissions = Mock()
    permissions.is_admin.side_effect = lambda user_id: user_id == "admin@example.com"
    app.include_router(
        create_sandbox_profiles_router(
            auth_provider=auth,
            permission_store=permissions if store_available else None,
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def error_handler(request, exc):
        return JSONResponse(status_code=exc.http_status, content={"detail": str(exc)})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/v1/sandbox-profiles")).status_code == 401
        headers = {"X-Forwarded-Email": "member@example.com"}
        response = await client.put("/v1/sandbox-profiles/build", json=_BODY, headers=headers)
        assert response.status_code == 403
        response = await client.get("/v1/sandbox-profiles", headers=headers)
        assert response.status_code == 200
        assert response.json()["can_manage"] is False
        headers = {"X-Forwarded-Email": "admin@example.com"}
        response = await client.put("/v1/sandbox-profiles/build", json=_BODY, headers=headers)
        if not store_available:
            assert response.status_code == 403
            return
        assert response.status_code == 200, response.text
        assert response.json()["id"] == "build"
        response = await client.get("/v1/sandbox-profiles?limit=1", headers=headers)
        assert response.json()["providers"] == ["kubernetes"]
        assert response.json()["next_cursor"] is None
        assert response.headers["Cache-Control"] == "private, no-store"
        response = await client.delete("/v1/sandbox-profiles/build", headers=headers)
        assert response.json()["enabled"] is False
        response = await client.get(
            "/v1/sandbox-profiles", headers={"X-Forwarded-Email": "member@example.com"}
        )
        assert response.json()["profiles"] == []

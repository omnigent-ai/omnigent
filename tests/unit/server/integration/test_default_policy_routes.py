"""Integration tests for the default policy CRUD routes.

Uses a real ``SqlAlchemyPolicyStore`` and
``SqlAlchemyPermissionStore`` so the full request -> store ->
response pipeline is exercised.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
from tests.unit.server.conftest import ControllableMockClient

pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """App with auth, permission, and default policy stores enabled.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Per-test SQLite URI.
    :param tmp_path: Pytest temp dir for artifacts.
    :returns: A :class:`FastAPI` instance with auth and default policy routes.
    """
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        policy_store=SqlAlchemyPolicyStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the auth-enabled app.

    :param auth_app: FastAPI app with permission and default policy stores.
    :param mock_llm: Controllable mock LLM — released on teardown.
    :param tmp_path: Pytest temp dir for the harness process manager.
    :yields: A ready-to-use :class:`httpx.AsyncClient`.
    """
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


def _admin_headers(email: str = "admin@example.com") -> dict[str, str]:
    """Return request headers simulating an authenticated user.

    :param email: The user email to present, e.g. ``"admin@example.com"``.
    :returns: Dict with ``X-Forwarded-Email`` header.
    """
    return {"X-Forwarded-Email": email}


def _make_admin(db_uri: str, email: str = "admin@example.com") -> None:
    """Seed the permission store with an admin user.

    :param db_uri: SQLite URI for the per-test database.
    :param email: Admin email to create, e.g. ``"admin@example.com"``.
    """
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(email, is_admin=True)


def _make_user(db_uri: str, email: str = "user@example.com") -> None:
    """Seed the permission store with a non-admin user.

    :param db_uri: SQLite URI for the per-test database.
    :param email: User email to create, e.g. ``"user@example.com"``.
    """
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(email, is_admin=False)


# ── CRUD happy path ──────────────────────────────────────────────────────────


async def test_create_default_policy(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """POST /v1/policies creates and returns the policy."""
    _make_admin(db_uri)
    resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "block_push",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=_admin_headers(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "default_policy"
    assert body["name"] == "block_push"
    assert body["type"] == "python"
    assert body["handler"] == "omnigent.policies.builtins.safety.ask_on_os_tools"
    assert body["enabled"] is True
    assert body["id"].startswith("pol_")
    assert body["created_by"] == "admin@example.com"


async def test_admin_create_unregistered_handler_rejected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Even an admin cannot create a default policy with an unregistered
    handler.

    Admins are not exempt from the registry allowlist: a custom handler
    must be added via the server's ``policy_modules`` config so it appears
    in the registry, rather than naming an arbitrary callable here. This
    keeps a single allowlist and closes the admin-side injection path.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    _make_admin(db_uri)
    resp = await auth_client.post(
        "/v1/policies",
        json={"name": "rce", "type": "python", "handler": "subprocess.Popen"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 400
    assert "not registered" in resp.json()["error"]["message"]


async def test_list_default_policies(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET /v1/policies returns all default policies."""
    _make_admin(db_uri)
    headers = _admin_headers()

    # Create two policies.
    await auth_client.post(
        "/v1/policies",
        json={
            "name": "first",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    await auth_client.post(
        "/v1/policies",
        json={
            "name": "second",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )

    resp = await auth_client.get("/v1/policies", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 2
    names = {p["name"] for p in body["data"]}
    assert names == {"first", "second"}


async def test_get_default_policy(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET /v1/policies/{id} returns a single policy."""
    _make_admin(db_uri)
    headers = _admin_headers()

    create_resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "get_test",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    resp = await auth_client.get(f"/v1/policies/{policy_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "get_test"


async def test_update_default_policy(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """PATCH /v1/policies/{id} updates mutable fields."""
    _make_admin(db_uri)
    headers = _admin_headers()

    create_resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "updatable",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    resp = await auth_client.patch(
        f"/v1/policies/{policy_id}",
        json={"name": "renamed", "enabled": False},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "renamed"
    assert body["enabled"] is False


async def test_admin_update_to_unregistered_handler_rejected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """PATCH cannot point a default policy at an unregistered handler.

    The PATCH path is a second write surface; like create, it must enforce
    the registry allowlist so it is not a back door to arbitrary callable
    injection.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    _make_admin(db_uri)
    headers = _admin_headers()
    create_resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "patchable",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    resp = await auth_client.patch(
        f"/v1/policies/{policy_id}",
        json={"handler": "os.system"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "not registered" in resp.json()["error"]["message"]


async def test_delete_default_policy(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """DELETE /v1/policies/{id} removes the policy."""
    _make_admin(db_uri)
    headers = _admin_headers()

    create_resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "deletable",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    resp = await auth_client.delete(f"/v1/policies/{policy_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Verify it's gone.
    get_resp = await auth_client.get(f"/v1/policies/{policy_id}", headers=headers)
    assert get_resp.status_code == 404


# ── Error cases ───────────────────────────────────────────────────────────────


async def test_create_duplicate_name_returns_409(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """POST /v1/policies with a duplicate name returns 409."""
    _make_admin(db_uri)
    headers = _admin_headers()

    await auth_client.post(
        "/v1/policies",
        json={
            "name": "unique",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )

    resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "unique",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    assert resp.status_code == 409


async def test_get_nonexistent_returns_404(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET /v1/policies/{id} with a bad ID returns 404."""
    _make_admin(db_uri)
    resp = await auth_client.get(
        "/v1/policies/dpol_nonexistent",
        headers=_admin_headers(),
    )
    assert resp.status_code == 404


async def test_update_nonexistent_returns_404(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """PATCH /v1/policies/{id} with a bad ID returns 404."""
    _make_admin(db_uri)
    resp = await auth_client.patch(
        "/v1/policies/dpol_nonexistent",
        json={"name": "x"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 404


# ── Auth / permission tests ──────────────────────────────────────────────────


async def test_non_admin_cannot_create(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """POST /v1/policies returns 403 for non-admin users."""
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "blocked",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=_admin_headers("user@example.com"),
    )
    assert resp.status_code == 403


async def test_non_admin_cannot_update(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """PATCH /v1/policies/{id} returns 403 for non-admin users."""
    _make_admin(db_uri)
    _make_user(db_uri)
    headers = _admin_headers()

    create_resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "admin_only",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    resp = await auth_client.patch(
        f"/v1/policies/{policy_id}",
        json={"enabled": False},
        headers=_admin_headers("user@example.com"),
    )
    assert resp.status_code == 403


async def test_non_admin_cannot_delete(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """DELETE /v1/policies/{id} returns 403 for non-admin users."""
    _make_admin(db_uri)
    _make_user(db_uri)
    headers = _admin_headers()

    create_resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "protected",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    resp = await auth_client.delete(
        f"/v1/policies/{policy_id}",
        headers=_admin_headers("user@example.com"),
    )
    assert resp.status_code == 403


async def test_non_admin_can_list(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET /v1/policies is readable by non-admin users."""
    _make_admin(db_uri)
    _make_user(db_uri)

    # Create a policy as admin.
    await auth_client.post(
        "/v1/policies",
        json={
            "name": "visible",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=_admin_headers(),
    )

    # List as non-admin.
    resp = await auth_client.get(
        "/v1/policies",
        headers=_admin_headers("user@example.com"),
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


async def test_non_admin_can_get(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET /v1/policies/{id} is readable by non-admin users."""
    _make_admin(db_uri)
    _make_user(db_uri)

    create_resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "readable",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=_admin_headers(),
    )
    policy_id = create_resp.json()["id"]

    resp = await auth_client.get(
        f"/v1/policies/{policy_id}",
        headers=_admin_headers("user@example.com"),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "readable"


# ── Unauthenticated request tests ────────────────────────────────────────────


async def test_non_admin_create_returns_403(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """POST /v1/policies from a non-admin identity returns 403.

    Verifies that the ``_require_admin`` helper correctly rejects
    authenticated but non-admin users.
    """
    _make_user(db_uri, "nonadmin@example.com")
    resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "anon",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=_admin_headers("nonadmin@example.com"),
    )
    assert resp.status_code == 403


async def test_non_admin_delete_returns_403(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """DELETE /v1/policies/{id} from a non-admin identity returns 403."""
    _make_admin(db_uri)
    _make_user(db_uri, "nonadmin@example.com")
    headers = _admin_headers()

    create_resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "nodeletion",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    resp = await auth_client.delete(
        f"/v1/policies/{policy_id}",
        headers=_admin_headers("nonadmin@example.com"),
    )
    assert resp.status_code == 403


async def test_update_rename_duplicate_returns_409(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """PATCH /v1/policies/{id} renaming to an existing name returns 409."""
    _make_admin(db_uri)
    headers = _admin_headers()

    await auth_client.post(
        "/v1/policies",
        json={
            "name": "existing",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    create_resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "to_rename",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    resp = await auth_client.patch(
        f"/v1/policies/{policy_id}",
        json={"name": "existing"},
        headers=headers,
    )
    assert resp.status_code == 409

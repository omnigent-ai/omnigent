"""End-to-end integration tests for policy CRUD lifecycle flows.

Covers multi-step scenarios and cross-cutting concerns that the
per-endpoint unit-style tests in ``test_default_policy_routes.py``
and ``test_session_policy_routes.py`` do not exercise:

- Full create → read → update → delete lifecycle in a single test
- Policy registry discovery (``GET /v1/policy-registry``)
- Cross-scope isolation (default vs. session policies)
- Enabled/disabled toggle round-trip
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
from omnigent.server.auth import LEVEL_EDIT
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
from tests.server.conftest import ControllableMockClient

pytestmark = pytest.mark.asyncio

_HANDLER = "omnigent.policies.builtins.safety.ask_on_os_tools"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _admin_headers(email: str = "admin@example.com") -> dict[str, str]:
    """Return request headers simulating an authenticated admin.

    :param email: The user email to present.
    :returns: Dict with ``X-Forwarded-Email`` header.
    """
    return {"X-Forwarded-Email": email}


def _make_admin(db_uri: str, email: str = "admin@example.com") -> None:
    """Seed the permission store with an admin user.

    :param db_uri: SQLite URI for the per-test database.
    :param email: Admin email to create.
    """
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(email, is_admin=True)


def _seed_session(db_uri: str, user_email: str = "admin@example.com") -> str:
    """Create a session and grant LEVEL_EDIT to the given user.

    :param db_uri: SQLite URI for the per-test database.
    :param user_email: User to grant edit access.
    :returns: The conversation ID.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conversation = conv_store.create_conversation()
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(user_email)
    perm_store.grant(user_email, conversation.id, LEVEL_EDIT)
    return conversation.id


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """App with auth, permission, and policy stores enabled.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Per-test SQLite URI.
    :param tmp_path: Pytest temp dir for artifacts.
    :returns: A :class:`FastAPI` instance with auth and policy routes.
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

    :param auth_app: FastAPI app with permission and policy stores.
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


# ── Full lifecycle ───────────────────────────────────────────────────────────


async def test_default_policy_full_lifecycle(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Exercise the complete default-policy lifecycle in a single flow.

    create -> get -> list -> update -> get (verify update) -> delete -> verify 404.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    _make_admin(db_uri)
    headers = _admin_headers()

    # 1. Create
    create_resp = await auth_client.post(
        "/v1/policies",
        json={"name": "lifecycle_policy", "type": "python", "handler": _HANDLER},
        headers=headers,
    )
    assert create_resp.status_code == 200
    policy = create_resp.json()
    policy_id = policy["id"]
    assert policy["name"] == "lifecycle_policy"
    assert policy["enabled"] is True

    # 2. Get — verify freshly created state
    get_resp = await auth_client.get(f"/v1/policies/{policy_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "lifecycle_policy"
    assert get_resp.json()["enabled"] is True

    # 3. List — appears in the full list
    list_resp = await auth_client.get("/v1/policies", headers=headers)
    assert list_resp.status_code == 200
    ids = {p["id"] for p in list_resp.json()["data"]}
    assert policy_id in ids

    # 4. Update — rename and disable
    patch_resp = await auth_client.patch(
        f"/v1/policies/{policy_id}",
        json={"name": "lifecycle_renamed", "enabled": False},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["name"] == "lifecycle_renamed"
    assert patch_resp.json()["enabled"] is False

    # 5. Get — verify the update persisted
    get_resp2 = await auth_client.get(f"/v1/policies/{policy_id}", headers=headers)
    assert get_resp2.status_code == 200
    body = get_resp2.json()
    assert body["name"] == "lifecycle_renamed"
    assert body["enabled"] is False
    assert body["updated_at"] is not None

    # 6. Delete
    del_resp = await auth_client.delete(f"/v1/policies/{policy_id}", headers=headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    # 7. Verify gone
    gone_resp = await auth_client.get(f"/v1/policies/{policy_id}", headers=headers)
    assert gone_resp.status_code == 404


async def test_session_policy_full_lifecycle(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Exercise the complete session-policy lifecycle in a single flow.

    create -> get -> list -> update -> get (verify update) -> delete -> verify 404.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    _make_admin(db_uri)
    session_id = _seed_session(db_uri)
    headers = _admin_headers()

    # 1. Create
    create_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={"name": "session_lifecycle", "type": "python", "handler": _HANDLER},
        headers=headers,
    )
    assert create_resp.status_code == 200
    policy = create_resp.json()
    policy_id = policy["id"]
    assert policy["source"] == "session"
    assert policy["enabled"] is True

    # 2. Get
    get_resp = await auth_client.get(
        f"/v1/sessions/{session_id}/policies/{policy_id}", headers=headers
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "session_lifecycle"

    # 3. List — policy appears among session-scoped entries
    list_resp = await auth_client.get(f"/v1/sessions/{session_id}/policies", headers=headers)
    assert list_resp.status_code == 200
    session_ids = {p["id"] for p in list_resp.json()["data"] if p["id"] is not None}
    assert policy_id in session_ids

    # 4. Update — disable
    patch_resp = await auth_client.patch(
        f"/v1/sessions/{session_id}/policies/{policy_id}",
        json={"enabled": False},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["enabled"] is False

    # 5. Get — verify update persisted
    get_resp2 = await auth_client.get(
        f"/v1/sessions/{session_id}/policies/{policy_id}", headers=headers
    )
    assert get_resp2.status_code == 200
    assert get_resp2.json()["enabled"] is False
    assert get_resp2.json()["updated_at"] is not None

    # 6. Delete
    del_resp = await auth_client.delete(
        f"/v1/sessions/{session_id}/policies/{policy_id}", headers=headers
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    # 7. Verify gone
    gone_resp = await auth_client.get(
        f"/v1/sessions/{session_id}/policies/{policy_id}", headers=headers
    )
    assert gone_resp.status_code == 404


# ── Policy registry discovery ────────────────────────────────────────────────


async def test_policy_registry_returns_entries(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET /v1/policy-registry returns available policy callables with schemas.

    The registry must contain at least the built-in safety policies,
    each entry should have handler, kind, name, and description fields.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    _make_admin(db_uri)
    resp = await auth_client.get("/v1/policy-registry", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert len(body["data"]) > 0

    # Each entry must have the expected shape.
    for entry in body["data"]:
        assert "handler" in entry
        assert "kind" in entry
        assert "name" in entry
        assert "description" in entry
        assert entry["kind"] in ("callable", "factory")

    # The handler used in tests must be present in the registry.
    handlers = {e["handler"] for e in body["data"]}
    assert _HANDLER in handlers


async def test_policy_registry_handler_matches_create_allowlist(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A handler from the registry can be used to create a policy.

    Picks the first handler from the registry and creates a default
    policy with it, confirming the registry and write API agree on
    the allowlist.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    _make_admin(db_uri)
    headers = _admin_headers()

    reg_resp = await auth_client.get("/v1/policy-registry", headers=headers)
    assert reg_resp.status_code == 200, reg_resp.text
    entries = reg_resp.json()["data"]
    # Pick a callable (not factory) entry to avoid needing factory_params.
    callable_entry = next((e for e in entries if e["kind"] == "callable"), None)
    assert callable_entry is not None, (
        f"No callable entry in registry; got kinds: {[e.get('kind') for e in entries]}"
    )

    create_resp = await auth_client.post(
        "/v1/policies",
        json={
            "name": "from_registry",
            "type": "python",
            "handler": callable_entry["handler"],
        },
        headers=headers,
    )
    assert create_resp.status_code == 200
    assert create_resp.json()["handler"] == callable_entry["handler"]


# ── Cross-scope isolation ────────────────────────────────────────────────────


async def test_default_policy_not_in_session_list(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A default policy does not appear in a session's policy list.

    The session policy list endpoint merges session-scoped and
    spec-declared (admin) policies, but store-persisted default
    policies (``session_id IS NULL``) must not leak into it.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    _make_admin(db_uri)
    session_id = _seed_session(db_uri)
    headers = _admin_headers()

    # Create a default (server-wide) policy.
    await auth_client.post(
        "/v1/policies",
        json={"name": "global_only", "type": "python", "handler": _HANDLER},
        headers=headers,
    )

    # Session policy list must not contain it.
    session_resp = await auth_client.get(f"/v1/sessions/{session_id}/policies", headers=headers)
    assert session_resp.status_code == 200
    session_names = {p["name"] for p in session_resp.json()["data"] if p["source"] == "session"}
    assert "global_only" not in session_names


async def test_session_policy_not_in_default_list(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A session policy does not appear in the default policy list.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    _make_admin(db_uri)
    session_id = _seed_session(db_uri)
    headers = _admin_headers()

    # Create a session-scoped policy.
    await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={"name": "session_only", "type": "python", "handler": _HANDLER},
        headers=headers,
    )

    # Default policy list must not contain it.
    default_resp = await auth_client.get("/v1/policies", headers=headers)
    assert default_resp.status_code == 200
    default_names = {p["name"] for p in default_resp.json()["data"]}
    assert "session_only" not in default_names


async def test_two_sessions_have_independent_policies(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Policies created in one session are not visible in another.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    _make_admin(db_uri)
    session_a = _seed_session(db_uri)
    session_b = _seed_session(db_uri)
    headers = _admin_headers()

    # Create a policy in session A.
    await auth_client.post(
        f"/v1/sessions/{session_a}/policies",
        json={"name": "only_in_a", "type": "python", "handler": _HANDLER},
        headers=headers,
    )

    # Session B must not see it.
    resp_b = await auth_client.get(f"/v1/sessions/{session_b}/policies", headers=headers)
    assert resp_b.status_code == 200
    names_b = {p["name"] for p in resp_b.json()["data"] if p["source"] == "session"}
    assert "only_in_a" not in names_b


# ── Enabled/disabled toggle ─────────────────────────────────────────────────


async def test_disable_and_reenable_default_policy(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Toggling enabled off and back on persists correctly.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    _make_admin(db_uri)
    headers = _admin_headers()

    create_resp = await auth_client.post(
        "/v1/policies",
        json={"name": "togglable", "type": "python", "handler": _HANDLER},
        headers=headers,
    )
    policy_id = create_resp.json()["id"]
    assert create_resp.json()["enabled"] is True

    # Disable
    await auth_client.patch(
        f"/v1/policies/{policy_id}",
        json={"enabled": False},
        headers=headers,
    )
    get_resp = await auth_client.get(f"/v1/policies/{policy_id}", headers=headers)
    assert get_resp.json()["enabled"] is False

    # Verify it shows as disabled in the list
    list_resp = await auth_client.get("/v1/policies", headers=headers)
    assert list_resp.status_code == 200, list_resp.text
    match = next((p for p in list_resp.json()["data"] if p["id"] == policy_id), None)
    assert match is not None, f"Policy {policy_id} not found in list"
    assert match["enabled"] is False

    # Re-enable
    await auth_client.patch(
        f"/v1/policies/{policy_id}",
        json={"enabled": True},
        headers=headers,
    )
    get_resp2 = await auth_client.get(f"/v1/policies/{policy_id}", headers=headers)
    assert get_resp2.json()["enabled"] is True

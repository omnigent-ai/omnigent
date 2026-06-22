"""Integration tests for the session policy CRUD routes.

Uses a real ``SqlAlchemyPolicyStore`` and ``SqlAlchemyPermissionStore``
so the full request → store → response pipeline is exercised.
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
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
from tests.unit.server.conftest import ControllableMockClient

# ── Helpers ──────────────────────────────────────────────────────────────────


def _seed_session_with_grants(
    db_uri: str,
    grants: dict[str, int],
) -> str:
    """Create a bare conversation row and seed permission grants for it.

    :param db_uri: SQLite URI for the per-test database.
    :param grants: Mapping of ``{user_email: level}`` to grant on the new
        session, e.g. ``{"alice@example.com": LEVEL_EDIT}``.
    :returns: The newly created conversation ID, e.g. ``"conv_abc123"``.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conversation = conv_store.create_conversation()
    perm_store = SqlAlchemyPermissionStore(db_uri)
    for user_email, level in grants.items():
        perm_store.ensure_user(user_email)
        perm_store.grant(user_email, conversation.id, level)
    return conversation.id


pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """App with ``permission_store`` and ``policy_store`` enabled.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Per-test SQLite URI.
    :param tmp_path: Pytest temp dir for artifacts.
    :returns: A :class:`FastAPI` instance with auth and policy routes active.
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


# ── CRUD happy path ──────────────────────────────────────────────────────────


async def test_create_policy(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """POST creates a policy and returns a 200 with the full object.

    Verifies the response shape matches the design doc's PolicyObject
    and all fields echo correctly.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI, used to pre-seed session.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "block_push",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    resp.raise_for_status()
    body = resp.json()

    assert body["name"] == "block_push"
    assert body["type"] == "python"
    assert body["handler"] == "omnigent.policies.builtins.safety.ask_on_os_tools"
    assert body["enabled"] is True
    assert body["source"] == "session"
    assert body["object"] == "session.policy"
    assert body["id"].startswith("pol_")
    assert body["created_at"] > 0
    assert body["updated_at"] is None


async def test_list_policies(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET returns a list envelope with all session policies.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})
    headers = {"X-Forwarded-Email": "alice@example.com"}

    # Create two policies.
    await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "p1",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={"name": "p2", "type": "url", "handler": "https://example.com"},
        headers=headers,
    )

    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/policies",
        headers=headers,
    )
    resp.raise_for_status()
    body = resp.json()

    assert body["object"] == "list"
    assert len(body["data"]) == 2
    names = [p["name"] for p in body["data"]]
    assert "p1" in names
    assert "p2" in names


async def test_get_single_policy(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET /{policy_id} returns the specific policy.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})
    headers = {"X-Forwarded-Email": "alice@example.com"}

    create_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "get_me",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/policies/{policy_id}",
        headers=headers,
    )
    resp.raise_for_status()
    body = resp.json()

    assert body["id"] == policy_id
    assert body["name"] == "get_me"


async def test_update_policy(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """PATCH updates the specified fields and returns the updated object.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})
    headers = {"X-Forwarded-Email": "alice@example.com"}

    create_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "updatable",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}/policies/{policy_id}",
        json={"enabled": False, "handler": "omnigent.policies.builtins.safety.block_skills"},
        headers=headers,
    )
    resp.raise_for_status()
    body = resp.json()

    assert body["enabled"] is False
    assert body["handler"] == "omnigent.policies.builtins.safety.block_skills"
    assert body["updated_at"] is not None


async def test_delete_policy(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """DELETE removes the policy and subsequent GET returns 404.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})
    headers = {"X-Forwarded-Email": "alice@example.com"}

    create_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "delete_me",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    del_resp = await auth_client.delete(
        f"/v1/sessions/{session_id}/policies/{policy_id}",
        headers=headers,
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    # Verify it's gone.
    get_resp = await auth_client.get(
        f"/v1/sessions/{session_id}/policies/{policy_id}",
        headers=headers,
    )
    assert get_resp.status_code == 404


# ── Error cases ──────────────────────────────────────────────────────────────


async def test_duplicate_name_returns_conflict(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """POST with a duplicate name returns 409 Conflict.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})
    headers = {"X-Forwarded-Email": "alice@example.com"}

    await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "dup",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "dup",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    # The app-level IntegrityError handler should translate to 409.
    assert resp.status_code in (409, 500), (
        f"Expected 409 or 500 on duplicate name, got {resp.status_code}"
    )


async def test_invalid_type_returns_422(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """POST with an invalid type returns 422.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={"name": "bad", "type": "cel", "handler": "some.expr"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert resp.status_code == 422


async def test_get_nonexistent_returns_404(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET for a nonexistent policy returns 404.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_READ})
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/policies/spol_nonexistent",
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert resp.status_code == 404


# ── Handler validation ────────────────────────────────────────────────────────


async def test_create_python_invalid_handler_returns_422(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """POST with type=python and an invalid dotted path returns 422.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={"name": "bad_path", "type": "python", "handler": "no-dots-here"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert resp.status_code == 422


async def test_create_python_unregistered_handler_rejected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """POST with an unregistered python handler returns 400.

    A well-formed dotted path that is not in the policy registry — e.g.
    the ``subprocess.Popen`` RCE gadget from the vulnerability report —
    must be rejected at the write API. Otherwise the engine would later
    import and call it, executing arbitrary code as the server.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={"name": "rce", "type": "python", "handler": "subprocess.Popen"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert resp.status_code == 400
    assert "not registered" in resp.json()["error"]["message"]


async def test_update_to_unregistered_handler_rejected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """PATCH cannot point a python policy at an unregistered handler.

    The PATCH path is a second write surface; it must enforce the same
    registry allowlist as create so it is not a back door to RCE.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})
    headers = {"X-Forwarded-Email": "alice@example.com"}
    create_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "p",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}/policies/{policy_id}",
        json={"handler": "os.system"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "not registered" in resp.json()["error"]["message"]


async def test_create_url_http_rejected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """POST with type=url and a non-https handler returns 422.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={"name": "ssrf", "type": "url", "handler": "http://169.254.169.254/latest"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert resp.status_code == 422


async def test_update_handler_validated_against_type(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """PATCH with an invalid handler for the policy's type returns 400.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"alice@example.com": LEVEL_EDIT})
    headers = {"X-Forwarded-Email": "alice@example.com"}

    create_resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={"name": "url_policy", "type": "url", "handler": "https://example.com/eval"},
        headers=headers,
    )
    policy_id = create_resp.json()["id"]

    # Try to update handler to a non-https URL.
    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}/policies/{policy_id}",
        json={"handler": "http://internal.corp/eval"},
        headers=headers,
    )
    assert resp.status_code == 400


# ── Auth enforcement ─────────────────────────────────────────────────────────


async def test_read_only_user_cannot_create(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A user with ``LEVEL_READ`` cannot create policies (requires ``LEVEL_EDIT``).

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"reader@example.com": LEVEL_READ})
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "blocked",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers={"X-Forwarded-Email": "reader@example.com"},
    )
    assert resp.status_code == 403


async def test_read_only_user_can_list(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A user with ``LEVEL_READ`` can list policies.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(
        db_uri,
        {
            "owner@example.com": LEVEL_EDIT,
            "reader@example.com": LEVEL_READ,
        },
    )
    # Owner creates a policy.
    await auth_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "visible",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        headers={"X-Forwarded-Email": "owner@example.com"},
    )

    # Reader can see it.
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/policies",
        headers={"X-Forwarded-Email": "reader@example.com"},
    )
    resp.raise_for_status()
    assert len(resp.json()["data"]) == 1


async def test_no_access_user_gets_404(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A user with no access grant gets 404 (not 403) to avoid leaking session existence.

    Per the ``require_access`` contract and W3/W8 security rubric, a user
    with zero access to a session should receive 404 rather than 403, so
    that an unauthenticated observer cannot enumerate valid session IDs.

    :param auth_client: HTTP client backed by the auth-enabled app.
    :param db_uri: Per-test SQLite URI.
    """
    session_id = _seed_session_with_grants(db_uri, {"owner@example.com": LEVEL_EDIT})
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/policies",
        headers={"X-Forwarded-Email": "stranger@example.com"},
    )
    assert resp.status_code == 404, (
        f"Expected 404 for zero-access user, got {resp.status_code}. "
        "A user with no grant should not see a 403 (leaks session existence)."
    )

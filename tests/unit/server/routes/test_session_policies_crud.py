"""Tests for the session policies CRUD routes.

Routes: ``/v1/sessions/{session_id}/policies[/{policy_id}]``

The session policies router is only mounted when ``create_app`` receives
a ``policy_store``. These tests provide their own app/client that include it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import generate_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore


@pytest.fixture()
def policy_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """Build a FastAPI app that includes the policy store."""
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
        comment_store=SqlAlchemyCommentStore(db_uri),
        policy_store=SqlAlchemyPolicyStore(db_uri),
    )


@pytest_asyncio.fixture()
async def client(policy_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the policy-enabled app."""
    transport = httpx.ASGITransport(app=policy_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture()
async def session_id(db_uri: str) -> str:
    """Seed a test agent and conversation, return the session ID."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="policy-test-agent", bundle_location="test:///bundle")
    conv = conv_store.create_conversation(agent_id=agent_id)
    return conv.id


def _policy_payload(**overrides: object) -> dict:
    """Build a valid CreateSessionPolicyRequest payload."""
    base: dict = {
        "name": "test_url_policy",
        "type": "url",
        "handler": "https://example.com/policies/eval",
    }
    base.update(overrides)  # type: ignore[arg-type]
    return base


# ── POST /sessions/{session_id}/policies ──────────────────────────────


async def test_create_session_policy(client: httpx.AsyncClient, session_id: str) -> None:
    """Creating a session URL policy returns the policy object."""
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json=_policy_payload(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "test_url_policy"
    assert body["type"] == "url"
    assert body["object"] == "session.policy"
    assert body["source"] == "session"
    assert body["id"].startswith("pol_")


async def test_create_session_policy_duplicate_name(
    client: httpx.AsyncClient, session_id: str
) -> None:
    """Duplicate policy name within a session returns 409."""
    await client.post(f"/v1/sessions/{session_id}/policies", json=_policy_payload(name="dup"))
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies", json=_policy_payload(name="dup")
    )
    assert resp.status_code == 409


async def test_create_session_policy_nonexistent_session(
    client: httpx.AsyncClient,
) -> None:
    """Creating a policy for a nonexistent session returns 404."""
    resp = await client.post(
        "/v1/sessions/conv_nonexistent/policies",
        json=_policy_payload(),
    )
    assert resp.status_code == 404


async def test_create_session_policy_unregistered_python(
    client: httpx.AsyncClient, session_id: str
) -> None:
    """A python policy with unregistered handler is rejected."""
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json=_policy_payload(
            name="bad_py",
            type="python",
            handler="some.unregistered.handler",
        ),
    )
    assert resp.status_code == 400


# ── GET /sessions/{session_id}/policies ───────────────────────────────


async def test_list_session_policies(client: httpx.AsyncClient, session_id: str) -> None:
    """Listing session policies returns an object list."""
    resp = await client.get(f"/v1/sessions/{session_id}/policies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)


async def test_list_session_policies_after_create(
    client: httpx.AsyncClient, session_id: str
) -> None:
    """Created policies appear in the list with source='session'."""
    create_resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json=_policy_payload(),
    )
    pid = create_resp.json()["id"]

    resp = await client.get(f"/v1/sessions/{session_id}/policies")
    assert resp.status_code == 200
    session_policies = [p for p in resp.json()["data"] if p["source"] == "session"]
    ids = [p["id"] for p in session_policies]
    assert pid in ids


async def test_list_session_policies_nonexistent_session(
    client: httpx.AsyncClient,
) -> None:
    """Listing policies for a nonexistent session returns 404."""
    resp = await client.get("/v1/sessions/conv_nonexistent/policies")
    assert resp.status_code == 404


# ── GET /sessions/{session_id}/policies/{policy_id} ───────────────────


async def test_get_session_policy(client: httpx.AsyncClient, session_id: str) -> None:
    """Get a specific session policy by ID."""
    create_resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json=_policy_payload(),
    )
    pid = create_resp.json()["id"]

    resp = await client.get(f"/v1/sessions/{session_id}/policies/{pid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == pid


async def test_get_session_policy_not_found(client: httpx.AsyncClient, session_id: str) -> None:
    """Getting a nonexistent policy returns 404."""
    resp = await client.get(f"/v1/sessions/{session_id}/policies/pol_nonexistent")
    assert resp.status_code == 404


# ── PATCH /sessions/{session_id}/policies/{policy_id} ─────────────────


async def test_update_session_policy(client: httpx.AsyncClient, session_id: str) -> None:
    """Patching a policy's name returns the updated policy."""
    create_resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json=_policy_payload(),
    )
    pid = create_resp.json()["id"]

    resp = await client.patch(
        f"/v1/sessions/{session_id}/policies/{pid}",
        json={"name": "renamed"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed"


async def test_update_session_policy_not_found(client: httpx.AsyncClient, session_id: str) -> None:
    """Patching a nonexistent policy returns 404."""
    resp = await client.patch(
        f"/v1/sessions/{session_id}/policies/pol_nonexistent",
        json={"name": "new_name"},
    )
    assert resp.status_code == 404


async def test_update_session_policy_toggle_enabled(
    client: httpx.AsyncClient, session_id: str
) -> None:
    """Disabling a session policy."""
    create_resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json=_policy_payload(),
    )
    pid = create_resp.json()["id"]

    resp = await client.patch(
        f"/v1/sessions/{session_id}/policies/{pid}",
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# ── DELETE /sessions/{session_id}/policies/{policy_id} ────────────────


async def test_delete_session_policy(client: httpx.AsyncClient, session_id: str) -> None:
    """Deleting a session policy returns deleted: true."""
    create_resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json=_policy_payload(),
    )
    pid = create_resp.json()["id"]

    resp = await client.delete(f"/v1/sessions/{session_id}/policies/{pid}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Verify it's gone from the session policies
    get_resp = await client.get(f"/v1/sessions/{session_id}/policies/{pid}")
    assert get_resp.status_code == 404

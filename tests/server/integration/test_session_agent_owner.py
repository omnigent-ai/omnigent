"""Integration tests: only the owner may mutate a session-scoped agent.

Exercises the full request → store pipeline against real SQLite-backed
stores with a header auth provider, so the owner-only gate on the agent
bundle PUT and the MCP-server CRUD routes is verified end to end. The core
regression: a user the session was *shared* with (editor), or who *reused*
the agent in their own session, must not be able to replace agent code that
later runs with the runner's authority.
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
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"
ADMIN = "root@example.com"


@pytest.fixture(autouse=True)
def _multi_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force multi-user semantics for the owner gate.

    The shared test harness sets ``OMNIGENT_LOCAL_SINGLE_USER`` (loopback
    single-user), which intentionally bypasses the owner check because there
    is no second identity. These tests model a deployed multi-user server, so
    turn the marker off for the modules that read it.
    """
    from omnigent.server.routes import _auth_helpers
    from omnigent.server.routes._sessions import orchestration

    monkeypatch.setattr(_auth_helpers, "local_single_user_enabled", lambda: False)
    monkeypatch.setattr(orchestration, "local_single_user_enabled", lambda: False)


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App with a permission store + header auth provider enabled."""
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the auth-enabled app."""
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


async def _share_editor(
    client: httpx.AsyncClient, session_id: str, owner: str, grantee: str
) -> None:
    """Owner shares a session with ``grantee`` at editor level."""
    resp = await client.put(
        f"/v1/sessions/{session_id}/permissions",
        json={"user_id": grantee, "level": LEVEL_EDIT},
        headers={"X-Forwarded-Email": owner},
    )
    assert resp.status_code in (200, 204), resp.text


async def test_owner_can_update_bundle_and_mcp(auth_client: httpx.AsyncClient) -> None:
    """The creating user may replace the bundle and edit MCP servers."""
    agent = await create_test_agent(auth_client, name="owned-agent", user=ALICE)
    session_id = agent["_session_id"]
    headers = {"X-Forwarded-Email": ALICE}

    put = await auth_client.put(
        f"/v1/sessions/{session_id}/agent",
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="owned-agent", description="v2"),
                "application/gzip",
            )
        },
        headers=headers,
    )
    assert put.status_code == 200, put.text

    mcp = await auth_client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={"name": "gh", "transport": "http", "url": "https://example.com/sse"},
        headers=headers,
    )
    assert mcp.status_code == 200, mcp.text


async def test_shared_editor_cannot_update_bundle(auth_client: httpx.AsyncClient) -> None:
    """An editor the session was shared with is refused (403), not allowed."""
    agent = await create_test_agent(auth_client, name="shared-agent", user=ALICE)
    session_id = agent["_session_id"]
    await _share_editor(auth_client, session_id, ALICE, BOB)

    put = await auth_client.put(
        f"/v1/sessions/{session_id}/agent",
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="shared-agent", description="pwn"),
                "application/gzip",
            )
        },
        headers={"X-Forwarded-Email": BOB},
    )
    assert put.status_code == 403, put.text


async def test_shared_editor_cannot_edit_mcp_servers(auth_client: httpx.AsyncClient) -> None:
    """An editor cannot inject an MCP server (arbitrary spawn command)."""
    agent = await create_test_agent(auth_client, name="shared-mcp", user=ALICE)
    session_id = agent["_session_id"]
    await _share_editor(auth_client, session_id, ALICE, BOB)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={"name": "pwn", "transport": "stdio", "command": "/bin/sh", "args": ["-c", "id"]},
        headers={"X-Forwarded-Email": BOB},
    )
    assert resp.status_code == 403, resp.text


async def test_reuse_then_patch_forbidden(auth_client: httpx.AsyncClient) -> None:
    """Binding the owner's agent into your own session does not let you patch it.

    This is the core exploit: BOB is shared ALICE's session, reuses her
    session-scoped agent in a brand-new session of his own (becoming its
    owner), then tries to replace the shared agent's code.
    """
    agent = await create_test_agent(auth_client, name="reused-agent", user=ALICE)
    alice_session = agent["_session_id"]
    await _share_editor(auth_client, alice_session, ALICE, BOB)

    # BOB reuses ALICE's agent in his own new session.
    reuse = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
        headers={"X-Forwarded-Email": BOB},
    )
    assert reuse.status_code == 201, reuse.text
    bob_session = reuse.json()["id"]

    # BOB owns his session, but not the agent — the patch must be refused.
    put = await auth_client.put(
        f"/v1/sessions/{bob_session}/agent",
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="reused-agent", description="pwn"),
                "application/gzip",
            )
        },
        headers={"X-Forwarded-Email": BOB},
    )
    assert put.status_code == 403, put.text


async def test_admin_can_update_any_agent(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    """A workspace admin may replace an agent they did not create."""
    agent = await create_test_agent(auth_client, name="admin-agent", user=ALICE)
    session_id = agent["_session_id"]
    SqlAlchemyPermissionStore(db_uri).ensure_user(ADMIN, is_admin=True)

    put = await auth_client.put(
        f"/v1/sessions/{session_id}/agent",
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="admin-agent", description="ops"),
                "application/gzip",
            )
        },
        headers={"X-Forwarded-Email": ADMIN},
    )
    assert put.status_code == 200, put.text

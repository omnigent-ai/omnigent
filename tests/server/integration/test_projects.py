"""Integration tests for the first-class ``/v1/projects`` endpoints.

Exercises the full middleware → route → store pipeline: project CRUD, sharing
(specific user, ``__members__``, ``__public__``), leaving, and — the headline
behaviour the old label fan-out couldn't do — a session's access **inheriting**
from its project, including a chat filed *after* the share.

Uses a header-auth app wired with a real project store; requests impersonate
users via ``X-Forwarded-Email``.
"""

from __future__ import annotations

import json as _json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def projects_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """Header-auth app with a project store wired (so inheritance is live)."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


@pytest_asyncio.fixture()
async def projects_client(
    projects_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)
    transport = httpx.ASGITransport(app=projects_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


def _h(user: str) -> dict[str, str]:
    return {"X-Forwarded-Email": user}


async def _create_project(client: httpx.AsyncClient, user: str, name: str) -> dict[str, Any]:
    resp = await client.post("/v1/projects", json={"name": name}, headers=_h(user))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_session(client: httpx.AsyncClient, user: str) -> str:
    bundle = build_agent_bundle(name="test-agent")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers=_h(user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _file(client: httpx.AsyncClient, user: str, session_id: str, project_id: str) -> None:
    resp = await client.patch(
        f"/v1/sessions/{session_id}", json={"project_id": project_id}, headers=_h(user)
    )
    assert resp.status_code == 200, resp.text


async def _can_read(client: httpx.AsyncClient, user: str, session_id: str) -> bool:
    resp = await client.get(
        f"/v1/sessions/{session_id}?include_items=false&include_liveness=false",
        headers=_h(user),
    )
    return resp.status_code == 200


async def test_create_list_rename_delete(projects_client: httpx.AsyncClient) -> None:
    project = await _create_project(projects_client, "alice", "Client X")
    assert project["permission_level"] == 4

    listed = await projects_client.get("/v1/projects", headers=_h("alice"))
    assert project["id"] in {p["id"] for p in listed.json()}

    renamed = await projects_client.patch(
        f"/v1/projects/{project['id']}", json={"name": "Client Y"}, headers=_h("alice")
    )
    assert renamed.status_code == 200 and renamed.json()["name"] == "Client Y"

    deleted = await projects_client.delete(f"/v1/projects/{project['id']}", headers=_h("alice"))
    assert deleted.status_code == 204
    gone = await projects_client.get(f"/v1/projects/{project['id']}", headers=_h("alice"))
    assert gone.status_code == 404


async def test_share_inherits_to_current_and_future_chats(
    projects_client: httpx.AsyncClient,
) -> None:
    """The headline regression: a chat filed AFTER the share is still visible."""
    project = await _create_project(projects_client, "alice", "Team")
    first = await _create_session(projects_client, "alice")
    await _file(projects_client, "alice", first, project["id"])

    # Before sharing, bob can't read alice's chat.
    assert await _can_read(projects_client, "bob", first) is False

    # Share the project with bob (read).
    resp = await projects_client.put(
        f"/v1/projects/{project['id']}/permissions",
        json={"user_id": "bob", "level": 1},
        headers=_h("alice"),
    )
    assert resp.status_code == 200, resp.text
    assert await _can_read(projects_client, "bob", first) is True

    # File a NEW chat after the share — bob sees it with no extra action.
    later = await _create_session(projects_client, "alice")
    await _file(projects_client, "alice", later, project["id"])
    assert await _can_read(projects_client, "bob", later) is True

    # ...but read-only: bob can't archive it (owner-level action).
    denied = await projects_client.patch(
        f"/v1/sessions/{later}", json={"archived": True}, headers=_h("bob")
    )
    assert denied.status_code == 403


async def test_members_scope_visible_to_any_user(projects_client: httpx.AsyncClient) -> None:
    project = await _create_project(projects_client, "alice", "AllHands")
    session = await _create_session(projects_client, "alice")
    await _file(projects_client, "alice", session, project["id"])

    assert await _can_read(projects_client, "carol", session) is False
    await projects_client.put(
        f"/v1/projects/{project['id']}/permissions",
        json={"user_id": "__members__", "level": 1},
        headers=_h("alice"),
    )
    # carol — never invited — now sees it, and the project appears in her list.
    assert await _can_read(projects_client, "carol", session) is True
    carol_projects = await projects_client.get("/v1/projects", headers=_h("carol"))
    assert project["id"] in {p["id"] for p in carol_projects.json()}


async def test_everyone_scopes_capped_read_only(projects_client: httpx.AsyncClient) -> None:
    project = await _create_project(projects_client, "alice", "Cap")
    for sentinel in ("__members__", "__public__"):
        resp = await projects_client.put(
            f"/v1/projects/{project['id']}/permissions",
            json={"user_id": sentinel, "level": 2},
            headers=_h("alice"),
        )
        assert resp.status_code == 400, f"{sentinel}: {resp.text}"


async def test_members_list_and_leave(projects_client: httpx.AsyncClient) -> None:
    project = await _create_project(projects_client, "alice", "Team")
    await projects_client.put(
        f"/v1/projects/{project['id']}/permissions",
        json={"user_id": "bob", "level": 1},
        headers=_h("alice"),
    )
    members = await projects_client.get(
        f"/v1/projects/{project['id']}/permissions", headers=_h("alice")
    )
    by_user = {m["user_id"]: m["level"] for m in members.json()}
    assert by_user == {"alice": 4, "bob": 1}

    # Bob sees the project, then leaves — it drops off his list.
    before = await projects_client.get("/v1/projects", headers=_h("bob"))
    assert project["id"] in {p["id"] for p in before.json()}
    left = await projects_client.delete(
        f"/v1/projects/{project['id']}/membership", headers=_h("bob")
    )
    assert left.status_code == 204
    after = await projects_client.get("/v1/projects", headers=_h("bob"))
    assert project["id"] not in {p["id"] for p in after.json()}


async def test_delete_unfiles_chats_rather_than_deleting_them(
    projects_client: httpx.AsyncClient,
) -> None:
    project = await _create_project(projects_client, "alice", "Temp")
    session = await _create_session(projects_client, "alice")
    await _file(projects_client, "alice", session, project["id"])

    await projects_client.delete(f"/v1/projects/{project['id']}", headers=_h("alice"))

    # The chat survives, now unfiled (project_id cleared), still owned by alice.
    snap = await projects_client.get(
        f"/v1/sessions/{session}?include_items=false&include_liveness=false",
        headers=_h("alice"),
    )
    assert snap.status_code == 200
    assert snap.json()["project_id"] is None


async def test_non_manager_cannot_file_into_others_project(
    projects_client: httpx.AsyncClient,
) -> None:
    """Filing a session into a project you don't manage is refused."""
    project = await _create_project(projects_client, "alice", "Private")
    bobs_session = await _create_session(projects_client, "bob")
    resp = await projects_client.patch(
        f"/v1/sessions/{bobs_session}",
        json={"project_id": project["id"]},
        headers=_h("bob"),
    )
    assert resp.status_code == 403

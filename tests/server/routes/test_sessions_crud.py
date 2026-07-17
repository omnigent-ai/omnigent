"""Tests for Sessions API CRUD endpoints (list, get, delete, patch).

Exercises the core session management routes through the ``client``
fixture. Since the lifespan event (which seeds agents) does not run
in test fixtures, we seed a test agent and conversation directly via
the stores.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.server.routes import sessions as sessions_module
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest_asyncio.fixture()
async def session_id(db_uri: str) -> str:
    """Seed a test agent and conversation, return the session ID."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="test-agent", bundle_location="test:///bundle")
    conv = conv_store.create_conversation(agent_id=agent_id)
    return conv.id


# ── GET /v1/sessions (list) ─────────────────────────────────────────


async def test_list_sessions_empty(client: httpx.AsyncClient) -> None:
    """Empty database returns an empty list."""
    resp = await client.get("/v1/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["has_more"] is False


async def test_list_sessions_after_create(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """A created session appears in the list."""
    resp = await client.get("/v1/sessions")
    assert resp.status_code == 200
    body = resp.json()
    ids = [s["id"] for s in body["data"]]
    assert session_id in ids


async def test_list_sessions_pagination(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Pagination with limit returns at most N sessions."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="pag-agent", bundle_location="test:///bundle")
    conv_store.create_conversation(agent_id=agent_id)
    conv_store.create_conversation(agent_id=agent_id)

    resp = await client.get("/v1/sessions?limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1


# ── GET /v1/sessions/{id} (get snapshot) ────────────────────────────


async def test_get_session(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Get a session by ID returns its snapshot."""
    resp = await client.get(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == session_id


async def test_get_session_not_found(client: httpx.AsyncClient) -> None:
    """Getting a nonexistent session returns 404."""
    resp = await client.get("/v1/sessions/4fe12335002377c209e501c3fe3bcffc")
    assert resp.status_code == 404


# ── DELETE /v1/sessions/{id} ────────────────────────────────────────


async def test_delete_session(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Deleting a session returns 200 with deleted: true."""
    resp = await client.delete(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True


async def test_delete_session_not_found(client: httpx.AsyncClient) -> None:
    """Deleting a nonexistent session returns 404."""
    resp = await client.delete("/v1/sessions/4fe12335002377c209e501c3fe3bcffc")
    assert resp.status_code == 404


async def test_delete_running_session_attempts_stop(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Deleting a running session calls ``_stop_session_via_runner``."""
    mock_stop = AsyncMock(return_value=True)
    sessions_module._session_status_cache[session_id] = "running"
    try:
        with patch.object(sessions_module, "_stop_session_via_runner", mock_stop):
            resp = await client.delete(f"/v1/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        mock_stop.assert_awaited_once()
    finally:
        sessions_module._session_status_cache.pop(session_id, None)


async def test_delete_idle_parent_stops_running_child(
    client: httpx.AsyncClient,
    session_id: str,
    db_uri: str,
) -> None:
    """Deleting an idle parent with a running child stops the child.

    Regression test: ``_best_effort_stop`` previously used the child
    rollup only to decide whether to act, then always issued the stop
    against the parent's own session id. A parent that has already gone
    idle while its sub-agent child keeps running would get a no-op stop,
    then the recursive subtree delete would remove the child's row while
    its runner process kept running, orphaning it.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=session_id,
    )

    mock_stop = AsyncMock(return_value=True)
    sessions_module._session_status_cache[child.id] = "running"
    try:
        with patch.object(sessions_module, "_stop_session_via_runner", mock_stop):
            resp = await client.delete(f"/v1/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        # The child must be the one stopped, not the (idle) parent.
        mock_stop.assert_awaited_once()
        assert mock_stop.await_args is not None
        assert mock_stop.await_args.args[0] == child.id
    finally:
        sessions_module._session_status_cache.pop(child.id, None)


async def test_delete_idle_parent_stops_running_grandchild(
    client: httpx.AsyncClient,
    session_id: str,
    db_uri: str,
) -> None:
    """Deleting an idle parent stops a running grandchild too.

    Regression test: ``_best_effort_stop`` used to walk only direct
    children, one level down. A sub-agent that itself spawns a sub-agent
    (parent -> child -> grandchild) with the child now idle but the
    grandchild still running was invisible to that one-level check, so
    the grandchild kept running unstopped -- the same bug as the direct-
    child case, just one generation deeper. ``delete_conversation``'s
    recursive subtree delete has no such depth limit, so the stop logic
    must match it.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=session_id,
    )
    grandchild = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:citations",
        parent_conversation_id=child.id,
    )

    mock_stop = AsyncMock(return_value=True)
    sessions_module._session_status_cache[grandchild.id] = "running"
    try:
        with patch.object(sessions_module, "_stop_session_via_runner", mock_stop):
            resp = await client.delete(f"/v1/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        # The grandchild must be the one stopped, not the idle parent/child.
        mock_stop.assert_awaited_once()
        assert mock_stop.await_args is not None
        assert mock_stop.await_args.args[0] == grandchild.id
    finally:
        sessions_module._session_status_cache.pop(grandchild.id, None)


async def test_delete_proceeds_when_stop_fails(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Delete succeeds even when the runner stop raises."""
    mock_stop = AsyncMock(side_effect=ConnectionError("runner gone"))
    sessions_module._session_status_cache[session_id] = "running"
    try:
        with patch.object(sessions_module, "_stop_session_via_runner", mock_stop):
            resp = await client.delete(f"/v1/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
    finally:
        sessions_module._session_status_cache.pop(session_id, None)


# ── PATCH /v1/sessions/{id} ─────────────────────────────────────────


async def test_patch_session_title(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Patching a session's title returns the updated session."""
    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"title": "New Title"},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200


async def test_patch_session_not_found(client: httpx.AsyncClient) -> None:
    """Patching a nonexistent session returns 404."""
    resp = await client.patch(
        "/v1/sessions/4fe12335002377c209e501c3fe3bcffc",
        json={"title": "New Title"},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 404


# ── GET /v1/sessions/projects ────────────────────────────────────────


async def test_list_projects_empty(client: httpx.AsyncClient) -> None:
    """No project labels anywhere → empty project list."""
    resp = await client.get("/v1/sessions/projects")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_projects_returns_names_sorted(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Projects surface as a sorted list of names."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    a = conv_store.create_conversation()
    b = conv_store.create_conversation()
    conv_store.set_labels(a.id, {"omni_project": "Sprint 42"})
    conv_store.set_labels(b.id, {"omni_project": "Customer X"})

    resp = await client.get("/v1/sessions/projects")
    assert resp.status_code == 200
    assert resp.json() == ["Customer X", "Sprint 42"]


# ── GET /v1/sessions?project= (filter) ───────────────────────────────


async def test_list_sessions_filtered_by_project(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """``?project=X`` returns only sessions in that project."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    # GET /v1/sessions filters has_agent_id=True, so bind the conversations to
    # a seeded agent — otherwise the list comes back empty.
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="project-agent", bundle_location="test:///bundle")
    filed = conv_store.create_conversation(agent_id=agent_id)
    conv_store.create_conversation(agent_id=agent_id)  # unfiled
    conv_store.set_labels(filed.id, {"omni_project": "X"})

    resp = await client.get("/v1/sessions?project=X")
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()["data"]]
    assert ids == [filed.id]


async def test_list_sessions_empty_project_returns_unfiled(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """``?project=`` (empty) returns only sessions with no project label."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="project-agent", bundle_location="test:///bundle")
    filed = conv_store.create_conversation(agent_id=agent_id)
    unfiled = conv_store.create_conversation(agent_id=agent_id)
    conv_store.set_labels(filed.id, {"omni_project": "X"})

    resp = await client.get("/v1/sessions?project=")
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()["data"]]
    assert unfiled.id in ids
    assert filed.id not in ids


# ── PATCH /v1/sessions/{id} project label ────────────────────────────


async def test_patch_session_sets_project_label(
    client: httpx.AsyncClient,
    session_id: str,
    db_uri: str,
) -> None:
    """PATCH with ``labels: {project: X}`` upserts the project label."""
    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"labels": {"omni_project": "Sprint 42"}},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200

    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.get_conversation(session_id)
    assert conv is not None
    assert conv.labels.get("omni_project") == "Sprint 42"


async def test_patch_session_empty_project_removes_label(
    client: httpx.AsyncClient,
    session_id: str,
    db_uri: str,
) -> None:
    """PATCH with ``labels: {project: ""}`` removes the project label rather
    than persisting an empty value — so the session returns to Unfiled."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_labels(session_id, {"omni_project": "Sprint 42"})

    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"labels": {"omni_project": ""}},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200

    conv = conv_store.get_conversation(session_id)
    assert conv is not None
    assert "omni_project" not in conv.labels

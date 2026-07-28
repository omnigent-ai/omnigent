"""Integration coverage for child-session directory inheritance."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.session_directories import SessionDirectory
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_DEFAULT = SessionDirectory("default", "/repo/main")
_SHARED = SessionDirectory(f"dir_{1:032x}", "/repo/shared")


async def _seed_parent(client: httpx.AsyncClient, db_uri: str) -> tuple[str, str]:
    """Create an agent and a parent session with two stable roots."""
    agent = await create_test_agent(client, name="directory-test-agent")
    parent = SqlAlchemyConversationStore(db_uri).create_conversation(
        agent_id=agent["id"],
        workspace=_DEFAULT.path,
        directories=(_DEFAULT, _SHARED),
    )
    return parent.id, agent["id"]


async def _create_child(
    client: httpx.AsyncClient,
    *,
    parent_id: str,
    agent_id: str,
    title: str,
    directory_ids: list[str] | None | object = ...,
) -> httpx.Response:
    """POST one child while preserving omitted-vs-empty scope semantics."""
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "parent_session_id": parent_id,
        "title": title,
    }
    if directory_ids is not ...:
        body["directory_ids"] = directory_ids
    return await client.post("/v1/sessions", json=body)


async def test_child_directory_scope_inherit_subset_and_empty(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Omitted inherits all, a subset narrows, and [] creates scratch."""
    parent_id, agent_id = await _seed_parent(client, db_uri)

    inherited = await _create_child(
        client,
        parent_id=parent_id,
        agent_id=agent_id,
        title="inherit",
    )
    assert inherited.status_code == 201, inherited.text
    assert inherited.json()["directories"] == [
        _DEFAULT.as_dict(),
        _SHARED.as_dict(),
    ]
    assert inherited.json()["workspace"] == _DEFAULT.path

    narrowed = await _create_child(
        client,
        parent_id=parent_id,
        agent_id=agent_id,
        title="narrowed",
        directory_ids=[_SHARED.id],
    )
    assert narrowed.status_code == 201, narrowed.text
    assert narrowed.json()["directories"] == [_SHARED.as_dict()]
    assert narrowed.json().get("workspace") is None

    scratch = await _create_child(
        client,
        parent_id=parent_id,
        agent_id=agent_id,
        title="scratch",
        directory_ids=[],
    )
    assert scratch.status_code == 201, scratch.text
    assert scratch.json()["directories"] == []
    assert scratch.json().get("workspace") is None


async def test_nested_child_cannot_widen_parent_directory_scope(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A grandchild may only select ids still visible to its direct parent."""
    parent_id, agent_id = await _seed_parent(client, db_uri)
    narrowed = await _create_child(
        client,
        parent_id=parent_id,
        agent_id=agent_id,
        title="narrowed",
        directory_ids=[_SHARED.id],
    )
    assert narrowed.status_code == 201, narrowed.text

    widened = await _create_child(
        client,
        parent_id=narrowed.json()["id"],
        agent_id=agent_id,
        title="widened",
        directory_ids=[_DEFAULT.id, _SHARED.id],
    )

    assert widened.status_code == 400
    assert "outside the parent scope" in widened.text


async def test_child_rejects_independent_host_or_worktree_fields(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Child sessions inherit placement and cannot create an orphan worktree."""
    parent_id, agent_id = await _seed_parent(client, db_uri)

    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "parent_session_id": parent_id,
            "host_id": "host_untrusted",
            "workspace": "/repo/other",
        },
    )

    assert response.status_code == 422
    assert "inherit their parent's host and directories" in response.text

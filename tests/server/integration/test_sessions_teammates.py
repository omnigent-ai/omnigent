"""Integration tests for ``GET /v1/sessions/{id}/teammates``.

The endpoint folds a session's ``teammate_message`` items — mirrored
from a native harness transcript by the claude-native bridge — into one
display-only summary per teammate, so the Agents rail can show
harness-internal teammates (which have no Omnigent session and can
never appear in ``child_sessions``). Tests seed items directly via the
SqlAlchemy store: the route depends only on
``list_items(type="teammate_message", order="desc")``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.entities.conversation import NewConversationItem, TeammateMessageData
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(
    client: httpx.AsyncClient,
    agent_name: str = "teammate-test-agent",
) -> dict[str, Any]:
    """Create a session bound to a fresh test agent."""
    agent = await create_test_agent(client, name=agent_name)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
    )
    assert resp.status_code == 201, f"session create failed: {resp.text}"
    return resp.json()


def _teammate_item(data: TeammateMessageData) -> NewConversationItem:
    return NewConversationItem(type="teammate_message", response_id="seed", data=data)


async def test_teammates_404_for_nonexistent_session(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/sessions/conv_missing/teammates")
    assert resp.status_code == 404


async def test_teammates_empty_without_teammate_items(client: httpx.AsyncClient) -> None:
    session = await _create_session(client)
    resp = await client.get(f"/v1/sessions/{session['id']}/teammates")
    assert resp.status_code == 200
    assert resp.json() == {"object": "list", "data": []}


async def test_teammates_folds_items_into_roster(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    The roster carries one summary per teammate with its newest state.

    ``buddy`` goes spawn → prose → idle, so its newest item decides
    ``status="idle"`` while the newest prose delivery supplies the
    summary and preview. ``scout`` has only a spawn item, so it reads
    ``active`` — the state a still-working teammate shows before its
    first delivery.
    """
    session = await _create_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.append(
        session["id"],
        [
            _teammate_item(TeammateMessageData(teammate_id="buddy", kind="spawn")),
            _teammate_item(
                TeammateMessageData(
                    teammate_id="buddy",
                    kind="message",
                    text="All good here. What else do you need?",
                    summary="All good over here",
                    color="blue",
                )
            ),
            _teammate_item(
                TeammateMessageData(
                    teammate_id="buddy",
                    kind="idle",
                    text="Waiting for your next message.",
                )
            ),
            _teammate_item(TeammateMessageData(teammate_id="scout", kind="spawn")),
        ],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/teammates")
    assert resp.status_code == 200
    rows = {row["teammate_id"]: row for row in resp.json()["data"]}
    assert set(rows) == {"buddy", "scout"}

    buddy = rows["buddy"]
    assert buddy["status"] == "idle"
    assert buddy["color"] == "blue"
    assert buddy["last_summary"] == "All good over here"
    assert buddy["last_message_preview"] == "All good here. What else do you need?"
    assert buddy["parent_session_id"] == session["id"]

    scout = rows["scout"]
    assert scout["status"] == "active"
    assert scout["last_summary"] is None
    assert scout["last_message_preview"] is None

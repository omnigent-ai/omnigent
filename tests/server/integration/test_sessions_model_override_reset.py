"""Conditional fallback bookkeeping must not erase a newer model selection."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.server.routes.sessions import routes_core
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_REJECTED_PICK = "gpt-5.4-retired"
_NEW_PICK = "gpt-5.6-sol"


async def _create_pinned_session(client: httpx.AsyncClient) -> str:
    """Create a session with the rejected pick and an unrelated effort setting."""
    agent = await create_test_agent(client)
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "model_override": _REJECTED_PICK,
            "reasoning_effort": "high",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_conditional_reset_clears_only_matching_pick_without_live_forward(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running fallback needs a metadata reset, not another terminal command."""
    session_id = await _create_pinned_session(client)
    forward = AsyncMock()
    monkeypatch.setattr(routes_core, "_forward_session_change_to_runner", forward)

    response = await client.post(
        f"/v1/sessions/{session_id}/model-override/reset",
        json={"expected_model_override": _REJECTED_PICK},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"reset": True}
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["model_override"] is None
    assert snapshot.json()["reasoning_effort"] == "high"
    forward.assert_not_awaited()


@pytest.mark.parametrize("new_pick", [_NEW_PICK, None])
async def test_conditional_reset_preserves_a_newer_selection(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    new_pick: str | None,
) -> None:
    """Choosing another model or Default makes the original reset a no-op."""
    session_id = await _create_pinned_session(client)
    changed = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"model_override": new_pick or "default", "silent": True},
    )
    assert changed.status_code == 200, changed.text
    forward = AsyncMock()
    monkeypatch.setattr(routes_core, "_forward_session_change_to_runner", forward)

    response = await client.post(
        f"/v1/sessions/{session_id}/model-override/reset",
        json={"expected_model_override": _REJECTED_PICK},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"reset": False}
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["model_override"] == new_pick
    assert snapshot.json()["reasoning_effort"] == "high"
    forward.assert_not_awaited()


async def test_conditional_reset_rechecks_selection_after_route_snapshot(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selection made after the route reads the session must also survive."""
    session_id = await _create_pinned_session(client)
    original = SqlAlchemyConversationStore.clear_model_override_if_matches

    def clear_after_model_change(
        store: SqlAlchemyConversationStore,
        conversation_id: str,
        expected_model_override: str,
    ) -> bool:
        store.update_conversation(conversation_id, model_override=_NEW_PICK)
        return original(store, conversation_id, expected_model_override)

    monkeypatch.setattr(
        SqlAlchemyConversationStore, "clear_model_override_if_matches", clear_after_model_change
    )

    response = await client.post(
        f"/v1/sessions/{session_id}/model-override/reset",
        json={"expected_model_override": _REJECTED_PICK},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"reset": False}
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["model_override"] == _NEW_PICK


@pytest.mark.parametrize(
    ("body", "expected_status"),
    [
        ({}, 422),
        ({"expected_model_override": None}, 422),
        ({"expected_model_override": ""}, 422),
        ({"expected_model_override": "   "}, 400),
        ({"expected_model_override": "a b"}, 400),
        ({"expected_model_override": "a" * 257}, 400),
        ({"expected_model_override": _REJECTED_PICK, "model_override": "default"}, 422),
    ],
)
async def test_conditional_reset_rejects_invalid_preconditions_without_mutation(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    expected_status: int,
) -> None:
    """A missing or malformed expected pick must never become an unconditional reset."""
    session_id = await _create_pinned_session(client)

    response = await client.post(f"/v1/sessions/{session_id}/model-override/reset", json=body)

    assert response.status_code == expected_status, response.text
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["model_override"] == _REJECTED_PICK


@pytest.mark.parametrize("exists", [False, True])
async def test_conditional_reset_requires_an_existing_agent_bound_session(
    client: httpx.AsyncClient,
    db_uri: str,
    exists: bool,
) -> None:
    """Missing rows and unbound conversations are not sessions to reset."""
    store = SqlAlchemyConversationStore(db_uri)
    session_id = "c3a813be1af44216b2ad6e3c6290a72"
    if exists:
        session_id = store.create_conversation().id
        store.update_conversation(session_id, model_override=_REJECTED_PICK)

    response = await client.post(
        f"/v1/sessions/{session_id}/model-override/reset",
        json={"expected_model_override": _REJECTED_PICK},
    )

    assert response.status_code == 404, response.text
    if exists:
        conversation = store.get_conversation(session_id)
        assert conversation is not None
        assert conversation.model_override == _REJECTED_PICK

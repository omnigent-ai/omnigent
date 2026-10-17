"""A concurrent binding change must not hide a genuinely unavailable runner."""

from __future__ import annotations

import httpx
import pytest

from omnigent.server.routes._sessions import common, helpers
from omnigent.server.routes.sessions import routes_events
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("wrapper", ["claude-code-native-ui", "codex-native-ui"])
@pytest.mark.parametrize(
    ("original_runner_id", "replacement_runner_id"),
    [(None, "runner_new"), ("runner_old", "runner_new"), ("runner_old", None)],
    ids=["first_binding", "replacement", "unbind"],
)
async def test_binding_change_does_not_hide_offline_runner(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    wrapper: str,
    original_runner_id: str | None,
    replacement_runner_id: str | None,
) -> None:
    """Re-address a concurrent rebind once; a stable offline binding still fails."""
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "labels": {"omnigent.wrapper": wrapper}},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    if original_runner_id is not None:
        assert store.set_runner_id(session_id, original_runner_id)

    changed = False
    real_guard = routes_events._raise_if_runner_on_another_replica

    async def change_binding_after_miss(conv, app_state, conversation_store):
        nonlocal changed
        if conv.id == session_id and not changed:
            changed = True
            if replacement_runner_id is None:
                store.clear_runner_id(session_id)
            else:
                store.replace_runner_id(session_id, replacement_runner_id)
        await real_guard(conv, app_state, conversation_store)

    monkeypatch.setattr(
        routes_events, "_raise_if_runner_on_another_replica", change_binding_after_miss
    )
    body = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
    }
    response = await client.post(f"/v1/sessions/{session_id}/events", json=body)
    assert changed
    if replacement_runner_id is not None:
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "wrong_replica", response.text
        items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
        assert not [item for item in items if item["type"] in {"message", "error"}], items
        response = await client.post(f"/v1/sessions/{session_id}/events", json=body)

    assert response.status_code == 202, response.text
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    assert len([item for item in items if item["type"] == "message"]) == 1, items
    errors = [item for item in items if item["type"] == "error"]
    assert len(errors) == 1, items
    assert errors[0]["code"] == "runner_failed_to_start"
    snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert snapshot["status"] == "failed", snapshot


@pytest.mark.parametrize("phase", ["before_preflight", "after_preflight"])
async def test_unbind_preflight_cannot_adopt_a_newly_accepted_turn(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "labels": {"omnigent.wrapper": "claude-code-native-ui"}},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    assert store.set_runner_id(session_id, "runner-old")
    changed = False
    current = None
    real_guard = routes_events._raise_if_runner_on_another_replica
    real_get_client = routes_events._get_runner_client

    def start_new_turn() -> None:
        nonlocal current
        running = store.record_session_observation(session_id, "running", response_id="new-turn")
        assert running is not None
        current = store.begin_terminal_startup(session_id, running, "new-startup")
        assert current is not None
        row = store.get_conversation(session_id)
        assert row is not None
        helpers._session_status_from_cache(session_id, row.live_status, conversation=row)

    async def change_binding_after_miss(conv, app_state, conversation_store):
        nonlocal changed
        if conv.id == session_id and not changed:
            changed = True
            store.clear_runner_id(session_id)
            if phase == "before_preflight":
                start_new_turn()
        await real_guard(conv, app_state, conversation_store)

    async def get_client(*args, **kwargs):
        if phase == "after_preflight" and changed and kwargs.get("conversation") is not None:
            if current is None:
                start_new_turn()
        return await real_get_client(*args, **kwargs)

    monkeypatch.setattr(
        routes_events, "_raise_if_runner_on_another_replica", change_binding_after_miss
    )
    monkeypatch.setattr(routes_events, "_get_runner_client", get_client)
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "old request"}]},
        },
    )
    assert response.status_code == 202, response.text
    assert changed and current is not None
    row = store.get_conversation(session_id)
    assert row is not None and row.session_observation == current
    assert row.live_status == "running" and row.terminal_pending is True
    assert common._session_status_cache[session_id] == "running"
    assert common._session_active_response_cache[session_id] == "new-turn"
    assert common._session_terminal_pending_cache.get(session_id) is True


@pytest.mark.parametrize("status", ["running", "waiting"])
async def test_unbind_preflight_preserves_legacy_active_state(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "labels": {"omnigent.wrapper": "claude-code-native-ui"}},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    assert store.set_runner_id(session_id, "runner-old")
    store.set_session_live_status(session_id, status)
    current = None
    real_guard = routes_events._raise_if_runner_on_another_replica

    async def change_binding_after_miss(conv, app_state, conversation_store):
        nonlocal current
        if conv.id == session_id and current is None:
            row = store.clear_runner_id(session_id)
            current = row.session_observation
            assert current.observation_id is None and not row.terminal_pending
            helpers._session_status_from_cache(session_id, row.live_status, conversation=row)
        await real_guard(conv, app_state, conversation_store)

    monkeypatch.setattr(
        routes_events, "_raise_if_runner_on_another_replica", change_binding_after_miss
    )
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "old request"}]},
        },
    )
    assert response.status_code == 202, response.text
    assert current is not None and store.get_session_observation(session_id) == current
    assert common._session_status_cache[session_id] == status

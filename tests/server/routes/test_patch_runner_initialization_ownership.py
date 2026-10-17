"""PATCH runner binding uses the same owned initialization as reconnects."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities.session_observation import SessionObservation
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.server.routes.sessions import routes_core
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent
from tests.server.test_host_registry import FakeWebSocket
from tests.server.test_runner_session_init import _ObservedInitClient


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [True, False], ids=["current-initializer", "legacy"])
@pytest.mark.parametrize("same_runner", [False, True], ids=["rebind", "same-runner"])
async def test_patch_bind_reserves_before_post_and_retains_owned_completion(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    owned: bool,
    same_runner: bool,
) -> None:
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "labels": {"omnigent.wrapper": "claude-code-native-ui"}},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    if same_runner:
        store.replace_runner_id(session_id, "runner_patch_init")
    previous = store.record_session_observation(
        session_id,
        "failed",
        response_id="previous-response",
        error_key="previous-error",
        error_labels={"omnigent.last_task_error_code": "native_turn_error"},
    )
    assert previous is not None
    runner = _ObservedInitClient(store, session_id)
    runner.response_json = {
        "status": "initialized",
        "session_init_protocol_version": 2,
        "terminal_ready": True,
    }
    recoveries: list[str] = []
    restorations: list[SessionObservation] = []
    at_lookup: list[SessionObservation] = []

    async def resolve(*_args: Any, **_kwargs: Any):
        before = store.get_session_observation(session_id)
        assert before is not None
        at_lookup.append(before)
        return runner

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def recover(conversation_id: str, *_args: Any, **_kwargs: Any) -> None:
        recoveries.append(conversation_id)

    async def restore(conversation: Any, *_args: Any, **_kwargs: Any) -> None:
        restorations.append(conversation.session_observation)

    monkeypatch.setattr(routes_core, "_get_runner_client", resolve)
    monkeypatch.setattr(routes_core, "_ensure_runner_relay_ready", noop)
    monkeypatch.setattr(routes_core, "_publish_runner_recovered_status", recover)
    monkeypatch.setattr("omnigent.server.child_session_recovery.restore_active_children", restore)
    if not owned:
        app.state.runner_session_initializer = None
    registry = app.state.tunnel_registry
    session = registry.register(
        "runner_patch_init",
        FakeWebSocket(),
        HelloFrame(runner_version="test", frame_protocol_version=1),
    )
    task = asyncio.create_task(
        client.patch(f"/v1/sessions/{session_id}", json={"runner_id": "runner_patch_init"})
    )
    try:
        await asyncio.wait_for(runner.entered.wait(), 10)
        pending = runner.at_post[0]
        assert pending is not None and pending.terminal_pending is owned
        [before] = at_lookup
        assert pending.session_observation.status == previous.status
        assert pending.session_observation.response_id == before.response_id
        assert pending.session_observation.error_key == before.error_key
        if same_runner:
            assert before.response_id == previous.response_id
            assert before.error_key == previous.error_key
        if owned:
            assert (
                runner.calls[0]["session_observation"] == pending.session_observation.model_dump()
            )
        else:
            assert runner.calls[0].get("session_observation") is None
        runner.release.set()
        response = await asyncio.wait_for(task, 10)
        assert response.status_code == 200, response.text
        current = store.get_conversation(session_id)
        assert current is not None and not current.terminal_pending
        if owned:
            assert current.session_observation.revision > pending.session_observation.revision
            assert current.live_status == previous.status
            assert current.session_observation.response_id == before.response_id
            assert current.session_observation.error_key == before.error_key
            if same_runner:
                assert current.labels["omnigent.last_task_error_code"] == "native_turn_error"
            assert response.json()["status"] == previous.status
            assert (
                SessionObservation.model_validate(response.json()["session_observation"])
                == current.session_observation
            )
        assert recoveries == ([] if owned else [session_id])
        assert restorations == ([current.session_observation] if owned else [])
    finally:
        runner.release.set()
        await asyncio.gather(task, return_exceptions=True)
        registry.deregister("runner_patch_init", session, retire=False)

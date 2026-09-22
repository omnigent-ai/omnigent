"""Quiescence inputs retain status effects without delivering runner completion."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.runtime import session_stream
from omnigent.server import session_live_state
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import common
from omnigent.server.routes.sessions import routes_events
from omnigent.server.schemas import BackgroundTaskInfo
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore

_QUIESCENCE_INPUTS = [
    pytest.param("subagent.status", {"idle": True}, id="subagent-status"),
    pytest.param("external_session_status", {"status": "quiesced"}, id="legacy-quiesced"),
]
_BACKGROUND_TASK = BackgroundTaskInfo(status="running", description="Wait for CI")


async def _flush_live_state() -> None:
    """Wait for ordered persistence and parent fanout before inspecting their effects."""
    done = threading.Event()
    session_live_state.submit("test_barrier", done.set)
    assert await asyncio.to_thread(done.wait, 10)


@dataclass
class _StatusRoute:
    client: httpx.AsyncClient
    store: SqlAlchemyConversationStore
    scheduled: SqlAlchemyScheduledTaskStore
    task_id: str
    parent_id: str
    child_id: str
    published: Mock
    telemetry: Mock
    forwarded: list[tuple[str, dict[str, Any]]]


@pytest.fixture
async def status_route(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_StatusRoute]:
    store = SqlAlchemyConversationStore(db_uri)
    scheduled = SqlAlchemyScheduledTaskStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id)
    task_id = uuid4().hex
    scheduled.create(
        scheduled_task_id=task_id,
        name="quiescence",
        prompt="test",
        rrule="FREQ=HOURLY;BYMINUTE=0",
        user_id=None,
        agent_id=uuid4().hex,
        timezone="UTC",
    )
    for conv in (parent, child):
        store.set_session_live_status(conv.id, "running")
        common._session_status_cache[conv.id] = "running"
        common._session_active_response_cache[conv.id] = "resp_active"
        common._session_background_task_count_cache[conv.id] = 2
        common._session_background_tasks_cache[conv.id] = [_BACKGROUND_TASK]
        scheduled.create_run(
            run_id=uuid4().hex,
            scheduled_task_id=task_id,
            status="running",
            scheduled_at=1000,
            conversation_id=conv.id,
            fired_at=1001,
        )

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        sessions.create_sessions_router(store, SqlAlchemyAgentStore(db_uri)), prefix="/v1"
    )
    published = Mock()
    telemetry = Mock()
    forwarded: list[tuple[str, dict[str, Any]]] = []

    def capture_forward(request: httpx.Request) -> httpx.Response:
        forwarded.append((request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(capture_forward), base_url="http://runner"
        ) as runner,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):

        async def get_runner_client(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
            return runner

        monkeypatch.setattr(sessions, "_get_runner_client", get_runner_client)
        monkeypatch.setattr(session_stream, "publish", published)
        monkeypatch.setattr(routes_events, "_tel_emit", telemetry)
        session_live_state.configure(store, scheduled)
        try:
            yield _StatusRoute(
                client,
                store,
                scheduled,
                task_id,
                parent.id,
                child.id,
                published,
                telemetry,
                forwarded,
            )
        finally:
            await _flush_live_state()
            session_live_state.configure(None)
            for conv in (parent, child):
                for cache in (
                    common._session_status_cache,
                    common._session_active_response_cache,
                    common._session_background_task_count_cache,
                    common._session_background_tasks_cache,
                ):
                    cache.pop(conv.id, None)


@pytest.mark.parametrize(("event_type", "data"), _QUIESCENCE_INPUTS)
@pytest.mark.parametrize("is_child", [True, False], ids=["child", "top-level"])
@pytest.mark.parametrize(
    ("optional", "wire_fields", "count", "tasks"),
    [
        pytest.param({}, {}, 2, [_BACKGROUND_TASK], id="bare"),
        pytest.param(
            {
                "response_id": "resp_turn",
                "background_task_count": 1,
                "background_tasks": [_BACKGROUND_TASK.model_dump()],
                "blocked_on": "permission",
            },
            {
                "response_id": "resp_turn",
                "background_task_count": 1,
                "background_tasks": [_BACKGROUND_TASK.model_dump()],
                "blocked_on": "permission",
            },
            1,
            [_BACKGROUND_TASK],
            id="optional-fields",
        ),
        pytest.param(
            {"response_id": None, "background_task_count": 0},
            {"background_task_count": 0},
            None,
            None,
            id="clear-background",
        ),
        pytest.param(
            {"background_task_count": True, "background_tasks": "invalid", "blocked_on": 1},
            {},
            2,
            [_BACKGROUND_TASK],
            id="best-effort-fields",
        ),
    ],
)
async def test_quiescence_inputs_have_identical_status_effects(
    status_route: _StatusRoute,
    event_type: str,
    data: dict[str, Any],
    is_child: bool,
    optional: dict[str, Any],
    wire_fields: dict[str, Any],
    count: int | None,
    tasks: list[BackgroundTaskInfo] | None,
) -> None:
    route = status_route
    sid = route.child_id if is_child else route.parent_id
    response = await route.client.post(
        f"/v1/sessions/{sid}/events", json={"type": event_type, "data": {**data, **optional}}
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"queued": False}
    await _flush_live_state()

    assert common._session_status_cache[sid] == "idle"
    assert sid not in common._session_active_response_cache
    assert common._session_background_task_count_cache.get(sid) == count
    assert common._session_background_tasks_cache.get(sid) == tasks
    conv = route.store.get_conversation(sid)
    assert conv is not None and conv.live_status == "idle"
    assert route.store.list_items(sid).data == []
    runs, _ = route.scheduled.list_runs(route.task_id)
    run = next(run for run in runs if run.conversation_id == sid)
    assert run.status == "succeeded"
    assert run.finished_at is not None

    events = [(call.args[0], call.args[1]) for call in route.published.call_args_list]
    assert [event for target, event in events if target == sid] == [
        {
            "sequence_number": None,
            "type": "session.status",
            "conversation_id": sid,
            "status": "idle",
            "error": None,
            **wire_fields,
        }
    ]
    if is_child:
        parent_events = [event for target, event in events if target == route.parent_id]
        assert len(parent_events) == 1
        assert parent_events[0]["type"] == "session.child_session.updated"
        assert parent_events[0]["child_session_id"] == sid
        assert parent_events[0]["child"]["busy"] is False
        assert common._session_status_cache[route.parent_id] == "running"
        assert common._session_active_response_cache[route.parent_id] == "resp_active"
        assert (
            next(run for run in runs if run.conversation_id == route.parent_id).status == "running"
        )
    else:
        assert len(events) == 1
    assert route.forwarded == []
    route.telemetry.assert_not_called()


@pytest.mark.parametrize(("event_type", "data"), _QUIESCENCE_INPUTS)
async def test_quiescence_preserves_failed_status(
    status_route: _StatusRoute, event_type: str, data: dict[str, Any]
) -> None:
    route = status_route
    sid = route.child_id
    common._session_status_cache[sid] = "failed"
    route.store.set_session_live_status(sid, "failed")
    response = await route.client.post(
        f"/v1/sessions/{sid}/events", json={"type": event_type, "data": data}
    )
    assert response.status_code == 202, response.text
    await _flush_live_state()
    assert common._session_status_cache[sid] == "failed"
    assert sid not in common._session_active_response_cache
    conv = route.store.get_conversation(sid)
    assert conv is not None and conv.live_status == "failed"
    assert route.scheduled.get_running_run_by_conversation(sid) is not None
    route.published.assert_not_called()
    route.telemetry.assert_not_called()
    assert route.forwarded == []


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"idle": False},
        {"idle": None},
        {"idle": 0},
        {"idle": 1},
        {"idle": 1.0},
        {"idle": "true"},
        {"idle": []},
        {"idle": {}},
    ],
)
async def test_subagent_status_requires_literal_true(
    status_route: _StatusRoute, data: dict[str, Any]
) -> None:
    route = status_route
    response = await route.client.post(
        f"/v1/sessions/{route.child_id}/events", json={"type": "subagent.status", "data": data}
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_input"
    assert "data.idle" in response.json()["error"]["message"]
    assert common._session_status_cache[route.child_id] == "running"
    assert common._session_active_response_cache[route.child_id] == "resp_active"
    route.published.assert_not_called()
    route.telemetry.assert_not_called()
    assert route.forwarded == []


@pytest.mark.parametrize(("event_type", "data"), _QUIESCENCE_INPUTS)
async def test_quiescence_validates_optional_response_id(
    status_route: _StatusRoute, event_type: str, data: dict[str, Any]
) -> None:
    route = status_route
    response = await route.client.post(
        f"/v1/sessions/{route.child_id}/events",
        json={"type": event_type, "data": {**data, "response_id": 123}},
    )
    assert response.status_code == 400, response.text
    assert "data.response_id" in response.json()["error"]["message"]
    route.published.assert_not_called()
    route.telemetry.assert_not_called()
    assert route.forwarded == []


@pytest.mark.parametrize("status", ["idle", "running", "failed"])
async def test_external_session_status_still_forwards_to_runner(
    status_route: _StatusRoute, status: str
) -> None:
    route = status_route
    sid = route.child_id
    data = {"status": status, "output": "authoritative result"}
    response = await route.client.post(
        f"/v1/sessions/{sid}/events", json={"type": "external_session_status", "data": data}
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"queued": False}
    assert len(route.forwarded) == 1
    path, body = route.forwarded[0]
    assert path == f"/v1/sessions/{sid}/events"
    assert body["type"] == "external_session_status"
    assert body["data"] == data
    assert route.telemetry.call_count == (0 if status == "running" else 1)

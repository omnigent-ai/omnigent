"""Subagent inactivity publishes idle status without delivering runner completion."""

from __future__ import annotations

import asyncio
import contextlib
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

from omnigent.entities import ErrorData, MessageData, NewConversationItem, SlashCommandData
from omnigent.errors import OmnigentError
from omnigent.runtime import session_stream
from omnigent.server import session_live_state, session_metadata_logging
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import common, helpers, orchestration
from omnigent.server.routes.sessions import routes_events
from omnigent.server.schemas import BackgroundTaskInfo, ErrorDetail, SessionEventInput
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.debug_log_helpers import capture_debug_rows
from tests.server.routes.test_sessions_runner_relay import _ScriptedRunnerClient

_BACKGROUND_TASK = BackgroundTaskInfo(status="running", description="Wait for CI")


async def _flush_live_state() -> None:
    """Wait for ordered persistence and parent fanout before inspecting their effects."""
    # A committed status queues child and scheduled projections after the first barrier.
    for _ in range(2):
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
        name="inactivity",
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
async def test_subagent_idle_publishes_status_without_completion(
    status_route: _StatusRoute,
    is_child: bool,
    optional: dict[str, Any],
    wire_fields: dict[str, Any],
    count: int | None,
    tasks: list[BackgroundTaskInfo] | None,
) -> None:
    route = status_route
    sid = route.child_id if is_child else route.parent_id
    response = await route.client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "subagent.status", "data": {"idle": True, **optional}},
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
            "session_observation": route.store.get_session_observation(sid).model_dump(),
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


@pytest.mark.parametrize("fail_idle_top_level", [False, True])
async def test_offline_sweep_refreshes_child_status_after_idle_observation(
    status_route: _StatusRoute, fail_idle_top_level: bool
) -> None:
    route = status_route
    snapshot = route.store.get_conversation(route.child_id)
    assert snapshot is not None and snapshot.live_status == "running"

    response = await route.client.post(
        f"/v1/sessions/{route.child_id}/events",
        json={"type": "subagent.status", "data": {"idle": True}},
    )
    assert response.status_code == 202, response.text
    await _flush_live_state()
    # Another replica's sweep has an older row and no local turn edges.
    common._session_status_cache.pop(route.child_id)
    route.published.reset_mock()

    with capture_debug_rows("server") as rows:
        await sessions._mark_runner_sessions_offline(
            [snapshot],
            ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly."),
            route.store,
            fail_idle_top_level=fail_idle_top_level,
        )
        await _flush_live_state()

    child = route.store.get_conversation(route.child_id)
    assert child is not None and child.live_status == "idle"
    assert sessions._last_task_error_from_labels(child.labels) is None
    assert route.store.list_items(route.parent_id).data == []
    route.published.assert_not_called()
    assert route.forwarded == []

    decision = next(row for row in rows if row["event_name"] == "runner_disconnect_decision")
    assert decision["session_id"] == route.child_id
    assert decision["level"] == "WARNING"
    assert not any(row["event_name"] == "session_turn_failed" for row in rows)
    assert (
        decision["attributes"].items()
        >= {
            "origin": "runner_offline_sweep",
            "decision": "idle_no_failure",
            "status_source": "persisted",
            "persisted_session_status": "idle",
            "snapshot_session_status": "running",
            "parent_session_id": route.parent_id,
            "session_kind": "sub_agent",
            "status_lookup": "found",
        }.items()
    )


async def test_subagent_idle_preserves_failed_status(status_route: _StatusRoute) -> None:
    route = status_route
    sid = route.child_id
    common._session_status_cache[sid] = "failed"
    route.store.set_session_live_status(sid, "failed")
    common._session_active_response_cache.pop(sid, None)
    response = await route.client.post(
        f"/v1/sessions/{sid}/events", json={"type": "subagent.status", "data": {"idle": True}}
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


async def test_subagent_idle_validates_optional_response_id(
    status_route: _StatusRoute,
) -> None:
    route = status_route
    response = await route.client.post(
        f"/v1/sessions/{route.child_id}/events",
        json={"type": "subagent.status", "data": {"idle": True, "response_id": 123}},
    )
    assert response.status_code == 400, response.text
    assert "data.response_id" in response.json()["error"]["message"]
    route.published.assert_not_called()
    route.telemetry.assert_not_called()
    assert route.forwarded == []


@pytest.mark.parametrize(
    ("status", "turn_outcome"),
    [("idle", None), ("running", None), ("failed", None), ("idle", "cancelled")],
)
@pytest.mark.parametrize("structured", [False, True])
async def test_external_session_status_still_forwards_to_runner(
    status_route: _StatusRoute, status: str, turn_outcome: str | None, structured: bool
) -> None:
    route = status_route
    sid = route.child_id
    data = {"status": status, "output": "authoritative result"}
    if structured:
        data["output"] = json.dumps(
            {
                "error": {
                    "type": "invalid_request_error",
                    "code": "array_above_max_length",
                    "param": "input[3].content",
                }
            }
        )
    if turn_outcome is not None:
        data["turn_outcome"] = turn_outcome
    with capture_debug_rows("server") as rows:
        response = await route.client.post(
            f"/v1/sessions/{sid}/events", json={"type": "external_session_status", "data": data}
        )
    assert response.status_code == 202, response.text
    assert response.json() == {"queued": False}
    assert len(route.forwarded) == 1
    path, body = route.forwarded[0]
    assert path == f"/v1/sessions/{sid}/events"
    assert body["type"] == "external_session_status"
    expected_data: dict[str, object] = dict(data)
    if status == "failed":
        expected_data["failure_context"] = {
            "failure_source": "external_status",
            "detail_source": "external_status_output",
            **(
                {
                    "inference_detail_source": "provider_error_json",
                    "provider_error_type": "invalid_request_error",
                    "provider_error_code": "array_above_max_length",
                    "provider_error_param": "input[3].content",
                }
                if structured
                else {}
            ),
        }
        [failure] = [r for r in rows if r["event_name"] == "session_turn_failed"]
        assert failure["session_id"] == sid
        assert failure["attributes"]["origin"] == "external_session_status"
        if structured:
            assert failure["attributes"]["provider_error_param"] == "input[3].content"
    projected_data = dict(body["data"])
    observation = projected_data.pop("session_observation")
    assert observation == route.store.get_session_observation(sid).model_dump()
    assert projected_data.pop("subagent_dispatch_id", None) is None
    assert projected_data == expected_data
    assert route.telemetry.call_count == (0 if status == "running" else 1)


async def test_external_status_counts_each_no_id_turn_failure_without_reusing_response(
    status_route: _StatusRoute,
) -> None:
    """A new native turn without an ID must not reuse the last failed turn's identity."""
    route = status_route
    observations = []
    with capture_debug_rows("server") as rows:
        for data in (
            {"status": "running", "response_id": "first-turn"},
            {"status": "failed", "output": "same genuine failure"},
            {"status": "running"},
            {"status": "failed", "output": "same genuine failure"},
            {"status": "failed", "output": "same genuine failure"},
        ):
            response = await route.client.post(
                f"/v1/sessions/{route.child_id}/events",
                json={"type": "external_session_status", "data": data},
            )
            assert response.status_code == 202, response.text
            await _flush_live_state()
            observations.append(route.store.get_session_observation(route.child_id))

    first_failure, no_id_running, second_failure, replay = observations[1:]
    assert first_failure.response_id == "first-turn"
    assert no_id_running.response_id is None
    assert second_failure.response_id is None
    assert second_failure.observation_id != first_failure.observation_id
    assert replay == second_failure
    failures = [row for row in rows if row["event_name"] == "session_turn_failed"]
    assert len(failures) == 2
    source_ids = [row["attributes"]["source_event_id"] for row in failures]
    assert source_ids == [first_failure.observation_id, second_failure.observation_id]
    assert failures[0]["attributes"]["response_id"] == "first-turn"
    assert failures[1]["attributes"].get("response_id") is None


async def test_external_status_counts_distinct_explicit_setup_failures_without_busy_edge(
    status_route: _StatusRoute,
) -> None:
    """Consecutive setup failures with distinct response IDs are separate source events."""
    route = status_route
    observations = []
    with capture_debug_rows("server") as rows:
        for response_id in ("first-turn", "second-turn", "second-turn"):
            response = await route.client.post(
                f"/v1/sessions/{route.child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {
                        "status": "failed",
                        "response_id": response_id,
                        "output": "same setup failure",
                    },
                },
            )
            assert response.status_code == 202, response.text
            await _flush_live_state()
            observations.append(route.store.get_session_observation(route.child_id))
    first, second, retry = observations
    assert first.response_id == "first-turn"
    assert second.response_id == "second-turn"
    assert second.observation_id != first.observation_id
    assert retry == second
    failures = [row for row in rows if row["event_name"] == "session_turn_failed"]
    assert len(failures) == 2
    assert [row["attributes"]["response_id"] for row in failures] == [
        "first-turn",
        "second-turn",
    ]
    assert [row["attributes"]["source_event_id"] for row in failures] == [
        first.observation_id,
        second.observation_id,
    ]


@pytest.mark.parametrize("boundary", ["ensure", "append"])
@pytest.mark.parametrize("replacement", ["same-binding", "rebound", "aba", "current"])
async def test_native_boot_failure_keeps_captured_dispatch_through_awaits(
    status_route: _StatusRoute,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    replacement: str,
) -> None:
    """Old setup failure keeps its history but cannot fail replacement work."""
    from omnigent.runner import create_runner_app, subagent_work
    from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
    from tests.runner.test_app_sessions_native_supervision import _WakeRecordingServerClient

    route = status_route
    sid = route.child_id
    route.store.replace_runner_id(sid, "runner-a")
    route.store.set_session_live_status(sid, "idle")
    route.store.set_labels(
        sid,
        {
            "omnigent.wrapper": "claude-code-native-ui",
            "omnigent.subagent.dispatch_id": "dispatch-a",
        },
    )
    snapshot = route.store.get_conversation(sid)
    assert snapshot is not None
    entered, release = threading.Event(), threading.Event()
    append = route.store.append

    def delayed_append(session_id, items, **kwargs):
        if boundary == "append" and session_id == sid and items[0].type == "message":
            entered.set()
            assert release.wait(10)
        return append(session_id, items, **kwargs)

    async def ensure_failure(*_args, **_kwargs):
        if boundary == "ensure":
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
        return helpers._NativeTerminalEnsureOutcome(
            error=ErrorData(
                source="execution", code="native_terminal_start_failed", message="boot A failed"
            )
        )

    monkeypatch.setattr(route.store, "append", delayed_append)
    monkeypatch.setattr(orchestration, "_ensure_native_terminal_ready", ensure_failure)
    server = _WakeRecordingServerClient(route.parent_id)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),
        server_client=server,
    )
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[route.parent_id] = inbox
    subagent_work.register_subagent_work(
        parent_session_id=route.parent_id,
        child_session_id=sid,
        agent="worker",
        title="Worker",
        work_id="dispatch-a",
    )
    forwarded = []
    try:
        async with _runner_client(app) as runner:

            async def forward(session_id, _router, body, **_kwargs):
                forwarded.append(body)
                response = await runner.post(f"/v1/sessions/{session_id}/events", json=body)
                return helpers._RunnerForwardResult(response.status_code, response.text)

            monkeypatch.setattr(orchestration, "_forward_session_change_to_runner", forward)
            with capture_debug_rows("server") as rows:
                pending = asyncio.create_task(
                    orchestration._dispatch_session_event_to_runner_impl(
                        sid,
                        snapshot,
                        SessionEventInput(
                            type="message",
                            data={
                                "role": "user",
                                "content": [{"type": "input_text", "text": "request A"}],
                            },
                        ),
                        route.store,
                        runner,
                        agent_name="worker",
                        file_store=None,
                        artifact_store=None,
                    )
                )
                try:
                    assert await asyncio.to_thread(entered.wait, 10)
                    if replacement != "current":
                        if replacement in {"rebound", "aba"}:
                            route.store.replace_runner_id(sid, "runner-b")
                        if replacement == "aba":
                            route.store.replace_runner_id(sid, "runner-a")
                        route.store.set_labels(
                            sid, {"omnigent.subagent.dispatch_id": "dispatch-b"}
                        )
                        current = route.store.record_session_observation(
                            sid, "running", response_id="turn-b"
                        )
                        assert current is not None
                        helpers._publish_status(
                            sid, "running", persist_live_status=False, session_observation=current
                        )
                        subagent_work.unregister_subagent_work(sid)
                        subagent_work.register_subagent_work(
                            parent_session_id=route.parent_id,
                            child_session_id=sid,
                            agent="worker",
                            title="Worker B",
                            work_id="dispatch-b",
                        )
                        subagent_work.mark_subagent_work_started(sid)
                        route.published.reset_mock()
                    release.set()
                    result = await asyncio.wait_for(pending, 10)
                    assert result.item_id is not None
                    await _flush_live_state()
                finally:
                    release.set()
                    if not pending.done():
                        pending.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await pending
            entry = subagent_work.get_subagent_work(sid)
            assert entry is not None
            if replacement == "current":
                assert route.store.get_session_observation(sid).status == "failed"
                assert entry.work_id == "dispatch-a" and entry.status == "failed"
                assert inbox.qsize() == 1
                assert len(forwarded) == 1
                assert forwarded[0]["data"]["subagent_dispatch_id"] == "dispatch-a"
                assert forwarded[0]["data"]["session_observation"] == (
                    route.store.get_session_observation(sid).model_dump()
                )
            else:
                assert route.store.get_session_observation(sid) == current
                assert entry.work_id == "dispatch-b" and entry.status == "running"
                assert inbox.empty()
                assert not forwarded
                assert not any(
                    call.args[1].get("type") in {"response.error", "session.terminal_pending"}
                    for call in route.published.call_args_list
                )
            items = route.store.list_items(sid).data
            assert [item.type for item in items] == ["message", "error"]
            assert items[1].data.message == "boot A failed"
            assert len([row for row in rows if row["event_name"] == "session_turn_failed"]) == 1
    finally:
        release.set()
        subagent_work.unregister_subagent_work(sid)
        subagent_work._session_inboxes_ref.pop(route.parent_id, None)


@pytest.mark.parametrize("producer", ["native", "host"])
@pytest.mark.parametrize("rebound", [False, True], ids=["same-binding", "rebound"])
async def test_setup_failure_resuming_after_commit_cannot_clear_new_turn(
    status_route: _StatusRoute,
    monkeypatch: pytest.MonkeyPatch,
    producer: str,
    rebound: bool,
) -> None:
    """A committed A result keeps its token when B starts before A's caller resumes."""
    from omnigent.runner import create_runner_app, subagent_work
    from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
    from tests.runner.test_app_sessions_native_supervision import _WakeRecordingServerClient

    route = status_route
    sid = route.child_id
    route.store.replace_runner_id(sid, "runner-a")
    route.store.set_session_live_status(sid, "idle")
    route.store.set_labels(sid, {"omnigent.subagent.dispatch_id": "dispatch-a"})
    snapshot = route.store.get_conversation(sid)
    assert snapshot is not None
    owner, predecessor = helpers._capture_status_observation(sid, snapshot.session_observation)
    committed, release = asyncio.Event(), asyncio.Event()
    await_status = orchestration._await_failure_status
    accepted_observations = []

    async def pause_after_commit(status_write):
        result = await await_status(status_write)
        accepted_observations.append(result[1])
        committed.set()
        await release.wait()
        return result

    monkeypatch.setattr(orchestration, "_await_failure_status", pause_after_commit)
    server = _WakeRecordingServerClient(route.parent_id)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])), server_client=server
    )
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[route.parent_id] = inbox
    forwarded = []
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "request A"}]},
    )
    try:
        async with _runner_client(app) as runner:

            async def forward(session_id, _router, event, **_kwargs):
                forwarded.append(event)
                response = await runner.post(f"/v1/sessions/{session_id}/events", json=event)
                return helpers._RunnerForwardResult(response.status_code, response.text)

            monkeypatch.setattr(orchestration, "_forward_session_change_to_runner", forward)
            kwargs = {
                "created_by": None,
                "owner": owner,
                "predecessor": predecessor,
                "dispatch_id": "dispatch-a",
            }
            if producer == "native":
                failure = orchestration._persist_native_terminal_failure(
                    sid,
                    snapshot,
                    body,
                    route.store,
                    ErrorData(
                        source="execution", code="native_terminal_start_failed", message="A failed"
                    ),
                    None,
                    **kwargs,
                )
            else:
                failure = orchestration._persist_host_launch_failure_turn(
                    sid,
                    snapshot,
                    body,
                    route.store,
                    "A failed",
                    None,
                    host_error_code=orchestration._HARNESS_NOT_CONFIGURED_ERROR_CODE,
                    **kwargs,
                )
            with capture_debug_rows("server") as rows:
                pending = asyncio.create_task(failure)
                try:
                    await asyncio.wait_for(committed.wait(), 10)
                    assert accepted_observations[0] is not None
                    assert not pending.done()
                    if rebound:
                        route.store.replace_runner_id(sid, "runner-b")
                    route.store.set_labels(sid, {"omnigent.subagent.dispatch_id": "dispatch-b"})
                    current = route.store.record_session_observation(
                        sid, "running", response_id="turn-b"
                    )
                    assert current is not None
                    current = route.store.begin_terminal_startup(sid, current, "startup-b")
                    assert current is not None
                    helpers._publish_status(
                        sid, "running", persist_live_status=False, session_observation=current
                    )
                    helpers._publish_terminal_pending(sid, True, session_observation=current)
                    subagent_work.register_subagent_work(
                        parent_session_id=route.parent_id,
                        child_session_id=sid,
                        agent="worker",
                        title="Worker B",
                        work_id="dispatch-b",
                    )
                    subagent_work.mark_subagent_work_started(sid)
                    route.published.reset_mock()
                    release.set()
                    await asyncio.wait_for(pending, 10)
                    await _flush_live_state()
                finally:
                    release.set()
                    if not pending.done():
                        pending.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await pending
            assert route.store.get_session_observation(sid) == current
            entry = subagent_work.get_subagent_work(sid)
            assert entry is not None and entry.status == "running"
            assert inbox.empty()
            assert common._session_terminal_pending_cache[sid] is True
            assert not any(
                call.args[1].get("type") in {"response.error", "session.terminal_pending"}
                for call in route.published.call_args_list
            )
            assert len(forwarded) == 1
            assert forwarded[0]["data"]["subagent_dispatch_id"] == "dispatch-a"
            assert (
                forwarded[0]["data"]["session_observation"]
                == accepted_observations[0].model_dump()
            )
            assert [item.type for item in route.store.list_items(sid).data] == ["message", "error"]
            assert len([row for row in rows if row["event_name"] == "session_turn_failed"]) == 1
    finally:
        release.set()
        subagent_work.unregister_subagent_work(sid)
        subagent_work._session_inboxes_ref.pop(route.parent_id, None)
        common._session_terminal_pending_cache.pop(sid, None)


def test_owned_terminal_pending_does_not_clear_a_newer_pending_owner_without_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending lifecycle can learn B before B's status edge reaches this replica."""
    from omnigent.entities import Conversation
    from omnigent.entities.session_observation import SessionObservation

    sid = uuid4().hex
    old = SessionObservation(runner_id="runner-a", binding_epoch=1, revision=1, observation_id="a")
    newer = old.model_copy(update={"revision": 2, "observation_id": "b"})
    published = Mock()
    monkeypatch.setattr(session_stream, "publish", published)
    try:
        helpers._publish_status(sid, "running", persist_live_status=False, session_observation=old)
        helpers._publish_terminal_pending(
            sid, True, session_observation=newer, response_id="turn-b"
        )
        assert common._session_terminal_pending_cache[sid] is True
        assert common._session_status_cache.projection(sid).observation == old
        snapshot = Conversation(
            id=sid,
            created_at=1,
            updated_at=1,
            root_conversation_id=sid,
            runner_id=old.runner_id,
            runner_binding_epoch=old.binding_epoch,
            live_status_revision=old.revision,
            live_status_observation_id=old.observation_id,
        )
        common._session_terminal_pending_cache.hydrate(snapshot)
        assert common._session_terminal_pending_cache[sid] is True
        published.reset_mock()
        helpers._publish_terminal_pending(
            sid, False, session_observation=old, response_id="turn-a"
        )
        assert common._session_terminal_pending_cache[sid] is True
        published.assert_not_called()
        helpers._publish_terminal_pending(
            sid, False, session_observation=newer, response_id="turn-b"
        )
        assert common._session_terminal_pending_cache.get(sid, False) is False
        assert published.call_count == 1
        helpers._publish_terminal_pending(
            sid, True, session_observation=newer, response_id="turn-b"
        )
        snapshot.live_status_revision = 3
        snapshot.live_status_observation_id = "c"
        common._session_terminal_pending_cache.hydrate(snapshot)
        assert common._session_terminal_pending_cache.get(sid, False) is False
        published.reset_mock()
        helpers._publish_terminal_pending(
            sid, True, session_observation=newer, response_id="turn-b"
        )
        assert common._session_terminal_pending_cache.get(sid, False) is False
        published.assert_not_called()
    finally:
        common._session_status_cache.pop(sid, None)
        common._session_terminal_pending_cache.pop(sid, None)


async def test_real_concurrent_terminal_ensures_reserve_order_without_running_edge(
    status_route: _StatusRoute,
) -> None:
    """A's actual ensure completion cannot clear B's newer reserved startup."""
    from omnigent.entities.session_observation import SessionObservation

    route = status_route
    sid = route.child_id
    route.store.replace_runner_id(sid, "runner-a")
    route.store.set_labels(sid, {"omnigent.wrapper": "claude-code-native-ui"})
    prior = route.store.record_session_observation(
        sid,
        "failed",
        response_id="prior-turn",
        error_key="prior-error",
        error_labels={
            "omnigent.last_task_error_code": "prior-error",
            "omnigent.last_task_error_message": "prior genuine failure",
        },
    )
    assert prior is not None
    snapshot = route.store.get_conversation(sid)
    assert snapshot is not None
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    observed = []

    async def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{sid}/resources/terminals"
        body = json.loads(request.content)
        assert body["ensure_native_terminal"] is True
        observation = SessionObservation.model_validate(body["session_observation"])
        index = len(observed)
        observed.append(observation)
        entered[index].set()
        await release[index].wait()
        return httpx.Response(200, json={})

    tasks = []
    with capture_debug_rows("server") as rows:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider), base_url="http://runner"
        ) as runner:
            try:
                tasks.append(
                    asyncio.create_task(
                        orchestration._ensure_native_terminal_ready(runner, sid, snapshot)
                    )
                )
                await asyncio.wait_for(entered[0].wait(), 10)
                before_b = route.store.get_conversation(sid)
                assert before_b is not None
                tasks.append(
                    asyncio.create_task(
                        orchestration._ensure_native_terminal_ready(runner, sid, before_b)
                    )
                )
                await asyncio.wait_for(entered[1].wait(), 10)
                first, second = observed
                assert second.revision > first.revision > prior.revision
                assert second.observation_id != first.observation_id
                assert (
                    (first.status, first.response_id, first.error_key)
                    == (second.status, second.response_id, second.error_key)
                    == (prior.status, prior.response_id, prior.error_key)
                )
                current = route.store.get_conversation(sid)
                assert current is not None and current.session_observation == second
                assert current.terminal_pending is True
                assert (
                    current.labels["omnigent.last_task_error_message"] == "prior genuine failure"
                )
                assert common._session_terminal_pending_cache[sid] is True
                route.published.reset_mock()
                helpers._publish_terminal_pending(sid, True, session_observation=first)
                common._session_terminal_pending_cache.hydrate(before_b)
                assert common._session_terminal_pending_cache[sid] is True
                release[0].set()
                with pytest.raises(OmnigentError, match="session changed"):
                    await asyncio.wait_for(tasks[0], 10)
                assert common._session_terminal_pending_cache[sid] is True
                assert route.store.get_conversation(sid).terminal_pending is True
                assert not any(
                    call.args[1].get("type") == "session.terminal_pending"
                    for call in route.published.call_args_list
                )
                release[1].set()
                outcome_b = await asyncio.wait_for(tasks[1], 10)
                completed = outcome_b.observation
                assert outcome_b.error is None and completed is not None
                assert completed.revision > second.revision
                assert (completed.status, completed.response_id, completed.error_key) == (
                    second.status,
                    second.response_id,
                    second.error_key,
                )
                assert common._session_terminal_pending_cache.get(sid, False) is False
                pending_events = [
                    call.args[1]
                    for call in route.published.call_args_list
                    if call.args[1].get("type") == "session.terminal_pending"
                ]
                assert len(pending_events) == 1
                assert pending_events[0]["pending"] is False
                assert pending_events[0]["session_observation"] == completed.model_dump()
                assert route.store.get_session_observation(sid) == completed
                assert route.store.get_conversation(sid).terminal_pending is False
                assert not any(row["event_name"] == "session_turn_failed" for row in rows)
            finally:
                for gate in release:
                    gate.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                common._session_terminal_pending_cache.pop(sid, None)


@pytest.mark.parametrize("busy_at_ingress", [False, True], ids=["delayed-denial", "already-busy"])
async def test_delayed_input_policy_denial_cannot_settle_a_newer_turn(
    status_route: _StatusRoute,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    busy_at_ingress: bool,
) -> None:
    """A denied input keeps its sentinel history without ending concurrent work."""
    route = status_route
    agent_id = uuid4().hex
    SqlAlchemyAgentStore(db_uri).create(agent_id, "policy-worker", "unused-test-bundle")
    conv = route.store.create_conversation(agent_id=agent_id, runner_id="runner-a")
    sid = conv.id
    old = route.store.record_session_observation(
        sid,
        "running" if busy_at_ingress else "idle",
        response_id="turn-b" if busy_at_ingress else None,
    )
    assert old is not None
    helpers._publish_status(
        sid,
        old.status,
        response_id=old.response_id,
        persist_live_status=False,
        session_observation=old,
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def deny_after_wait(*_args, **_kwargs):
        entered.set()
        if not busy_at_ingress:
            await release.wait()
        return {"verdict": "deny", "reason": "denied input A"}

    monkeypatch.setattr(routes_events, "_evaluate_input_policy", deny_after_wait)
    pending = asyncio.create_task(
        route.client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "A"}]},
            },
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        current = (
            old
            if busy_at_ingress
            else route.store.record_session_observation(sid, "running", response_id="turn-b")
        )
        assert current is not None
        helpers._publish_status(
            sid,
            "running",
            response_id="turn-b",
            persist_live_status=False,
            session_observation=current,
        )
        route.published.reset_mock()
        release.set()
        response = await asyncio.wait_for(pending, 10)
        assert response.status_code == 202, response.text
        assert response.json()["denied"] is True
        await _flush_live_state()
        assert route.store.get_session_observation(sid) == current
        assert common._session_active_response_cache[sid] == "turn-b"
        assert len(route.store.list_items(sid).data) == 1
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        common._session_status_cache.pop(sid, None)
        common._session_active_response_cache.pop(sid, None)


def test_rejected_sticky_idle_does_not_clear_another_active_response() -> None:
    """A rejected A idle cannot erase B's response staged before its status commits."""
    from omnigent.entities.session_observation import SessionObservation

    sid = uuid4().hex
    failed = SessionObservation(
        runner_id="runner-a",
        binding_epoch=1,
        revision=2,
        observation_id="failed-a",
        status="failed",
        response_id="turn-a",
    )
    try:
        common._session_active_response_cache[sid] = "turn-a"
        helpers._publish_status(
            sid,
            "failed",
            persist_live_status=False,
            session_observation=failed,
            failure_logged=True,
        )
        assert sid not in common._session_active_response_cache
        common._session_active_response_cache[sid] = "turn-b"
        helpers._publish_status(sid, "idle", response_id="turn-a", owner=failed)
        assert common._session_status_cache[sid] == "failed"
        assert common._session_active_response_cache[sid] == "turn-b"
    finally:
        common._session_status_cache.pop(sid, None)
        common._session_active_response_cache.pop(sid, None)


async def test_delayed_relay_running_and_terminal_keep_new_owner_state_and_old_history(
    status_route: _StatusRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected relay edge cannot consume B's deny marker or lose A's prose."""
    route = status_route
    sid = route.child_id
    route.store.replace_runner_id(sid, "runner-a")
    original = route.store.record_session_observation(sid, "running", response_id="turn-a")
    assert original is not None
    helpers._publish_status(
        sid,
        "running",
        response_id="turn-a",
        persist_live_status=False,
        session_observation=original,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    persist_labels = orchestration._persist_session_status_error_labels

    async def pause_running_labels(session_id, error, store, **kwargs):
        if session_id == sid and error is None:
            entered.set()
            await release.wait()
        return await persist_labels(session_id, error, store, **kwargs)

    monkeypatch.setattr(
        orchestration, "_persist_session_status_error_labels", pause_running_labels
    )
    start = asyncio.Event()
    start.set()
    runner = _ScriptedRunnerClient(
        start,
        [
            {"type": "response.in_progress", "response": {"id": "turn-a", "model": "worker"}},
            {"type": "response.output_text.delta", "delta": "A narration"},
            {"type": "session.status", "status": "running", "response_id": "turn-a"},
            {"type": "response.completed", "response": {"id": "turn-a", "model": "worker"}},
        ],
    )
    pending = asyncio.create_task(
        orchestration._relay_runner_stream_once(
            sid, runner, route.store, runner_id="runner-a", owner=original
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        route.store.replace_runner_id(sid, "runner-b")
        current = route.store.record_session_observation(sid, "running", response_id="turn-b")
        assert current is not None
        helpers._publish_status(
            sid,
            "running",
            response_id="turn-b",
            persist_live_status=False,
            session_observation=current,
        )
        common._llm_response_denied_turns[sid] = "deny B only"
        release.set()
        await asyncio.wait_for(pending, 10)
        await _flush_live_state()
        assert route.store.get_session_observation(sid) == current
        assert common._session_active_response_cache[sid] == "turn-b"
        assert common._llm_response_denied_turns[sid] == "deny B only"
        narration = [
            item
            for item in route.store.list_items(sid).data
            if isinstance(item.data, MessageData) and item.data.role == "assistant"
        ]
        assert len(narration) == 1
        assert narration[0].response_id == "turn-a"
        assert narration[0].data.content == [{"type": "output_text", "text": "A narration"}]
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        common._llm_response_denied_turns.pop(sid, None)


async def test_delayed_external_terminal_status_cannot_forward_into_new_child_turn(
    status_route: _StatusRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual HTTP callback rejects old terminal effects after enrichment yields."""
    route = status_route
    sid = route.child_id
    route.store.set_runner_id(sid, "runner-a")
    route.store.set_labels(sid, {"omnigent.subagent.dispatch_id": "dispatch-a"})
    route.store.record_session_observation(sid, "running", response_id="turn-a")
    historical = route.store.append(
        sid,
        [
            NewConversationItem(
                type="message",
                response_id="turn-a",
                data=MessageData(
                    role="assistant",
                    agent="worker",
                    content=[{"type": "text", "text": "old source failure"}],
                ),
            )
        ],
    )[0]
    entered = asyncio.Event()
    release = asyncio.Event()
    enrich = routes_events._enrich_terminal_status_with_subagent_output

    async def delayed_enrichment(data, status, session_id, store, **kwargs):
        if data.get("response_id") == "turn-a":
            entered.set()
            await release.wait()
        return await enrich(data, status, session_id, store, **kwargs)

    monkeypatch.setattr(
        routes_events, "_enrich_terminal_status_with_subagent_output", delayed_enrichment
    )
    with capture_debug_rows("server") as rows:
        pending = asyncio.create_task(
            route.client.post(
                f"/v1/sessions/{sid}/events",
                json={
                    "type": "external_session_status",
                    "data": {
                        "status": "failed",
                        "response_id": "turn-a",
                        "output": "old source failure",
                        "turn_outcome": "failed",
                    },
                },
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            route.store.set_labels(sid, {"omnigent.subagent.dispatch_id": "dispatch-b"})
            running = await route.client.post(
                f"/v1/sessions/{sid}/events",
                json={
                    "type": "external_session_status",
                    "data": {
                        "status": "running",
                        "response_id": "turn-b",
                    },
                },
            )
            assert running.status_code == 202, running.text
            await _flush_live_state()
            expected = route.store.get_session_observation(sid)
            assert expected is not None and expected.response_id == "turn-b"
            new_turn_forward = route.forwarded[-1][1]
            route.forwarded.clear()
            route.published.reset_mock()
            release.set()
            response = await asyncio.wait_for(pending, timeout=10)
            assert response.status_code == 202, response.text
            await _flush_live_state()
            assert route.store.get_session_observation(sid) == expected
            assert common._session_status_cache[sid] == "running"
            assert route.forwarded == []
            assert new_turn_forward["data"]["subagent_dispatch_id"] == "dispatch-b"
            assert new_turn_forward["data"]["session_observation"] == expected.model_dump()
            assert all(
                call.args[1].get("status") != "failed" for call in route.published.call_args_list
            )
            assert historical.id in [item.id for item in route.store.list_items(sid).data]
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
    failures = [row for row in rows if row["event_name"] == "session_turn_failed"]
    assert len(failures) == 1
    assert failures[0]["attributes"]["response_id"] == "turn-a"


@pytest.mark.parametrize("is_child", [False, True], ids=["main", "native-child"])
@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        ("external_session_status", {"status": "running"}),
        ("external_session_status", {"status": "failed"}),
        ("subagent.status", {"idle": True}),
    ],
)
async def test_status_observes_existing_session_metadata_off_event_loop(
    status_route: _StatusRoute,
    monkeypatch: pytest.MonkeyPatch,
    is_child: bool,
    event_type: str,
    data: dict[str, Any],
) -> None:
    route = status_route
    sid = route.child_id if is_child else route.parent_id
    if is_child:
        route.store.set_labels(sid, {"omnigent.wrapper": "claude-code-native-ui-subagent"})
    monkeypatch.setattr(routes_events, "debug_sink_enabled", lambda: True)
    monkeypatch.setattr(session_metadata_logging, "debug_sink_enabled", lambda: True)
    event_loop_thread = threading.get_ident()
    observe = routes_events.log_session_metadata

    def observe_in_worker(*args: Any, **kwargs: Any) -> None:
        assert threading.get_ident() != event_loop_thread
        observe(*args, **kwargs)

    monkeypatch.setattr(routes_events, "log_session_metadata", observe_in_worker)
    with capture_debug_rows("server") as rows:
        response = await route.client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": event_type, "data": data},
        )

    assert response.status_code == 202, response.text
    observations = [row for row in rows if row["event_name"] == "session_metadata"]
    assert len(observations) == 1
    assert observations[0]["session_id"] == sid
    attrs = observations[0]["attributes"]
    assert attrs["observation"] == event_type
    assert attrs["root_session_id"] == route.parent_id
    if is_child:
        assert attrs["session_kind"] == "sub_agent"
        assert attrs["parent_session_id"] == route.parent_id
        assert attrs["harness"] == "claude-native"
    else:
        assert attrs["session_kind"] == "default"
        assert "parent_session_id" not in attrs
        assert "harness" not in attrs
        assert attrs["harness_source"] == "missing_agent"
        assert attrs["harness_resolution"] == "unknown"


@pytest.mark.parametrize(
    ("harness", "status", "confirmation", "wrapper", "expected"),
    [
        ("claude-native", "idle", {}, None, None),
        ("auto", "idle", {}, None, None),
        ("any", "idle", {}, None, None),
        ("claude-native", "idle", {"turn_completed": True}, None, "completed"),
        ("cursor-native", "idle", {"turn_outcome": "cancelled"}, None, "cancelled"),
        ("cursor-native", "idle", {"turn_outcome": "failed"}, None, "failed"),
        ("codex-native", "idle", {}, None, "completed"),
        ("claude-sdk", "idle", {}, "claude-code-native-ui", "completed"),
        ("claude-native", "failed", {}, None, "failed"),
        (
            "claude-native",
            "idle",
            {"turn_completed": True},
            "claude-code-native-ui-subagent",
            None,
        ),
        ("claude-native", "failed", {}, "claude-code-native-ui-subagent", None),
    ],
)
async def test_external_child_activity_uses_confirmed_outcome(
    status_route: _StatusRoute,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    status: str,
    confirmation: dict[str, Any],
    wrapper: str | None,
    expected: str | None,
) -> None:
    route = status_route
    if wrapper is not None:
        route.store.set_labels(route.child_id, {"omnigent.wrapper": wrapper})
    monkeypatch.setattr(sessions, "_resolve_harness", lambda *args, **kwargs: harness)
    response = await route.client.post(
        f"/v1/sessions/{route.child_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": status, "response_id": "child-turn", **confirmation},
        },
    )
    assert response.status_code == 202, response.text
    items = route.store.list_items(route.parent_id).data
    if expected is None:
        assert items == []
    else:
        assert len(items) == 1
        assert items[0].data.event_type == "session.subagent.returned"
        assert items[0].data.resource["status"] == expected


@pytest.mark.parametrize("harness", ["claude-native", "auto"])
async def test_claude_child_idle_observations_do_not_complete_new_response_ids(
    status_route: _StatusRoute, harness: str
) -> None:
    route = status_route
    route.store.update_conversation(route.child_id, harness_override=harness)
    route.store.set_labels(route.child_id, {"omnigent.wrapper": "claude-code-native-ui"})

    for turn in range(2):
        for observation in range(3):
            response_id = f"claude-turn-{turn}-observation-{observation}"
            route.store.append(
                route.child_id,
                [
                    NewConversationItem(
                        type="message",
                        response_id=response_id,
                        data=MessageData(
                            role="assistant",
                            agent="claude-native",
                            content=[
                                {"type": "output_text", "text": "Still working on the task."}
                            ],
                        ),
                    )
                ],
            )
            response = await route.client.post(
                f"/v1/sessions/{route.child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "response_id": response_id},
                },
            )
            assert response.status_code == 202, response.text
            assert len(route.store.list_items(route.parent_id).data) == turn

        response = await route.client.post(
            f"/v1/sessions/{route.child_id}/events",
            json={
                "type": "external_session_status",
                "data": {
                    "status": "idle",
                    "response_id": response_id,
                    "turn_completed": True,
                },
            },
        )
        assert response.status_code == 202, response.text
        notices = route.store.list_items(route.parent_id).data
        assert len(notices) == turn + 1
        assert all(item.data.event_type == "session.subagent.returned" for item in notices)
        assert all(item.data.resource_id == route.child_id for item in notices)
        assert all(item.data.resource["status"] == "completed" for item in notices)


@pytest.mark.parametrize(
    ("harness", "wrapper"),
    [
        ("claude-native", None),
        ("codex-native", None),
        ("auto", "claude-code-native-ui"),
    ],
)
@pytest.mark.parametrize("request_type", ["message", "slash_command"])
async def test_native_completion_notices_share_the_initiating_request(
    status_route: _StatusRoute, harness: str, wrapper: str | None, request_type: str
) -> None:
    """Native notification turns finish the same request; a real follow-up starts another."""
    route = status_route
    route.store.update_conversation(route.child_id, harness_override=harness)
    if wrapper is not None:
        route.store.set_labels(route.child_id, {"omnigent.wrapper": wrapper})

    def append_turn(response_id: str, *, is_meta: bool = False) -> None:
        request = NewConversationItem(
            type="message",
            response_id=f"input-{response_id}",
            data=MessageData(
                role="user",
                is_meta=is_meta,
                content=[
                    {
                        "type": "input_text",
                        "text": "Native task update" if is_meta else "Audit this change",
                    }
                ],
            ),
        )
        if request_type == "slash_command" and not is_meta:
            request = NewConversationItem(
                type="slash_command",
                response_id=f"input-{response_id}",
                data=SlashCommandData(
                    agent="native", kind="skill", name="review", arguments="Audit this change"
                ),
            )
        route.store.append(
            route.child_id,
            [
                request,
                NewConversationItem(
                    type="message",
                    response_id=response_id,
                    data=MessageData(
                        role="assistant",
                        agent="native",
                        content=[{"type": "output_text", "text": "Done"}],
                    ),
                ),
            ],
        )

    async def complete(response_id: str) -> None:
        response = await route.client.post(
            f"/v1/sessions/{route.child_id}/events",
            json={
                "type": "external_session_status",
                "data": {
                    "status": "idle",
                    "response_id": response_id,
                    "turn_completed": True,
                },
            },
        )
        assert response.status_code == 202, response.text

    append_turn("native-first")
    await complete("native-first")
    first_notice = route.store.list_items(route.parent_id).data[0]
    for index in range(112):
        response_id = f"native-notification-{index}"
        append_turn(response_id, is_meta=True)
        if index in (0, 1, 111):
            await complete(response_id)
            assert [item.id for item in route.store.list_items(route.parent_id).data] == [
                first_notice.id
            ]

    append_turn("native-follow-up")
    # A delayed status retry must still belong to the earlier request.
    await complete("native-first")
    assert len(route.store.list_items(route.parent_id).data) == 1
    await complete("native-follow-up")
    await complete("native-follow-up")
    notices = route.store.list_items(route.parent_id).data
    assert len(notices) == 2
    assert notices[0].id == first_notice.id
    assert all(item.data.event_type == "session.subagent.returned" for item in notices)
    assert all(item.data.resource["status"] == "completed" for item in notices)


@pytest.mark.parametrize(
    ("harness", "wrapper", "native_harness"),
    [
        ("claude-native", None, "claude-native"),
        ("codex-native", None, "codex-native"),
        ("auto", "claude-code-native-ui", "claude-native"),
        ("auto", "codex-native-ui", "codex-native"),
    ],
)
async def test_native_child_completes_once_per_confirmed_turn(
    status_route: _StatusRoute, harness: str, wrapper: str | None, native_harness: str
) -> None:
    """Delivery acknowledgments and repeated status posts do not duplicate actual results."""
    from omnigent.server.routes._sessions.orchestration import _relay_runner_stream_once

    route = status_route
    route.store.update_conversation(route.child_id, harness_override=harness)
    if wrapper is not None:
        route.store.set_labels(route.child_id, {"omnigent.wrapper": wrapper})
    release = asyncio.Event()
    release.set()
    for index in range(2):
        submission_id = f"submission-{index}"
        await _relay_runner_stream_once(
            route.child_id,
            _ScriptedRunnerClient(
                release,
                [
                    {"type": "response.in_progress", "response": {"id": submission_id}},
                    {"type": "response.completed", "response": {"id": submission_id}},
                    {"type": "session.status", "status": "idle"},
                    {"type": "session.status", "status": "idle"},
                ],
            ),
            route.store,
        )
        assert len(route.store.list_items(route.parent_id).data) == index
        if index == 0:
            assert route.store.list_items(route.child_id).data == []

        native_turn_id = f"native-turn-{index}"
        route.store.append(
            route.child_id,
            [
                NewConversationItem(
                    type="message",
                    response_id=native_turn_id,
                    data=MessageData(
                        role="assistant",
                        agent=native_harness,
                        content=[{"type": "output_text", "text": "Finished the child task."}],
                    ),
                )
            ],
        )
        for _ in range(2):
            response = await route.client.post(
                f"/v1/sessions/{route.child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {
                        "status": "idle",
                        "response_id": native_turn_id,
                        **({"turn_completed": True} if native_harness == "claude-native" else {}),
                    },
                },
            )
            assert response.status_code == 202, response.text
        notices = route.store.list_items(route.parent_id).data
        assert len(notices) == index + 1
        assert all(item.data.event_type == "session.subagent.returned" for item in notices)
        assert all(item.data.resource_id == route.child_id for item in notices)
        assert all(item.data.resource["status"] == "completed" for item in notices)

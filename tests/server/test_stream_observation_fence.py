"""Paused runner publication must preserve a newer response's replay and live tail."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from omnigent.entities import MessageData
from omnigent.entities.session_observation import SessionObservation
from omnigent.runtime import inflight_text, session_stream
from omnigent.server import session_live_state
from omnigent.server.routes._sessions import common, orchestration
from omnigent.server.routes.sessions import routes_events
from omnigent.server.session_observations import SessionProjection
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent, start_session_stream_collector


@pytest.fixture
def store(db_uri: str) -> Iterator[SqlAlchemyConversationStore]:
    value = SqlAlchemyConversationStore(db_uri)
    common._session_status_cache.clear()
    common._session_active_response_cache.clear()
    inflight_text.reset_for_tests()
    session_live_state.configure(value)
    yield value
    session_live_state.submit("test_barrier", lambda: None).result(timeout=10)
    session_live_state.configure(None)
    common._session_status_cache.clear()
    common._session_active_response_cache.clear()
    inflight_text.reset_for_tests()


class _Frames:
    def __init__(
        self,
        events: list[dict[str, Any]],
        processed: asyncio.Event,
        release_close: asyncio.Event,
    ) -> None:
        self.events = events
        self.processed = processed
        self.release_close = release_close

    async def __aenter__(self) -> _Frames:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    def raise_for_status(self) -> None:
        pass

    async def aiter_text(self) -> AsyncIterator[str]:
        for event in self.events:
            yield "data: " + json.dumps(event) + "\n\n"
        self.processed.set()
        await self.release_close.wait()
        yield "data: [DONE]\n\n"


class _Runner:
    def __init__(
        self,
        events: list[dict[str, Any]],
        processed: asyncio.Event,
        release_close: asyncio.Event,
    ) -> None:
        self.events = events
        self.processed = processed
        self.release_close = release_close

    def stream(self, *_args: object, **_kwargs: object) -> _Frames:
        return _Frames(self.events, self.processed, self.release_close)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["response.in_progress", "session.status"])
@pytest.mark.parametrize("newer_turn", [False, True], ids=["completed", "newer-native-turn"])
@pytest.mark.parametrize(
    "harness", ["codex-wrapper", "codex-override", "pi-wrapper", "pi-override", "provider"]
)
async def test_native_scaffold_status_cannot_replace_the_forwarder_turn(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    newer_turn: bool,
    harness: str,
) -> None:
    native_terminal = harness != "provider"
    key = harness.rsplit("-", 1)[0]
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "labels": {"omnigent.wrapper": f"{key}-native-ui"}
            if harness.endswith("-wrapper")
            else {},
            "harness_override": f"{key}-native" if harness.endswith("-override") else None,
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    store.replace_runner_id(session_id, "runner-native")

    async def no_forward(*_args: Any, **_kwargs: Any) -> None:
        pass

    monkeypatch.setattr(routes_events, "_forward_session_change_to_runner", no_forward)

    async def status(value: str, response_id: str) -> httpx.Response:
        response = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": value, "response_id": response_id},
            },
        )
        assert response.status_code == 202, response.text
        return response

    first_response = f"{key}_turn-a" if native_terminal else "resp_provider-initial"
    completed_response = first_response if native_terminal else "resp_scaffold"
    next_response = f"{key}_turn-b"
    assert not (await status("running", first_response)).json().get("ignored")
    owner = store.get_session_observation(session_id)
    assert owner is not None and owner.response_id == first_response
    frame: dict[str, Any] = {"type": event_type}
    if event_type == "response.in_progress":
        frame["response"] = {"id": "resp_scaffold"}
    else:
        frame.update(status="running", response_id="resp_scaffold")
    processed, release_close = asyncio.Event(), asyncio.Event()
    relay = asyncio.create_task(
        orchestration._relay_runner_stream(
            session_id,
            _Runner([frame], processed, release_close),
            store,
            runner_id="runner-native",
        )
    )
    try:
        await asyncio.wait_for(processed.wait(), 10)
        if not native_terminal:
            assert store.get_session_observation(session_id).response_id == "resp_scaffold"
        if newer_turn:
            assert not (await status("running", next_response)).json().get("ignored")
        completed = await status("idle", completed_response)
        current = store.get_session_observation(session_id)
        assert current is not None
        if newer_turn:
            assert completed.json().get("ignored") is True
            assert (current.status, current.response_id) == ("running", next_response)
        else:
            assert not completed.json().get("ignored")
            assert (current.status, current.response_id) == ("idle", completed_response)
    finally:
        release_close.set()
        await asyncio.wait_for(relay, 10)


@pytest.mark.asyncio
async def test_native_output_and_status_hints_survive_an_older_relay_adoption(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "labels": {"omnigent.wrapper": "codex-native-ui"}},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    store.replace_runner_id(session_id, "runner-native")
    idle = store.record_session_observation(session_id, "idle")
    assert idle is not None
    assert common._session_status_cache.accept(session_id, SessionProjection(idle, "idle"))

    async def no_forward(*_args: Any, **_kwargs: Any) -> None:
        pass

    monkeypatch.setattr(routes_events, "_forward_session_change_to_runner", no_forward)
    connected, release_events = asyncio.Event(), asyncio.Event()
    processed, release_close = asyncio.Event(), asyncio.Event()

    class GatedFrames(_Frames):
        async def aiter_text(self) -> AsyncIterator[str]:
            connected.set()
            await release_events.wait()
            async for chunk in super().aiter_text():
                yield chunk

    class GatedRunner(_Runner):
        def stream(self, *_args: object, **_kwargs: object) -> _Frames:
            return GatedFrames(self.events, self.processed, self.release_close)

    frames = [
        {"type": "response.in_progress", "response": {"id": "resp_scaffold"}},
        {"type": "session.status", "status": "waiting", "blocked_on": "native approval"},
        {"type": "response.output_text.delta", "delta": "scaffold narration"},
        {"type": "response.completed", "response": {"id": "resp_scaffold"}},
        {"type": "session.status", "status": "idle", "response_id": "resp_scaffold"},
    ]
    collector = await start_session_stream_collector(session_id)
    relay = asyncio.create_task(
        orchestration._relay_runner_stream(
            session_id,
            GatedRunner(frames, processed, release_close),
            store,
            runner_id="runner-native",
        )
    )
    try:
        await asyncio.wait_for(connected.wait(), 10)
        running = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_session_status",
                "data": {
                    "status": "running",
                    "response_id": "codex_turn-a",
                    "background_task_count": 2,
                },
            },
        )
        assert running.status_code == 202 and not running.json().get("ignored")
        release_events.set()
        await asyncio.wait_for(processed.wait(), 10)
        current = store.get_session_observation(session_id)
        assert current is not None and current.status == "waiting"
        assert current.response_id == "codex_turn-a"
        assert common._session_background_task_count_cache[session_id] == 2
        native_delta = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_output_text_delta",
                "data": {"delta": "native live tail", "message_id": "native-message"},
            },
        )
        assert native_delta.status_code == 202, native_delta.text
        async with asyncio.timeout(10):
            while True:
                event = await collector.queue.get()
                if event.get("message_id") == "native-message":
                    assert event["delta"] == "native live tail"
                    break
        native_item = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "item_type": "message",
                    "response_id": "codex_turn-a",
                    "source_id": "native-message",
                    "item_data": {
                        "role": "assistant",
                        "agent": "codex-native-ui",
                        "content": [{"type": "output_text", "text": "native live tail"}],
                    },
                },
            },
        )
        assert native_item.status_code == 202, native_item.text
        completed = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_session_status",
                "data": {
                    "status": "idle",
                    "response_id": "codex_turn-a",
                    "background_task_count": 0,
                },
            },
        )
        assert completed.status_code == 202 and not completed.json().get("ignored")
        current = store.get_session_observation(session_id)
        assert current is not None and current.status == "idle"
        assert current.response_id == "codex_turn-a"
        assert session_id not in common._session_background_task_count_cache
        text = [
            block["text"]
            for item in store.list_items(session_id).data
            if isinstance(item.data, MessageData)
            for block in item.data.content
            if block.get("type") == "output_text"
        ]
        assert "scaffold narration" in text and "native live tail" in text
    finally:
        release_events.set()
        release_close.set()
        await asyncio.wait_for(relay, 10)
        await collector.stop()
        common._session_background_task_count_cache.pop(session_id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["response.completed", "response.failed"])
@pytest.mark.parametrize("owned", [False, True], ids=["scaffold", "owned-terminal"])
async def test_native_terminal_frames_preserve_source_history_and_fence_ownership(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    owned: bool,
) -> None:
    conv = store.create_conversation(
        runner_id="runner-native", labels={"omnigent.wrapper": "codex-native-ui"}
    )
    owner = store.record_session_observation(conv.id, "running", response_id="codex_turn-a")
    assert owner is not None
    assert common._session_status_cache.accept(conv.id, SessionProjection(owner, "running"))
    original_flush = orchestration._flush_relay_text

    async def fenced_flush(*args: Any, **kwargs: Any) -> Any:
        common._interrupt_fenced_sessions.add(conv.id)
        return await original_flush(*args, **kwargs)

    monkeypatch.setattr(orchestration, "_flush_relay_text", fenced_flush)
    terminal: dict[str, Any] = {
        "type": event_type,
        "response": {"id": owner.response_id if owned else "resp_scaffold"},
    }
    if owned:
        terminal["session_observation"] = owner.model_dump()
    if event_type == "response.failed":
        terminal["response"]["error"] = {
            "code": "runner_error",
            "message": "synthetic delivery failure",
        }
    processed, release_close = asyncio.Event(), asyncio.Event()
    release_close.set()
    try:
        await orchestration._relay_runner_stream(
            conv.id,
            _Runner(
                [
                    {"type": "response.in_progress", "response": {"id": "resp_scaffold"}},
                    {"type": "response.output_text.delta", "delta": "scaffold narration"},
                    terminal,
                    {
                        "type": "session.status",
                        "status": "failed" if event_type == "response.failed" else "idle",
                        "response_id": "resp_scaffold",
                    },
                ],
                processed,
                release_close,
            ),
            store,
            runner_id="runner-native",
        )
        assert store.get_session_observation(conv.id) == owner
        assert (conv.id in common._interrupt_fenced_sessions) is (not owned)
        items = store.list_items(conv.id).data
        assert any(isinstance(item.data, MessageData) for item in items)
        assert sum(item.type == "error" for item in items) == (event_type == "response.failed")
    finally:
        common._interrupt_fenced_sessions.discard(conv.id)


@pytest.mark.asyncio
async def test_native_relay_keeps_an_explicitly_owned_startup_failure(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(
        runner_id="runner-native", labels={"omnigent.wrapper": "codex-native-ui"}
    )
    startup = store.begin_terminal_startup(conv.id, conv.session_observation, "startup-token")
    assert startup is not None
    processed, release_close = asyncio.Event(), asyncio.Event()
    release_close.set()
    await orchestration._relay_runner_stream(
        conv.id,
        _Runner(
            [
                {
                    "type": "session.status",
                    "status": "failed",
                    "error": {"code": "native_terminal_start_failed", "message": "synthetic"},
                    "session_observation": startup.model_dump(),
                }
            ],
            processed,
            release_close,
        ),
        store,
        runner_id="runner-native",
    )
    current = store.get_conversation(conv.id)
    assert current is not None and current.live_status == "failed"
    assert not current.terminal_pending
    assert current.labels["omnigent.last_task_error_code"] == "native_terminal_start_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["kiro", "cursor", "goose", "qwen", "kimi", "hermes"])
@pytest.mark.parametrize("newer_turn", [False, True], ids=["delivery-failed", "newer-turn"])
@pytest.mark.parametrize("identified_by", ["wrapper", "harness"])
async def test_pty_native_delivery_failure_keeps_its_exact_owner(
    store: SqlAlchemyConversationStore,
    harness: str,
    newer_turn: bool,
    identified_by: str,
) -> None:
    conv = store.create_conversation(
        runner_id="runner-native",
        labels={"omnigent.wrapper": f"{harness}-native-ui"} if identified_by == "wrapper" else {},
        harness_override=f"{harness}-native" if identified_by == "harness" else None,
    )
    owner = store.record_session_observation(conv.id, "running")
    assert owner is not None
    assert common._session_status_cache.accept(conv.id, SessionProjection(owner, "running"))
    error = {"code": "runner_error", "message": "synthetic terminal delivery refusal"}
    processed, release_close = asyncio.Event(), asyncio.Event()
    release_close.set()

    class Frames(_Frames):
        async def aiter_text(self) -> AsyncIterator[str]:
            yield 'data: {"type":"response.in_progress","response":{"id":"resp_delivery"}}\n\n'
            if newer_turn:
                newer = store.record_session_observation(
                    conv.id, "running", response_id="native_newer-turn"
                )
                assert newer is not None
                assert common._session_status_cache.accept(
                    conv.id, SessionProjection(newer, "running")
                )
            async for frame in super().aiter_text():
                yield frame

    class Runner(_Runner):
        def stream(self, *_args: object, **_kwargs: object) -> _Frames:
            return Frames(self.events, processed, release_close)

    await orchestration._relay_runner_stream(
        conv.id,
        Runner(
            [
                {"type": "response.failed", "response": {"id": "resp_delivery", "error": error}},
                {
                    "type": "session.status",
                    "status": "failed",
                    "response_id": "resp_delivery",
                    "error": error,
                },
            ],
            processed,
            release_close,
        ),
        store,
        runner_id="runner-native",
    )
    current = store.get_conversation(conv.id)
    assert current is not None
    if newer_turn:
        assert current.live_status == "running"
        assert current.session_observation.response_id == "native_newer-turn"
        assert "omnigent.last_task_error_code" not in current.labels
    else:
        assert current.live_status == "failed"
        assert current.session_observation.response_id == "resp_delivery"
        assert current.labels["omnigent.last_task_error_code"] == "runner_error"
    assert sum(item.type == "error" for item in store.list_items(conv.id).data) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["response.completed", "response.failed"])
@pytest.mark.parametrize("owned", [False, True], ids=["scaffold", "owned-terminal"])
@pytest.mark.parametrize("newer_turn", [False, True], ids=["current", "newer-turn"])
async def test_codex_scaffold_terminal_does_not_complete_an_intentional_stop(
    store: SqlAlchemyConversationStore,
    event_type: str,
    owned: bool,
    newer_turn: bool,
) -> None:
    conv = store.create_conversation(
        runner_id="runner-native", labels={"omnigent.wrapper": "codex-native-ui"}
    )
    owner = store.record_session_observation(conv.id, "running", response_id="codex_turn-a")
    assert owner is not None
    assert common._session_status_cache.accept(conv.id, SessionProjection(owner, "running"))
    terminal: dict[str, Any] = {
        "type": event_type,
        "response": {"id": owner.response_id if owned else "resp_delivery"},
    }
    if owned:
        terminal["session_observation"] = owner.model_dump()
    if newer_turn:
        newer = store.record_session_observation(conv.id, "running", response_id="codex_turn-b")
        assert newer is not None
        assert common._session_status_cache.accept(conv.id, SessionProjection(newer, "running"))
    processed, release_close = asyncio.Event(), asyncio.Event()
    relay = asyncio.create_task(
        orchestration._relay_runner_stream(
            conv.id,
            _Runner(
                [
                    {"type": "response.in_progress", "response": {"id": "resp_delivery"}},
                    terminal,
                ],
                processed,
                release_close,
            ),
            store,
            runner_id="runner-native",
        )
    )
    handle = common._RelayHandle("runner-native", relay, asyncio.Event())
    common._runner_relay_tasks[conv.id] = handle
    common._intentional_stop_sessions[conv.id] = "runner-native"
    try:
        await asyncio.wait_for(processed.wait(), 10)
        assert handle.intentional_stop_turn_ended is (owned and not newer_turn)
        assert common._intentional_stop_sessions[conv.id] == "runner-native"
        if newer_turn:
            assert store.get_session_observation(conv.id) == newer
    finally:
        release_close.set()
        await asyncio.wait_for(relay, 10)
        common._runner_relay_tasks.pop(conv.id, None)
        common._intentional_stop_sessions.pop(conv.id, None)


@pytest.mark.asyncio
async def test_real_native_running_after_scaffold_idle_retires_an_older_stop(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(
        runner_id="runner-native", labels={"omnigent.wrapper": "codex-native-ui"}
    )
    owner = store.record_session_observation(conv.id, "running", response_id="codex_turn-a")
    assert owner is not None
    assert common._session_status_cache.accept(conv.id, SessionProjection(owner, "running"))
    processed, release_close = asyncio.Event(), asyncio.Event()
    relay = asyncio.create_task(
        orchestration._relay_runner_stream(
            conv.id,
            _Runner(
                [
                    {"type": "response.in_progress", "response": {"id": "resp_delivery"}},
                    {"type": "session.status", "status": "idle"},
                    {"type": "session.status", "status": "running"},
                ],
                processed,
                release_close,
            ),
            store,
            runner_id="runner-native",
        )
    )
    handle = common._RelayHandle(
        "runner-native", relay, asyncio.Event(), intentional_stop_turn_ended=True
    )
    common._runner_relay_tasks[conv.id] = handle
    common._intentional_stop_sessions[conv.id] = "runner-native"
    try:
        await asyncio.wait_for(processed.wait(), 10)
        current = store.get_session_observation(conv.id)
        assert current is not None and current.status == "running"
        assert handle.running_event_count == 1
        assert not handle.intentional_stop_turn_ended
        assert conv.id not in common._intentional_stop_sessions
    finally:
        release_close.set()
        await asyncio.wait_for(relay, 10)
        common._runner_relay_tasks.pop(conv.id, None)
        common._intentional_stop_sessions.pop(conv.id, None)


@pytest.mark.asyncio
async def test_heartbeat_only_relay_can_disable_persistence_with_a_configured_global_store(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-heartbeat")
    assert session_live_state.conversation_store() is store
    ready, processed, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_close.set()
    await orchestration._relay_runner_stream_once(
        conv.id,
        _Runner([{"type": "session.heartbeat"}], processed, release_close),
        None,
        ready,
        runner_id="runner-heartbeat",
    )
    assert ready.is_set()
    assert processed.is_set()


def _publish_text(session_id: str, observation: SessionObservation) -> None:
    session_stream.publish(
        session_id,
        {
            "type": "response.in_progress",
            "response": {"id": observation.response_id},
            "session_observation": observation.model_dump(),
        },
    )
    session_stream.publish(
        session_id,
        {
            "type": "response.output_text.delta",
            "delta": "current response prefix",
            "session_observation": observation.model_dump(),
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True], ids=["owned", "replacement"])
async def test_paused_relay_terminal_preserves_new_response(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch, replacement: bool
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    owner = store.record_session_observation(conv.id, "running", response_id="response-a")
    assert owner is not None
    assert common._session_status_cache.accept(conv.id, SessionProjection(owner, "running"))
    entered, release = asyncio.Event(), asyncio.Event()
    processed, release_close = asyncio.Event(), asyncio.Event()
    original = orchestration._relay_persist_error_once

    async def persist(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(orchestration, "_relay_persist_error_once", persist)
    event = {
        "type": "response.failed",
        "response": {
            "id": "response-a",
            "error": {"code": "runner_error", "message": "synthetic old failure"},
        },
        "session_observation": owner.model_dump(),
    }
    received: list[dict[str, Any]] = []

    async def consume() -> None:
        async for item in session_stream.subscribe(conv.id):
            received.append(item)

    consumer = asyncio.create_task(consume())
    relay = asyncio.create_task(
        orchestration._relay_runner_stream_once(
            conv.id,
            _Runner([event], processed, release_close),
            store,
            runner_id="runner-a",
            owner=owner,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        while not session_stream.has_subscribers(conv.id):
            await asyncio.sleep(0)
        current = owner
        if replacement:
            store.replace_runner_id(conv.id, "runner-b")
            current = store.record_session_observation(
                conv.id, "running", response_id="response-b"
            )
            assert current is not None
            assert common._session_status_cache.accept(
                conv.id, SessionProjection(current, "running")
            )
        _publish_text(conv.id, current)
        release.set()
        await asyncio.wait_for(processed.wait(), 10)
        if replacement:
            before_exit = inflight_text.snapshot_for(conv.id)
            assert before_exit and before_exit[0]["response"]["id"] == "response-b"
            assert before_exit[-1]["delta"] == "current response prefix"
        release_close.set()
        await asyncio.wait_for(relay, 10)
        snapshot = inflight_text.snapshot_for(conv.id)
        session_stream.close(conv.id)
        await asyncio.wait_for(consumer, 10)
        if replacement:
            assert snapshot and snapshot[0]["response"]["id"] == "response-b"
            assert snapshot[-1]["delta"] == "current response prefix"
            assert all(item.get("type") != "response.failed" for item in received)
        else:
            assert snapshot == []
            assert any(item.get("type") == "response.failed" for item in received)
        assert store.get_session_observation(conv.id) == current
        errors = [item for item in store.list_items(conv.id).data if item.type == "error"]
        assert len(errors) == 1
    finally:
        release.set()
        release_close.set()
        session_stream.close(conv.id)
        for task in (relay, consumer):
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True], ids=["owned", "replacement"])
async def test_paused_relay_flush_preserves_new_response(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch, replacement: bool
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    owner = store.record_session_observation(conv.id, "running", response_id="response-a")
    assert owner is not None
    assert common._session_status_cache.accept(conv.id, SessionProjection(owner, "running"))
    entered, release = threading.Event(), threading.Event()
    processed, release_close = asyncio.Event(), asyncio.Event()
    append = store.append

    def paused_append(*args: Any, **kwargs: Any):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return append(*args, **kwargs)

    monkeypatch.setattr(store, "append", paused_append)
    events = [
        {"type": "response.in_progress", "response": {"id": "response-a"}},
        {"type": "response.output_text.delta", "delta": "old persisted segment"},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "status": "completed",
                "call_id": "old-call",
                "name": "synthetic_tool",
                "agent": "synthetic",
                "arguments": "{}",
            },
        },
    ]
    relay = asyncio.create_task(
        orchestration._relay_runner_stream_once(
            conv.id,
            _Runner(events, processed, release_close),
            store,
            runner_id="runner-a",
            owner=owner,
        )
    )
    try:
        async with asyncio.timeout(10):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        await asyncio.wrap_future(session_live_state.submit("test_barrier", lambda: None))
        current = store.get_session_observation(conv.id)
        if replacement:
            store.replace_runner_id(conv.id, "runner-b")
            current = store.record_session_observation(
                conv.id, "running", response_id="response-b"
            )
            assert current is not None
            assert common._session_status_cache.accept(
                conv.id, SessionProjection(current, "running")
            )
            _publish_text(conv.id, current)
        release.set()
        await asyncio.wait_for(processed.wait(), 10)
        if replacement:
            before_exit = inflight_text.snapshot_for(conv.id)
            assert before_exit and before_exit[0]["response"]["id"] == "response-b"
            assert before_exit[-1]["delta"] == "current response prefix"
        release_close.set()
        await asyncio.wait_for(relay, 10)
        snapshot = inflight_text.snapshot_for(conv.id)
        if replacement:
            assert snapshot and snapshot[0]["response"]["id"] == "response-b"
            assert snapshot[-1]["delta"] == "current response prefix"
        else:
            assert snapshot == []
    finally:
        release.set()
        release_close.set()
        if not relay.done():
            relay.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay


def test_store_reconfiguration_resets_all_runtime_observation_floors(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    old = store.record_session_observation(conv.id, "running", response_id="old")
    current = store.record_session_observation(conv.id, "running", response_id="current")
    assert old is not None and current is not None
    assert session_stream.learn_observation(conv.id, current)
    assert not session_stream.learn_observation(conv.id, old)
    session_live_state.configure(None)
    assert session_stream.learn_observation(conv.id, old)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["session.status", "response.completed"])
@pytest.mark.parametrize("ownership", ["stale", "owned", "legacy"])
async def test_stale_annotated_relay_event_cannot_remove_a_newer_interrupt_fence(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    ownership: str,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    old = store.record_session_observation(conv.id, "running", response_id="response-a")
    assert old is not None
    store.replace_runner_id(conv.id, "runner-b")
    current = store.record_session_observation(conv.id, "running", response_id="response-b")
    assert current is not None
    assert common._session_status_cache.accept(conv.id, SessionProjection(current, "running"))
    observed = old if ownership == "stale" else current
    event: dict[str, Any] = {"type": event_type}
    if event_type == "session.status":
        event.update(status="running", response_id=observed.response_id)
    else:
        event["response"] = {"id": observed.response_id}
    if ownership != "legacy":
        event["session_observation"] = observed.model_dump()
    attempted: list[dict[str, Any]] = []
    received: list[dict[str, Any]] = []
    publish = session_stream.publish

    def record_publish(session_id: str, frame: dict[str, Any]) -> None:
        if frame.get("type") == event_type:
            attempted.append(frame.copy())
        publish(session_id, frame)

    async def consume() -> None:
        async for frame in session_stream.subscribe(conv.id):
            received.append(frame)

    monkeypatch.setattr(session_stream, "publish", record_publish)
    consumer = asyncio.create_task(consume())
    processed, release_close = asyncio.Event(), asyncio.Event()
    release_close.set()
    common._interrupt_fenced_sessions.add(conv.id)
    try:
        async with asyncio.timeout(10):
            while not session_stream.has_subscribers(conv.id):
                await asyncio.sleep(0)
        await orchestration._relay_runner_stream_once(
            conv.id,
            _Runner([event], processed, release_close),
            store,
            runner_id="runner-b",
            owner=current,
        )
        assert (conv.id in common._interrupt_fenced_sessions) is (ownership == "stale")
        session_stream.close(conv.id)
        await asyncio.wait_for(consumer, 10)
        if event_type == "response.completed":
            [terminal] = attempted
            assert SessionObservation.model_validate(terminal["session_observation"]) == observed
            terminals = [frame for frame in received if frame.get("type") == event_type]
            assert bool(terminals) is (ownership != "stale")
    finally:
        common._interrupt_fenced_sessions.discard(conv.id)
        session_stream.close(conv.id)
        if not consumer.done():
            consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer

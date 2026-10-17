"""Adversarial ordering of runner-owned live-state effects."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from omnigent.entities.session_observation import RunnerOfflineDecision, SessionObservation
from omnigent.runtime import session_stream
from omnigent.server import session_live_state
from omnigent.server.routes._sessions import common, helpers, orchestration
from omnigent.server.schemas import ErrorDetail
from omnigent.server.session_observations import SessionProjection, SessionStatusCache
from omnigent.server.subagent_activity import record_subagent_activity
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore


@pytest.fixture
def live_store(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[SqlAlchemyConversationStore, list[dict]]]:
    events: list[dict] = []
    conversation_store = SqlAlchemyConversationStore(db_uri)
    common._session_status_cache.clear()
    common._session_active_response_cache.clear()
    monkeypatch.setattr(session_stream, "publish", lambda _id, event: events.append(event))
    session_live_state.configure(conversation_store)
    try:
        yield conversation_store, events
    finally:
        session_live_state.submit("test_barrier", lambda: None).result(timeout=10)
        session_live_state.configure(None)
        common._session_status_cache.clear()
        common._session_active_response_cache.clear()


async def _drain() -> None:
    await asyncio.wrap_future(session_live_state.submit("test_barrier", lambda: None))


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_result", ["found", "missing", "error"])
@pytest.mark.parametrize("arriving_status", ["running", "waiting", "idle"])
@pytest.mark.parametrize("publication", ["committed", "pending"])
async def test_disconnect_rechecks_live_activity_after_a_hydrated_status_read(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    lookup_result: str,
    arriving_status: str,
    publication: str,
) -> None:
    """A real edge arriving during a cold read outranks the adopted database value."""
    from tests.debug_log_helpers import capture_debug_rows

    store, _ = live_store
    parent = store.create_conversation()
    child = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        runner_id="runner-a",
        labels={"omnigent.wrapper": "claude-code-native-ui-subagent"},
    )
    owner = store.record_session_observation(child.id, "running", response_id="child-turn")
    snapshot = store.get_conversation(child.id)
    assert snapshot is not None and owner is not None
    assert common._session_status_cache.hydrate(snapshot)
    read_started = asyncio.Event()
    release_read = threading.Event()
    writer_started = threading.Event()
    release_writer = threading.Event()
    loop = asyncio.get_running_loop()
    get_conversation = store.get_conversation
    read_held = False

    def delayed_get(session_id: str):
        nonlocal read_held
        captured = get_conversation(session_id)
        if session_id == child.id and not read_held:
            read_held = True
            loop.call_soon_threadsafe(read_started.set)
            assert release_read.wait(10)
            if lookup_result == "error":
                raise RuntimeError("status lookup unavailable")
            if lookup_result == "missing":
                return None
        return captured

    def hold_writer() -> None:
        writer_started.set()
        assert release_writer.wait(10)

    monkeypatch.setattr(store, "get_conversation", delayed_get)
    task = None
    try:
        with capture_debug_rows("server") as rows:
            task = asyncio.create_task(
                orchestration._runner_disconnect_requires_failure(
                    child.id, store, origin="runner_offline_sweep", snapshot=snapshot
                )
            )
            await asyncio.wait_for(read_started.wait(), 10)
            if publication == "pending":
                session_live_state.submit("held_writer", hold_writer)
                assert await asyncio.to_thread(writer_started.wait, 10)
            published = helpers._publish_status(child.id, arriving_status, owner=owner)
            assert published is not None
            if publication == "committed":
                assert await asyncio.wrap_future(published) is not None
            else:
                assert not published.done()
            release_read.set()
            assert await asyncio.wait_for(task, 10) is (arriving_status != "idle")
        logged = next(row for row in rows if row["event_name"] == "runner_disconnect_decision")
        assert logged["attributes"]["cached_session_status"] == arriving_status
        assert logged["attributes"]["status_source"] == "cache"
        assert logged["attributes"]["status_lookup"] == lookup_result
        assert logged["attributes"]["decision"] == (
            "idle_no_failure" if arriving_status == "idle" else "failed_mid_turn"
        )
    finally:
        release_read.set()
        release_writer.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await _drain()
        session_stream.close(child.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["runner_disconnected", "runner_error"])
async def test_versioned_recovery_from_a_legacy_failed_cache_preserves_real_failures(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]], code: str
) -> None:
    store, _ = live_store
    conv = store.create_conversation(runner_id="runner-a")
    store.set_session_live_status(conv.id, "failed")
    store.set_labels(
        conv.id,
        {
            "omnigent.last_task_error_code": code,
            "omnigent.last_task_error_message": "Original cause",
        },
    )
    owner = store.get_session_observation(conv.id)
    assert owner is not None
    common._session_status_cache[conv.id] = "failed"
    assert common._session_status_cache.projection(conv.id) is None
    await orchestration._publish_runner_recovered_status_impl(
        conv.id, store, require_disconnect_code=True, owner=owner
    )
    await _drain()
    current = store.get_conversation(conv.id)
    assert current is not None
    if code == "runner_disconnected":
        assert current.live_status == "idle" and common._session_status_cache[conv.id] == "idle"
        assert current.live_status_revision == owner.revision + 1
        assert "omnigent.last_task_error_code" not in current.labels
    else:
        assert current.session_observation == owner
        assert (
            current.live_status == "failed" and common._session_status_cache[conv.id] == "failed"
        )
        assert current.labels["omnigent.last_task_error_code"] == code


@pytest.mark.parametrize("annotated", [False, True])
def test_unversioned_idle_cannot_clear_a_legacy_failed_cache(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]], annotated: bool
) -> None:
    store, events = live_store
    conv = store.create_conversation()
    common._session_status_cache[conv.id] = "failed"
    helpers._publish_status(
        conv.id,
        "idle",
        persist_live_status=False,
        session_observation=conv.session_observation if annotated else None,
    )
    assert common._session_status_cache[conv.id] == "failed"
    assert events == []


@pytest.mark.parametrize("replacement", ["turn", "runner", "aba"])
def test_orphan_settlement_cannot_adopt_a_replacement_lifecycle(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    store, _ = live_store
    conv = store.create_conversation(runner_id="runner-a")
    original = store.record_session_observation(conv.id, "running", response_id="old-turn")
    assert original is not None
    settle = store.settle_orphaned_session_observation
    current = None

    def replace_before_write(*args, **kwargs):
        nonlocal current
        if replacement in {"runner", "aba"}:
            store.replace_runner_id(conv.id, "runner-b")
        if replacement == "aba":
            store.replace_runner_id(conv.id, "runner-a")
        current = store.record_session_observation(conv.id, "running", response_id="new-turn")
        assert current is not None
        assert common._session_status_cache.accept(conv.id, SessionProjection(current, "running"))
        return settle(*args, **kwargs)

    monkeypatch.setattr(store, "settle_orphaned_session_observation", replace_before_write)
    assert not helpers.reconcile_orphaned_running_status(conv.id, store, 9999999999)
    assert current is not None and store.get_session_observation(conv.id) == current
    assert common._session_status_cache[conv.id] == "running"


def test_hydration_keeps_status_without_claiming_a_live_local_publisher(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
) -> None:
    store, _ = live_store
    conv = store.create_conversation(runner_id="runner-a")
    owner = store.record_session_observation(conv.id, "running", response_id="turn-a")
    assert owner is not None
    row = store.get_conversation(conv.id)
    assert row is not None
    cache = SessionStatusCache()
    assert cache.hydrate(row) and cache[conv.id] == "running"
    assert not cache.has_live_status(conv.id)
    assert cache.accept(conv.id, SessionProjection(owner, "running"))
    assert cache.has_live_status(conv.id)
    assert cache.hydrate(row) and cache.has_live_status(conv.id)
    cache.pop(conv.id)
    assert cache.hydrate(row) and not cache.has_live_status(conv.id)
    newer = store.record_session_observation(conv.id, "running", response_id="turn-b")
    assert newer is not None
    row = store.get_conversation(conv.id)
    assert row is not None
    assert cache.hydrate(row) and not cache.has_live_status(conv.id)
    cache["legacy"] = "running"
    assert cache.has_live_status("legacy")


@pytest.mark.asyncio
@pytest.mark.parametrize("annotation", ["current", "superseded", "malformed"])
async def test_relay_startup_status_keeps_its_explicit_owner(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]], annotation: str
) -> None:
    from tests.debug_log_helpers import capture_debug_rows
    from tests.server.routes.test_sessions_runner_relay import _ScriptedRunnerClient

    store, events = live_store
    conv = store.create_conversation(runner_id="runner-a")
    original = store.record_session_observation(conv.id, "idle")
    assert original is not None
    startup_a = store.begin_terminal_startup(conv.id, original, "startup-a")
    assert startup_a is not None
    current = startup_a
    if annotation != "current":
        current = store.begin_terminal_startup(conv.id, startup_a, "startup-b")
        assert current is not None
    helpers._publish_status(
        conv.id, "idle", persist_live_status=False, session_observation=current
    )
    helpers._publish_terminal_pending(conv.id, True, session_observation=current)
    events.clear()
    failed = {
        "type": "session.status",
        "status": "failed",
        "error": {"code": "actual_a", "message": "A actual source failure"},
        "session_observation": (
            {"binding_epoch": "invalid"} if annotation == "malformed" else startup_a.model_dump()
        ),
    }
    release = asyncio.Event()
    release.set()
    frames = [failed, failed]
    if annotation == "malformed":
        frames.extend(
            [
                {"type": "response.output_text.delta", "delta": "Historical source output."},
                {"type": "response.completed", "response": {"id": "historical-a"}},
            ]
        )
    client = _ScriptedRunnerClient(release, frames)
    with capture_debug_rows("server") as rows:
        await orchestration._relay_runner_stream_once(
            conv.id, client, store, runner_id="runner-a", owner=startup_a
        )
        await _drain()
    fresh = store.get_conversation(conv.id)
    assert fresh is not None
    if annotation == "current":
        assert fresh.live_status == "failed" and fresh.terminal_pending is False
        assert common._session_terminal_pending_cache.get(conv.id, False) is False
        assert any(event.get("status") == "failed" for event in events)
    else:
        assert fresh.session_observation == current
        assert fresh.terminal_pending is True
        assert common._session_terminal_pending_cache.get(conv.id) is True
        assert not any(event.get("status") == "failed" for event in events)
    failures = [row for row in rows if row["event_name"] == "session_turn_failed"]
    if annotation == "malformed":
        assert failures == []
        [historical] = store.list_items(conv.id, type="message").data
        assert historical.data.content == [
            {"type": "output_text", "text": "Historical source output."}
        ]
    else:
        assert len(failures) == 1
        assert failures[0]["attributes"]["live_status_revision"] == str(startup_a.revision)


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize(
    "changed",
    [
        {"runner_id": "runner-other"},
        {"observation_id": "different-owner"},
        {"status": "waiting"},
        {"response_id": "different-turn"},
        {"error_key": "different-error"},
    ],
)
async def test_status_write_requires_the_complete_captured_observation(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]], changed: dict, queued: bool
) -> None:
    store, events = live_store
    conv = store.create_conversation(runner_id="runner-a")
    current = store.record_session_observation(conv.id, "running", response_id="turn-a")
    assert current is not None
    predecessor = None
    if queued:
        helpers._publish_status(conv.id, "running", response_id="turn-a", owner=current)
        predecessor = session_live_state.pending_observation(conv.id)
        assert predecessor is not None
    captured = current.model_copy(update=changed)
    result = helpers._publish_status(
        conv.id,
        "failed",
        ErrorDetail(code="actual_a", message="A actual source failure"),
        owner=captured,
        predecessor=predecessor,
        response_id="turn-a",
    )
    assert result is not None
    assert await asyncio.wrap_future(result) is None
    await _drain()
    assert store.get_session_observation(conv.id) == current
    assert not any(event.get("status") == "failed" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_response_id", [None, "previous-turn"])
@pytest.mark.parametrize("variant", ["current", "superseded", "completion_retry"])
async def test_repeated_startup_failure_settles_its_new_reservation(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
    previous_response_id: str | None,
) -> None:
    from tests.debug_log_helpers import capture_debug_rows
    from tests.server.routes.test_sessions_runner_relay import _ScriptedRunnerClient

    store, _ = live_store
    conv = store.create_conversation(runner_id="runner-a")
    error = ErrorDetail(code="startup_failed", message="Actual provider startup failure")
    helpers._publish_status(
        conv.id,
        "failed",
        error,
        owner=conv.session_observation,
        response_id=previous_response_id,
    )
    await _drain()
    failed = store.get_session_observation(conv.id)
    assert failed is not None
    startup = await orchestration._begin_native_terminal_startup(conv.id, failed)
    assert startup is not None
    expected = startup
    if variant == "superseded":
        expected = await orchestration._begin_native_terminal_startup(conv.id, startup)
        assert expected is not None
    attempts = 0
    finish = store.finish_terminal_startup

    def finish_then_raise(*args):
        nonlocal attempts
        attempts += 1
        completed = finish(*args)
        if attempts == 1:
            raise RuntimeError("synthetic lost completion acknowledgement")
        return completed

    if variant == "completion_retry":
        monkeypatch.setattr(store, "finish_terminal_startup", finish_then_raise)
    frame = {
        "type": "session.status",
        "status": "failed",
        "error": error.model_dump(),
        "session_observation": startup.model_dump(),
    }
    release = asyncio.Event()
    release.set()
    with capture_debug_rows("server") as rows:
        await orchestration._relay_runner_stream_once(
            conv.id,
            _ScriptedRunnerClient(release, [frame, frame]),
            store,
            runner_id="runner-a",
            owner=startup,
        )
        await _drain()
    current = store.get_conversation(conv.id)
    assert current is not None and current.live_status == "failed"
    assert current.terminal_pending is (variant == "superseded")
    if variant == "superseded":
        assert current.session_observation == expected
    else:
        assert current.live_status_revision == startup.revision + 1
    if variant == "completion_retry":
        assert attempts >= 2
    assert len([row for row in rows if row["event_name"] == "session_turn_failed"]) == 1


def test_durable_startup_projects_across_replicas_without_mixing_snapshot_owners(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    db_uri: str,
) -> None:
    from omnigent.entities.session_observation import accept_session_projection

    store, _ = live_store
    conv = store.create_conversation(agent_id=uuid.uuid4().hex, runner_id="runner-a")
    labels = {
        "omnigent.last_task_error_code": "actual-a",
        "omnigent.last_task_error_message": "A source detail",
    }
    original = store.record_session_observation(
        conv.id, "failed", response_id="turn-a", error_key="actual-a", error_labels=labels
    )
    before = store.get_conversation(conv.id)
    assert original is not None and before is not None
    startup = store.begin_terminal_startup(conv.id, original, "startup-b")
    assert startup is not None
    replica = SqlAlchemyConversationStore(db_uri)
    starting = replica.get_conversation(conv.id)
    assert starting is not None and starting.terminal_pending is True
    common._session_terminal_pending_cache.clear()
    response = orchestration._build_session_response(starting, [], status="failed")
    assert response.terminal_pending is True and response.session_observation == startup
    completed = store.finish_terminal_startup(conv.id, startup, "ready-b")
    assert completed is not None and completed.revision == startup.revision + 1
    finished = replica.get_conversation(conv.id)
    assert finished is not None and finished.terminal_pending is False
    assert common._session_status_cache.accept(conv.id, SessionProjection(completed, "failed"))
    common._session_terminal_pending_cache[conv.id] = True
    response = orchestration._build_session_response(finished, [], status="failed")
    assert response.terminal_pending is False and response.session_observation == completed
    stale = orchestration._build_session_response(
        before, [], status="running", last_task_error={"code": "wrong-b", "message": "B detail"}
    )
    assert stale.status == "failed" and stale.session_observation == original
    assert stale.terminal_pending is False and stale.last_task_error["code"] == "actual-a"
    stale_pending = orchestration._build_session_response(starting, [], status="running")
    assert stale_pending.status == "failed" and stale_pending.terminal_pending is True
    assert stale_pending.session_observation == startup
    assert not accept_session_projection(startup, None, completed, None)
    newer = store.begin_terminal_startup(conv.id, completed, "startup-c")
    assert newer is not None and not accept_session_projection(completed, None, newer, None)
    assert common._session_status_cache.projection(conv.id).observation == completed


@pytest.mark.asyncio
async def test_startup_metadata_outage_retries_before_provider_or_input_acceptance(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import httpx

    from omnigent.errors import ErrorCode, OmnigentError
    from omnigent.server.schemas import SessionEventInput

    store, _ = live_store
    conv = store.create_conversation(
        runner_id="runner-a", labels={"omnigent.wrapper": "claude-code-native-ui"}
    )
    owner = store.get_session_observation(conv.id)
    attempts, provider_calls = 0, 0

    def unavailable(*_args):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("synthetic startup metadata outage")

    def provider(_request):
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, json={})

    monkeypatch.setattr(store, "begin_terminal_startup", unavailable)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(OmnigentError) as failure:
            await orchestration._dispatch_session_event_to_runner_impl(
                conv.id,
                conv,
                SessionEventInput(
                    type="message",
                    data={
                        "role": "user",
                        "content": [{"type": "input_text", "text": "new request"}],
                    },
                ),
                store,
                client,
                agent_name="worker",
                file_store=None,
                artifact_store=None,
            )
    assert failure.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert attempts == 3 and provider_calls == 0
    assert store.get_session_observation(conv.id) == owner
    assert store.list_items(conv.id).data == []
    assert not any("session turn failed for" in row.message for row in caplog.records)


@pytest.mark.asyncio
async def test_setup_failure_retry_preserves_consumed_input_response_and_source_accounting(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from omnigent.entities import ErrorData
    from omnigent.errors import OmnigentError
    from omnigent.server.schemas import SessionEventInput

    store, _ = live_store
    conv = store.create_conversation(runner_id="runner-a")
    stable_id = uuid.uuid4().hex
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "accepted request"}],
            "stable_id": stable_id,
        },
    )
    error = ErrorData(source="execution", code="native_turn_error", message="actual setup failure")
    record = store.record_session_observation

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic status metadata outage")

    monkeypatch.setattr(store, "record_session_observation", unavailable)
    with pytest.raises(OmnigentError):
        await orchestration._persist_native_terminal_failure(
            conv.id, conv, body, store, error, None, created_by=None
        )
    source_response = store.get_item(conv.id, stable_id).response_id
    monkeypatch.setattr(store, "record_session_observation", record)
    consumed_id = await orchestration._persist_native_terminal_failure(
        conv.id, conv, body, store, error, None, created_by=None
    )
    assert consumed_id == stable_id
    history = store.list_items(conv.id).data
    assert len(history) == 2 and {item.response_id for item in history} == {source_response}
    assert store.get_session_observation(conv.id).response_id == source_response
    assert sum("session turn failed for" in row.message for row in caplog.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["admission", "reply"])
@pytest.mark.parametrize("replacement", ["turn", "runner", "aba", "unbind"])
async def test_host_launch_keeps_its_owner_before_admission_and_reply_awaits(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
    replacement: str,
) -> None:
    store, _ = live_store
    conv = store.create_conversation(
        runner_id="runner-a", host_id=uuid.uuid4().hex, workspace="/tmp/owned-launch"
    )
    entered, release = asyncio.Event(), asyncio.Event()
    conn = SimpleNamespace(pending_launches={})
    sent: list[str] = []

    class Host:
        async def admit_launch(self, *_args):
            if gate == "admission":
                entered.set()
                await release.wait()

        def send_text(self, _conn, frame):
            sent.append(frame)
            entered.set()

    monkeypatch.setattr(helpers, "_spawn_superseded_runner_stop", lambda *_args: None)
    monkeypatch.setattr(helpers, "_resolve_harness", lambda _conv: None)
    task = asyncio.create_task(helpers._launch_runner_on_host_locked(conv, store, Host(), conn))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        captured = store.get_session_observation(conv.id)
        if replacement in {"runner", "aba"}:
            store.replace_runner_id(conv.id, "runner-b")
        if replacement == "aba":
            store.replace_runner_id(conv.id, captured.runner_id)
        if replacement == "unbind":
            store.clear_runner_id(conv.id)
        current = store.record_session_observation(conv.id, "running", response_id="new-turn")
        if gate == "reply":
            next(iter(conn.pending_launches.values())).set_result(
                {
                    "status": "failed",
                    "error_code": "harness_not_configured",
                    "error": "old refusal",
                }
            )
    finally:
        release.set()
    attempt = await asyncio.wait_for(task, 10)
    assert attempt.observation == captured
    assert store.get_session_observation(conv.id) == current
    if gate == "admission":
        assert sent == [] and attempt.error_code == "launch_superseded"
    else:
        assert len(sent) == 1 and attempt.error == "old refusal"


def test_evicted_status_keeps_the_fence_against_legacy_and_old_runner_callbacks() -> None:
    cache = SessionStatusCache()
    old = SessionObservation(runner_id="runner-a", binding_epoch=1, revision=1, observation_id="a")
    new = SessionObservation(runner_id="runner-b", binding_epoch=2, revision=1, observation_id="b")
    assert cache.accept("session", SessionProjection(old, "running"))
    assert cache.pop("session") == "running"
    cache["session"] = "failed"
    assert cache.get("session") is None
    assert cache.accept("session", SessionProjection(new, "running"))
    assert not cache.accept("session", SessionProjection(old, "failed", "old-episode"))
    assert cache["session"] == "running"


@pytest.mark.asyncio
async def test_owned_child_activity_does_not_collide_when_a_new_binding_reuses_a_turn_id(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
) -> None:
    store, _ = live_store
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id, runner_id="runner-a")
    for runner_id, episode in (("runner-a", "old-episode"), ("runner-b", "new-episode")):
        store.replace_runner_id(child.id, runner_id)
        observed = store.record_session_observation(child.id, "running", response_id="reused-turn")
        assert observed is not None
        decision = RunnerOfflineDecision(
            observation=observed,
            episode_id=episode,
            error_labels={},
            activity_turn_id="reused-turn",
        )
        assert store.record_runner_offline_decision(child.id, decision) is not None
        await record_subagent_activity(
            child.id,
            "returned",
            store,
            turn_id="reused-turn",
            status="failed",
            offline_decision=decision,
        )
        await record_subagent_activity(
            child.id,
            "returned",
            store,
            turn_id="reused-turn",
            status="failed",
            offline_decision=decision,
        )
        visible = store.list_items(parent.id).data
        assert len(visible) == 1
        assert visible[0].data.resource["runner_offline_episode_id"] == episode


@pytest.mark.asyncio
async def test_normal_publication_is_durable_and_duplicate_activity_keeps_the_turn(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
) -> None:
    store, events = live_store
    conv = store.create_conversation(runner_id="runner-a")
    owner = conv.session_observation
    helpers._publish_status(conv.id, "running", response_id="turn-one", owner=owner)
    await _drain()
    first = store.get_session_observation(conv.id)
    assert first is not None and first.revision == 1
    assert common._session_status_cache.projection(conv.id).observation == first
    helpers._publish_status(conv.id, "running", owner=owner)
    await _drain()
    assert store.get_session_observation(conv.id) == first
    assert common._session_active_response_cache[conv.id] == "turn-one"
    published = [event for event in events if event["type"] == "session.status"]
    assert published[-1]["session_observation"] == first.model_dump()


@pytest.mark.asyncio
async def test_a_queued_old_runner_edge_cannot_mutate_or_publish_for_its_replacement(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
) -> None:
    store, events = live_store
    conv = store.create_conversation(runner_id="runner-a")
    owner = conv.session_observation
    replacement = store.replace_runner_id(conv.id, "runner-b")
    helpers._publish_status(
        conv.id, "running", response_id="replacement-turn", owner=replacement.session_observation
    )
    await _drain()
    expected = store.get_session_observation(conv.id)
    events.clear()
    helpers._publish_status(
        conv.id,
        "failed",
        ErrorDetail(code="runner_disconnected", message="old runner exit"),
        owner=owner,
    )
    await _drain()
    assert store.get_session_observation(conv.id) == expected
    assert common._session_active_response_cache[conv.id] == "replacement-turn"
    assert common._session_status_cache[conv.id] == "running"
    assert events == []


@pytest.mark.asyncio
async def test_delayed_normal_error_labels_do_not_clobber_replacement_state(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
) -> None:
    store, _ = live_store
    conv = store.create_conversation(runner_id="runner-a")
    replacement = store.replace_runner_id(conv.id, "runner-b")
    helpers._publish_status(
        conv.id, "running", response_id="new-turn", owner=replacement.session_observation
    )
    await _drain()
    old_error = ErrorDetail(code="native_turn_error", message="old owner failure")
    await helpers._persist_session_status_error_labels(
        conv.id, old_error, store, owner_runner_id="runner-a", owner=conv.session_observation
    )
    helpers._publish_status(conv.id, "failed", old_error, owner=conv.session_observation)
    await _drain()
    refreshed = store.get_conversation(conv.id)
    assert refreshed is not None and refreshed.live_status == "running"
    assert helpers._last_task_error_from_labels(refreshed.labels) is None


@pytest.mark.asyncio
async def test_captured_predecessors_preserve_queued_normal_status_order(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
) -> None:
    store, _ = live_store
    conv = store.create_conversation(runner_id="runner-a")
    for status in ("running", "waiting", "idle"):
        owner, predecessor = helpers._capture_status_observation(conv.id, conv.session_observation)
        helpers._publish_status(
            conv.id,
            status,
            response_id="turn" if status == "running" else None,
            owner=owner,
            predecessor=predecessor,
        )
    await _drain()
    current = store.get_session_observation(conv.id)
    assert current is not None and current.status == "idle" and current.revision == 3
    assert common._session_status_cache[conv.id] == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_status", ["running", "failed"])
@pytest.mark.parametrize("delayed_status", ["running", "failed"])
async def test_delayed_normal_status_does_not_overwrite_a_newer_same_runner_turn(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    newer_status: str,
    delayed_status: str,
) -> None:
    store, events = live_store
    conv = store.create_conversation(runner_id="runner-a")
    helpers._publish_status(
        conv.id, "running", response_id="old-turn", owner=conv.session_observation
    )
    await _drain()
    owner, predecessor = helpers._capture_status_observation(conv.id, conv.session_observation)
    newer_labels = (
        {
            "omnigent.last_task_error_code": "provider-error",
            "omnigent.last_task_error_message": "newer genuine failure",
        }
        if newer_status == "failed"
        else {}
    )
    expected = store.record_session_observation(
        conv.id,
        newer_status,
        response_id="new-turn",
        error_key="new-provider-error" if newer_status == "failed" else None,
        error_labels=newer_labels,
    )
    error = (
        ErrorDetail(code="old-error", message="delayed old failure")
        if delayed_status == "failed"
        else None
    )
    await helpers._persist_session_status_error_labels(
        conv.id, error, store, owner_runner_id="runner-a", owner=owner
    )
    events.clear()
    helpers._publish_status(
        conv.id,
        delayed_status,
        error,
        response_id="old-turn",
        owner=owner,
        predecessor=predecessor,
    )
    await _drain()
    assert store.get_session_observation(conv.id) == expected
    assert common._session_status_cache[conv.id] == newer_status
    current = store.get_conversation(conv.id)
    assert current is not None
    assert current.labels == newer_labels
    assert events == []
    # The current owner can still report a new, genuine failure after reconciliation.
    current_owner, current_predecessor = helpers._capture_status_observation(
        conv.id, conv.session_observation
    )
    helpers._publish_status(
        conv.id,
        "failed",
        ErrorDetail(code="current-error", message="current genuine failure"),
        response_id="new-turn",
        owner=current_owner,
        predecessor=current_predecessor,
    )
    await _drain()
    assert store.get_session_observation(conv.id).revision > expected.revision
    assert (
        store.get_conversation(conv.id).labels["omnigent.last_task_error_code"] == "current-error"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("rebound", [False, True])
async def test_explicit_source_failure_is_counted_once_during_metadata_outage(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    rebound: bool,
) -> None:
    store, events = live_store
    conv = store.create_conversation(runner_id="runner-a")
    helpers._publish_status(
        conv.id, "running", response_id="old-turn", owner=conv.session_observation
    )
    await _drain()
    owner, predecessor = helpers._capture_status_observation(conv.id, conv.session_observation)
    if rebound:
        replacement = store.replace_runner_id(conv.id, "runner-b")
        helpers._publish_status(
            conv.id,
            "running",
            response_id="replacement-turn",
            owner=replacement.session_observation,
        )
        await _drain()
    before = store.get_session_observation(conv.id)
    record = store.record_session_observation

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic metadata outage")

    monkeypatch.setattr(store, "record_session_observation", unavailable)
    error = ErrorDetail(code="native_turn_error", message="actual source failure")
    events.clear()
    for _ in range(2):
        helpers._publish_status(
            conv.id,
            "failed",
            error,
            response_id="old-turn",
            owner=owner,
            predecessor=predecessor,
            failure_origin="explicit_source_test",
        )
        await _drain()
    failures = [row for row in caplog.records if "session turn failed for" in row.message]
    assert len(failures) == 1
    assert failures[0].attributes["runner_id"] == "runner-a"
    assert failures[0].attributes["source_event_id"]
    assert failures[0].attributes["observation_committed"] is False
    assert store.get_session_observation(conv.id) == before
    assert store.get_conversation(conv.id).labels == {}
    assert events == []
    monkeypatch.setattr(store, "record_session_observation", record)
    helpers._publish_status(
        conv.id,
        "failed",
        error,
        response_id="old-turn",
        owner=owner,
        predecessor=predecessor,
        failure_origin="explicit_source_test",
    )
    await _drain()
    if rebound:
        assert store.get_session_observation(conv.id) == before
        assert common._session_active_response_cache[conv.id] == "replacement-turn"
        assert events == []
    else:
        current_owner, current_predecessor = helpers._capture_status_observation(conv.id, owner)
        helpers._publish_status(
            conv.id,
            "failed",
            error,
            response_id="old-turn",
            owner=current_owner,
            predecessor=current_predecessor,
            failure_origin="explicit_source_test",
        )
        await _drain()
        assert store.get_conversation(conv.id).live_status == "failed"
        assert (
            store.get_conversation(conv.id).labels["omnigent.last_task_error_code"] == error.code
        )
    assert sum("session turn failed for" in row.message for row in caplog.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("supersession", [None, "runner", "turn"])
async def test_one_shot_source_failure_retries_its_exact_observation_automatically(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    supersession: str | None,
) -> None:
    store, events = live_store
    conv = store.create_conversation(runner_id="runner-a")
    helpers._publish_status(
        conv.id, "running", response_id="old-turn", owner=conv.session_observation
    )
    await _drain()
    owner, predecessor = helpers._capture_status_observation(conv.id, conv.session_observation)
    record = store.record_session_observation
    entered, release = threading.Event(), threading.Event()
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            assert release.wait(10)
            raise RuntimeError("synthetic transient metadata outage")
        return record(*args, **kwargs)

    monkeypatch.setattr(store, "record_session_observation", fail_once)
    events.clear()
    future = helpers._publish_status(
        conv.id,
        "failed",
        ErrorDetail(code="native_turn_error", message="actual failure"),
        response_id="old-turn",
        owner=owner,
        predecessor=predecessor,
        failure_origin="one_shot_source_test",
    )
    assert future is not None
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if supersession == "runner":
            store.replace_runner_id(conv.id, "runner-b")
        if supersession is not None:
            record(conv.id, "running", response_id="replacement-turn")
    finally:
        release.set()
    accepted = await asyncio.wrap_future(future)
    await _drain()
    assert attempts == 2
    assert sum("session turn failed for" in row.message for row in caplog.records) == 1
    current = store.get_conversation(conv.id)
    if supersession is None:
        assert accepted is not None
        assert current.live_status == "failed"
        assert current.labels["omnigent.last_task_error_code"] == "native_turn_error"
        assert len([event for event in events if event.get("status") == "failed"]) == 1
    else:
        assert accepted is None
        assert (
            current.live_status == "running"
            and current.live_status_response_id == "replacement-turn"
        )
        assert current.labels == {}
        assert common._session_active_response_cache[conv.id] == "replacement-turn"
        assert events == []


@pytest.mark.asyncio
async def test_rejected_old_pending_failure_does_not_suppress_current_runner_loss(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
) -> None:
    store, _ = live_store
    old = store.create_conversation(runner_id="runner-a")
    replacement = store.replace_runner_id(old.id, "runner-b")
    helpers._publish_status(
        old.id, "running", response_id="replacement-turn", owner=replacement.session_observation
    )
    await _drain()
    rejected = helpers._publish_status(
        old.id,
        "failed",
        ErrorDetail(code="old_error", message="old source failed"),
        owner=old.session_observation,
    )
    assert rejected is not None and await asyncio.wrap_future(rejected) is None
    current = store.get_conversation(old.id)
    await orchestration._mark_runner_sessions_offline_impl(
        [current],
        ErrorDetail(code="runner_disconnected", message="current runner disappeared"),
        store,
    )
    assert store.get_conversation(old.id).runner_offline_decision is not None
    assert common._session_status_cache[old.id] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, "runner", "turn", "aba"])
async def test_actual_parent_recovery_cannot_rebind_a_superseding_child_turn(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    replacement: str | None,
) -> None:
    store, _ = live_store
    parent = store.create_conversation(runner_id="runner-parent")
    child = store.create_conversation(parent_conversation_id=parent.id, runner_id="runner-a")
    snapshot = child
    accepted = store.record_session_observation(
        child.id, "failed", response_id="old-turn", error_key="old-error"
    )
    assert accepted is not None
    entered, release = asyncio.Event(), asyncio.Event()
    forwarded: list[dict] = []
    client = object()

    async def resolve_parent(*_args):
        entered.set()
        await release.wait()
        return client

    async def forward(_session_id, _router, body):
        forwarded.append(body)
        return helpers._RunnerForwardResult(204, "")

    monkeypatch.setattr(orchestration, "_get_runner_client", resolve_parent)
    monkeypatch.setattr(orchestration, "_forward_session_change_to_runner", forward)
    body = {
        "type": "external_session_status",
        "data": {
            "status": "failed",
            "session_observation": accepted.model_dump(),
            "subagent_dispatch_id": "old-dispatch",
        },
    }
    task = asyncio.create_task(
        orchestration._recover_subagent_status_forward_via_parent(
            snapshot,
            None,
            None,
            store,
            body,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if replacement in {"runner", "aba"}:
            store.replace_runner_id(child.id, "runner-b")
        if replacement == "aba":
            store.replace_runner_id(child.id, "runner-a")
        if replacement is not None:
            store.record_session_observation(child.id, "running", response_id="new-turn")
        before = store.get_conversation(child.id)
    finally:
        release.set()
    result = await asyncio.wait_for(task, 10)
    current = store.get_conversation(child.id)
    assert result is not None
    if replacement is None:
        assert current.runner_id == "runner-parent"
        assert forwarded == [body]
    else:
        assert current.runner_id == before.runner_id
        assert current.session_observation == before.session_observation
        assert forwarded == []


@pytest.mark.asyncio
@pytest.mark.parametrize("exact_response", [False, True])
@pytest.mark.parametrize("newer_turn", [False, True])
async def test_terminal_enrichment_never_borrows_a_newer_turns_output(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    exact_response: bool,
    newer_turn: bool,
) -> None:
    from omnigent.entities import MessageData, NewConversationItem

    store, _ = live_store
    conv = store.create_conversation(runner_id="runner-a")
    helpers._publish_status(conv.id, "running", owner=conv.session_observation)
    await _drain()
    owner, predecessor = helpers._capture_status_observation(conv.id, conv.session_observation)
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="old-turn",
                data=MessageData(
                    role="assistant",
                    agent="test-agent",
                    content=[{"type": "text", "text": "old source detail"}],
                ),
            )
        ],
    )
    entered, release = threading.Event(), threading.Event()
    read = orchestration._latest_assistant_text_from_store

    def delayed_read(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return read(*args, **kwargs)

    monkeypatch.setattr(orchestration, "_latest_assistant_text_from_store", delayed_read)
    task = asyncio.create_task(
        orchestration._enrich_terminal_status_with_subagent_output(
            {"status": "failed", **({"response_id": "old-turn"} if exact_response else {})},
            "failed",
            conv.id,
            store,
            expected_observation=owner,
            predecessor=predecessor,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if newer_turn:
            store.record_session_observation(conv.id, "running", response_id="new-turn")
            store.append(
                conv.id,
                [
                    NewConversationItem(
                        type="message",
                        response_id="new-turn",
                        data=MessageData(
                            role="assistant",
                            agent="test-agent",
                            content=[{"type": "text", "text": "new source detail"}],
                        ),
                    )
                ],
            )
    finally:
        release.set()
    enriched = await asyncio.wait_for(task, 10)
    if newer_turn and not exact_response:
        assert "output" not in enriched
    else:
        assert enriched["output"] == "old source detail"


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("new_state", ["idle", "new_turn", "genuine_error", "aba"])
async def test_offline_decision_cannot_project_over_a_newer_observation(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    after_commit: bool,
    new_state: str,
) -> None:
    store, events = live_store
    parent = store.create_conversation()
    child = store.create_conversation(
        runner_id="runner-a", parent_conversation_id=parent.id, title="worker:Audit"
    )
    helpers._publish_status(
        child.id, "running", response_id="old-turn", owner=child.session_observation
    )
    await _drain()
    stale = store.get_conversation(child.id)
    assert stale is not None
    entered = threading.Event()
    release = threading.Event()
    record = store.record_runner_offline_decision

    def delayed_record(session_id: str, decision: RunnerOfflineDecision):
        result = record(session_id, decision) if after_commit else None
        entered.set()
        assert release.wait(10)
        return result if after_commit else record(session_id, decision)

    monkeypatch.setattr(store, "record_runner_offline_decision", delayed_record)
    events.clear()
    sweep = asyncio.create_task(
        orchestration._mark_runner_sessions_offline_impl(
            [stale], ErrorDetail(code="runner_disconnected", message="old exit"), store
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if new_state == "aba":
            store.replace_runner_id(child.id, "runner-b")
            store.replace_runner_id(child.id, "runner-a")
        status = {"idle": "idle", "genuine_error": "failed"}.get(new_state, "running")
        expected = store.record_session_observation(
            child.id,
            status,
            response_id=None if status == "idle" else "new-turn",
            error_key="provider-error" if status == "failed" else None,
        )
        assert expected is not None
        refreshed = store.get_conversation(child.id)
        assert refreshed is not None
        helpers._session_status_from_cache(child.id, refreshed.live_status, conversation=refreshed)
    finally:
        release.set()
        await asyncio.wait_for(sweep, 10)
    await _drain()
    assert store.get_session_observation(child.id) == expected
    assert store.get_conversation(child.id).runner_offline_decision is None
    assert common._session_status_cache[child.id] == status
    assert not any(event.get("runner_offline_episode_id") for event in events)
    assert store.list_items(parent.id).data == []
    failures = [record for record in caplog.records if "session turn failed for" in record.message]
    assert len(failures) == int(after_commit)
    if status == "running":
        assert common._session_active_response_cache[child.id] == "new-turn"


@pytest.mark.asyncio
async def test_delayed_no_id_failure_does_not_borrow_a_newer_queued_response(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
) -> None:
    store, _ = live_store
    conv = store.create_conversation(runner_id="runner-a")
    helpers._publish_status(conv.id, "running", owner=conv.session_observation)
    await _drain()
    owner, predecessor = helpers._capture_status_observation(conv.id, conv.session_observation)
    entered, release = threading.Event(), threading.Event()

    def block_worker() -> None:
        entered.set()
        assert release.wait(10)

    session_live_state.submit("test_paused_worker", block_worker)
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        helpers._publish_status(
            conv.id, "running", response_id="new-turn", owner=owner, predecessor=predecessor
        )
        failure = helpers._publish_status(
            conv.id,
            "failed",
            ErrorDetail(code="old-error", message="old failure"),
            owner=owner,
            predecessor=predecessor,
            failure_origin="explicit_source_test",
        )
        pending = session_live_state.pending_observation(conv.id)
        assert pending is not None and pending.response_id is None
    finally:
        release.set()
    assert failure is not None and await asyncio.wrap_future(failure) is None
    assert store.get_session_observation(conv.id).response_id == "new-turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("second_response, publish_busy", [(None, True), ("second-turn", False)])
async def test_a_new_turn_does_not_reuse_the_previous_failure_identity(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    caplog: pytest.LogCaptureFixture,
    second_response: str | None,
    publish_busy: bool,
) -> None:
    store, _ = live_store
    conv = store.create_conversation(runner_id="runner-a")
    error = ErrorDetail(code="native_turn_error", message="same source error")
    failure_ids: list[str | None] = []
    for index, response_id in enumerate(("first-turn", second_response)):
        if index == 0 or publish_busy:
            owner, predecessor = helpers._capture_status_observation(
                conv.id, conv.session_observation
            )
            helpers._publish_status(
                conv.id, "running", response_id=response_id, owner=owner, predecessor=predecessor
            )
            await _drain()
        owner, predecessor = helpers._capture_status_observation(conv.id, conv.session_observation)
        helpers._publish_status(
            conv.id,
            "failed",
            error,
            response_id=response_id,
            owner=owner,
            predecessor=predecessor,
            failure_origin="explicit_source_test",
        )
        await _drain()
        failure_ids.append(store.get_session_observation(conv.id).observation_id)
    assert failure_ids[0] != failure_ids[1]
    assert sum("session turn failed for" in row.message for row in caplog.records) == 2
    assert store.get_session_observation(conv.id).response_id == second_response


@pytest.mark.asyncio
async def test_no_response_source_failure_keeps_one_identity_across_queued_retries(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, events = live_store
    conv = store.create_conversation(runner_id="runner-a")
    helpers._publish_status(conv.id, "running", owner=conv.session_observation)
    await _drain()
    entered, release = threading.Event(), threading.Event()
    record = store.record_session_observation
    attempts = 0

    def delayed_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            assert release.wait(10)
            raise RuntimeError("synthetic transient metadata outage")
        return record(*args, **kwargs)

    monkeypatch.setattr(store, "record_session_observation", delayed_once)
    error = ErrorDetail(code="native_turn_error", message="source failed without response ID")
    observed_ids: list[str] = []
    events.clear()
    try:
        for callback in range(4):
            owner, predecessor = helpers._capture_status_observation(
                conv.id, conv.session_observation
            )
            helpers._publish_status(
                conv.id,
                "failed",
                error,
                owner=owner,
                predecessor=predecessor,
                failure_origin="explicit_source_test",
            )
            observed_ids.append(session_live_state.pending_observation(conv.id).observation_id)
            if callback == 0:
                assert await asyncio.to_thread(entered.wait, 10)
    finally:
        release.set()
    await _drain()
    assert len(set(observed_ids)) == 1
    current = store.get_session_observation(conv.id)
    assert current is not None and current.revision == 2 and current.response_id is None
    assert current.observation_id == observed_ids[0]
    assert sum("session turn failed for" in row.message for row in caplog.records) == 1
    failed = [event for event in events if event.get("status") == "failed"]
    assert failed and {event["session_observation"]["observation_id"] for event in failed} == set(
        observed_ids
    )


@pytest.mark.asyncio
async def test_metadata_outage_retries_without_manufacturing_or_losing_the_failure(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, events = live_store
    conv = store.create_conversation(runner_id="runner-a")
    helpers._publish_status(conv.id, "running", response_id="turn", owner=conv.session_observation)
    await _drain()
    stale = store.get_conversation(conv.id)
    assert stale is not None
    record = store.record_runner_offline_decision
    attempts = 0

    def fail_once(session_id: str, decision: RunnerOfflineDecision):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic metadata outage")
        return record(session_id, decision)

    monkeypatch.setattr(store, "record_runner_offline_decision", fail_once)
    monkeypatch.setattr(orchestration, "_RUNNER_OFFLINE_RETRY_DELAYS_S", (0.0, 0.0))
    await orchestration._mark_runner_sessions_offline_impl(
        [stale], ErrorDetail(code="runner_disconnected", message="genuine old-owner exit"), store
    )
    await _drain()
    assert attempts == 2
    assert store.get_conversation(conv.id).runner_offline_decision is not None
    assert common._session_status_cache[conv.id] == "failed"
    assert sum("session turn failed for" in record.message for record in caplog.records) == 1
    assert sum(bool(event.get("runner_offline_episode_id")) for event in events) == 1


@pytest.mark.asyncio
async def test_offline_retry_keeps_the_original_turn_after_same_runner_resumes(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, events = live_store
    conv = store.create_conversation(runner_id="runner-a")
    helpers._publish_status(
        conv.id, "running", response_id="old-turn", owner=conv.session_observation
    )
    await _drain()
    stale = store.get_conversation(conv.id)
    assert stale is not None
    record = store.record_runner_offline_decision
    attempts = 0

    def fail_then_resume(session_id: str, decision: RunnerOfflineDecision):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            resumed = store.record_session_observation(
                session_id, "running", response_id="new-turn"
            )
            assert resumed is not None
            current = store.get_conversation(session_id)
            helpers._session_status_from_cache(
                session_id, current.live_status, conversation=current
            )
            raise RuntimeError("synthetic transient settlement failure")
        return record(session_id, decision)

    monkeypatch.setattr(store, "record_runner_offline_decision", fail_then_resume)
    monkeypatch.setattr(orchestration, "_RUNNER_OFFLINE_RETRY_DELAYS_S", (0.0, 0.0))
    events.clear()
    await orchestration._mark_runner_sessions_offline_impl(
        [stale], ErrorDetail(code="runner_disconnected", message="old exit"), store
    )
    assert attempts == 2
    assert store.get_session_observation(conv.id).response_id == "new-turn"
    assert store.get_conversation(conv.id).runner_offline_decision is None
    assert common._session_status_cache[conv.id] == "running"
    assert common._session_active_response_cache[conv.id] == "new-turn"
    assert events == []
    assert not any("session turn failed for" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_committed_offline_decision_retries_projection_without_recounting_failure(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, events = live_store
    conv = store.create_conversation(runner_id="runner-a")
    helpers._publish_status(conv.id, "running", response_id="turn", owner=conv.session_observation)
    await _drain()
    stale = store.get_conversation(conv.id)
    get_conversation = store.get_conversation
    reads = 0

    def fail_first_projection(session_id: str):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError("synthetic projection read outage")
        return get_conversation(session_id)

    monkeypatch.setattr(store, "get_conversation", fail_first_projection)
    monkeypatch.setattr(orchestration, "_RUNNER_OFFLINE_RETRY_DELAYS_S", (0.0, 0.0))
    events.clear()
    await orchestration._mark_runner_sessions_offline_impl(
        [stale], ErrorDetail(code="runner_disconnected", message="genuine interruption"), store
    )
    await _drain()
    assert get_conversation(conv.id).runner_offline_decision is not None
    assert common._session_status_cache[conv.id] == "failed"
    assert sum(bool(event.get("runner_offline_episode_id")) for event in events) == 1
    assert sum("session turn failed for" in row.message for row in caplog.records) == 1


@pytest.mark.asyncio
async def test_postcommit_rebind_still_settles_only_the_interrupted_scheduled_firing(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, events = live_store
    scheduled = SqlAlchemyScheduledTaskStore(db_uri)
    session_live_state.configure(store, scheduled)
    conv = store.create_conversation(runner_id="runner-a")
    helpers._publish_status(
        conv.id, "running", response_id="old-turn", owner=conv.session_observation
    )
    await _drain()
    stale = store.get_conversation(conv.id)
    assert stale is not None
    task_id, old_run_id, new_run_id = [uuid.uuid4().hex for _ in range(3)]
    scheduled.create_run(old_run_id, task_id, "running", 1, conversation_id=conv.id)
    record = store.record_runner_offline_decision

    def rebind_after_commit(session_id: str, decision: RunnerOfflineDecision):
        result = record(session_id, decision)
        assert result is not None
        assert result.decision.scheduled_run_id == old_run_id
        store.replace_runner_id(session_id, "runner-b")
        scheduled.create_run(new_run_id, task_id, "running", 2, conversation_id=session_id)
        store.record_session_observation(session_id, "running", response_id="replacement-turn")
        current = store.get_conversation(session_id)
        helpers._session_status_from_cache(session_id, current.live_status, conversation=current)
        return result

    monkeypatch.setattr(store, "record_runner_offline_decision", rebind_after_commit)
    events.clear()
    await orchestration._mark_runner_sessions_offline_impl(
        [stale], ErrorDetail(code="runner_disconnected", message="old runner exit"), store
    )
    await _drain()
    runs = {run.id: run for run in scheduled.list_runs(task_id)[0]}
    assert runs[old_run_id].status == "failed"
    assert runs[old_run_id].error_code == "runner_disconnected"
    assert runs[new_run_id].status == "running"
    assert common._session_status_cache[conv.id] == "running"
    assert common._session_active_response_cache[conv.id] == "replacement-turn"
    assert events == []
    assert sum("session turn failed for" in row.message for row in caplog.records) == 1


@pytest.mark.parametrize("already_terminal", [False, True])
def test_delayed_offline_completion_only_names_the_captured_scheduled_run(
    live_store: tuple[SqlAlchemyConversationStore, list[dict]],
    db_uri: str,
    already_terminal: bool,
) -> None:
    store, _ = live_store
    scheduled = SqlAlchemyScheduledTaskStore(db_uri)
    session_live_state.configure(store, scheduled)
    conv = store.create_conversation(runner_id="runner-a")
    observed = store.record_session_observation(conv.id, "running", response_id="old-turn")
    assert observed is not None
    task_id, old_run_id, new_run_id = [uuid.uuid4().hex for _ in range(3)]
    scheduled.create_run(old_run_id, task_id, "running", 1, conversation_id=conv.id)
    if already_terminal:
        scheduled.update_run(old_run_id, status="succeeded", finished_at=2)
    decision = RunnerOfflineDecision(
        observation=observed,
        episode_id="old-episode",
        error_labels={},
        scheduled_run_id=old_run_id,
    )
    assert store.record_runner_offline_decision(conv.id, decision) is not None
    entered, release = threading.Event(), threading.Event()

    def hold_effects() -> None:
        entered.set()
        assert release.wait(10)

    blocked = session_live_state.submit("hold_old_run_completion", hold_effects)
    try:
        assert entered.wait(10)
        session_live_state.persist_owned_scheduled_run_completion(
            conv.id, decision, error_code="runner_disconnected", error="old exit"
        )
        store.replace_runner_id(conv.id, "runner-b")
        scheduled.create_run(new_run_id, task_id, "running", 3, conversation_id=conv.id)
    finally:
        release.set()
        blocked.result(timeout=10)
    session_live_state.submit("test_barrier", lambda: None).result(timeout=10)
    runs = {run.id: run for run in scheduled.list_runs(task_id)[0]}
    assert runs[old_run_id].status == ("succeeded" if already_terminal else "failed")
    assert runs[new_run_id].status == "running"
    old_outcome = runs[old_run_id]
    session_live_state.persist_owned_scheduled_run_completion(
        conv.id, decision, error_code="different_error", error="late duplicate"
    )
    session_live_state.submit("test_barrier", lambda: None).result(timeout=10)
    runs = {run.id: run for run in scheduled.list_runs(task_id)[0]}
    assert runs[old_run_id] == old_outcome
    assert runs[new_run_id].status == "running"

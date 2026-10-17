"""Native input startup follows only its captured normal completion."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import pending_inputs
from omnigent.server import session_live_state
from omnigent.server.routes._sessions import common, helpers, orchestration
from omnigent.server.schemas import ErrorDetail, SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture
def startup_store(db_uri: str) -> Iterator[SqlAlchemyConversationStore]:
    store = SqlAlchemyConversationStore(db_uri)
    common._session_status_cache.clear()
    common._session_terminal_pending_cache.clear()
    session_live_state.configure(store)
    try:
        yield store
    finally:
        session_live_state.submit("test_barrier", lambda: None).result(timeout=10)
        session_live_state.configure(None)
        common._session_status_cache.clear()
        common._session_terminal_pending_cache.clear()
        pending_inputs.reset_for_tests()


def _running_codex(store: SqlAlchemyConversationStore) -> Conversation:
    conv = store.create_conversation(
        agent_id=generate_agent_id(),
        runner_id="runner-a",
        harness_override="codex-native",
        labels={"omnigent.wrapper": "codex-native-ui"},
    )
    owner = store.record_session_observation(conv.id, "running", response_id="turn-a")
    current = store.get_conversation(conv.id)
    assert owner is not None and current is not None and current.session_observation == owner
    return current


def _message() -> SessionEventInput:
    return SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "new request"}]},
    )


async def _dispatch(
    conv: Conversation,
    store: SqlAlchemyConversationStore,
    client: httpx.AsyncClient,
    **kwargs: Any,
) -> Any:
    return await orchestration._dispatch_session_event_to_runner_impl(
        conv.id,
        conv,
        _message(),
        store,
        client,
        agent_name=None,
        file_store=None,
        artifact_store=None,
        background_titles_enabled=False,
        **kwargs,
    )


def _client(requests: list[httpx.Request]) -> httpx.AsyncClient:
    def post(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"status": "ready"})

    return httpx.AsyncClient(base_url="http://runner", transport=httpx.MockTransport(post))


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["done", "pending"])
async def test_dispatch_retains_its_exact_prior_idle_predecessor(
    startup_store: SqlAlchemyConversationStore, resolution: str
) -> None:
    store = startup_store
    conv = _running_codex(store)
    owner = conv.session_observation
    entered, release = threading.Event(), threading.Event()

    def hold_writer() -> None:
        entered.set()
        assert release.wait(10)

    barrier = session_live_state.submit("held_writer", hold_writer)
    assert await asyncio.to_thread(entered.wait, 10)
    idle = helpers._publish_status(conv.id, "idle", response_id="turn-a", owner=owner)
    assert idle is not None
    if resolution == "done":
        release.set()
        await asyncio.wrap_future(idle)
    requests: list[httpx.Request] = []
    task = None
    try:
        async with _client(requests) as client:
            task = asyncio.create_task(_dispatch(conv, store, client))
            await asyncio.sleep(0)
            if resolution == "pending":
                assert not idle.done() and not task.done()
                release.set()
            result = await asyncio.wait_for(task, 10)
        assert result.pending_id is not None
        assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
            "terminals",
            "events",
        ]
        current = store.get_conversation(conv.id)
        assert current is not None and not current.terminal_pending
        assert (
            current.live_status == "idle" and current.session_observation.response_id == "turn-a"
        )
        assert conv.session_observation == owner
    finally:
        release.set()
        await asyncio.wrap_future(barrier)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transition", ["none", "prior-idle", "new-turn", "failure", "rebind", "new-startup"]
)
async def test_compatibility_wait_is_protected_by_the_original_startup_claim(
    startup_store: SqlAlchemyConversationStore, transition: str
) -> None:
    store = startup_store
    conv = _running_codex(store)
    entered, release = threading.Event(), threading.Event()

    def compatibility(_conv: Conversation) -> None:
        entered.set()
        assert release.wait(10)

    requests: list[httpx.Request] = []
    task = None
    try:
        async with _client(requests) as client:
            task = asyncio.create_task(
                _dispatch(
                    conv,
                    store,
                    client,
                    runner_router=SimpleNamespace(require_mcp_registry_support=compatibility),
                )
            )
            assert await asyncio.to_thread(entered.wait, 10)
            reserved = store.get_conversation(conv.id)
            assert reserved is not None and reserved.terminal_pending
            startup = reserved.session_observation
            assert startup != conv.session_observation and startup.response_id == "turn-a"
            if transition in {"prior-idle", "new-turn", "failure"}:
                status, response_id = {
                    "prior-idle": ("idle", "turn-a"),
                    "new-turn": ("running", "turn-b"),
                    "failure": ("failed", "turn-a"),
                }[transition]
                changed = helpers._publish_status(
                    conv.id,
                    status,
                    response_id=response_id,
                    owner=startup,
                    starts_new_turn=transition == "new-turn",
                    error=ErrorDetail(code="source-error", message="real source failure")
                    if transition == "failure"
                    else None,
                )
                assert changed is not None and await asyncio.wrap_future(changed) is not None
            elif transition == "rebind":
                rebound = store.replace_runner_id(
                    conv.id,
                    "runner-b",
                    expected_observation=startup,
                )
                assert rebound.runner_id == "runner-b"
            elif transition == "new-startup":
                assert store.begin_terminal_startup(conv.id, startup, "newer-startup") is not None
            before = store.get_conversation(conv.id)
            assert before is not None
            release.set()
            if transition in {"none", "prior-idle"}:
                result = await asyncio.wait_for(task, 10)
                assert result.pending_id is not None and len(requests) == 2
                current = store.get_conversation(conv.id)
                assert current is not None and not current.terminal_pending
                if transition == "prior-idle":
                    assert current.live_status == "idle"
                    assert current.session_observation.response_id == "turn-a"
            else:
                with pytest.raises(OmnigentError) as failure:
                    await asyncio.wait_for(task, 10)
                assert failure.value.code == ErrorCode.RUNNER_UNAVAILABLE
                assert len(requests) == 1 and requests[0].url.path.endswith("/terminals")
                current = store.get_conversation(conv.id)
                assert (
                    current is not None
                    and current.session_observation == before.session_observation
                )
                assert current.runner_id == before.runner_id
                assert current.live_status_error_labels == before.live_status_error_labels
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "response_id", "starts_new_turn"),
    [
        ("running", "turn-b", True),
        ("waiting", "turn-a", False),
        ("failed", "turn-a", False),
        ("idle", "turn-a", True),
        ("running", "turn-a", True),
    ],
)
async def test_dispatch_never_adopts_captured_queued_new_work(
    startup_store: SqlAlchemyConversationStore,
    status: str,
    response_id: str,
    starts_new_turn: bool,
) -> None:
    store = startup_store
    conv = _running_codex(store)
    successor = helpers._publish_status(
        conv.id,
        status,
        response_id=response_id,
        owner=conv.session_observation,
        starts_new_turn=starts_new_turn,
        error=ErrorDetail(code="source-error", message="real source failure")
        if status == "failed"
        else None,
    )
    assert successor is not None
    committed = await asyncio.wrap_future(successor)
    assert committed is not None
    requests: list[httpx.Request] = []
    async with _client(requests) as client:
        with pytest.raises(OmnigentError) as failure:
            await _dispatch(conv, store, client)
    assert failure.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert requests == []
    assert store.get_session_observation(conv.id) == committed


@pytest.mark.asyncio
async def test_prior_idle_predecessor_cannot_follow_completion_then_new_work_aba(
    startup_store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = startup_store
    conv = _running_codex(store)
    stopped = helpers._publish_status(
        conv.id,
        "idle",
        response_id="turn-a",
        owner=conv.session_observation,
    )
    assert stopped is not None
    idle = await asyncio.wrap_future(stopped)
    assert idle is not None
    entered, release = threading.Event(), threading.Event()
    begin = store.begin_terminal_startup
    captured = []

    def held_begin(session_id, expected, token):
        captured.append(expected)
        entered.set()
        assert release.wait(10)
        return begin(session_id, expected, token)

    monkeypatch.setattr(store, "begin_terminal_startup", held_begin)
    requests: list[httpx.Request] = []
    task = None
    try:
        async with _client(requests) as client:
            task = asyncio.create_task(_dispatch(conv, store, client))
            assert await asyncio.to_thread(entered.wait, 10)
            assert captured == [idle]
            busy = store.record_session_observation(
                conv.id,
                "running",
                response_id="turn-a",
                expected_observation=idle,
                starts_new_turn=True,
            )
            assert busy is not None and busy != idle
            newer_idle = store.record_session_observation(
                conv.id,
                "idle",
                response_id="turn-a",
                expected_observation=busy,
            )
            assert newer_idle is not None and newer_idle != idle
            release.set()
            with pytest.raises(OmnigentError):
                await asyncio.wait_for(task, 10)
        assert requests == [] and store.get_session_observation(conv.id) == newer_idle
        assert captured == [idle]
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_shape_new_turn_cannot_be_mistaken_for_a_prior_completion(
    startup_store: SqlAlchemyConversationStore,
) -> None:
    store = startup_store
    conv = _running_codex(store)
    work = helpers._publish_status(
        conv.id,
        "running",
        response_id="turn-a",
        owner=conv.session_observation,
        starts_new_turn=True,
    )
    assert work is not None
    newer = await asyncio.wrap_future(work)
    assert newer is not None
    repeated = helpers._publish_status(
        conv.id,
        "running",
        response_id="turn-a",
        owner=conv.session_observation,
        starts_new_turn=True,
    )
    assert repeated is not None and await asyncio.wrap_future(repeated) == newer
    assert store.get_session_observation(conv.id) == newer
    stopped = helpers._publish_status(conv.id, "idle", response_id="turn-a", owner=newer)
    assert stopped is not None
    idle = await asyncio.wrap_future(stopped)
    assert idle is not None
    requests: list[httpx.Request] = []
    async with _client(requests) as client:
        with pytest.raises(OmnigentError) as failure:
            await _dispatch(conv, store, client)
    assert failure.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert requests == [] and store.get_session_observation(conv.id) == idle
    assert newer.revision == conv.session_observation.revision + 1

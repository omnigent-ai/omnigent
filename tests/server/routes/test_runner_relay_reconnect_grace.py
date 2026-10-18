"""Recovered streams get a fresh grace period for their next disconnect."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest

from omnigent.runtime import session_stream
from omnigent.server.routes._sessions import orchestration
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.conversation_rows import conversation_rows
from tests.server.helpers import start_session_stream_collector


class _RelayClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay
        await asyncio.sleep(0)


class _InterruptedStream(httpx.AsyncByteStream):
    def __init__(self, *, ready: bool, pause: asyncio.Event | None = None) -> None:
        self.ready = ready
        self.pause = pause

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.ready:
            yield b'data: {"type":"session.heartbeat"}\n\n'
        else:
            yield b": transport connected, no runner heartbeat\n\n"
        if self.pause is not None:
            await self.pause.wait()
        raise httpx.ReadError("test stream interrupted")


class _NaturalEofStream(httpx.AsyncByteStream):
    """Stream that ends without a terminal SSE sentinel."""

    def __init__(self, *frames: bytes) -> None:
        self.frames = frames

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for frame in self.frames:
            yield frame


@pytest.fixture
def relay_clock(monkeypatch: pytest.MonkeyPatch) -> _RelayClock:
    clock = _RelayClock()
    relay_asyncio = SimpleNamespace(**vars(asyncio))
    relay_asyncio.get_running_loop = lambda: clock
    relay_asyncio.sleep = clock.sleep
    monkeypatch.setattr(orchestration, "asyncio", relay_asyncio)
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 10.0)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 1.0)
    return clock


@pytest.mark.asyncio
async def test_natural_eof_reconnects_and_recovers_lifecycle(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An abrupt successful HTTP EOF must use the relay recovery window."""
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation()
    session_id = conversation.id
    orchestration._session_status_cache.pop(session_id, None)
    attempts = 0
    collector = await start_session_stream_collector(session_id)

    def respond(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        heartbeat = b'data: {"type":"session.heartbeat"}\n\n'
        if attempts == 1:
            # The runner reported a live turn, then the HTTP body ended before
            # its terminal lifecycle event or the stream's [DONE] sentinel.
            return httpx.Response(
                200,
                stream=_NaturalEofStream(
                    heartbeat,
                    b'data: {"type":"session.status","status":"running"}\n\n',
                ),
            )
        return httpx.Response(
            200,
            stream=_NaturalEofStream(
                heartbeat,
                b'data: {"type":"session.status","status":"idle"}\n\n',
                b"data: [DONE]\n\n",
            ),
        )

    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 10.0)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.0)
    try:
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            task = asyncio.create_task(
                orchestration._relay_runner_stream(session_id, client, store)
            )
            try:
                statuses: list[str] = []
                while len(statuses) < 2:
                    event = await collector.next_event(timeout=10.0)
                    if event.get("type") == "session.status":
                        statuses.append(event["status"])
                await asyncio.wait_for(task, timeout=10.0)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        assert attempts == 2
        assert statuses == ["running", "idle"]
        assert orchestration._session_status_cache[session_id] == "idle"
    finally:
        await collector.stop()
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_persistent_natural_eof_exhausts_disconnect_grace(
    db_uri: str, relay_clock: _RelayClock
) -> None:
    """Repeated EOFs fail through the existing bounded grace decision."""
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation()
    session_id = conversation.id
    orchestration._session_status_cache[session_id] = "running"
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        relay_clock.now += 4.0
        return httpx.Response(
            200,
            stream=_NaturalEofStream(b'data: {"type":"session.status","status":"running"}\n\n'),
        )

    try:
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            await asyncio.wait_for(
                orchestration._relay_runner_stream(session_id, client, store), timeout=10
            )
        assert attempts == 3
        assert orchestration._session_status_cache[session_id] == "failed"
        persisted = store.get_conversation(session_id)
        assert persisted is not None
        assert persisted.labels["omnigent.last_task_error_code"] == "runner_disconnected"
    finally:
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped_runner", [None, "runner-current", "runner-old"])
async def test_natural_eof_matches_intent_to_the_relay_runner(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, stopped_runner: str | None
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    runner_id = "runner-current"
    conversation = store.create_conversation(runner_id=runner_id)
    session_id = conversation.id
    store.set_session_live_status(session_id, "running")
    orchestration._session_status_cache[session_id] = "running"
    if stopped_runner is not None:
        orchestration._intentional_stop_sessions[session_id] = stopped_runner
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.0)

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=_NaturalEofStream(b'data: {"type":"session.heartbeat"}\n\n')
        )

    try:
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            await asyncio.wait_for(
                orchestration._relay_runner_stream(session_id, client, store, runner_id=runner_id),
                timeout=10,
            )
        intentional = stopped_runner == runner_id
        assert orchestration._session_status_cache[session_id] == (
            "idle" if intentional else "failed"
        )
        persisted = store.get_conversation(session_id)
        assert persisted is not None
        error = orchestration._last_task_error_from_labels(persisted.labels)
        if intentional:
            assert error is None
            assert session_id not in orchestration._intentional_stop_sessions
        else:
            assert error is not None and error["code"] == "runner_disconnected"
    finally:
        orchestration._intentional_stop_sessions.pop(session_id, None)
        orchestration._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_short_recovered_streams_do_not_share_a_disconnect_deadline(
    db_uri: str, relay_clock: _RelayClock
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation()
    session_id = conversation.id
    orchestration._session_status_cache[session_id] = "running"
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        relay_clock.now += 4.0
        if attempts <= 4:
            return httpx.Response(200, stream=_InterruptedStream(ready=True))
        return httpx.Response(
            200,
            text=(
                'data: {"type":"session.heartbeat"}\n\n'
                'data: {"type":"session.status","status":"idle"}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    try:
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            await asyncio.wait_for(
                orchestration._relay_runner_stream(session_id, client, store), timeout=10
            )
        assert attempts == 5, "the relay gave up despite receiving fresh runner heartbeats"
        assert orchestration._session_status_cache[session_id] == "idle"
        persisted = store.get_conversation(session_id)
        assert persisted is not None
        assert not persisted.labels.get("omnigent.last_task_error_code")
    finally:
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_repeated_recovery_keeps_backoff_and_remains_cancellable(
    db_uri: str, relay_clock: _RelayClock
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation()
    session_id = conversation.id
    orchestration._session_status_cache[session_id] = "running"
    recovered = asyncio.Event()
    pause = asyncio.Event()
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        relay_clock.now += 4.0
        if attempts == 20:
            recovered.set()
        return httpx.Response(
            200, stream=_InterruptedStream(ready=True, pause=pause if attempts == 20 else None)
        )

    try:
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            task = asyncio.create_task(
                orchestration._relay_runner_stream(session_id, client, store)
            )
            try:
                await asyncio.wait_for(recovered.wait(), timeout=10)
                assert not task.done()
                assert relay_clock.delays == [1.0] * 19
                assert orchestration._session_status_cache[session_id] == "running"
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert attempts == 20
    finally:
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy_attempts", [0, 2])
async def test_an_unrecovered_stream_still_exhausts_its_grace(
    db_uri: str, relay_clock: _RelayClock, healthy_attempts: int
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation()
    session_id = conversation.id
    orchestration._session_status_cache[session_id] = "running"
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        relay_clock.now += 4.0
        return httpx.Response(200, stream=_InterruptedStream(ready=attempts <= healthy_attempts))

    try:
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            await asyncio.wait_for(
                orchestration._relay_runner_stream(session_id, client, store), timeout=10
            )
        assert attempts == max(1, healthy_attempts) + 2
        assert orchestration._session_status_cache[session_id] == "failed"
        persisted = store.get_conversation(session_id)
        assert persisted is not None
        assert persisted.labels["omnigent.last_task_error_code"] == "runner_disconnected"
    finally:
        orchestration._session_status_cache.pop(session_id, None)
        orchestration._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_does_not_fail_a_session_deleted_during_its_grace(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session and its sub-agent deleted mid-grace must not get a failed turn.

    Another replica serves the delete and removes both rows from the shared
    database; this process keeps their relays and a ``running`` status cache.
    """
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 2.0)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.05)
    store = SqlAlchemyConversationStore(db_uri)
    parent_id = store.create_conversation().id
    child_id = store.create_conversation(kind="sub_agent", parent_conversation_id=parent_id).id
    session_ids = (parent_id, child_id)
    attempts = dict.fromkeys(session_ids, 0)
    retrying = asyncio.Event()

    def respond(request: httpx.Request) -> httpx.Response:
        session_id = request.url.path.split("/")[3]
        attempts[session_id] += 1
        if attempts[session_id] == 1:
            # The runner reported a live turn, then its tunnel closed.
            return httpx.Response(
                200,
                stream=_NaturalEofStream(
                    b'data: {"type":"session.status","status":"running"}\n\n'
                ),
            )
        if all(count >= 2 for count in attempts.values()):
            retrying.set()
        return httpx.Response(200, stream=_NaturalEofStream())

    collectors = {}
    tasks: list[asyncio.Task[None]] = []
    try:
        for session_id in session_ids:
            collectors[session_id] = await start_session_stream_collector(session_id)
        async with httpx.AsyncClient(
            base_url="http://runner", transport=httpx.MockTransport(respond)
        ) as client:
            tasks = [
                asyncio.create_task(
                    orchestration._relay_runner_stream(
                        session_id, client, store, runner_id="runner-on-this-replica"
                    )
                )
                for session_id in session_ids
            ]
            await asyncio.wait_for(retrying.wait(), timeout=10.0)
            assert all(
                orchestration._session_status_cache.get(session_id) == "running"
                for session_id in session_ids
            )
            assert await store.delete_conversation(parent_id) is True
            assert all(store.get_conversation(session_id) is None for session_id in session_ids)
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=10.0)

        outcomes = {}
        for session_id in session_ids:
            failed_events = []
            while not collectors[session_id].queue.empty():
                event = collectors[session_id].queue.get_nowait()
                if event.get("type") == "session.status" and event.get("status") == "failed":
                    failed_events.append(event)
            outcomes[session_id] = {
                "cached_status": orchestration._session_status_cache.get(session_id),
                "failed_events": failed_events,
                **conversation_rows(db_uri, session_id),
            }
        assert all(
            outcome["cached_status"] != "failed"
            and not outcome["failed_events"]
            and not outcome["labels"]
            for outcome in outcomes.values()
        ), f"deleted sessions were failed by the relay give-up: {outcomes}"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for session_id in session_ids:
            if session_id in collectors:
                await collectors[session_id].stop()
            orchestration._session_status_cache.pop(session_id, None)
            orchestration._session_active_response_cache.pop(session_id, None)
            session_stream.close(session_id)

"""A message forward the server repeats after a tunnel drop runs the prompt once.

The server persists a user message, forwards it with its ``persisted_item_id``,
and repeats the forward when the runner tunnel drops before the response
arrives. The first frame may already have reached the runner, so the runner
acknowledges a repeat of a message it has taken instead of running it again,
and releases that claim again when the message turns out not to have run.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.runner.app import _ACCEPTED_FORWARD_IDS_PER_SESSION
from omnigent.runner.session_history import pending_user_item_ids
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import (
    _BlockingHarnessClient,
    _build_app_for_spec,
    _FakeProcessManager,
    _ordered_user_texts,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient
from tests.runner.native_helpers import _harness_spec

AGENT_ID = "ag_repeat_forward"
SESSION_ID = "conv_repeat_forward"
EVENTS_PATH = f"/v1/sessions/{SESSION_ID}/events"

NATIVE_AGENT_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
NATIVE_SESSION_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
NATIVE_EVENTS_PATH = f"/v1/sessions/{NATIVE_SESSION_ID}/events"

DEDUPED = "Message already accepted; not run again."
STARTED = "Turn started."


def _turn_frames(*, delta: object = "hi") -> list[str]:
    """One complete harness turn; a non-string *delta* trips the runner's reply join."""
    return [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.output_text.delta", "delta": delta}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]


def _build_app_with_manager(manager: _FakeProcessManager) -> FastAPI:
    """Runner app over a caller-supplied process manager."""
    spec = AgentSpec(spec_version=1, name="t")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    return create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


def _build_app(gate: asyncio.Event) -> tuple[FastAPI, _BlockingHarnessClient]:
    """Runner app whose harness holds the first turn open until *gate* is set."""
    harness = _BlockingHarnessClient(_turn_frames(), gate)
    return _build_app_with_manager(_FakeProcessManager(harness)), harness


def _forward(item_id: str, text: str) -> dict[str, Any]:
    """The body the server forwards for a persisted user message."""
    return {
        "type": "message",
        "role": "user",
        "agent_id": AGENT_ID,
        "content": [{"type": "input_text", "text": text}],
        "persisted_item_id": item_id,
    }


async def _wait_for(condition: Callable[[], bool], *, timeout: float = 10.0) -> None:
    """Poll *condition* until it holds or *timeout* seconds pass."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_repeated_forward_of_a_taken_message_is_acknowledged_not_run_again() -> None:
    """A repeat of a running or buffered message is acknowledged; other items still queue."""
    from omnigent.runner.app import _session_histories_ref

    gate = asyncio.Event()
    app, harness = _build_app(gate)
    buffers = app.state.session_message_buffers
    try:
        async with _runner_client(app) as client:
            first = await client.post(EVENTS_PATH, json=_forward("msg_001", "hello"))
            assert first.status_code == 202, first.text
            assert first.json()["status"] == "accepted"
            await asyncio.wait_for(harness.post_seen.wait(), timeout=5.0)

            # The server's repeat of the running message: nothing queued, no second turn.
            repeat = await client.post(EVENTS_PATH, json=_forward("msg_001", "hello"))
            assert repeat.status_code == 202, repeat.text
            assert repeat.json()["detail"] == DEDUPED
            assert buffers.get(SESSION_ID, []) == []
            assert len(harness.posted_bodies) == 1

            # A different persisted item is new input and queues behind the turn once,
            # however often its forward is repeated.
            other = await client.post(EVENTS_PATH, json=_forward("msg_002", "and this"))
            assert other.status_code == 202, other.text
            assert other.json()["status"] == "buffered"
            repeat_other = await client.post(EVENTS_PATH, json=_forward("msg_002", "and this"))
            assert repeat_other.json()["detail"] == DEDUPED
            assert [m["persisted_item_id"] for m in buffers[SESSION_ID]] == ["msg_002"]

            gate.set()
            await _wait_for(lambda: len(harness.posted_bodies) >= 2)
            # The first turn and the queued item each ran exactly once, and the
            # second run is the queued item itself — not a re-run of the first.
            assert len(harness.posted_bodies) == 2
            assert harness.turn_user_texts[0][-1] == "hello"
            assert harness.turn_user_texts[1][-1] == "and this"
    finally:
        gate.set()
        _session_histories_ref.pop(SESSION_ID, None)


@pytest.mark.asyncio
async def test_live_forwards_beyond_the_completed_id_cache_still_dedupe_a_repeat() -> None:
    """Running and buffered messages dedupe their repeats however many are pending.

    The accepted-id ledger is bounded, so a burst of forwards during one long
    turn ages the running message's id and the oldest buffered ids out of it.
    Those messages are still live, so a repeat of any of them must be
    acknowledged rather than appended to the buffer as new input.
    """
    from omnigent.runner.app import _session_histories_ref

    gate = asyncio.Event()
    app, harness = _build_app(gate)
    buffers = app.state.session_message_buffers
    try:
        async with _runner_client(app) as client:
            first = await client.post(EVENTS_PATH, json=_forward("msg_000", "first"))
            assert first.status_code == 202, first.text
            await asyncio.wait_for(harness.post_seen.wait(), timeout=5.0)

            queued_ids = [f"msg_{n:03d}" for n in range(1, _ACCEPTED_FORWARD_IDS_PER_SESSION + 2)]
            for item_id in queued_ids:
                queued = await client.post(EVENTS_PATH, json=_forward(item_id, item_id))
                assert queued.json()["status"] == "buffered", queued.text
            assert [m["persisted_item_id"] for m in buffers[SESSION_ID]] == queued_ids

            # Both the running message and the oldest buffered one have aged out
            # of the bounded ledger; their repeats must still be deduplicated.
            for item_id, text in (("msg_000", "first"), (queued_ids[0], queued_ids[0])):
                repeat = await client.post(EVENTS_PATH, json=_forward(item_id, text))
                assert repeat.status_code == 202, repeat.text
                assert repeat.json()["detail"] == DEDUPED, item_id
            assert [m["persisted_item_id"] for m in buffers[SESSION_ID]] == queued_ids
            assert len(harness.posted_bodies) == 1

            gate.set()
            await _wait_for(lambda: len(harness.posted_bodies) >= 2)
            assert len(harness.posted_bodies) == 2
    finally:
        gate.set()
        _session_histories_ref.pop(SESSION_ID, None)


@pytest.mark.asyncio
async def test_streaming_turn_registers_its_running_id_so_an_aged_out_repeat_dedupes() -> None:
    """A ``stream=true`` turn dedupes a repeat even after its id ages out of the ledger.

    The direct-stream path has no background ``_run_turn_bg`` to mark the turn's
    forward id running. Once a burst of later forwards ages that id out of the
    bounded accepted ledger, a repeat of the still-running streamed message must
    stay deduplicated rather than start a second run of it.
    """
    from omnigent.runner.app import _session_histories_ref

    gate = asyncio.Event()
    app, harness = _build_app(gate)
    buffers = app.state.session_message_buffers
    stream_task: asyncio.Task[None] | None = None
    try:
        async with _runner_client(app) as client:
            drained = asyncio.Event()

            async def _drive_stream() -> None:
                async with client.stream(
                    "POST", f"{EVENTS_PATH}?stream=true", json=_forward("msg_000", "first")
                ) as resp:
                    assert resp.status_code == 200
                    async for _ in resp.aiter_bytes():
                        pass
                drained.set()

            stream_task = asyncio.create_task(_drive_stream())
            await asyncio.wait_for(harness.post_seen.wait(), timeout=5.0)

            # Age the streamed message's id out of the bounded ledger with a burst
            # of buffered forwards while its stream is still open.
            queued_ids = [f"msg_{n:03d}" for n in range(1, _ACCEPTED_FORWARD_IDS_PER_SESSION + 2)]
            for item_id in queued_ids:
                queued = await client.post(EVENTS_PATH, json=_forward(item_id, item_id))
                assert queued.json()["status"] == "buffered", queued.text

            # The streamed turn registered its id as running, so its repeat is
            # deduplicated even though it has left the accepted ledger.
            repeat = await client.post(EVENTS_PATH, json=_forward("msg_000", "first"))
            assert repeat.status_code == 202, repeat.text
            assert repeat.json()["detail"] == DEDUPED
            assert all(m["persisted_item_id"] != "msg_000" for m in buffers.get(SESSION_ID, []))
            assert len(harness.posted_bodies) == 1

            gate.set()
            await asyncio.wait_for(drained.wait(), timeout=10.0)
            # The buffered burst runs as one coalesced continuation, so the streamed
            # message ran once and never a second time from its repeat.
            await _wait_for(lambda: len(harness.posted_bodies) >= 2)
            assert len(harness.posted_bodies) == 2
    finally:
        gate.set()
        if stream_task is not None and not stream_task.done():
            stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stream_task
        _session_histories_ref.pop(SESSION_ID, None)


@pytest.mark.asyncio
async def test_deduplicated_native_forward_does_not_remark_the_turn_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deduplicated repeat on a native session must not re-mark the turn running.

    ``note_session_turn_started`` sets the status memo to ``running`` and leans
    on the turn's completion to flip it back to ``idle``. A repeat whose original
    turn is already gone starts no turn, so the note must fire only once the
    runner commits to a turn, never from the dedup acknowledgement.
    """
    from omnigent.runner.app import _session_histories_ref

    app, _ = await _build_app_for_spec(_harness_spec("claude-native"))
    registry = app.state.session_resource_registry
    note_calls: list[str] = []
    _real_note = registry.note_session_turn_started

    def _spy_note(session_id: str) -> None:
        note_calls.append(session_id)
        _real_note(session_id)

    monkeypatch.setattr(registry, "note_session_turn_started", _spy_note)

    try:
        async with _runner_client(app) as client:
            create = await client.post(
                "/v1/sessions",
                json={"session_id": NATIVE_SESSION_ID, "agent_id": NATIVE_AGENT_ID},
            )
            assert create.status_code == 201, create.text
            note_calls.clear()

            # Pin an active turn so the forward buffers without launching a turn.
            holder = asyncio.ensure_future(asyncio.Event().wait())
            app.state.active_turns[NATIVE_SESSION_ID] = holder
            try:
                first = await client.post(NATIVE_EVENTS_PATH, json=_forward("msg_dedup", "hi"))
                assert first.status_code == 202, first.text
                assert first.json()["status"] == "buffered"
                assert note_calls == [NATIVE_SESSION_ID]

                repeat = await client.post(NATIVE_EVENTS_PATH, json=_forward("msg_dedup", "hi"))
                assert repeat.status_code == 202, repeat.text
                assert repeat.json()["detail"] == DEDUPED
                # The deduped repeat starts no turn, so it must not re-mark running.
                assert note_calls == [NATIVE_SESSION_ID]
            finally:
                app.state.active_turns.pop(NATIVE_SESSION_ID, None)
                holder.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await holder
    finally:
        _session_histories_ref.pop(NATIVE_SESSION_ID, None)


class _SpawnFailureProcessManager(_FakeProcessManager):
    """Fails the next harness spawn while ``fail_next`` is armed, then behaves normally.

    Models a transient ``get_client`` spawn failure during turn setup, before
    any turn runs; the following turn gets a working client.
    """

    def __init__(self, client: _ScriptedHarnessClient, *, fail_next: bool = True) -> None:
        super().__init__(client)
        self.fail_next = fail_next

    async def get_client(
        self, conversation_id: str, harness: str, env: Any = None
    ) -> _ScriptedHarnessClient:
        if self.fail_next:
            self.fail_next = False
            self.get_client_calls.append((conversation_id, harness, env))
            raise RuntimeError("transient harness spawn failure")
        return await super().get_client(conversation_id, harness, env)


class _RejectFirstHarnessClient(_ScriptedHarnessClient):
    """Answers the first turn delivery with HTTP 503, then streams normally."""

    def __init__(self, sse_frames: list[str]) -> None:
        super().__init__(sse_frames)
        self.reject_next = True

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        if not self.reject_next:
            return super().stream(method, url, json=json, timeout=timeout)
        self.reject_next = False
        self.posted_bodies.append(json)

        class _Rejected:
            status_code = 503

            async def aiter_text(self) -> AsyncIterator[str]:
                for frame in ():
                    yield frame

            async def __aenter__(self) -> _Rejected:
                return self

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _Rejected()


class _DropMidStreamHarnessClient(_ScriptedHarnessClient):
    """Accepts the turn, streams the first frame, then loses the connection."""

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.posted_bodies.append(json)
        frames = self._sse_frames

        class _Dropping:
            status_code = 200

            async def aiter_text(self) -> AsyncIterator[str]:
                yield frames[0]
                raise RuntimeError("harness connection closed mid-stream")

            async def __aenter__(self) -> _Dropping:
                return self

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _Dropping()


@pytest.mark.asyncio
async def test_setup_failure_forgets_the_accept_so_a_repeated_forward_retries() -> None:
    """A forward whose turn setup fails is retried by the server's repeat.

    The runner accepts the forward and launches the background turn, but the
    harness spawn fails before any turn runs. The accept marker must be dropped
    so the server's repeat of the same ``persisted_item_id`` runs the message
    instead of being acknowledged as already taken and silently lost.
    """
    from omnigent.runner.app import _session_histories_ref

    harness = _ScriptedHarnessClient(_turn_frames())
    manager = _SpawnFailureProcessManager(harness)
    app = _build_app_with_manager(manager)
    try:
        async with _runner_client(app) as client:
            first = await client.post(EVENTS_PATH, json=_forward("msg_fail", "hello"))
            assert first.status_code == 202, first.text

            # The failed setup turn drops its slot right after forgetting the
            # accept marker, so a cleared slot means the forget has run.
            await _wait_for(
                lambda: bool(manager.get_client_calls) and SESSION_ID not in app.state.active_turns
            )
            assert manager.get_client_calls, "first turn never attempted a spawn"
            assert SESSION_ID not in app.state.active_turns
            assert harness.posted_bodies == []

            # The server repeats the forward; the marker is gone, so it runs.
            repeat = await client.post(EVENTS_PATH, json=_forward("msg_fail", "hello"))
            assert repeat.status_code == 202, repeat.text
            assert repeat.json()["detail"] == STARTED
            await _wait_for(lambda: bool(harness.posted_bodies))
            assert len(harness.posted_bodies) == 1
    finally:
        _session_histories_ref.pop(SESSION_ID, None)


@pytest.mark.asyncio
async def test_buffered_message_whose_continuation_setup_fails_is_retried_by_a_repeat() -> None:
    """A buffered forward whose turn never starts is retried by the server's repeat.

    A message that arrives mid-turn is buffered and acknowledged, and runs later
    from the continuation dispatch. When that dispatch fails before the harness
    takes the turn, the buffered message's accept marker must be released as
    well; otherwise the server's repeat is acknowledged as already taken and
    the message is silently lost.
    """
    from omnigent.runner.app import _session_histories_ref

    gate = asyncio.Event()
    harness = _BlockingHarnessClient(_turn_frames(), gate)
    manager = _SpawnFailureProcessManager(harness, fail_next=False)
    app = _build_app_with_manager(manager)
    try:
        async with _runner_client(app) as client:
            first = await client.post(EVENTS_PATH, json=_forward("msg_001", "hello"))
            assert first.status_code == 202, first.text
            await asyncio.wait_for(harness.post_seen.wait(), timeout=5.0)
            queued = await client.post(EVENTS_PATH, json=_forward("msg_002", "and this"))
            assert queued.status_code == 202, queued.text
            assert queued.json()["status"] == "buffered"

            # The continuation turn for the buffered message fails its spawn.
            manager.fail_next = True
            gate.set()
            await _wait_for(
                lambda: not manager.fail_next and SESSION_ID not in app.state.active_turns
            )
            assert not manager.fail_next, "the continuation never attempted a spawn"
            assert SESSION_ID not in app.state.active_turns
            assert len(harness.posted_bodies) == 1

            # The server repeats the buffered message's forward; it runs now.
            repeat = await client.post(EVENTS_PATH, json=_forward("msg_002", "and this"))
            assert repeat.status_code == 202, repeat.text
            assert repeat.json()["detail"] == STARTED
            await _wait_for(lambda: len(harness.posted_bodies) >= 2)
            assert len(harness.posted_bodies) == 2
            assert harness.turn_user_texts[1][-1] == "and this"
    finally:
        gate.set()
        _session_histories_ref.pop(SESSION_ID, None)


@pytest.mark.asyncio
async def test_lazy_harness_rejection_forgets_the_accept_so_a_repeated_forward_retries() -> None:
    """A harness that rejects the delivery inside the stream releases the accept.

    The runner acknowledges the forward with 202 and only then opens the harness
    stream. A non-200 answer there means no turn ran, so the marker must go even
    though the rejection surfaces from the lazy response generator rather than
    from the dispatch call itself.
    """
    from omnigent.runner.app import _session_histories_ref

    harness = _RejectFirstHarnessClient(_turn_frames())
    app = _build_app_with_manager(_FakeProcessManager(harness))
    try:
        async with _runner_client(app) as client:
            first = await client.post(EVENTS_PATH, json=_forward("msg_rej", "hello"))
            assert first.status_code == 202, first.text
            await _wait_for(
                lambda: (
                    len(harness.posted_bodies) == 1 and SESSION_ID not in app.state.active_turns
                )
            )
            assert len(harness.posted_bodies) == 1
            assert SESSION_ID not in app.state.active_turns

            # The server repeats the forward; the rejected delivery left no claim.
            repeat = await client.post(EVENTS_PATH, json=_forward("msg_rej", "hello"))
            assert repeat.status_code == 202, repeat.text
            assert repeat.json()["detail"] == STARTED
            await _wait_for(lambda: len(harness.posted_bodies) >= 2)
            assert len(harness.posted_bodies) == 2
            assert _ordered_user_texts(harness.posted_bodies[1])[-1] == "hello"
    finally:
        _session_histories_ref.pop(SESSION_ID, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stream_drops", "bad_delta"])
async def test_failure_after_the_harness_accepted_keeps_the_accept(failure: str) -> None:
    """A turn that fails after the harness took it is not re-run by the repeat.

    Once the harness has accepted the delivery the message ran, whatever happens
    to the stream afterwards: a transport drop mid-stream, or a runner-side
    processing error such as a non-string text delta breaking the reply join.
    Releasing the marker there would let the server's repeat execute the
    message, and its side effects, a second time.
    """
    from omnigent.runner.app import _session_histories_ref

    harness: _ScriptedHarnessClient
    if failure == "stream_drops":
        harness = _DropMidStreamHarnessClient(_turn_frames())
    else:
        harness = _ScriptedHarnessClient(_turn_frames(delta=5))
    app = _build_app_with_manager(_FakeProcessManager(harness))
    try:
        async with _runner_client(app) as client:
            first = await client.post(EVENTS_PATH, json=_forward("msg_ran", "hello"))
            assert first.status_code == 202, first.text
            await _wait_for(
                lambda: (
                    len(harness.posted_bodies) == 1 and SESSION_ID not in app.state.active_turns
                )
            )
            assert SESSION_ID not in app.state.active_turns, "the failed turn kept its slot"

            # The server repeats the forward; the message already ran once.
            repeat = await client.post(EVENTS_PATH, json=_forward("msg_ran", "hello"))
            assert repeat.status_code == 202, repeat.text
            assert repeat.json()["detail"] == DEDUPED
            await asyncio.sleep(0.1)
            assert len(harness.posted_bodies) == 1
    finally:
        _session_histories_ref.pop(SESSION_ID, None)


def test_pending_user_item_ids_collects_the_trailing_unanswered_user_messages() -> None:
    """Dedup keys on every pending user message, skipping items conversion drops."""
    # Two user messages after the last answered turn: both pending, in order.
    assert pending_user_item_ids(
        [
            {"id": "a", "type": "message", "role": "assistant", "content": []},
            {"id": "b", "type": "message", "role": "user", "content": []},
            {"id": "c", "type": "message", "role": "user", "content": []},
        ]
    ) == ["b", "c"]
    # A trailing non-input item (dropped by conversion) does not end the run.
    assert pending_user_item_ids(
        [
            {"id": "b", "type": "message", "role": "user", "content": []},
            {"id": "c", "type": "reasoning", "summary": "…"},
        ]
    ) == ["b"]
    # An assistant reply or tool call ends the pending run.
    assert (
        pending_user_item_ids(
            [
                {"id": "b", "type": "message", "role": "user", "content": []},
                {
                    "id": "f",
                    "type": "function_call",
                    "call_id": "x",
                    "name": "n",
                    "arguments": "{}",
                },
            ]
        )
        == []
    )
    assert pending_user_item_ids([]) == []
    assert pending_user_item_ids([{"type": "message", "role": "assistant", "content": []}]) == []
    # A user message without a usable id is skipped but does not crash.
    assert pending_user_item_ids([{"type": "message", "role": "user"}]) == []


def test_create_runner_app_mints_a_fresh_dedup_epoch_per_process() -> None:
    """Each runner process gets its own forward-dedup epoch.

    The epoch scopes the in-memory accept ledger to this process, so the server
    repeats a forward only while the advertising process is still on the tunnel.
    A separate app models a same-id restart: its empty ledger must carry a
    different epoch so the server declines the repeat instead of re-running it.
    """
    first = _build_app_with_manager(_FakeProcessManager(_ScriptedHarnessClient([])))
    second = _build_app_with_manager(_FakeProcessManager(_ScriptedHarnessClient([])))
    assert isinstance(first.state.runner_dedup_epoch, str)
    assert first.state.runner_dedup_epoch
    assert first.state.runner_dedup_epoch != second.state.runner_dedup_epoch

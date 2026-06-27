"""Tests for the RPC read driver (:mod:`omnigent.antigravity_native_reader`).

The reader replaces the transcript-tail forwarder's read loop: it polls agy's
connect-RPC for trajectory steps, maps each new step to Omnigent conversation
items (via the pure Task 4 mapper), POSTs them, emits session-status edges on
transition, and hands WAITING steps to the Task 8 interaction bridge through an
``on_pending_interaction`` callback.

These tests drive the loop with NO real agy and NO real sockets:

* ``get_trajectory_steps`` is monkeypatched to return a scripted sequence of
  step-list snapshots (one per poll).
* port discovery (``_candidate_agy_rpc_ports`` / ``_conversation_matches``) and
  the cascade-id resolution (``read_bridge_state``) are monkeypatched so the
  reader resolves immediately without OS/network access.
* posts are captured by replacing ``post_session_event_with_retry`` with a fake
  sink that records every ``(event_type, data)`` it is asked to deliver.

The loop is made finite by an injectable ``stop`` predicate (checked once per
poll) so a test drives a bounded number of iterations rather than looping
forever.

Key assertions (the plan's Step 1 + status + error):

* Each new step posts exactly once; re-reads of the same steps post nothing.
* A USER_INPUT step posts nothing (already persisted by the direct POST).
* A WAITING step invokes ``on_pending_interaction`` exactly once (not on re-read).
* RUNNING/IDLE ``external_session_status`` edges are emitted on transition only.
* A ``get_trajectory_steps`` raising ``httpx.HTTPError`` does not crash the loop.

Task T-D adds STREAM mode (live ``output_text_delta`` typing). The reader now
prefers :func:`stream_agent_state_updates` (a scripted async generator of
cumulative frames in the tests) and only falls back to the poll loop on a stream
error. The stream-mode assertions:

* Growing ``plannerResponse.modifiedResponse`` while a step is GENERATING emits
  incremental ``external_output_text_delta`` events whose ``delta`` suffixes
  concatenate to the full text, share a stable per-step ``message_id``, and never
  overlap/duplicate.
* The DONE frame emits exactly ONE committed ``message`` (via the mapper), AFTER
  the deltas; a re-sent DONE (on-connect snapshot replay) does NOT re-post it.
* A stream raising ``httpx.HTTPError`` / ``AntigravityRpcError`` falls back to the
  poll loop (committed-only) without crashing the reader.
* A WAITING frame hands its interaction to the bridge exactly once.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from omnigent import antigravity_native_reader as reader
from omnigent.antigravity_native_bridge import read_bridge_state
from omnigent.antigravity_native_rpc import AntigravityRpcError
from omnigent.antigravity_native_steps import PendingInteraction

# ---------------------------------------------------------------------------
# Fixtures + scaffolding
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "antigravity" / "steps"
_CASCADE_ID = "efb134b2-d69f-43de-bb54-c9ece346d8a3"
_SESSION_ID = "conv_reader_test"
_PORT = 52548


def _load(name: str) -> dict[str, Any]:
    """Load one recorded step fixture by filename (without extension)."""
    path = _FIXTURES / f"{name}.json"
    return cast(dict[str, Any], json.loads(path.read_text()))


class _PostSink:
    """Capture every event the reader asks to POST (no HTTP)."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []

    async def __call__(
        self,
        *,
        client: object,
        url: str,
        payload: dict[str, object],
        event_type: str,
        max_attempts: int,
        retry_status_codes: object,
        sleep: object,
        retry_delay: object,
        logger_name: str,
    ) -> httpx.Response:
        data = payload.get("data")
        self.posts.append((event_type, cast(dict[str, object], data)))
        return httpx.Response(200, json={"ok": True})

    def item_types(self) -> list[str]:
        """Return the ``item_type`` of every conversation-item post, in order."""
        out: list[str] = []
        for event_type, data in self.posts:
            if event_type == "external_conversation_item":
                item_type = data.get("item_type")
                out.append(item_type if isinstance(item_type, str) else "<none>")
        return out

    def statuses(self) -> list[str]:
        """Return the ``status`` of every session-status edge, in order."""
        out: list[str] = []
        for event_type, data in self.posts:
            if event_type == "external_session_status":
                status = data.get("status")
                out.append(status if isinstance(status, str) else "<none>")
        return out

    def message_roles(self) -> list[str]:
        """Return the ``role`` of every committed ``message`` item, in order."""
        out: list[str] = []
        for event_type, data in self.posts:
            if event_type == "external_conversation_item" and data.get("item_type") == "message":
                item_data = data.get("item_data")
                role = item_data.get("role") if isinstance(item_data, dict) else None
                out.append(role if isinstance(role, str) else "<none>")
        return out

    def deltas(self) -> list[dict[str, object]]:
        """Return the ``data`` payload of every ``external_output_text_delta``."""
        return [
            data for event_type, data in self.posts if event_type == "external_output_text_delta"
        ]

    def reasonings(self) -> list[dict[str, object]]:
        """Return the ``data`` payload of every ``external_output_reasoning_delta``."""
        return [
            data
            for event_type, data in self.posts
            if event_type == "external_output_reasoning_delta"
        ]

    def event_types(self) -> list[str]:
        """Return the ``type`` of every posted event, in order."""
        return [event_type for event_type, _data in self.posts]


class _StepScript:
    """A scripted ``get_trajectory_steps`` returning one snapshot per call.

    The final snapshot repeats once exhausted so re-reads (a steady-state poll
    that returns the same finished list) can be asserted to post nothing.
    """

    def __init__(self, snapshots: list[list[dict[str, Any]]]) -> None:
        self._snapshots = snapshots
        self.calls = 0

    def __call__(self, port: int, cascade_id: str) -> list[dict[str, object]]:
        self.calls += 1
        idx = min(self.calls - 1, len(self._snapshots) - 1)
        # Return a deep-ish copy so the reader cannot mutate the script.
        return [dict(step) for step in self._snapshots[idx]]


class _RaisingThenOk:
    """``get_trajectory_steps`` that raises on the first call, then succeeds."""

    def __init__(self, exc: Exception, snapshot: list[dict[str, Any]]) -> None:
        self._exc = exc
        self._snapshot = snapshot
        self.calls = 0

    def __call__(self, port: int, cascade_id: str) -> list[dict[str, object]]:
        self.calls += 1
        if self.calls == 1:
            raise self._exc
        return [dict(step) for step in self._snapshot]


# ---------------------------------------------------------------------------
# Stream-mode scaffolding (Task T-D)
# ---------------------------------------------------------------------------


def _frame(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a step list in a ``StreamAgentStateUpdates`` update frame.

    Mirrors the live shape ``update.mainTrajectoryUpdate.stepsUpdate.steps[]``
    (design §10.2) that :func:`stream_agent_state_updates` yields per frame.
    """
    return {"mainTrajectoryUpdate": {"stepsUpdate": {"steps": copy.deepcopy(steps)}}}


def _generating_planner(text: str, *, step_index: int = 2) -> dict[str, Any]:
    """A PLANNER_RESPONSE step mid-generation (status GENERATING).

    Built from the committed ``planner_response_text`` fixture but with the
    partial-text contract verified live (design §10.2): ``modifiedResponse``
    holds the growing partial, ``response`` is ABSENT during generation, and
    ``status == CORTEX_STEP_STATUS_GENERATING``.
    """
    step = copy.deepcopy(_load("planner_response_text"))
    step["status"] = "CORTEX_STEP_STATUS_GENERATING"
    planner = cast(dict[str, Any], step["plannerResponse"])
    planner.pop("response", None)
    planner["modifiedResponse"] = text
    cast(dict[str, Any], step["metadata"])["sourceTrajectoryStepInfo"]["stepIndex"] = step_index
    return step


def _generating_planner_with_thinking(
    *, thinking: str, text: str = "", step_index: int = 2
) -> dict[str, Any]:
    """A GENERATING PLANNER_RESPONSE carrying a growing ``thinking`` block.

    Gemini Thinking-model variants stream chain-of-thought at
    ``plannerResponse.thinking`` (design §10.2) alongside the growing
    ``modifiedResponse``. Built from :func:`_generating_planner` so the partial
    text contract (``response`` absent, status GENERATING) is preserved; the
    ``thinking`` field is added to model the reasoning stream.
    """
    step = _generating_planner(text, step_index=step_index)
    cast(dict[str, Any], step["plannerResponse"])["thinking"] = thinking
    return step


def _done_planner(text: str, *, step_index: int = 2) -> dict[str, Any]:
    """A DONE PLANNER_RESPONSE step whose committed text is ``text``.

    On DONE both ``response`` and ``modifiedResponse`` are present and equal
    (design §10.2); the mapper emits one committed ``message`` from it.
    """
    step = copy.deepcopy(_load("planner_response_text"))
    step["status"] = "CORTEX_STEP_STATUS_DONE"
    planner = cast(dict[str, Any], step["plannerResponse"])
    planner["response"] = text
    planner["modifiedResponse"] = text
    cast(dict[str, Any], step["metadata"])["sourceTrajectoryStepInfo"]["stepIndex"] = step_index
    return step


def _running_run_command() -> dict[str, Any]:
    """A RUN_COMMAND step still executing (status RUNNING; no output yet).

    Built from the DONE fixture but rolled back to RUNNING with its output
    stripped — the pre-DONE shape the stream surfaces before the command
    completes. The mapper emits nothing for it (output only at DONE).
    """
    step = copy.deepcopy(_load("run_command_done"))
    step["status"] = "CORTEX_STEP_STATUS_RUNNING"
    run_command = cast(dict[str, Any], step["runCommand"])
    run_command.pop("combinedOutput", None)
    return step


class _FrameScript:
    """A scripted ``stream_agent_state_updates`` async generator.

    Yields one pre-built update frame per scripted entry, then ends cleanly (a
    real stream long-polls; the test ends the turn by exhausting the script).
    Records ``calls`` so a test can assert the stream was (re)entered.
    """

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self._frames = frames
        self.calls = 0

    def __call__(self, port: int, conversation_id: str) -> AsyncIterator[dict[str, object]]:
        self.calls += 1

        async def _gen() -> AsyncIterator[dict[str, object]]:
            for frame in self._frames:
                yield copy.deepcopy(frame)

        return _gen()


class _RaisingStream:
    """A ``stream_agent_state_updates`` that raises before yielding any frame."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    def __call__(self, port: int, conversation_id: str) -> AsyncIterator[dict[str, object]]:
        self.calls += 1

        async def _gen() -> AsyncIterator[dict[str, object]]:
            raise self._exc
            yield {}  # pragma: no cover  (unreachable; marks this an async gen)

        return _gen()


async def _run_stream(
    *,
    bridge_dir: Path,
    sink: _PostSink,
    stream: object,
    poll_steps: object,
    monkeypatch: pytest.MonkeyPatch,
    iterations: int,
    on_pending: object | None = None,
) -> None:
    """Drive ``supervise_reader`` in STREAM mode for a bounded run.

    Injects both the scripted ``stream_agent_state_updates`` (primary) and a
    ``get_trajectory_steps`` (poll fallback). ``stop`` bounds the poll loop so a
    fallback path still terminates; the stream script ends on its own.
    """
    monkeypatch.setattr(reader, "stream_agent_state_updates", stream)
    monkeypatch.setattr(reader, "get_trajectory_steps", poll_steps)
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    async def _noop_sleep(_seconds: float) -> None:
        # Yield to the event loop (no real delay) so the off-loop interaction
        # bridge tasks the reader spawns get a turn to run in the bounded loop.
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    async def _default_pending(_cascade_id: str, _port: int, _pending: PendingInteraction) -> None:
        return None

    callback = on_pending if on_pending is not None else _default_pending
    await reader.supervise_reader(
        bridge_dir,
        _SESSION_ID,
        client=cast(httpx.AsyncClient, object()),
        on_pending_interaction=cast(Any, callback),
        poll_interval_s=0.0,
        stop=_stop_after(iterations),
    )


def _stop_after(n: int) -> _StopAfter:
    """Build a stop predicate that returns True once it has been polled ``n`` times."""
    return _StopAfter(n)


class _StopAfter:
    """Stop the reader loop after a bounded number of poll iterations."""

    def __init__(self, n: int) -> None:
        self._remaining = n

    def __call__(self) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False


@pytest.fixture
def patched_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make port + cascade-id discovery resolve immediately (no OS/network)."""
    monkeypatch.setattr(reader, "_candidate_agy_rpc_ports", lambda: [_PORT])
    monkeypatch.setattr(reader, "_conversation_matches", lambda port, cid: port == _PORT)


@pytest.fixture(autouse=True)
def no_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the Task T-G rotation detector to "no rotation" for every test.

    ``supervise_reader`` always spawns the ``GetAllCascadeTrajectories`` rotation
    detector; by default it must hit no real socket and report no rotation, so the
    existing reader tests exercise only the stream/poll body. A rotation-specific
    test overrides ``reader.get_all_cascade_trajectories`` itself.
    """
    monkeypatch.setattr(reader, "get_all_cascade_trajectories", lambda port: {})


def _bridge_dir(tmp_path: Path) -> Path:
    """A bridge dir whose state.json names the real (non-placeholder) cascade id."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "state.json").write_text(
        json.dumps({"session_id": _SESSION_ID, "conversation_id": _CASCADE_ID}),
        encoding="utf-8",
    )
    return bridge_dir


async def _run(
    *,
    bridge_dir: Path,
    sink: _PostSink,
    steps: object,
    monkeypatch: pytest.MonkeyPatch,
    iterations: int,
    on_pending: object | None = None,
) -> None:
    """Drive ``supervise_reader`` for a bounded number of poll iterations.

    The reader is stream-primary (Task T-D), so to exercise the POLL path these
    tests inject a ``stream_agent_state_updates`` that fails immediately —
    forcing the documented graceful fallback to the (committed-only) poll loop.
    """
    monkeypatch.setattr(
        reader,
        "stream_agent_state_updates",
        _RaisingStream(httpx.ConnectError("stream disabled for poll test")),
    )
    monkeypatch.setattr(reader, "get_trajectory_steps", steps)
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    async def _noop_sleep(_seconds: float) -> None:
        # Yield to the event loop (no real delay) so the off-loop interaction
        # bridge tasks the reader spawns get a turn to run in the bounded loop.
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    async def _default_pending(_cascade_id: str, _port: int, _pending: PendingInteraction) -> None:
        return None

    callback = on_pending if on_pending is not None else _default_pending
    await reader.supervise_reader(
        bridge_dir,
        _SESSION_ID,
        client=cast(httpx.AsyncClient, object()),
        on_pending_interaction=cast(Any, callback),
        poll_interval_s=0.0,
        stop=_stop_after(iterations),
    )


# ---------------------------------------------------------------------------
# Dedup: each new step posts exactly once; re-reads post nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_new_step_posts_once_and_rereads_dedup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A planner text step posts one assistant message; re-reads post nothing."""
    planner = _load("planner_response_text")
    # Three polls all return the SAME one-step snapshot (a steady finished list).
    script = _StepScript([[planner], [planner], [planner]])
    sink = _PostSink()

    await _run(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        steps=script,
        monkeypatch=monkeypatch,
        iterations=3,
    )

    # Exactly one assistant message, despite three reads of the same step.
    assert sink.item_types() == ["message"]


@pytest.mark.asyncio
async def test_incremental_steps_each_post_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """Steps appearing across polls each post exactly once (no re-post)."""
    text = _load("planner_response_text")
    tool_call = _load("planner_response_tool_call_run_command")
    result = _load("run_command_done")
    # Snapshot grows by one step each poll, then holds steady.
    script = _StepScript(
        [
            [text],
            [text, tool_call],
            [text, tool_call, result],
            [text, tool_call, result],
        ]
    )
    sink = _PostSink()

    await _run(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        steps=script,
        monkeypatch=monkeypatch,
        iterations=4,
    )

    # message (text) + function_call (tool call) + function_call_output (result),
    # each exactly once across the growing snapshots.
    assert sink.item_types() == ["message", "function_call", "function_call_output"]


@pytest.mark.asyncio
async def test_poll_planner_generating_then_done_posts_one_final_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """POLL path: a planner caught GENERATING then DONE posts ONE final message.

    Regression for the double-render the rework prevents. The poll loop does NOT
    intercept GENERATING (only the stream path emits deltas), and the mapper now
    gates the committed planner message on DONE. So a poll that sees the planner
    GENERATING ("Hi") then DONE ("Hi there") must post exactly one ``message``
    whose text is the FINAL "Hi there" — not "Hi", and not two messages.
    """
    script = _StepScript(
        [
            [_generating_planner("Hi")],
            [_done_planner("Hi there")],
            [_done_planner("Hi there")],
        ]
    )
    sink = _PostSink()

    await _run(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        steps=script,
        monkeypatch=monkeypatch,
        iterations=3,
    )

    # Exactly one committed message (no GENERATING message, no double-post).
    assert sink.item_types() == ["message"]
    # And it carries the FINAL text.
    messages = [
        data for event_type, data in sink.posts if event_type == "external_conversation_item"
    ]
    item_data = cast(dict[str, Any], messages[0]["item_data"])
    content = cast(list[dict[str, Any]], item_data["content"])
    assert content[0]["text"] == "Hi there"
    # The poll path emits no deltas.
    assert sink.deltas() == []


# ---------------------------------------------------------------------------
# USER_INPUT commits the user message exactly once (#1155)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_input_commits_user_message_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A USER_INPUT step posts one user ``message`` item, deduped across reads.

    Regression guard for #1155: the user turn must be committed (so the web UI
    reconciles its optimistic bubble) and committed only ONCE — the reader
    dedups the step by its per-turn ``executionId`` across repeated polls.
    """
    user = _load("user_input")
    script = _StepScript([[user], [user]])
    sink = _PostSink()

    await _run(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        steps=script,
        monkeypatch=monkeypatch,
        iterations=2,
    )

    assert sink.item_types() == ["message"]


# ---------------------------------------------------------------------------
# WAITING step → on_pending_interaction invoked exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waiting_step_invokes_callback_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A WAITING step hands its pending interaction to the callback exactly once.

    The callback also receives the SAME cascade id + port the reader discovered,
    so the interaction bridge it drives targets agy's live conversation without
    re-discovering (and risking a recycled/foreign port).
    """
    waiting = _load("ask_question_waiting")
    script = _StepScript([[waiting], [waiting], [waiting]])
    sink = _PostSink()
    captured: list[tuple[str, int, PendingInteraction]] = []

    async def _on_pending(cascade_id: str, port: int, pending: PendingInteraction) -> None:
        captured.append((cascade_id, port, pending))

    await _run(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        steps=script,
        monkeypatch=monkeypatch,
        iterations=3,
        on_pending=_on_pending,
    )

    # Despite three reads of the same WAITING step, the bridge is called once.
    assert len(captured) == 1
    cascade_id, port, pending = captured[0]
    # The callback is handed the SAME cascade id + port the reader bound to.
    assert cascade_id == _CASCADE_ID
    assert port == _PORT
    assert pending["kind"] == "ask_question"
    assert pending["trajectory_id"] == _CASCADE_ID


# ---------------------------------------------------------------------------
# Interaction bridge runs OFF the reader loop (no streaming starvation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_continues_while_interaction_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A pending (blocking) interaction must NOT freeze mirroring of later steps.

    Regression for the inline-await starvation (gemini repro): with the bridge run
    off the reader loop, a DONE planner that follows a WAITING step in the same
    snapshot is still mirrored while the human is still answering.
    """
    waiting = _load("ask_question_waiting")
    planner = _done_planner("Answer arrives later", step_index=99)
    script = _StepScript([[waiting, planner], [waiting, planner]])
    sink = _PostSink()

    async def _block(_cascade_id: str, _port: int, _pending: PendingInteraction) -> None:
        await asyncio.Event().wait()  # never completes — simulates a human holding

    await asyncio.wait_for(
        _run(
            bridge_dir=_bridge_dir(tmp_path),
            sink=sink,
            steps=script,
            monkeypatch=monkeypatch,
            iterations=2,
            on_pending=_block,
        ),
        timeout=5.0,
    )

    # The planner message was mirrored despite the interaction still pending.
    assert "message" in sink.item_types()


@pytest.mark.asyncio
async def test_single_in_flight_guard_skips_second_interaction() -> None:
    """While one interaction is handled off-loop, a second (e.g. agy's higher-index
    WAITING retry) is NOT fired again — the in-flight bridge owns the retries."""
    state = reader._ReaderState(
        allocator=reader._ToolCallIdAllocator(conversation_id=_CASCADE_ID),
        seen=set(),
        interacted=set(),
        port=_PORT,
    )
    waiting = _load("ask_question_waiting")
    fired: list[int] = []

    async def _block(_cascade_id: str, _port: int, pending: PendingInteraction) -> None:
        fired.append(pending["step_index"])
        await asyncio.Event().wait()

    reader._maybe_handle_interaction(
        waiting,
        key=("traj", 0),
        cascade_id=_CASCADE_ID,
        state=state,
        on_pending_interaction=cast(Any, _block),
    )
    reader._maybe_handle_interaction(
        waiting,
        key=("traj", 1),
        cascade_id=_CASCADE_ID,
        state=state,
        on_pending_interaction=cast(Any, _block),
    )
    await asyncio.sleep(0)  # let the spawned task start

    assert len(fired) == 1  # the guard suppressed the second despite a distinct key

    task = state.interaction_task
    assert task is not None
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_interaction_done_callback_clears_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """When an interaction task completes, the slot clears so a later distinct
    interaction can fire."""
    # The clear now ALSO re-scans the freshest steps (#1472); keep that a no-op here
    # so this test stays focused on slot-clearing + the manual re-fire.
    monkeypatch.setattr(reader, "get_trajectory_steps", lambda _port, _cascade_id: [])
    state = reader._ReaderState(
        allocator=reader._ToolCallIdAllocator(conversation_id=_CASCADE_ID),
        seen=set(),
        interacted=set(),
        port=_PORT,
    )
    waiting = _load("ask_question_waiting")
    fired: list[int] = []

    async def _quick(_cascade_id: str, _port: int, pending: PendingInteraction) -> None:
        fired.append(pending["step_index"])

    reader._maybe_handle_interaction(
        waiting,
        key=("traj", 0),
        cascade_id=_CASCADE_ID,
        state=state,
        on_pending_interaction=cast(Any, _quick),
    )
    first = state.interaction_task
    assert first is not None
    await first
    await asyncio.sleep(0)  # let the done-callback run
    assert state.interaction_task is None  # slot cleared

    reader._maybe_handle_interaction(
        waiting,
        key=("traj", 1),
        cascade_id=_CASCADE_ID,
        state=state,
        on_pending_interaction=cast(Any, _quick),
    )
    second = state.interaction_task
    assert second is not None
    await second
    assert len(fired) == 2  # the slot cleared, so the second interaction fired
    await _drain_interactions(state)  # settle the no-op re-scans both clears scheduled


async def _drain_interactions(state: reader._ReaderState) -> None:
    """Await the interaction bridge + any chained re-scan tasks to quiescence.

    A cleared bridge schedules a re-scan (#1472) that may spawn the next bridge,
    whose clear re-scans again; this awaits each link (letting done-callbacks run
    between rounds) so a test ends with no pending interaction tasks. Bounded so a
    bug that keeps respawning surfaces as a test hang in CI rather than an infinite
    loop here.
    """
    for _ in range(10):
        inflight = [
            task
            for task in (state.interaction_task, *state.interaction_rescans)
            if task is not None and not task.done()
        ]
        if not inflight:
            return
        await asyncio.gather(*inflight, return_exceptions=True)
        await asyncio.sleep(0)  # let each done-callback schedule the next link


@pytest.mark.asyncio
async def test_clear_slot_resurfaces_deferred_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bridge clearing re-scans and surfaces a gate the guard DEFERRED (#1472).

    Models a chained ``a && b`` command where each segment is permission-gated: the
    first gate surfaces and is answered; the second arrives while the first bridge
    is still finishing and is skipped by the single-in-flight guard. On the stream
    path no further frame carries it, so the clear's re-scan is what surfaces it.
    """
    state = reader._ReaderState(
        allocator=reader._ToolCallIdAllocator(conversation_id=_CASCADE_ID),
        seen=set(),
        interacted=set(),
        port=_PORT,
    )
    gate1 = _load("run_command_waiting")  # stepIndex 6 (the `pwd` segment)
    gate2 = copy.deepcopy(gate1)  # a distinct later gate (the `ls` segment)
    gate2["metadata"]["sourceTrajectoryStepInfo"]["stepIndex"] = 8
    gate2["runCommand"]["commandLine"] = "ls"
    # The freshest snapshot the re-scan re-reads: gate1 is still present (already
    # surfaced → must NOT re-fire) and gate2 is the newly-arrived, deferred gate.
    monkeypatch.setattr(reader, "get_trajectory_steps", lambda _port, _cascade_id: [gate1, gate2])

    fired: list[int] = []

    async def _quick(_cascade_id: str, _port: int, pending: PendingInteraction) -> None:
        fired.append(pending["step_index"])

    reader._maybe_handle_interaction(
        gate1,
        key=reader._step_key(gate1),
        cascade_id=_CASCADE_ID,
        state=state,
        on_pending_interaction=cast(Any, _quick),
    )
    first = state.interaction_task
    assert first is not None
    await first
    await asyncio.sleep(0)  # let _clear_slot run → schedule the re-scan
    await _drain_interactions(state)  # re-scan surfaces gate2 → its bridge fires

    assert fired == [6, 8]  # gate1 directly; gate2 via the clear's re-scan
    assert reader._step_key(gate1) in state.interacted  # gate1 was not re-surfaced
    assert reader._step_key(gate2) in state.interacted


@pytest.mark.asyncio
async def test_resurface_pending_interaction_swallows_poll_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-scan whose steps re-read fails is logged and swallowed, not raised."""

    def _boom(_port: int, _cascade_id: str) -> list[dict[str, Any]]:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(reader, "get_trajectory_steps", _boom)
    state = reader._ReaderState(
        allocator=reader._ToolCallIdAllocator(conversation_id=_CASCADE_ID),
        seen=set(),
        interacted=set(),
        port=_PORT,
    )
    fired: list[int] = []

    async def _quick(_cascade_id: str, _port: int, pending: PendingInteraction) -> None:
        fired.append(pending["step_index"])

    # Must not raise despite the read failing.
    await reader._resurface_pending_interaction(
        cascade_id=_CASCADE_ID,
        state=state,
        on_pending_interaction=cast(Any, _quick),
    )

    assert fired == []  # no gate surfaced
    assert state.interaction_task is None  # no bridge spawned


@pytest.mark.asyncio
async def test_reader_teardown_cancels_in_flight_interaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A stopped reader cancels an interaction bridge still awaiting a verdict."""
    waiting = _load("ask_question_waiting")
    script = _StepScript([[waiting], [waiting]])
    sink = _PostSink()
    tasks: list[asyncio.Task[object]] = []

    async def _block(_cascade_id: str, _port: int, _pending: PendingInteraction) -> None:
        current = asyncio.current_task()
        assert current is not None
        tasks.append(current)
        await asyncio.Event().wait()

    await asyncio.wait_for(
        _run(
            bridge_dir=_bridge_dir(tmp_path),
            sink=sink,
            steps=script,
            monkeypatch=monkeypatch,
            iterations=2,
            on_pending=_block,
        ),
        timeout=5.0,
    )

    assert len(tasks) == 1  # the bridge started
    assert tasks[0].cancelled()  # reader teardown cancelled it


# ---------------------------------------------------------------------------
# #1200 direction 2: withdraw a surfaced elicitation resolved out-of-band
# ---------------------------------------------------------------------------


def _capturing_client() -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    """Build a real AsyncClient over a MockTransport that records every POST body.

    ``_post_external_elicitation_resolved`` calls ``client.post`` directly (not
    the post-retry sink), so a withdraw test needs a usable client. The transport
    captures the JSON body of each POST and answers 200 so the reader proceeds.
    """
    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.content:
            captured.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(_handler))
    return client, captured


def _permission_waiting() -> dict[str, Any]:
    """A WAITING command-permission step (the #1200 fixture)."""
    return _load("run_command_waiting")


def _permission_done() -> dict[str, Any]:
    """The same permission step advanced to DONE (answered/timed out → no WAITING).

    Built from the WAITING fixture by flipping the status and dropping the
    ``requestedInteraction`` block, so ``pending_interaction`` returns ``None`` —
    exactly what the reader sees once the step leaves WAITING.
    """
    step = copy.deepcopy(_load("run_command_waiting"))
    step["status"] = "CORTEX_STEP_STATUS_DONE"
    step.pop("requestedInteraction", None)
    return step


@pytest.mark.asyncio
async def test_step_leaving_waiting_withdraws_surfaced_elicitation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A surfaced WAITING step that later is NOT WAITING → withdraw the web card.

    Models the terminal-answered / timed-out case: the reader surfaces the
    permission elicitation, then on a later poll the step is DONE (no
    ``requestedInteraction``). The reader must POST exactly one
    ``external_elicitation_resolved`` for that step's deterministic elicitation id
    so the lingering web card clears (#1200, direction 2).
    """
    from omnigent.antigravity_native_interactions import agy_elicitation_id

    waiting = _permission_waiting()
    done = _permission_done()
    # WAITING twice (surface + dedup), then DONE (withdraw), then steady DONE.
    script = _StepScript([[waiting], [waiting], [done], [done]])
    sink = _PostSink()
    client, captured = _capturing_client()

    monkeypatch.setattr(
        reader,
        "stream_agent_state_updates",
        _RaisingStream(httpx.ConnectError("stream disabled for poll test")),
    )
    monkeypatch.setattr(reader, "get_trajectory_steps", script)
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    async def _noop_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    async def _on_pending(_cascade_id: str, _port: int, _pending: PendingInteraction) -> None:
        # Simulate a long-poll await that the terminal answer / timeout will
        # short-circuit — it never returns a verdict here.
        await asyncio.Event().wait()

    async with client:
        await asyncio.wait_for(
            reader.supervise_reader(
                _bridge_dir(tmp_path),
                _SESSION_ID,
                client=client,
                on_pending_interaction=cast(Any, _on_pending),
                poll_interval_s=0.0,
                stop=_stop_after(4),
            ),
            timeout=5.0,
        )

    # Exactly one withdraw was posted, for THIS step's deterministic id.
    expected_traj = waiting["metadata"]["sourceTrajectoryStepInfo"]["trajectoryId"]
    expected_idx = waiting["metadata"]["sourceTrajectoryStepInfo"]["stepIndex"]
    expected_id = agy_elicitation_id(_CASCADE_ID, expected_traj, expected_idx)
    withdraws = [body for body in captured if body.get("type") == "external_elicitation_resolved"]
    assert len(withdraws) == 1
    assert withdraws[0]["data"]["elicitation_id"] == expected_id


@pytest.mark.asyncio
async def test_still_waiting_does_not_withdraw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A still-WAITING step is NOT withdrawn (the interaction is still live)."""
    waiting = _permission_waiting()
    script = _StepScript([[waiting], [waiting], [waiting]])
    sink = _PostSink()
    client, captured = _capturing_client()

    monkeypatch.setattr(
        reader,
        "stream_agent_state_updates",
        _RaisingStream(httpx.ConnectError("stream disabled for poll test")),
    )
    monkeypatch.setattr(reader, "get_trajectory_steps", script)
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    async def _noop_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    async def _on_pending(_cascade_id: str, _port: int, _pending: PendingInteraction) -> None:
        await asyncio.Event().wait()

    async with client:
        await asyncio.wait_for(
            reader.supervise_reader(
                _bridge_dir(tmp_path),
                _SESSION_ID,
                client=client,
                on_pending_interaction=cast(Any, _on_pending),
                poll_interval_s=0.0,
                stop=_stop_after(3),
            ),
            timeout=5.0,
        )

    withdraws = [body for body in captured if body.get("type") == "external_elicitation_resolved"]
    assert withdraws == []


@pytest.mark.asyncio
async def test_withdraw_posts_at_most_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """Even across many DONE re-reads, the withdraw is posted exactly once.

    The dedup lives in ``surfaced_elicitations`` (popped on first withdraw), so a
    steady-state poll that keeps returning the DONE step must not re-post.
    """
    waiting = _permission_waiting()
    done = _permission_done()
    script = _StepScript([[waiting], [done], [done], [done], [done]])
    sink = _PostSink()
    client, captured = _capturing_client()

    monkeypatch.setattr(
        reader,
        "stream_agent_state_updates",
        _RaisingStream(httpx.ConnectError("stream disabled for poll test")),
    )
    monkeypatch.setattr(reader, "get_trajectory_steps", script)
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    async def _noop_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    async def _on_pending(_cascade_id: str, _port: int, _pending: PendingInteraction) -> None:
        await asyncio.Event().wait()

    async with client:
        await asyncio.wait_for(
            reader.supervise_reader(
                _bridge_dir(tmp_path),
                _SESSION_ID,
                client=client,
                on_pending_interaction=cast(Any, _on_pending),
                poll_interval_s=0.0,
                stop=_stop_after(5),
            ),
            timeout=5.0,
        )

    withdraws = [body for body in captured if body.get("type") == "external_elicitation_resolved"]
    assert len(withdraws) == 1


@pytest.mark.asyncio
async def test_withdraw_helper_pops_and_posts_once_directly() -> None:
    """Unit-level: ``_maybe_withdraw_interaction`` posts once then no-ops.

    Drives the helper directly with a surfaced id so the pop/no-double-post
    contract is asserted without the full supervise loop.
    """
    from omnigent.antigravity_native_interactions import agy_elicitation_id

    waiting = _permission_waiting()
    done = _permission_done()
    key = reader._step_key(waiting)
    eid = agy_elicitation_id(
        _CASCADE_ID,
        waiting["metadata"]["sourceTrajectoryStepInfo"]["trajectoryId"],
        waiting["metadata"]["sourceTrajectoryStepInfo"]["stepIndex"],
    )
    state = reader._ReaderState(
        allocator=reader._ToolCallIdAllocator(conversation_id=_CASCADE_ID),
        seen=set(),
        interacted=set(),
        port=_PORT,
    )
    state.surfaced_elicitations[key] = eid
    client, captured = _capturing_client()

    async with client:
        # First call on a DONE step → posts the withdraw and pops the entry.
        await reader._maybe_withdraw_interaction(
            done, key=key, client=client, session_id=_SESSION_ID, state=state
        )
        # Second call → entry gone, no further post.
        await reader._maybe_withdraw_interaction(
            done, key=key, client=client, session_id=_SESSION_ID, state=state
        )

    assert key not in state.surfaced_elicitations
    withdraws = [b for b in captured if b.get("type") == "external_elicitation_resolved"]
    assert len(withdraws) == 1
    assert withdraws[0]["data"]["elicitation_id"] == eid


@pytest.mark.asyncio
async def test_withdraw_helper_noop_while_still_waiting() -> None:
    """``_maybe_withdraw_interaction`` does nothing while the step is still WAITING."""
    from omnigent.antigravity_native_interactions import agy_elicitation_id

    waiting = _permission_waiting()
    key = reader._step_key(waiting)
    eid = agy_elicitation_id(
        _CASCADE_ID,
        waiting["metadata"]["sourceTrajectoryStepInfo"]["trajectoryId"],
        waiting["metadata"]["sourceTrajectoryStepInfo"]["stepIndex"],
    )
    state = reader._ReaderState(
        allocator=reader._ToolCallIdAllocator(conversation_id=_CASCADE_ID),
        seen=set(),
        interacted=set(),
        port=_PORT,
    )
    state.surfaced_elicitations[key] = eid
    client, captured = _capturing_client()

    async with client:
        await reader._maybe_withdraw_interaction(
            waiting, key=key, client=client, session_id=_SESSION_ID, state=state
        )

    # Still WAITING → entry retained, nothing posted.
    assert state.surfaced_elicitations.get(key) == eid
    assert captured == []


# ---------------------------------------------------------------------------
# Status edges: RUNNING on user turn, IDLE on assistant-text close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_running_then_idle_on_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """USER_INPUT emits RUNNING; a closing assistant-text step emits IDLE; once each."""
    user = _load("user_input")
    text = _load("planner_response_text")
    script = _StepScript(
        [
            [user],
            [user, text],
            [user, text],
        ]
    )
    sink = _PostSink()

    await _run(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        steps=script,
        monkeypatch=monkeypatch,
        iterations=3,
    )

    # RUNNING (user turn) then IDLE (assistant answered, no tool calls), deduped.
    assert sink.statuses() == ["running", "idle"]


@pytest.mark.asyncio
async def test_status_not_idle_while_tools_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A planner step that only invokes a tool does not close the turn (no IDLE)."""
    user = _load("user_input")
    tool_call = _load("planner_response_tool_call_run_command")
    script = _StepScript([[user], [user, tool_call], [user, tool_call]])
    sink = _PostSink()

    await _run(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        steps=script,
        monkeypatch=monkeypatch,
        iterations=3,
    )

    # Turn opened (RUNNING) but never closed: the planner step has tool calls.
    assert sink.statuses() == ["running"]


@pytest.mark.asyncio
async def test_status_failed_on_error_planner_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A turn that ends in a terminal-ERROR planner closes as FAILED (not idle).

    A model/turn ERROR must surface as a failed turn — not a clean idle that
    looks identical to a normal empty reply (#6). The turn still closes (the
    spinner clears; ``turn_active`` resets so the next turn can re-open RUNNING),
    but on the ``failed`` status edge, and the mapper commits a visible error
    message item.
    """
    user = _load("user_input")
    error_planner = _load("planner_response_text")
    error_planner["status"] = "CORTEX_STEP_STATUS_ERROR"
    # No closing text — prove the close is from the ERROR-terminal rule.
    error_planner["plannerResponse"] = {}
    script = _StepScript([[user], [user, error_planner], [user, error_planner]])
    sink = _PostSink()

    await _run(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        steps=script,
        monkeypatch=monkeypatch,
        iterations=3,
    )

    # Closes as FAILED, not idle.
    assert sink.statuses() == ["running", "failed"]
    # The user message + a visible error item are committed (no silent empty reply).
    assert sink.message_roles() == ["user", "assistant"]


# ---------------------------------------------------------------------------
# Error handling: a transient RPC failure does not crash the loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_does_not_crash_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """An ``httpx.HTTPError`` on one poll is swallowed; the next poll recovers.

    The reader is stream-primary, so ``_run`` injects a failing stream first
    (consuming one ``stop`` tick on entry); the poll loop then needs two of its
    own iterations to exercise the raise-then-recover ``get_trajectory_steps``,
    hence ``iterations=3``.
    """
    text = _load("planner_response_text")
    steps = _RaisingThenOk(httpx.ConnectError("boom"), [text])
    sink = _PostSink()

    await _run(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        steps=steps,
        monkeypatch=monkeypatch,
        iterations=3,
    )

    # First poll raised; second poll delivered the message — loop survived.
    assert steps.calls == 2
    assert sink.item_types() == ["message"]


@pytest.mark.asyncio
async def test_value_error_does_not_crash_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A non-JSON 200 (``ValueError``) is swallowed too; the loop keeps polling.

    ``iterations=3`` for the same reason as the HTTP-error case: one tick is
    spent on the stream attempt before the poll loop runs its two iterations.
    """
    text = _load("planner_response_text")
    steps = _RaisingThenOk(ValueError("not json"), [text])
    sink = _PostSink()

    await _run(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        steps=steps,
        monkeypatch=monkeypatch,
        iterations=3,
    )

    assert steps.calls == 2
    assert sink.item_types() == ["message"]


# ---------------------------------------------------------------------------
# Discovery: a placeholder cascade id is treated as "not ready"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_placeholder_conversation_id_waits_for_real_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """The reader polls past an ``agy_conv_*`` placeholder until the real id appears."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    state_path = bridge_dir / "state.json"
    state_path.write_text(
        json.dumps({"session_id": _SESSION_ID, "conversation_id": "agy_conv_placeholder"}),
        encoding="utf-8",
    )

    text = _load("planner_response_text")
    script = _StepScript([[text], [text]])
    sink = _PostSink()

    # Stream-primary reader: force the (committed-only) poll fallback so this test
    # exercises poll-path discovery rather than a real stream connection.
    monkeypatch.setattr(
        reader,
        "stream_agent_state_updates",
        _RaisingStream(httpx.ConnectError("stream disabled for poll test")),
    )
    monkeypatch.setattr(reader, "get_trajectory_steps", script)
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    flip_calls = {"n": 0}

    def _read_then_flip(bd: Path) -> object:
        # Promote the placeholder to the real id after the first resolution poll
        # so the reader is forced to wait for a real id before discovering.
        flip_calls["n"] += 1
        if flip_calls["n"] >= 2:
            state_path.write_text(
                json.dumps({"session_id": _SESSION_ID, "conversation_id": _CASCADE_ID}),
                encoding="utf-8",
            )
        return read_bridge_state(bd)

    monkeypatch.setattr(reader, "read_bridge_state", _read_then_flip)

    async def _noop_sleep(_seconds: float) -> None:
        # Yield to the event loop (no real delay) so the off-loop interaction
        # bridge tasks the reader spawns get a turn to run in the bounded loop.
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    async def _on_pending(_cascade_id: str, _port: int, _pending: PendingInteraction) -> None:
        return None

    await reader.supervise_reader(
        bridge_dir,
        _SESSION_ID,
        client=cast(httpx.AsyncClient, object()),
        on_pending_interaction=cast(Any, _on_pending),
        poll_interval_s=0.0,
        # Budget covers: one discovery retry past the placeholder, the stream
        # attempt (which fails), then the poll-fallback iteration that mirrors.
        stop=_stop_after(4),
    )

    # The placeholder forced at least two cascade-id resolution passes, then the
    # reader bound the real id and mirrored the step.
    assert flip_calls["n"] >= 2
    assert sink.item_types() == ["message"]


# ---------------------------------------------------------------------------
# Stream mode: incremental deltas during GENERATING, one committed message DONE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_generating_emits_incremental_deltas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """Growing ``modifiedResponse`` while GENERATING emits non-overlapping deltas.

    The deltas concatenate to the full partial text and share one stable
    ``message_id`` for the step (so the SPA coalesces them into one live block).
    """
    full = "Hello! I am Antigravity, your AI coding assistant, ready to help."
    cut1, cut2 = 6, 30  # "Hello!" then "Hello! I am Antigravity, your "
    frames = [
        _frame([_generating_planner(full[:cut1])]),
        _frame([_generating_planner(full[:cut2])]),
        _frame([_generating_planner(full)]),
    ]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    deltas = sink.deltas()
    # Three growing frames → three non-empty deltas.
    assert [d["delta"] for d in deltas] == [full[:cut1], full[cut1:cut2], full[cut2:]]
    # Suffixes concatenate exactly to the full text (no overlap, no gap).
    assert "".join(cast(str, d["delta"]) for d in deltas) == full
    # One stable per-step message_id; deltas are not final (committed item follows).
    message_ids = {d["message_id"] for d in deltas}
    assert message_ids == {f"antigravity:{_CASCADE_ID}:2:planner"}
    assert all(d["final"] is False for d in deltas)
    # No committed message yet — the step never reached DONE in this script.
    assert sink.item_types() == []


@pytest.mark.asyncio
async def test_stream_done_emits_one_committed_message_after_deltas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """GENERATING deltas precede exactly ONE committed message on DONE."""
    full = "Hello there, friend."
    frames = [
        _frame([_generating_planner("Hello")]),
        _frame([_generating_planner(full)]),
        _frame([_done_planner(full)]),
    ]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    # Exactly one committed assistant message.
    assert sink.item_types() == ["message"]
    # Delta-first ordering: every delta is posted BEFORE the committed item.
    types = sink.event_types()
    committed_idx = types.index("external_conversation_item")
    delta_idxs = [i for i, t in enumerate(types) if t == "external_output_text_delta"]
    assert delta_idxs, "expected at least one delta before the committed message"
    assert max(delta_idxs) < committed_idx
    # Deltas concatenate to the full committed text.
    assert "".join(cast(str, d["delta"]) for d in sink.deltas()) == full
    # The committed message carries the FINAL text (from the DONE step).
    messages = [
        data for event_type, data in sink.posts if event_type == "external_conversation_item"
    ]
    item_data = cast(dict[str, Any], messages[0]["item_data"])
    content = cast(list[dict[str, Any]], item_data["content"])
    assert content[0]["text"] == full


@pytest.mark.asyncio
async def test_stream_generating_emits_incremental_reasoning_deltas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """Growing ``thinking`` while GENERATING emits non-overlapping reasoning deltas.

    Mirrors the text-delta contract for ``plannerResponse.thinking`` (design
    §10.2): suffixes concatenate to the full reasoning, only the FIRST delta
    carries ``started=True`` (so the server emits one ``response.reasoning.started``
    before the block), and the rest carry ``started=False``.
    """
    full = "Let me consider the request, then outline a plan before answering."
    cut1, cut2 = 12, 38
    frames = [
        _frame([_generating_planner_with_thinking(thinking=full[:cut1])]),
        _frame([_generating_planner_with_thinking(thinking=full[:cut2])]),
        _frame([_generating_planner_with_thinking(thinking=full)]),
    ]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    reasonings = sink.reasonings()
    # Three growing frames → three non-empty reasoning deltas.
    assert [r["delta"] for r in reasonings] == [full[:cut1], full[cut1:cut2], full[cut2:]]
    # Suffixes concatenate exactly to the full reasoning (no overlap, no gap).
    assert "".join(cast(str, r["delta"]) for r in reasonings) == full
    # Only the first delta starts a new reasoning block.
    assert [r["started"] for r in reasonings] == [True, False, False]
    # Reasoning has no committed conversation item (the step never reached DONE).
    assert sink.item_types() == []


@pytest.mark.asyncio
async def test_stream_reasoning_precedes_text_and_has_no_committed_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """Reasoning deltas precede text deltas (§10.2); DONE commits ONE message only.

    Asserts the §10.2 ordering (thinking before response) within the frame and
    that reasoning, unlike text, never produces a committed conversation item —
    only the assistant ``message`` is committed on DONE.
    """
    thinking = "First, parse intent. Then answer."
    text = "Sure — here is the answer."
    frames = [
        _frame([_generating_planner_with_thinking(thinking="First, parse intent.", text="Sure")]),
        _frame([_generating_planner_with_thinking(thinking=thinking, text=text)]),
        _frame([_done_planner(text)]),
    ]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    types = sink.event_types()
    # Within each GENERATING frame the reasoning delta is posted before the text
    # delta: the first reasoning post precedes the first text post.
    first_reasoning = types.index("external_output_reasoning_delta")
    first_text = types.index("external_output_text_delta")
    assert first_reasoning < first_text
    # Reasoning deltas concatenate to the full thinking text.
    assert "".join(cast(str, r["delta"]) for r in sink.reasonings()) == thinking
    # Exactly ONE committed item — the assistant message; NO committed reasoning.
    assert sink.item_types() == ["message"]
    # Every reasoning post is a delta (transient); none is a conversation item.
    assert all(
        event_type != "external_conversation_item"
        or cast(dict[str, Any], data).get("item_type") != "reasoning"
        for event_type, data in sink.posts
    )


@pytest.mark.asyncio
async def test_stream_planner_without_thinking_emits_no_reasoning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A planner with no ``thinking`` field emits NO reasoning events (no regression).

    The non-thinking model path must be untouched: text deltas still stream and
    commit exactly as before, with zero reasoning deltas.
    """
    full = "Plain answer, no chain-of-thought."
    frames = [
        _frame([_generating_planner(full[:10])]),
        _frame([_generating_planner(full)]),
        _frame([_done_planner(full)]),
    ]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    # No reasoning events at all.
    assert sink.reasonings() == []
    # Text streaming + commit unchanged: deltas concatenate to the committed text.
    assert "".join(cast(str, d["delta"]) for d in sink.deltas()) == full
    assert sink.item_types() == ["message"]


@pytest.mark.asyncio
async def test_stream_reasoning_no_growth_frame_emits_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A re-sent ``thinking`` snapshot (no growth) emits no duplicate reasoning delta.

    Frames are cumulative; a frame that repeats the prior ``thinking`` must not
    re-emit it. Only genuine growth produces a delta, and ``started`` fires once.
    """
    thinking = "Reasoning that stops growing."
    frames = [
        _frame([_generating_planner_with_thinking(thinking=thinking)]),
        # Same thinking again (no growth) → no second reasoning delta.
        _frame([_generating_planner_with_thinking(thinking=thinking)]),
    ]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    reasonings = sink.reasonings()
    # Exactly one reasoning delta (the no-growth re-send adds nothing).
    assert [r["delta"] for r in reasonings] == [thinking]
    assert [r["started"] for r in reasonings] == [True]


@pytest.mark.asyncio
async def test_stream_reasoning_reanchors_after_non_monotonic_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A non-monotonic ``thinking`` rewrite re-anchors the tracker so growth resumes.

    Regression for Fix A: the reasoning tracker must re-anchor UNCONDITIONALLY
    (like the text path), not only when a delta is emitted. A frame streams
    ``"abcdef"``; the next frame is a non-monotonic rewrite ``"XYZ"`` (does not
    extend the forwarded prefix → no delta); the third frame ``"XYZmore"`` DOES
    extend the rewrite. Before the fix the tracker stayed pinned at ``"abcdef"``,
    so ``"XYZmore"`` (which does not start with ``"abcdef"``) emitted nothing and
    the step's reasoning froze forever; after the fix the rewrite re-anchors the
    tracker to ``"XYZ"`` and the final ``"more"`` suffix is emitted.
    """
    frames = [
        _frame([_generating_planner_with_thinking(thinking="abcdef")]),
        # Non-monotonic rewrite: does not extend "abcdef" → no delta, but must
        # re-anchor the tracker to "XYZ".
        _frame([_generating_planner_with_thinking(thinking="XYZ")]),
        # Extends the re-anchored "XYZ" → the "more" suffix must be emitted.
        _frame([_generating_planner_with_thinking(thinking="XYZmore")]),
    ]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    deltas = [r["delta"] for r in sink.reasonings()]
    # The first frame emits "abcdef"; the rewrite emits nothing; the recovery
    # frame emits "more" only because the tracker re-anchored to "XYZ".
    assert deltas == ["abcdef", "more"]


@pytest.mark.asyncio
async def test_stream_resent_done_snapshot_does_not_repost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A re-sent DONE step (on-connect snapshot replay) is deduped, not re-posted."""
    full = "Done and done."
    frames = [
        _frame([_generating_planner(full)]),
        _frame([_done_planner(full)]),
        # Snapshot replay: the same DONE step arrives again in a later frame.
        _frame([_done_planner(full)]),
        _frame([_done_planner(full)]),
    ]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    # Despite the DONE step repeating across three frames, one committed message.
    assert sink.item_types() == ["message"]


@pytest.mark.asyncio
async def test_stream_on_connect_prior_done_snapshot_deduped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A prior-turn DONE step replayed on connect posts once, then never again."""
    prior = _done_planner("Prior turn answer.", step_index=2)
    # First frame is the on-connect snapshot of a prior (already-DONE) step; it
    # repeats in the next frame (cumulative snapshot).
    frames = [
        _frame([prior]),
        _frame([prior]),
    ]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    # The committed prior step posts exactly once across the two snapshot frames.
    assert sink.item_types() == ["message"]


@pytest.mark.asyncio
async def test_stream_tool_result_running_then_done_emits_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A tool step seen RUNNING then DONE still emits its output (no early dedup).

    Regression guard: the stream observes every intermediate status, so a
    RUN_COMMAND surfaces RUNNING (mapper → ``[]``) before DONE. Recording its
    identity as ``seen`` on the RUNNING sighting would dedup the DONE frame and
    DROP the ``function_call_output``; the settled-only de-dup prevents that.
    """
    tool_call = _load("planner_response_tool_call_run_command")
    running = _running_run_command()
    done = _load("run_command_done")
    frames = [
        _frame([tool_call, running]),
        _frame([tool_call, running]),  # still running — re-sent snapshot
        _frame([tool_call, done]),  # now complete
        _frame([tool_call, done]),  # snapshot replay of the DONE step
    ]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    # The invocation commits once and the output commits once (despite the step
    # being seen RUNNING twice before DONE, and DONE being replayed once).
    assert sink.item_types() == ["function_call", "function_call_output"]


# ---------------------------------------------------------------------------
# Stream mode: WAITING frame → bridge callback once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_waiting_frame_invokes_callback_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A WAITING step delivered over the stream hands its interaction once.

    The stream path threads the SAME cascade id + port to the callback as the
    poll path does, so the bridge targets agy's live conversation regardless of
    which read path surfaced the interaction.
    """
    waiting = _load("ask_question_waiting")
    frames = [_frame([waiting]), _frame([waiting]), _frame([waiting])]
    sink = _PostSink()
    captured: list[tuple[str, int, PendingInteraction]] = []

    async def _on_pending(cascade_id: str, port: int, pending: PendingInteraction) -> None:
        captured.append((cascade_id, port, pending))

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=2,
        on_pending=_on_pending,
    )

    assert len(captured) == 1
    cascade_id, port, pending = captured[0]
    assert cascade_id == _CASCADE_ID
    assert port == _PORT
    assert pending["kind"] == "ask_question"
    assert pending["trajectory_id"] == _CASCADE_ID


@pytest.mark.asyncio
async def test_stream_waiting_then_non_waiting_withdraws_elicitation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """Over the STREAM path too, a WAITING step that goes DONE withdraws the card.

    Confirms #1200 direction 2 is covered on the stream-primary path, not just the
    poll fallback (a permission answered in the TUI / timed out surfaces as a
    DONE frame after the WAITING frame).
    """
    from omnigent.antigravity_native_interactions import agy_elicitation_id

    waiting = _permission_waiting()
    done = _permission_done()
    frames = [_frame([waiting]), _frame([done]), _frame([done])]
    sink = _PostSink()
    client, captured = _capturing_client()

    monkeypatch.setattr(reader, "stream_agent_state_updates", _FrameScript(frames))
    monkeypatch.setattr(reader, "get_trajectory_steps", _StepScript([[]]))
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    async def _noop_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    async def _on_pending(_cascade_id: str, _port: int, _pending: PendingInteraction) -> None:
        await asyncio.Event().wait()

    async with client:
        await asyncio.wait_for(
            reader.supervise_reader(
                _bridge_dir(tmp_path),
                _SESSION_ID,
                client=client,
                on_pending_interaction=cast(Any, _on_pending),
                poll_interval_s=0.0,
                stop=_stop_after(1),
            ),
            timeout=5.0,
        )

    expected_id = agy_elicitation_id(
        _CASCADE_ID,
        waiting["metadata"]["sourceTrajectoryStepInfo"]["trajectoryId"],
        waiting["metadata"]["sourceTrajectoryStepInfo"]["stepIndex"],
    )
    withdraws = [b for b in captured if b.get("type") == "external_elicitation_resolved"]
    assert len(withdraws) == 1
    assert withdraws[0]["data"]["elicitation_id"] == expected_id


# ---------------------------------------------------------------------------
# Stream mode: status edges (USER_INPUT → RUNNING, assistant-text DONE → IDLE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_status_running_then_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A user turn then a closing assistant-text step emit RUNNING then IDLE."""
    user = _load("user_input")
    full = "All set."
    frames = [
        _frame([user]),
        _frame([user, _generating_planner(full)]),
        _frame([user, _done_planner(full)]),
    ]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    assert sink.statuses() == ["running", "idle"]
    # USER_INPUT commits the user message first, then the assistant message —
    # so the web UI renders the user turn ABOVE the reply (#1155).
    assert sink.item_types() == ["message", "message"]
    assert sink.message_roles() == ["user", "assistant"]


# ---------------------------------------------------------------------------
# Stream mode: /clear-rotation guard (design §10.5)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stream mode: a stream error falls back to the poll loop (no crash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_http_error_falls_back_to_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A stream ``httpx.HTTPError`` falls back to the committed-only poll loop."""
    text = _load("planner_response_text")
    stream = _RaisingStream(httpx.ConnectError("stream boom"))
    poll = _StepScript([[text], [text]])
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=stream,
        poll_steps=poll,
        monkeypatch=monkeypatch,
        iterations=2,
    )

    # The stream was attempted, then the poll loop delivered the committed item.
    assert stream.calls >= 1
    assert poll.calls >= 1
    # Poll path is committed-only (no deltas), so exactly one message, no deltas.
    assert sink.item_types() == ["message"]
    assert sink.deltas() == []


@pytest.mark.asyncio
async def test_stream_trailer_error_falls_back_to_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """An ``AntigravityRpcError`` (connect trailer error) also falls back to poll."""
    text = _load("planner_response_text")
    stream = _RaisingStream(AntigravityRpcError("agy connect-stream error: boom"))
    poll = _StepScript([[text], [text]])
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=stream,
        poll_steps=poll,
        monkeypatch=monkeypatch,
        iterations=2,
    )

    assert stream.calls >= 1
    assert sink.item_types() == ["message"]
    assert sink.deltas() == []


@pytest.mark.asyncio
async def test_stream_error_midway_falls_back_without_losing_prior_deltas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A stream that yields a delta then errors falls back without crashing.

    Verifies the reader survives a mid-stream failure (deltas already forwarded
    stay forwarded) and the poll loop then delivers the committed item.
    """
    full = "Half a message"
    done_full = "Half a message, now complete."

    class _DeltaThenRaise:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, port: int, conversation_id: str) -> AsyncIterator[dict[str, object]]:
            self.calls += 1

            async def _gen() -> AsyncIterator[dict[str, object]]:
                yield _frame([_generating_planner(full)])
                raise httpx.ReadError("mid-stream drop")

            return _gen()

    stream = _DeltaThenRaise()
    poll = _StepScript([[_done_planner(done_full)], [_done_planner(done_full)]])
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=stream,
        poll_steps=poll,
        monkeypatch=monkeypatch,
        iterations=2,
    )

    # The pre-error delta was forwarded.
    assert [d["delta"] for d in sink.deltas()] == [full]
    # The poll fallback delivered the committed message.
    assert sink.item_types() == ["message"]


# ---------------------------------------------------------------------------
# Telemetry: external_session_usage (Task T-EF)
# ---------------------------------------------------------------------------

# Fake model catalog returned by the injected ``get_available_models`` stub.
_FAKE_CATALOG: dict[str, object] = {
    "models": {
        "m20": {
            "model": "MODEL_PLACEHOLDER_M20",
            "displayName": "Gemini 2.5 Flash",
            "recommended": True,
            "supportsThinking": True,
            "thinkingBudget": 8192,
        },
        "m132": {
            "model": "MODEL_PLACEHOLDER_M132",
            "displayName": "Gemini 2.5 Pro",
            "recommended": False,
            "supportsThinking": True,
            "thinkingBudget": 16384,
        },
    }
}


def _planner_with_model_usage(
    *,
    step_index: int = 2,
    input_tokens: str = "1000",
    output_tokens: str = "100",
    thinking_tokens: str = "40",
    response_tokens: str = "60",
    cache_read_tokens: str = "200",
    model_enum: str = "MODEL_PLACEHOLDER_M20",
    with_requested_model: bool = True,
) -> dict[str, Any]:
    """A DONE PLANNER_RESPONSE step with modelUsage and requestedModel populated.

    Built from the real fixture so metadata shape is authentic; the usage
    fields are overridden so tests can control exact values.
    """
    step = copy.deepcopy(_load("planner_response_text"))
    step["status"] = "CORTEX_STEP_STATUS_DONE"
    metadata = cast(dict[str, Any], step["metadata"])
    metadata["modelUsage"] = {
        "model": model_enum,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "thinkingOutputTokens": thinking_tokens,
        "responseOutputTokens": response_tokens,
        "cacheReadTokens": cache_read_tokens,
    }
    metadata["sourceTrajectoryStepInfo"]["stepIndex"] = step_index
    if with_requested_model:
        metadata.setdefault("requestedModel", {})["model"] = model_enum
    return step


def _user_input_with_model(
    model_enum: str = "MODEL_PLACEHOLDER_M20",
    *,
    step_index: int | None = None,
) -> dict[str, Any]:
    """A USER_INPUT step with a specific ``planModel`` enum (live wire shape).

    :param model_enum: agy model enum string, written as the live
        ``plannerConfig.planModel`` string.
    :param step_index: Optional step index override; when provided it is written
        into ``metadata.sourceTrajectoryStepInfo.stepIndex`` so two consecutive
        turns can be assigned distinct dedup keys (the base fixture has no
        ``stepIndex``, so they would otherwise share the same ``(trajectory_id,
        None)`` key and the second turn would be silently de-duped).
    """
    step = copy.deepcopy(_load("user_input"))
    user_input = cast(dict[str, Any], step["userInput"])
    planner_cfg = cast(dict[str, Any], user_input["userConfig"]["plannerConfig"])
    planner_cfg["planModel"] = model_enum
    if step_index is not None:
        traj_info = cast(dict[str, Any], step["metadata"])["sourceTrajectoryStepInfo"]
        traj_info["stepIndex"] = step_index
    return step


def _user_input_real_wire(
    *,
    execution_id: str,
    model_enum: str = "MODEL_PLACEHOLDER_M20",
) -> dict[str, Any]:
    """A USER_INPUT step shaped like the REAL agy wire: per-turn id, NO stepIndex.

    Unlike :func:`_user_input_with_model` (which can inject a synthetic
    ``stepIndex`` to hand two turns distinct dedup keys), this models the live
    shape exactly: USER_INPUT carries ``metadata.executionId`` (a per-turn uuid)
    but NO ``sourceTrajectoryStepInfo.stepIndex``, so the only thing that
    distinguishes two turns' USER_INPUT steps is the discriminator. The static
    fixture's fixed ``executionId`` is overridden per turn so the two turns do
    not themselves collide.
    """
    step = copy.deepcopy(_load("user_input"))
    metadata = cast(dict[str, Any], step["metadata"])
    metadata["executionId"] = execution_id
    # Make absolutely sure no stepIndex sneaks in (the base fixture has none).
    metadata["sourceTrajectoryStepInfo"].pop("stepIndex", None)
    user_input = cast(dict[str, Any], step["userInput"])
    planner_cfg = cast(dict[str, Any], user_input["userConfig"]["plannerConfig"])
    planner_cfg["planModel"] = model_enum
    return step


async def _run_with_telemetry(
    *,
    bridge_dir: Path,
    sink: _PostSink,
    stream: object,
    poll_steps: object,
    monkeypatch: pytest.MonkeyPatch,
    iterations: int,
    catalog: dict[str, object] | None = None,
) -> None:
    """Drive ``supervise_reader`` in STREAM mode with a fake model catalog."""
    fake_catalog = catalog if catalog is not None else _FAKE_CATALOG
    monkeypatch.setattr(reader, "stream_agent_state_updates", stream)
    monkeypatch.setattr(reader, "get_trajectory_steps", poll_steps)
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)
    monkeypatch.setattr(
        reader,
        "get_available_models",
        lambda port: fake_catalog,
    )

    async def _noop_sleep(_seconds: float) -> None:
        # Yield to the event loop (no real delay) so the off-loop interaction
        # bridge tasks the reader spawns get a turn to run in the bounded loop.
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    async def _on_pending(_cascade_id: str, _port: int, _pending: PendingInteraction) -> None:
        return None

    await reader.supervise_reader(
        bridge_dir,
        _SESSION_ID,
        client=cast(httpx.AsyncClient, object()),
        on_pending_interaction=cast(Any, _on_pending),
        poll_interval_s=0.0,
        stop=_stop_after(iterations),
    )


@pytest.mark.asyncio
async def test_planner_done_emits_session_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A PLANNER_RESPONSE DONE with modelUsage emits exactly one external_session_usage.

    The event data must map agy's string-int fields onto the Omnigent shape:
    - cumulative_input_tokens = inputTokens (int)
    - cumulative_output_tokens = outputTokens (int)
    - cumulative_cache_read_input_tokens = cacheReadTokens (int)
    - model = the displayName from the catalog (not the raw enum)
    """
    planner = _planner_with_model_usage(
        input_tokens="1000",
        output_tokens="100",
        cache_read_tokens="200",
        model_enum="MODEL_PLACEHOLDER_M20",
    )
    frames = [_frame([planner])]
    sink = _PostSink()

    await _run_with_telemetry(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    usage_events = [(et, d) for et, d in sink.posts if et == "external_session_usage"]
    assert len(usage_events) == 1, f"expected 1 usage event, got {len(usage_events)}"
    _, usage_data = usage_events[0]
    assert usage_data["cumulative_input_tokens"] == 1000
    assert usage_data["cumulative_output_tokens"] == 100
    assert usage_data["cumulative_cache_read_input_tokens"] == 200
    assert usage_data["model"] == "Gemini 2.5 Flash"


@pytest.mark.asyncio
async def test_usage_replay_does_not_re_emit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A DONE planner step replayed on the same stream does NOT re-emit usage.

    The step's ``(trajectory_id, step_index)`` identity is already in
    ``state.seen`` after the first DONE frame, so subsequent re-sends of the
    same step post nothing (usage included).
    """
    planner = _planner_with_model_usage()
    # Same DONE step repeated across three frames (snapshot replay pattern).
    frames = [_frame([planner]), _frame([planner]), _frame([planner])]
    sink = _PostSink()

    await _run_with_telemetry(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    usage_events = [et for et, _ in sink.posts if et == "external_session_usage"]
    assert usage_events == ["external_session_usage"]


@pytest.mark.asyncio
async def test_usage_missing_fields_skipped_gracefully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A planner DONE step without modelUsage emits no usage event (no crash)."""
    planner = copy.deepcopy(_load("planner_response_text"))
    planner["status"] = "CORTEX_STEP_STATUS_DONE"
    metadata = cast(dict[str, Any], planner["metadata"])
    metadata.pop("modelUsage", None)
    frames = [_frame([planner])]
    sink = _PostSink()

    await _run_with_telemetry(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    usage_events = [et for et, _ in sink.posts if et == "external_session_usage"]
    assert usage_events == []


# ---------------------------------------------------------------------------
# Telemetry: external_model_change (Task T-EF)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_turn_emits_model_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """The first USER_INPUT step with a new model enum emits external_model_change.

    The event data must carry the resolved displayName, NOT the raw enum.
    """
    user = _user_input_with_model("MODEL_PLACEHOLDER_M20")
    frames = [_frame([user])]
    sink = _PostSink()

    await _run_with_telemetry(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    model_change_events = [(et, d) for et, d in sink.posts if et == "external_model_change"]
    assert len(model_change_events) == 1, (
        f"expected 1 model_change event, got {len(model_change_events)}"
    )
    _, mc_data = model_change_events[0]
    assert mc_data["model"] == "Gemini 2.5 Flash"


@pytest.mark.asyncio
async def test_same_model_second_turn_no_new_model_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A second turn with the SAME model enum emits no additional model_change event."""
    user1 = _user_input_with_model("MODEL_PLACEHOLDER_M20", step_index=0)
    user2 = _user_input_with_model("MODEL_PLACEHOLDER_M20", step_index=4)
    # Two separate turns each start with a USER_INPUT, same model.
    frames = [_frame([user1]), _frame([user1, user2])]
    sink = _PostSink()

    await _run_with_telemetry(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    model_change_events = [et for et, _ in sink.posts if et == "external_model_change"]
    assert len(model_change_events) == 1, (
        f"expected exactly 1 model_change, got {len(model_change_events)}"
    )


@pytest.mark.asyncio
async def test_model_switch_mid_session_emits_new_model_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A turn with a DIFFERENT model enum triggers a new external_model_change."""
    user_m20 = _user_input_with_model("MODEL_PLACEHOLDER_M20", step_index=0)
    user_m132 = _user_input_with_model("MODEL_PLACEHOLDER_M132", step_index=4)
    # Turn 1 with M20, then turn 2 with M132.
    frames = [_frame([user_m20]), _frame([user_m20, user_m132])]
    sink = _PostSink()

    await _run_with_telemetry(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    model_change_events = [(et, d) for et, d in sink.posts if et == "external_model_change"]
    assert len(model_change_events) == 2, (
        f"expected 2 model_change events (one per distinct model), got {len(model_change_events)}"
    )
    assert model_change_events[0][1]["model"] == "Gemini 2.5 Flash"
    assert model_change_events[1][1]["model"] == "Gemini 2.5 Pro"


@pytest.mark.asyncio
async def test_model_change_replay_no_re_emit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A USER_INPUT step replayed across frames emits model_change only once."""
    user = _user_input_with_model("MODEL_PLACEHOLDER_M20")
    frames = [_frame([user]), _frame([user]), _frame([user])]
    sink = _PostSink()

    await _run_with_telemetry(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    model_change_events = [et for et, _ in sink.posts if et == "external_model_change"]
    assert len(model_change_events) == 1


@pytest.mark.asyncio
async def test_unknown_model_enum_posts_raw_enum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """An unresolvable model enum falls back to the raw enum string as the model name."""
    unknown_enum = "MODEL_PLACEHOLDER_M999"
    user = _user_input_with_model(unknown_enum)
    frames = [_frame([user])]
    sink = _PostSink()

    await _run_with_telemetry(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    model_change_events = [(et, d) for et, d in sink.posts if et == "external_model_change"]
    assert len(model_change_events) == 1
    # Falls back to the raw enum when the catalog does not contain it.
    assert model_change_events[0][1]["model"] == unknown_enum


def test_requested_model_enum_from_step_falls_back_to_requested_model() -> None:
    """A legacy ``requestedModel.model`` (dict) shape is read when ``planModel`` is absent.

    The live wire carries ``plannerConfig.planModel`` (a string), but a TUI-origin
    step may still use the older ``requestedModel.model`` dict shape. The reader
    must read that fallback so a model switch from such a step is not silently
    dropped.
    """
    legacy_step: dict[str, Any] = {
        "type": reader._TYPE_USER_INPUT,
        "userInput": {
            "userConfig": {"plannerConfig": {"requestedModel": {"model": "MODEL_PLACEHOLDER_M20"}}}
        },
    }
    assert reader._requested_model_enum_from_step(legacy_step) == "MODEL_PLACEHOLDER_M20"


@pytest.mark.asyncio
async def test_two_turn_usage_is_cumulative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """Two turns each with 1000 input tokens → turn 1 posts 1000, turn 2 posts 2000.

    Regression guard for the SET-semantics bug: if the reader emitted per-call
    values, turn 2 would also post 1000, causing the server to compute a zero
    delta for that turn and the cost badge to freeze after turn 1.
    """
    planner_turn1 = _planner_with_model_usage(
        step_index=2,
        input_tokens="1000",
        output_tokens="50",
        cache_read_tokens="100",
    )
    planner_turn2 = _planner_with_model_usage(
        step_index=6,
        input_tokens="1000",
        output_tokens="50",
        cache_read_tokens="100",
    )
    # Two separate DONE planner steps (different step indices = different turns).
    frames = [
        _frame([planner_turn1]),
        _frame([planner_turn1, planner_turn2]),
    ]
    sink = _PostSink()

    await _run_with_telemetry(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    usage_events = [(et, d) for et, d in sink.posts if et == "external_session_usage"]
    assert len(usage_events) == 2, (
        f"expected 2 usage events (one per turn), got {len(usage_events)}"
    )
    # Turn 1: per-call values (first turn, cumulative == per-call).
    assert usage_events[0][1]["cumulative_input_tokens"] == 1000
    assert usage_events[0][1]["cumulative_output_tokens"] == 50
    assert usage_events[0][1]["cumulative_cache_read_input_tokens"] == 100
    # Turn 2: RUNNING total (2000 input, not 1000 again).
    assert usage_events[1][1]["cumulative_input_tokens"] == 2000
    assert usage_events[1][1]["cumulative_output_tokens"] == 100
    assert usage_events[1][1]["cumulative_cache_read_input_tokens"] == 200


# ---------------------------------------------------------------------------
# USER_INPUT dedup key: real-wire turns (no stepIndex) must not collide
# ---------------------------------------------------------------------------


def test_step_key_distinct_for_user_input_turns_without_step_index() -> None:
    """Two USER_INPUT steps with distinct executionId key distinctly.

    Regression for the dedup-key collision (Fix I-1): on the real wire a
    USER_INPUT step has a per-conversation-stable ``trajectory_id`` and NO
    ``stepIndex``, so a ``(trajectory_id, None)`` key collides across every turn.
    Folding the per-turn ``executionId`` into the key keeps each turn distinct.
    Without the fix both keys are ``(trajectory_id, None)`` and compare equal.
    """
    turn1 = _user_input_real_wire(execution_id="exec-turn-1")
    turn2 = _user_input_real_wire(execution_id="exec-turn-2")
    key1 = reader._step_key(turn1)
    key2 = reader._step_key(turn2)
    assert key1 != key2, f"USER_INPUT keys collided across turns: {key1} == {key2}"
    # The discriminator (3rd element) is what separates them — the first two
    # elements are identical (same trajectory_id, both step_index None).
    assert key1[:2] == key2[:2]
    assert key1[2] == "exec-turn-1"
    assert key2[2] == "exec-turn-2"


@pytest.mark.asyncio
async def test_two_real_wire_turns_each_emit_running_then_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """Two turns whose USER_INPUT carries NO stepIndex each fire RUNNING + IDLE.

    End-to-end regression for Fix I-1. Each turn is a USER_INPUT (opens the
    turn → RUNNING) followed by a DONE assistant-text planner (closes the turn →
    IDLE). The USER_INPUT steps are built with the REAL wire shape (per-turn
    ``executionId``, no ``stepIndex``) — NOT the synthetic-stepIndex helper that
    masks this bug. Before the fix, turn 2's USER_INPUT collides with turn 1's on
    ``(trajectory_id, None)``, is treated as already-seen, and its emit block is
    skipped → no second RUNNING edge (the web spinner never restarts), so the
    sequence would be ``["running", "idle", "idle"]`` instead of the expected
    per-turn ``["running", "idle", "running", "idle"]``.
    """
    user1 = _user_input_real_wire(execution_id="exec-turn-1")
    user2 = _user_input_real_wire(execution_id="exec-turn-2")
    done1 = _done_planner("First answer.", step_index=2)
    done2 = _done_planner("Second answer.", step_index=6)
    # The stream delivers cumulative snapshots: turn 2's frames include turn 1's
    # already-committed steps.
    frames = [
        _frame([user1]),
        _frame([user1, done1]),
        _frame([user1, done1, user2]),
        _frame([user1, done1, user2, done2]),
    ]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(frames),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    assert sink.statuses() == ["running", "idle", "running", "idle"]
    # Each turn commits a user message (from USER_INPUT) then an assistant
    # message — user-before-assistant ordering, per turn (#1155).
    assert sink.item_types() == ["message", "message", "message", "message"]
    assert sink.message_roles() == ["user", "assistant", "user", "assistant"]


# ---------------------------------------------------------------------------
# _is_assistant_text_close_step: the turn-close edge fires only on DONE
# ---------------------------------------------------------------------------


def test_close_step_false_for_generating_planner_with_text() -> None:
    """A GENERATING planner with text but no tool calls does NOT close the turn.

    Regression: the IDLE status edge must fire only on the DONE closing step.
    A GENERATING frame already carries growing ``modifiedResponse`` text with no
    ``toolCalls`` yet, so without the DONE gate the reader would close the turn
    (the spinner) mid-response on the stream path.
    """
    generating = {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_GENERATING",
        "plannerResponse": {"modifiedResponse": "Partial answer so far"},
    }
    assert reader._is_assistant_text_close_step(generating) is False


def test_close_step_true_for_done_planner_with_text() -> None:
    """The SAME step at status DONE (text, no tool calls) DOES close the turn."""
    done = {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_DONE",
        "plannerResponse": {
            "modifiedResponse": "Partial answer so far",
            "response": "Partial answer so far",
        },
    }
    assert reader._is_assistant_text_close_step(done) is True


def test_close_step_false_for_done_planner_with_tool_calls() -> None:
    """A DONE planner that still issues a tool call does NOT close the turn.

    Confirms the DONE gate did not regress the tool-call carve-out: a planner
    step that invokes a tool is followed by the tool result (and possibly more
    planner steps), so it must not be treated as the closing edge.
    """
    done_with_tool = {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_DONE",
        "plannerResponse": {
            "response": "Running a command",
            "toolCalls": [{"id": "call_1"}],
        },
    }
    assert reader._is_assistant_text_close_step(done_with_tool) is False


# ---------------------------------------------------------------------------
# _is_turn_close_step: also closes on a terminal-ERROR or degenerate-DONE
# planner so the turn never sticks open (the stuck-spinner regression)
# ---------------------------------------------------------------------------


def test_turn_close_true_for_error_planner() -> None:
    """A terminal-ERROR planner closes the turn even with no closing text.

    Regression: without this the IDLE edge never fires when a turn ends in an
    ERROR planner — ``turn_active`` sticks True, the spinner never clears, and
    the next USER_INPUT can't re-open RUNNING (it is gated on ``not
    turn_active``). The text-close predicate (which requires DONE + text) does
    NOT catch this case.
    """
    error_planner = {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_ERROR",
        "plannerResponse": {},
    }
    assert reader._is_assistant_text_close_step(error_planner) is False
    assert reader._is_turn_close_step(error_planner) is True


def test_turn_close_true_for_done_planner_no_text_no_tools() -> None:
    """A DONE planner with neither text nor tool calls is a degenerate close."""
    empty_done = {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_DONE",
        "plannerResponse": {},
    }
    assert reader._is_turn_close_step(empty_done) is True


def test_turn_close_false_for_done_planner_with_tool_calls() -> None:
    """A DONE planner that dispatches a tool call is a continuation, not a close."""
    done_with_tool = {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_DONE",
        "plannerResponse": {"toolCalls": [{"id": "call_1"}]},
    }
    assert reader._is_turn_close_step(done_with_tool) is False


def test_turn_close_false_for_generating_planner() -> None:
    """A GENERATING planner never closes the turn (mid-stream)."""
    generating = {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_GENERATING",
        "plannerResponse": {"modifiedResponse": "partial"},
    }
    assert reader._is_turn_close_step(generating) is False


def test_turn_close_false_for_tool_result_step() -> None:
    """A tool-result step never closes the turn (a recovery planner follows)."""
    tool_result = {
        "type": "CORTEX_STEP_TYPE_RUN_COMMAND",
        "status": "CORTEX_STEP_STATUS_DONE",
        "metadata": {"toolCall": {"id": "cbawg2v8"}},
    }
    assert reader._is_turn_close_step(tool_result) is False


# ---------------------------------------------------------------------------
# Stream re-entry backoff: an immediate clean trailer must not busy-spin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_reentry_backoff_between_clean_immediate_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A stream that returns immediately with no frames backs off between re-entries.

    Regression for the busy-spin: if agy returns an immediate clean trailer (no
    frames) repeatedly, ``_stream_loop`` must NOT re-POST the stream at zero
    delay — it must await ``_STREAM_REENTRY_BACKOFF_S`` between re-entries.

    The ``stop`` predicate here fires once the stream has been entered
    ``target_entries`` times, so the assertion is tied to stream re-entries (not
    to how many times ``stop`` is consulted per loop turn). Each backoff is gated
    on ``not stop()`` AFTER the stream returns, so the run records exactly one
    backoff per re-entry that is followed by another entry.

    The reader body runs as a cancellable task alongside the rotation detector
    (both share the ``_sleep`` seam), so the detector's own coarse interval sleep
    can also be recorded before it is cancelled. That sleep is unrelated to the
    busy-spin this test guards, so the recorded sleeps are filtered to the stream
    re-entry backoff: the invariant is "no zero-delay re-POST", i.e. every re-entry
    that precedes another entry paid exactly one backoff.
    """
    empty_stream = _FrameScript([])  # each entry yields no frames, returns at once
    backoff_sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        backoff_sleeps.append(seconds)

    target_entries = 3

    def _stop_after_entries() -> bool:
        # Stop once the stream has been (re-)entered the target number of times.
        return empty_stream.calls >= target_entries

    monkeypatch.setattr(reader, "stream_agent_state_updates", empty_stream)
    monkeypatch.setattr(reader, "get_trajectory_steps", _StepScript([[]]))
    monkeypatch.setattr(reader, "post_session_event_with_retry", _PostSink())
    monkeypatch.setattr(reader, "_sleep", _record_sleep)

    async def _noop_pending(_cid: str, _port: int, _pending: PendingInteraction) -> None:
        return None

    await reader.supervise_reader(
        _bridge_dir(tmp_path),
        _SESSION_ID,
        client=cast(httpx.AsyncClient, object()),
        on_pending_interaction=cast(Any, _noop_pending),
        poll_interval_s=0.0,
        stop=_stop_after_entries,
    )

    # The stream was re-entered exactly target_entries times (no crash, no
    # fallback to the poll loop). Filtering out the rotation detector's coarse
    # interval sleep (it shares the ``_sleep`` seam), the re-entry backoffs are
    # exactly the documented value — proving the loop did NOT busy-spin re-POSTing
    # at zero delay, and that no poll-interval sleep ran. There is one backoff per
    # re-entry that precedes another entry: target_entries - 1.
    assert empty_stream.calls == target_entries
    reentry_backoffs = [s for s in backoff_sleeps if s == reader._STREAM_REENTRY_BACKOFF_S]
    assert reentry_backoffs == [reader._STREAM_REENTRY_BACKOFF_S] * (target_entries - 1)
    # No zero-delay re-POST and no unexpected poll-interval sleep crept in: every
    # non-backoff sleep recorded is the rotation detector's coarse interval.
    assert all(
        s in (reader._STREAM_REENTRY_BACKOFF_S, reader._DEFAULT_ROTATION_INTERVAL_S)
        for s in backoff_sleeps
    )


# ---------------------------------------------------------------------------
# T-G: _detect_rotated_cascade (pure /clear-rotation detection)
# ---------------------------------------------------------------------------
#
# Fixtures mirror the real ``GetAllCascadeTrajectories`` capture shape: each
# summary is keyed by its root conversation id and carries a ``trajectoryType``
# plus ISO-8601 ``lastUserInputTime`` / ``lastModifiedTime`` (a ``Z`` UTC
# suffix), with the freshly-minted-but-unused cascade omitting both.

_BOUND_CASCADE = "0715c922-02fc-4278-bab8-3a6ea565bbbf"
_OTHER_CASCADE = "ef42f24d-7dfd-4810-a5f0-9e069c88709a"


def _summary(
    *,
    last_user_input_time: str | None = None,
    last_modified_time: str | None = None,
    trajectory_type: str = "CORTEX_TRAJECTORY_TYPE_CASCADE",
) -> dict[str, Any]:
    """Build one ``trajectorySummaries`` entry (real-capture shape).

    Omitting a timestamp (``None``) models a never-set field, exactly as the live
    capture omits ``lastUserInputTime`` / ``lastModifiedTime`` for a freshly
    ``/clear``-minted, never-used cascade.
    """
    entry: dict[str, Any] = {
        "trajectoryId": "tid-" + trajectory_type[-4:],
        "status": "CASCADE_RUN_STATUS_IDLE",
        "trajectoryType": trajectory_type,
    }
    if last_user_input_time is not None:
        entry["lastUserInputTime"] = last_user_input_time
    if last_modified_time is not None:
        entry["lastModifiedTime"] = last_modified_time
    return entry


def test_detect_rotation_newer_active_sibling_returns_its_id() -> None:
    """A sibling cascade with strictly newer activity is the rotation target."""
    summaries = {
        _BOUND_CASCADE: _summary(last_user_input_time="2026-06-23T17:34:54.152668Z"),
        _OTHER_CASCADE: _summary(last_user_input_time="2026-06-23T17:50:29.232919Z"),
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) == _OTHER_CASCADE


def test_detect_rotation_minted_but_unused_sibling_returns_none() -> None:
    """A freshly /clear-minted sibling (no activity timestamps) is NOT a rotation.

    The bare mint reports no ``lastUserInputTime`` / ``lastModifiedTime`` until its
    first turn runs, so it must not trigger rotation — only a USED new conversation
    does.
    """
    summaries = {
        _BOUND_CASCADE: _summary(last_user_input_time="2026-06-23T17:34:54.152668Z"),
        _OTHER_CASCADE: _summary(),  # minted, never used
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) is None


def test_detect_rotation_only_bound_present_returns_none() -> None:
    """With only the bound cascade present there is nothing to rotate to."""
    summaries = {
        _BOUND_CASCADE: _summary(last_user_input_time="2026-06-23T17:34:54.152668Z"),
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) is None


def test_detect_rotation_older_sibling_returns_none() -> None:
    """A sibling that is OLDER than the bound cascade is not a rotation."""
    summaries = {
        _BOUND_CASCADE: _summary(last_user_input_time="2026-06-23T17:50:29.232919Z"),
        _OTHER_CASCADE: _summary(last_user_input_time="2026-06-23T17:34:54.152668Z"),
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) is None


def test_detect_rotation_non_cascade_sibling_returns_none() -> None:
    """A newer NON-cascade (subagent) sibling must not be a rotation target.

    Rotating to a child/subagent trajectory would mirror a sub-conversation, not
    the user's top-level conversation — so a newer subagent is ignored.
    """
    summaries = {
        _BOUND_CASCADE: _summary(last_user_input_time="2026-06-23T17:34:54.152668Z"),
        _OTHER_CASCADE: _summary(
            last_user_input_time="2026-06-23T17:50:29.232919Z",
            trajectory_type="CORTEX_TRAJECTORY_TYPE_SUBAGENT",
        ),
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) is None


def test_detect_rotation_bound_absent_returns_none() -> None:
    """When the bound cascade is missing from the summaries, do not rotate blindly.

    We cannot prove the bound conversation is staler than a sibling, so the reader
    must stay on its current binding rather than chase a sibling on incomplete
    information.
    """
    summaries = {
        _OTHER_CASCADE: _summary(last_user_input_time="2026-06-23T17:50:29.232919Z"),
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) is None


def test_detect_rotation_falls_back_to_last_modified_time() -> None:
    """``lastModifiedTime`` is used when ``lastUserInputTime`` is absent.

    A cascade whose only activity signal is ``lastModifiedTime`` is still a valid
    comparison/candidate (the helper prefers ``lastUserInputTime`` but falls back).
    """
    summaries = {
        _BOUND_CASCADE: _summary(last_modified_time="2026-06-23T17:34:54.000000Z"),
        _OTHER_CASCADE: _summary(last_modified_time="2026-06-23T17:50:32.565300Z"),
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) == _OTHER_CASCADE


def test_detect_rotation_equal_activity_is_not_a_rotation() -> None:
    """A sibling whose activity merely EQUALS the bound cascade is not a rotation.

    Rotation requires STRICTLY newer activity, so a steady state (equal stamps)
    never flaps.
    """
    stamp = "2026-06-23T17:50:29.232919Z"
    summaries = {
        _BOUND_CASCADE: _summary(last_user_input_time=stamp),
        _OTHER_CASCADE: _summary(last_user_input_time=stamp),
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) is None


def test_detect_rotation_malformed_timestamp_is_not_candidate() -> None:
    """A sibling with a malformed activity timestamp is treated as no-activity.

    A parse failure must never spuriously rotate: the malformed sibling reports no
    activity and is skipped.
    """
    summaries = {
        _BOUND_CASCADE: _summary(last_user_input_time="2026-06-23T17:34:54.152668Z"),
        _OTHER_CASCADE: _summary(last_user_input_time="not-a-timestamp"),
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) is None


def test_detect_rotation_real_capture_shape() -> None:
    """The exact two-entry capture: the used sibling rotates away from the idle one.

    Mirrors ``wire_ref/all_cascade_trajectories.json`` — the bound cascade has no
    activity timestamps (never used) while the sibling carries a
    ``lastUserInputTime`` — so the active sibling is the rotation target.
    """
    summaries = {
        # The capture's first entry: created, never used (no activity stamps).
        _BOUND_CASCADE: _summary(),
        # The capture's second entry: used (has lastUserInputTime/lastModifiedTime).
        _OTHER_CASCADE: _summary(
            last_user_input_time="2026-06-23T17:50:29.232919Z",
            last_modified_time="2026-06-23T17:50:32.565300Z",
        ),
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) == _OTHER_CASCADE


# ---------------------------------------------------------------------------
# T-G: supervise_reader signals rotation; _rotate_session_for_cascade API;
#      run_reader_with_bridge rebind loop
# ---------------------------------------------------------------------------


def _rotation_body(new_cascade_id: str) -> dict[str, Any]:
    """A ``GetAllCascadeTrajectories`` body where ``new_cascade_id`` is newer-active.

    The bound cascade (``_CASCADE_ID``, the discovery fixture's id) is present but
    never-used; the new cascade carries a ``lastUserInputTime`` so the detector
    treats it as the current conversation.
    """
    return {
        "trajectorySummaries": {
            _CASCADE_ID: _summary(),
            new_cascade_id: _summary(last_user_input_time="2026-06-23T18:00:00.000000Z"),
        }
    }


@pytest.mark.asyncio
async def test_supervise_reader_returns_new_cascade_on_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A detected /clear rotation makes ``supervise_reader`` stop and return the id.

    With the rotation detector reporting a newer-active sibling, the stream body
    must stop mirroring the (now-dead) bound conversation and ``supervise_reader``
    must return the NEW cascade id so the caller can rotate + rebind.
    """
    new_cascade = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(
        reader, "get_all_cascade_trajectories", lambda port: _rotation_body(new_cascade)
    )
    monkeypatch.setattr(reader, "stream_agent_state_updates", _FrameScript([]))
    monkeypatch.setattr(reader, "get_trajectory_steps", _StepScript([[]]))
    monkeypatch.setattr(reader, "post_session_event_with_retry", _PostSink())

    async def _noop_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    async def _noop_pending(_cid: str, _port: int, _pending: PendingInteraction) -> None:
        return None

    # No bounded ``stop``: the run ends ONLY because the detector fires (proving
    # rotation, not the test harness, terminated the body).
    result = await reader.supervise_reader(
        _bridge_dir(tmp_path),
        _SESSION_ID,
        client=cast(httpx.AsyncClient, object()),
        on_pending_interaction=cast(Any, _noop_pending),
        poll_interval_s=0.0,
        detect_rotation_interval_s=0.0,
    )
    assert result == new_cascade


@pytest.mark.asyncio
async def test_supervise_reader_skips_failed_rotation_cascade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A cascade in ``skip_cascade_ids`` does NOT trigger a rotation signal.

    After a failed rotation attempt the caller re-enters with the failed cascade in
    ``skip_cascade_ids``; the detector must ignore it, so the bounded body runs to
    its ``stop`` and ``supervise_reader`` returns ``None`` (no rotation), letting
    the reader keep serving the old binding instead of hot-looping.
    """
    skipped = "deadbeef-0000-0000-0000-000000000000"
    monkeypatch.setattr(
        reader, "get_all_cascade_trajectories", lambda port: _rotation_body(skipped)
    )
    monkeypatch.setattr(reader, "stream_agent_state_updates", _FrameScript([]))
    monkeypatch.setattr(reader, "get_trajectory_steps", _StepScript([[]]))
    monkeypatch.setattr(reader, "post_session_event_with_retry", _PostSink())

    async def _noop_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    async def _noop_pending(_cid: str, _port: int, _pending: PendingInteraction) -> None:
        return None

    result = await reader.supervise_reader(
        _bridge_dir(tmp_path),
        _SESSION_ID,
        client=cast(httpx.AsyncClient, object()),
        on_pending_interaction=cast(Any, _noop_pending),
        poll_interval_s=0.0,
        detect_rotation_interval_s=0.0,
        stop=_stop_after(2),
        skip_cascade_ids=frozenset({skipped}),
    )
    assert result is None


class _RotationFetchScript:
    """``get_all_cascade_trajectories`` that raises a scripted exception sequence.

    Each call raises the next exception in ``exceptions``; once exhausted the
    final exception repeats. A test ends the otherwise-infinite
    :func:`_watch_for_rotation` loop by scripting a terminal
    :class:`asyncio.CancelledError` — a ``BaseException`` (not ``Exception``), so
    the detector's ``httpx``/``ValueError`` arms do not catch it and it
    propagates out of the worker thread to stop the loop.
    """

    def __init__(self, exceptions: list[BaseException]) -> None:
        self._exceptions = exceptions
        self.calls = 0

    def __call__(self, port: int) -> dict[str, object]:
        idx = min(self.calls, len(self._exceptions) - 1)
        self.calls += 1
        raise self._exceptions[idx]


def _fail_rotation(_cid: str) -> None:
    """``on_rotation`` that must never fire when every fetch fails."""
    raise AssertionError("on_rotation must not fire when the fetch keeps failing")


async def _watch_until_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    first_error: BaseException,
) -> _RotationFetchScript:
    """Run :func:`_watch_for_rotation` for one ``first_error`` tick, then cancel.

    Scripts the fetch to raise ``first_error`` then a terminal ``CancelledError``,
    no-ops the interval sleep, and drives the loop under DEBUG capture until the
    cancellation propagates out. Returns the script so the caller can assert it
    reached its second call (i.e. the loop retried past ``first_error``); the
    per-test log-level assertions read ``caplog.records`` directly.
    """
    script = _RotationFetchScript([first_error, asyncio.CancelledError()])
    monkeypatch.setattr(reader, "get_all_cascade_trajectories", script)

    async def _noop_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    with caplog.at_level(logging.DEBUG, logger=reader.__name__):
        with pytest.raises(asyncio.CancelledError):
            await reader._watch_for_rotation(
                port=_PORT,
                bound_cascade_id=_BOUND_CASCADE,
                interval_s=0.0,
                skip_cascade_ids=frozenset(),
                on_rotation=_fail_rotation,
            )
    return script


@pytest.mark.asyncio
async def test_watch_for_rotation_connect_error_logs_debug_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A benign ``ConnectError`` is logged at DEBUG (not WARNING) and the loop retries.

    When the agy port is force-killed during teardown/rotation/shutdown before this
    detector is cancelled, every poll tick raises ``httpx.ConnectError`` (connection
    refused). That is benign (no spawn/leak; the supervisor cancels us), so it must
    log at DEBUG to avoid per-tick WARNING spam — and the loop must still advance to
    the next tick (proven here by the script reaching its second call).
    """
    script = await _watch_until_cancelled(
        monkeypatch, caplog, httpx.ConnectError("connection refused")
    )

    # The loop continued past the ConnectError tick to the terminal stop tick.
    assert script.calls == 2
    connect_records = [r for r in caplog.records if "connect refused" in r.getMessage()]
    assert len(connect_records) == 1
    assert connect_records[0].levelno == logging.DEBUG
    # The benign connect failure produced no WARNING (the whole point of the fix).
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_watch_for_rotation_other_http_error_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-connect ``httpx.HTTPError`` (e.g. ``ReadTimeout``) still logs WARNING.

    A hung-but-listening port that raises ``ReadTimeout`` is a real fault, not the
    benign connection-refused case, so it must keep WARNING (the broad arm is
    unchanged) while the loop still retries on the next tick.
    """
    script = await _watch_until_cancelled(monkeypatch, caplog, httpx.ReadTimeout("read timed out"))

    assert script.calls == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "GetAllCascadeTrajectories failed" in warnings[0].getMessage()


def _rotation_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    calls: list[tuple[str, str, dict[str, object]]],
    snapshot: dict[str, object],
    new_session_id: str = "conv_new",
) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient over a MockTransport that records the rotation calls.

    Records every ``(method, path, json_body)`` so a test can assert the exact
    session-rotation API sequence the codex forwarder mirrors. The session-create
    POST returns ``{"id": new_session_id}``; the GET returns ``snapshot``; PATCHes
    and the terminal transfer return 200.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, object] = {}
        if request.content:
            with contextlib.suppress(ValueError):
                parsed = json.loads(request.content)
                if isinstance(parsed, dict):
                    body = parsed
        calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json=snapshot)
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": new_session_id})
        return httpx.Response(200, json={"id": "terminal_antigravity_main"})

    return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_rotate_session_for_cascade_mirrors_claude_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_rotate_session_for_cascade`` runs claude's exact session-rotation sequence.

    Asserts: GET old snapshot → POST /v1/sessions (agent_id + inherited labels) →
    PATCH runner_id → POST terminal transfer → PATCH old runner_id="" — and that
    bridge state is rewritten with the new session id + new cascade id. Crucially,
    NO ``external_session_id`` PATCH is made: agy is one long-lived process hosting
    many cascades, so the new cascade is already live (reached via the rewritten
    bridge state, not a later ``--resume``), exactly as claude's
    ``_create_clear_replacement_session`` makes no such PATCH. The old code PATCHed
    it, which 400'd on the auto-cold-started session and looped the rotation.
    """
    from omnigent.antigravity_native_bridge import (
        ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
        read_bridge_state,
    )

    bridge_dir = _bridge_dir(tmp_path)
    new_cascade = "99999999-8888-7777-6666-555555555555"
    snapshot: dict[str, object] = {
        "agent_id": "agent_xyz",
        "runner_id": "runner_abc",
        "labels": {ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY: "bridge-123"},
    }
    calls: list[tuple[str, str, dict[str, object]]] = []
    async with _rotation_client(monkeypatch, calls=calls, snapshot=snapshot) as client:
        new_session_id = await reader._rotate_session_for_cascade(
            client=client,
            old_session_id=_SESSION_ID,
            new_cascade_id=new_cascade,
            bridge_dir=bridge_dir,
        )

    assert new_session_id == "conv_new"
    # The exact ordered API sequence (method, path) mirroring claude rotation —
    # ONE PATCH on the new session (runner_id bind), then transfer, then release.
    methods_paths = [(m, p) for (m, p, _b) in calls]
    assert methods_paths == [
        ("GET", f"/v1/sessions/{_SESSION_ID}"),
        ("POST", "/v1/sessions"),
        ("PATCH", "/v1/sessions/conv_new"),  # runner_id bind
        (
            "POST",
            f"/v1/sessions/{_SESSION_ID}/resources/terminals/terminal_antigravity_main/transfer",
        ),
        ("PATCH", f"/v1/sessions/{_SESSION_ID}"),  # release old runner
    ]
    # No external_session_id PATCH is made anywhere (the loop-bug source): every
    # PATCH body is a runner_id bind/release, never an external_session_id write.
    assert all("external_session_id" not in body for (_m, _p, body) in calls), (
        f"rotation must not PATCH external_session_id (claude parity); calls={calls!r}"
    )
    # The create POST inherited the old agent_id + bridge-id label (so the new
    # session resolves to the same bridge_dir).
    create_body = calls[1][2]
    assert create_body["agent_id"] == "agent_xyz"
    assert isinstance(create_body["labels"], dict)
    assert create_body["labels"][ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY] == "bridge-123"
    # The runner_id bind carried the old runner; the new session is owned by it.
    assert calls[2][2] == {"runner_id": "runner_abc"}
    # The terminal transfer targeted the new session (the SAME agy moves over).
    assert calls[3][2] == {"target_session_id": "conv_new"}
    # Old runner released.
    assert calls[4][2] == {"runner_id": ""}
    # Bridge state was rewritten to the new session + new cascade (the reader
    # rebinds to the new cascade on the SAME agy via this shared bridge_dir).
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.session_id == "conv_new"
    assert state.conversation_id == new_cascade
    assert state.active_turn_id is None


@pytest.mark.asyncio
async def test_rotate_session_for_cascade_returns_none_on_create_failure(
    tmp_path: Path,
) -> None:
    """A failed session-create yields ``None`` and does NOT rewrite bridge state.

    The replacement could not be created, so the reader must stay on the old
    binding: ``_rotate_session_for_cascade`` returns ``None`` and bridge state still
    names the OLD cascade (no half-rotation).
    """
    from omnigent.antigravity_native_bridge import read_bridge_state

    bridge_dir = _bridge_dir(tmp_path)
    snapshot: dict[str, object] = {"agent_id": "agent_xyz", "runner_id": "runner_abc"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=snapshot)
        # The create POST fails (500) — rotation must abort cleanly.
        return httpx.Response(500, json={"error": {"message": "boom"}})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        result = await reader._rotate_session_for_cascade(
            client=client,
            old_session_id=_SESSION_ID,
            new_cascade_id="new-cascade-xyz",
            bridge_dir=bridge_dir,
        )

    assert result is None
    # Bridge state untouched: still the original cascade id.
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.conversation_id == _CASCADE_ID


@pytest.mark.asyncio
async def test_run_reader_with_bridge_rebinds_after_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rebind loop: supervise_reader signals rotation → rotate → re-bind once.

    Mocks ``supervise_reader`` to return a new cascade id on the FIRST call and
    ``None`` on the second (the rebound run ending), and ``_rotate_session_for_cascade``
    to create a replacement session. Asserts: the replacement session was created
    for the detected cascade, and the SECOND supervise_reader run was driven with
    the NEW (rotated) session id — proving the loop rebinds and advances ownership.
    """
    new_cascade = "abcdef00-1111-2222-3333-444444444444"
    new_session = "conv_rotated"
    supervise_session_ids: list[str] = []
    rotate_calls: list[tuple[str, str]] = []

    async def _fake_supervise(
        bridge_dir: Path,
        session_id: str,
        *,
        client: object,
        on_pending_interaction: object,
        skip_cascade_ids: frozenset[str] = frozenset(),
        committed_steps_out: list[int] | None = None,
        **_kwargs: object,
    ) -> str | None:
        supervise_session_ids.append(session_id)
        # Model a GENUINE /clear: the bound cascade HAD committed turns, so the
        # loop FORKS a replacement session (not the first-cascade adopt-in-place
        # path, which fires only when the bound cascade committed zero turns).
        if committed_steps_out is not None:
            committed_steps_out.append(3)
        # First run detects a rotation; the rebound second run ends normally.
        return new_cascade if len(supervise_session_ids) == 1 else None

    async def _fake_rotate(
        *,
        client: object,
        old_session_id: str,
        new_cascade_id: str,
        bridge_dir: Path,
    ) -> str | None:
        rotate_calls.append((old_session_id, new_cascade_id))
        return new_session

    monkeypatch.setattr(reader, "supervise_reader", _fake_supervise)
    monkeypatch.setattr(reader, "_rotate_session_for_cascade", _fake_rotate)
    # Avoid importing the heavy interaction-bridge module in this unit test.
    monkeypatch.setattr(
        "omnigent.antigravity_native_interactions.bridge_interaction",
        lambda *a, **k: None,
    )

    await reader.run_reader_with_bridge(
        base_url="http://test",
        headers={},
        auth=None,
        session_id=_SESSION_ID,
        bridge_dir=_bridge_dir(tmp_path),
    )

    # Exactly one rotation, for the detected cascade, off the original session.
    assert rotate_calls == [(_SESSION_ID, new_cascade)]
    # supervise_reader ran twice: first on the original session, then rebound on
    # the new (rotated) session id.
    assert supervise_session_ids == [_SESSION_ID, new_session]


@pytest.mark.asyncio
async def test_run_reader_with_bridge_adopts_first_cascade_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-cascade adoption: a rotation off a ZERO-turn bound cascade rebinds in place.

    The cold-start ``StartCascade`` cascade is a headless placeholder the agy TUI
    never shows; the TUI mints its OWN cascade on the first typed turn. That first
    transition is the conversation STARTING, not a ``/clear`` — so the loop must
    adopt the new cascade in the SAME Omnigent session (rewrite bridge state, NO
    fork) so the user's current session starts mirroring (#1156/#1158). Modeled by
    a supervise_reader that reports ZERO committed turns on the rotation run.
    """
    new_cascade = "11111111-2222-3333-4444-555555555555"
    supervise_session_ids: list[str] = []
    rotate_calls: list[str] = []

    async def _fake_supervise(
        bridge_dir: Path,
        session_id: str,
        *,
        client: object,
        on_pending_interaction: object,
        skip_cascade_ids: frozenset[str] = frozenset(),
        committed_steps_out: list[int] | None = None,
        **_kwargs: object,
    ) -> str | None:
        supervise_session_ids.append(session_id)
        # The bound cascade committed ZERO turns → first-cascade adoption.
        if committed_steps_out is not None:
            committed_steps_out.append(0)
        return new_cascade if len(supervise_session_ids) == 1 else None

    async def _fake_rotate(**_kwargs: object) -> str | None:
        rotate_calls.append("forked")  # must NOT happen on adopt-in-place
        return "conv_should_not_be_used"

    recorded_external: list[tuple[str, str]] = []

    async def _fake_record_external(client: object, session_id: str, cascade_id: str) -> None:
        recorded_external.append((session_id, cascade_id))

    monkeypatch.setattr(reader, "supervise_reader", _fake_supervise)
    monkeypatch.setattr(reader, "_rotate_session_for_cascade", _fake_rotate)
    monkeypatch.setattr(reader, "_record_external_session_id", _fake_record_external)
    monkeypatch.setattr(
        "omnigent.antigravity_native_interactions.bridge_interaction",
        lambda *a, **k: None,
    )

    bridge_dir = _bridge_dir(tmp_path)
    await reader.run_reader_with_bridge(
        base_url="http://test",
        headers={},
        auth=None,
        session_id=_SESSION_ID,
        bridge_dir=bridge_dir,
    )

    # NO fork: _rotate_session_for_cascade must never be called.
    assert rotate_calls == []
    # Both supervise runs stay on the SAME (original) session id; the second
    # rebinds to the adopted cascade via the rewritten bridge state.
    assert supervise_session_ids == [_SESSION_ID, _SESSION_ID]
    # Bridge state now names the adopted cascade under the SAME session.
    state = reader.read_bridge_state(bridge_dir)
    assert state is not None
    assert state.session_id == _SESSION_ID
    assert state.conversation_id == new_cascade
    # The adopted cascade is recorded as external_session_id so a later --resume /
    # server restart loads THIS conversation, not the headless cold-start phantom
    # (#2 data-loss). It is recorded against the SAME session.
    assert recorded_external == [(_SESSION_ID, new_cascade)]


@pytest.mark.asyncio
async def test_run_reader_with_bridge_keeps_old_binding_when_rotation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rotation keeps the old binding and skips the failed cascade.

    When ``_rotate_session_for_cascade`` returns ``None``, the loop must re-enter
    supervise_reader on the SAME (old) session id with the failed cascade in
    ``skip_cascade_ids`` — so it keeps serving rather than losing the reader, and
    never hot-loops on the un-rotatable cascade.
    """
    failed_cascade = "00000000-9999-8888-7777-666666666666"
    supervise_calls: list[tuple[str, frozenset[str]]] = []

    async def _fake_supervise(
        bridge_dir: Path,
        session_id: str,
        *,
        client: object,
        on_pending_interaction: object,
        skip_cascade_ids: frozenset[str] = frozenset(),
        committed_steps_out: list[int] | None = None,
        **_kwargs: object,
    ) -> str | None:
        supervise_calls.append((session_id, skip_cascade_ids))
        # Genuine /clear (bound cascade had committed turns) → the loop attempts a
        # FORK (which fails here), not the zero-turn adopt-in-place path.
        if committed_steps_out is not None:
            committed_steps_out.append(2)
        # First run detects the rotation; the second (post-failure) run ends.
        return failed_cascade if len(supervise_calls) == 1 else None

    async def _fake_rotate_fail(
        *,
        client: object,
        old_session_id: str,
        new_cascade_id: str,
        bridge_dir: Path,
    ) -> str | None:
        return None  # rotation fails

    monkeypatch.setattr(reader, "supervise_reader", _fake_supervise)
    monkeypatch.setattr(reader, "_rotate_session_for_cascade", _fake_rotate_fail)
    monkeypatch.setattr(
        "omnigent.antigravity_native_interactions.bridge_interaction",
        lambda *a, **k: None,
    )

    await reader.run_reader_with_bridge(
        base_url="http://test",
        headers={},
        auth=None,
        session_id=_SESSION_ID,
        bridge_dir=_bridge_dir(tmp_path),
    )

    # Two runs, both on the ORIGINAL session id (rotation failed, no advance); the
    # second carries the failed cascade in skip_cascade_ids.
    assert len(supervise_calls) == 2
    assert supervise_calls[0] == (_SESSION_ID, frozenset())
    assert supervise_calls[1] == (_SESSION_ID, frozenset({failed_cascade}))


class _BlockingStream:
    """A ``stream_agent_state_updates`` that blocks forever after one frame.

    Models the live deadlock shape: the connect stream long-polls inside a
    deadline-less ``aiter_bytes`` read with no further frame and no trailer (after
    a ``/clear`` the bound cascade goes idle). The generator awaits an
    ``asyncio.Event`` that never fires, so the cooperative ``stop`` re-check the
    body would do between stream re-entries / poll iterations is NEVER reached —
    only cancellation can unwedge it. ``await``-ing the event (rather than a true
    busy-hang) keeps the generator cancellable so the test can't actually hang.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = False

    def __call__(self, port: int, conversation_id: str) -> AsyncIterator[dict[str, object]]:
        self.calls += 1

        async def _gen() -> AsyncIterator[dict[str, object]]:
            never = asyncio.Event()
            try:
                await never.wait()  # blocks until cancelled — never set
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            yield {}  # pragma: no cover  (unreachable; marks this an async gen)

        return _gen()


@pytest.mark.asyncio
async def test_supervise_reader_actuates_rotation_when_stream_is_wedged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A rotation detected while the stream is BLOCKED still returns the new id.

    Regression test for the T-G actuation deadlock: the stream blocks forever on a
    deadline-less idle read (the live ``/clear``-then-idle shape), so the body's
    cooperative ``_body_should_stop`` checkpoint is never reached. The rotation
    detector must therefore CANCEL the wedged body task — not merely flip ``stop``
    — so ``supervise_reader`` returns the new cascade id instead of hanging.

    Before the fix this would hang (the body ``await``-ed the wedged stream
    directly); the tight :func:`asyncio.wait_for` budget makes a regression fail
    loudly as a timeout rather than wedging the suite. The poll fallback is never
    reached (the stream never raises), so its step source is asserted untouched.
    """
    new_cascade = "deadlock-1111-2222-3333-444444444444"
    blocking = _BlockingStream()
    poll = _StepScript([[]])
    monkeypatch.setattr(
        reader, "get_all_cascade_trajectories", lambda port: _rotation_body(new_cascade)
    )
    monkeypatch.setattr(reader, "stream_agent_state_updates", blocking)
    monkeypatch.setattr(reader, "get_trajectory_steps", poll)
    monkeypatch.setattr(reader, "post_session_event_with_retry", _PostSink())

    async def _noop_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    async def _noop_pending(_cid: str, _port: int, _pending: PendingInteraction) -> None:
        return None

    # No bounded ``stop``: the ONLY thing that can end this run is the rotation
    # cancelling the wedged body. A tight timeout turns a regression (hang) into a
    # loud failure instead of a stuck suite.
    result = await asyncio.wait_for(
        reader.supervise_reader(
            _bridge_dir(tmp_path),
            _SESSION_ID,
            client=cast(httpx.AsyncClient, object()),
            on_pending_interaction=cast(Any, _noop_pending),
            poll_interval_s=0.0,
            detect_rotation_interval_s=0.0,
        ),
        timeout=5,
    )

    assert result == new_cascade
    # The wedged stream was interrupted by cancellation (the actuation path), and
    # the poll fallback was never reached (the stream never errored).
    assert blocking.cancelled is True
    assert poll.calls == 0


@pytest.mark.asyncio
async def test_supervise_reader_external_cancel_propagates_not_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """An external cancel of a wedged reader propagates — never a phantom rotation.

    With NO rotation pending, cancelling the ``supervise_reader`` task (a shutdown)
    must raise :class:`asyncio.CancelledError` out of it rather than being mistaken
    for a rotation and returning a (non-existent) new cascade id. The autouse
    ``no_rotation`` fixture keeps the detector quiet, so the only way the run ends
    is the external cancel.
    """
    blocking = _BlockingStream()
    monkeypatch.setattr(reader, "stream_agent_state_updates", blocking)
    monkeypatch.setattr(reader, "get_trajectory_steps", _StepScript([[]]))
    monkeypatch.setattr(reader, "post_session_event_with_retry", _PostSink())

    async def _noop_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    async def _noop_pending(_cid: str, _port: int, _pending: PendingInteraction) -> None:
        return None

    task = asyncio.create_task(
        reader.supervise_reader(
            _bridge_dir(tmp_path),
            _SESSION_ID,
            client=cast(httpx.AsyncClient, object()),
            on_pending_interaction=cast(Any, _noop_pending),
            poll_interval_s=0.0,
            detect_rotation_interval_s=0.0,
        )
    )
    # Let the reader discover + start the body and wedge on the blocking stream.
    while blocking.calls == 0:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

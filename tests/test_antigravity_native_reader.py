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
        self.urls: list[str] = []

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
        self.urls.append(url)
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
        seen=set(),
        interacted=set(),
        port=_PORT,
    )
    gate1 = _load("run_command_waiting")  # stepIndex 6 (the `pwd` segment)
    gate2 = copy.deepcopy(gate1)  # a distinct later gate (the `ls` segment)
    gate2["metadata"]["sourceTrajectoryStepInfo"]["stepIndex"] = 8
    gate2["runCommand"]["commandLine"] = "ls"
    # The freshest snapshot the re-scan re-reads. gate1 still reads WAITING here —
    # the dedup-race case: its verdict landed but the DONE transition has not yet
    # propagated to the snapshot, so ``state.interacted`` (not status) is what stops
    # it re-firing. gate2 is the newly-arrived, deferred gate.
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
async def test_clear_slot_rescan_empty_then_stream_frame_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case B (#1472 review): the clear's re-scan finds NOTHING because gate-2 is
    not WAITING yet (agy must run segment 1 first). The re-scan must then leave the
    slot OPEN so gate-2's later live stream frame surfaces it directly — proving the
    single-shot re-scan does not strand a gate that arrives after it runs.
    """
    state = reader._ReaderState(
        seen=set(),
        interacted=set(),
        port=_PORT,
    )
    gate1 = _load("run_command_waiting")  # stepIndex 6
    gate1_done = copy.deepcopy(gate1)  # answered → DONE, no longer WAITING
    gate1_done["status"] = "CORTEX_STEP_STATUS_DONE"
    gate1_done.pop("requestedInteraction", None)
    gate2 = copy.deepcopy(gate1)  # the next segment — arrives only LATER
    gate2["metadata"]["sourceTrajectoryStepInfo"]["stepIndex"] = 8
    gate2["runCommand"]["commandLine"] = "ls"
    # Re-scan snapshot: gate-1 resolved (DONE), gate-2 not present yet → the re-scan
    # surfaces nothing and must leave the slot open.
    monkeypatch.setattr(reader, "get_trajectory_steps", lambda _port, _cascade_id: [gate1_done])

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
    await _drain_interactions(state)  # re-scan runs, finds no pending gate

    assert fired == [6]  # only gate-1 so far
    assert state.interaction_task is None  # slot OPEN — re-scan neither hung nor held it

    # gate-2 now arrives as a live stream frame: with the slot open the guard passes
    # and it surfaces directly (the backstop that makes the single-shot re-scan safe).
    reader._maybe_handle_interaction(
        gate2,
        key=reader._step_key(gate2),
        cascade_id=_CASCADE_ID,
        state=state,
        on_pending_interaction=cast(Any, _quick),
    )
    second = state.interaction_task
    assert second is not None
    await second
    await _drain_interactions(state)

    assert fired == [6, 8]  # gate-2 surfaced via the live frame, not lost


@pytest.mark.asyncio
async def test_rescan_skips_auto_allowed_step_and_surfaces_next_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ALREADY-ALLOWED segment in a chain (e.g. ``rm a && echo hi && rm b`` with
    ``echo`` auto-allowed) runs WITHOUT a gate — a DONE step with no
    ``requestedInteraction`` — so it must be transparent to the re-scan: skipped (not
    surfaced), and the NEXT real gate after it still surfaces. Guards the edge case
    where not every chained command is permission-gated.
    """
    state = reader._ReaderState(
        seen=set(),
        interacted=set(),
        port=_PORT,
    )
    gate1 = _load("run_command_waiting")  # stepIndex 6 — the `rm a` gate
    gate1_done = copy.deepcopy(gate1)
    gate1_done["status"] = "CORTEX_STEP_STATUS_DONE"
    gate1_done.pop("requestedInteraction", None)
    allowed = copy.deepcopy(gate1)  # the auto-allowed `echo hi` — ran, NEVER gated
    allowed["metadata"]["sourceTrajectoryStepInfo"]["stepIndex"] = 7
    allowed["runCommand"]["commandLine"] = "echo hi"
    allowed["status"] = "CORTEX_STEP_STATUS_DONE"
    allowed.pop("requestedInteraction", None)  # no interaction → never a delivery target
    gate2 = copy.deepcopy(gate1)  # the next real gate `rm b`
    gate2["metadata"]["sourceTrajectoryStepInfo"]["stepIndex"] = 8
    gate2["runCommand"]["commandLine"] = "rm b"
    # Re-scan snapshot: gate-1 resolved, the allowed command DONE (no gate), gate-2 WAITING.
    monkeypatch.setattr(
        reader, "get_trajectory_steps", lambda _port, _cascade_id: [gate1_done, allowed, gate2]
    )

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
    await _drain_interactions(state)

    assert fired == [6, 8]  # gate-1, then gate-2 — the allowed step (7) was NEVER surfaced
    assert reader._step_key(allowed) not in state.interacted  # not an interaction at all


@pytest.mark.asyncio
async def test_resurface_pending_interaction_swallows_poll_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-scan whose steps re-read fails on EVERY bounded attempt is logged and
    swallowed, not raised — and the read is retried, not given up after one shot
    (#1472 review: the re-scan is the sole backstop on the healthy-stream path)."""
    calls = 0

    def _boom(_port: int, _cascade_id: str) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused")

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(reader, "get_trajectory_steps", _boom)
    monkeypatch.setattr(reader, "_sleep", _noop_sleep)  # no real backoff in the test
    state = reader._ReaderState(
        seen=set(),
        interacted=set(),
        port=_PORT,
    )
    fired: list[int] = []

    async def _quick(_cascade_id: str, _port: int, pending: PendingInteraction) -> None:
        fired.append(pending["step_index"])

    # Must not raise despite the read failing on every attempt.
    await reader._resurface_pending_interaction(
        cascade_id=_CASCADE_ID,
        state=state,
        on_pending_interaction=cast(Any, _quick),
    )

    assert calls == reader._INTERACTION_RESCAN_POLL_ATTEMPTS  # retried, not one-shot
    assert fired == []  # no gate surfaced
    assert state.interaction_task is None  # no bridge spawned


@pytest.mark.asyncio
async def test_resurface_pending_interaction_retries_transient_poll_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TRANSIENT re-scan poll error is retried and the deferred gate STILL surfaces
    on a later attempt (#1472 review): the re-scan is the only backstop on the
    healthy-stream path, so it must not abandon the gate after one failed read."""
    gate = _load("run_command_waiting")  # stepIndex 6 — the deferred gate
    failed_once = False

    def _flaky(_port: int, _cascade_id: str) -> list[dict[str, Any]]:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise httpx.ConnectError("refused")  # transient blip on the first read
        return [gate]

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(reader, "get_trajectory_steps", _flaky)
    monkeypatch.setattr(reader, "_sleep", _noop_sleep)  # no real backoff in the test
    state = reader._ReaderState(
        seen=set(),
        interacted=set(),
        port=_PORT,
    )
    fired: list[int] = []

    async def _quick(_cascade_id: str, _port: int, pending: PendingInteraction) -> None:
        fired.append(pending["step_index"])

    await reader._resurface_pending_interaction(
        cascade_id=_CASCADE_ID,
        state=state,
        on_pending_interaction=cast(Any, _quick),
    )
    await _drain_interactions(state)

    assert failed_once  # the first read raised and was retried, not abandoned
    assert fired == [6]  # the gate surfaced despite the transient blip


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
async def test_stream_done_closes_the_live_block_with_a_final_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A committed planner step closes its stream with ``final=True``.

    The server retires a native in-flight message only when it has seen a
    ``final: true`` delta AND the joined deltas are byte-equal to the committed
    text (``omnigent.runtime.inflight_text``). agy emitted neither: every delta
    was ``final=False``, and the last growth increment before DONE was never
    sent, so the buffered text stayed truncated.

    The entry therefore lived forever and was replayed to EVERY new subscriber —
    a page load or the next turn re-rendered a stale, cut-off copy of an earlier
    answer beside the current one (the duplicated + truncated responses).

    So on commit the reader must flush the remaining suffix and mark the stream
    final, leaving the buffered text byte-equal to what it commits.
    """
    full = "Hello there, friend."
    frames = [
        # The last GENERATING frame is BEHIND the committed text — the real
        # trajectory shape, and the reason the buffer stayed truncated.
        _frame([_generating_planner("Hello there,")]),
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

    deltas = sink.deltas()
    assert deltas, "expected deltas"
    # The stream is closed exactly once, and only by the last delta.
    finals = [d for d in deltas if d.get("final") is True]
    assert len(finals) == 1, f"expected exactly one final delta, got {len(finals)}"
    assert deltas[-1].get("final") is True
    # Byte-equality with the committed text is the server's other retirement
    # condition, so the closing delta must carry the missing suffix.
    assert "".join(cast(str, d["delta"]) for d in deltas) == full
    # Still exactly one committed message, still after the deltas.
    assert sink.item_types() == ["message"]
    types = sink.event_types()
    assert max(i for i, t in enumerate(types) if t == "external_output_text_delta") < types.index(
        "external_conversation_item"
    )


@pytest.mark.asyncio
async def test_a_transient_shrink_does_not_duplicate_text_or_strand_the_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A rewrite that shrinks and then RE-GROWS still streams the answer exactly.

    The prefix tracker is the reader's record of what it already put on the wire,
    and the closing delta computes its suffix from it. Re-anchoring it on a frame
    that emitted NOTHING breaks that: the tracker moves to text the server never
    received, so the next real delta is cut from the wrong offset and repeats the
    overlap — the live block renders duplicated text, and the joined deltas no
    longer equal the committed message, which is one of the server's two
    retirement conditions, so the preview is replayed to every later subscriber.

    Companion to the shrink-to-a-non-prefix case below: that one the producer
    cannot fully close (published deltas cannot be unsent), this one it can — the
    text re-grows past what was sent, so tracking sent-so-far is exactly enough.
    """
    from omnigent.runtime import inflight_text

    full = "The plan is complete."
    frames = [
        _frame([_generating_planner("The plan is ")]),
        # Moderation momentarily replaces the partial with a SHORTER one, then
        # the answer grows past it again.
        _frame([_generating_planner("The plan ")]),
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

    streamed = "".join(cast(str, d["delta"]) for d in sink.deltas())
    assert streamed == full, (
        f"the streamed text must equal the committed answer, not repeat an "
        f"overlap; got {streamed!r}"
    )

    inflight_text.reset_for_tests()
    conv = "conv_transient_shrink"
    for event_type, data in sink.posts:
        if event_type == "external_output_text_delta":
            inflight_text.record_publish(conv, {"type": "response.output_text.delta", **data})
        elif event_type == "external_conversation_item":
            item = cast(dict[str, Any], data.get("item_data") or {})
            if item.get("role") == "assistant":
                inflight_text.record_publish(
                    conv,
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "id": "ap_1",
                            "type": "message",
                            "role": "assistant",
                            "content": item.get("content"),
                        },
                    },
                )

    leftover = [
        s
        for s in inflight_text.snapshot_for(conv)
        if s.get("type") == "response.output_text.delta"
    ]
    assert leftover == [], f"stale text would be replayed to the next subscriber: {leftover}"


@pytest.mark.asyncio
async def test_a_shrinking_rewrite_mid_stream_still_closes_the_live_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A rewrite that SHRINKS the answer still delivers the closing delta.

    agy can replace a growing ``modifiedResponse`` with a shorter, non-prefix
    rewrite. The reader emits no delta for it (there is no new suffix) but does
    re-anchor its tracker to the new text, so an index derived from the forwarded
    byte count MOVES BACKWARDS — and the server drops any chunk whose index does
    not exceed the last accepted one. The closing ``final`` chunk was therefore
    discarded and the block never closed. Indices are a per-step chunk count for
    exactly this reason: growth cannot make a counter go backwards.

    Scoped to what the producer controls. The server ALSO retires only on text
    byte-equal to the commit, and deltas already published cannot be unsent, so a
    shrink still leaves a stale preview — that half needs a reset signal in
    ``inflight_text`` and is not fixable from here.
    """
    rewritten = "Redacted."
    frames = [
        _frame([_generating_planner("The answer is ")]),
        _frame([_generating_planner("The answer is quite long ")]),
        # Moderation replaces the answer: shorter, and not a prefix of it.
        _frame([_generating_planner(rewritten)]),
        _frame([_done_planner(rewritten)]),
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
    indices = [cast(int, d["index"]) for d in deltas]
    assert indices == sorted(set(indices)), (
        f"indices must strictly increase across a rewrite, got {indices}"
    )
    # The closer is what marks the block retirable, so it must survive the
    # server's index check rather than being silently dropped.
    finals = [d for d in deltas if d.get("final") is True]
    assert len(finals) == 1, f"expected exactly one final delta, got {len(finals)}"
    assert deltas[-1] is finals[0], "the final delta must be the last one sent"
    assert cast(int, finals[0]["index"]) > max(
        cast(int, d["index"]) for d in deltas if d.get("final") is not True
    ), "the closing chunk must outrank every chunk before it or the server drops it"


@pytest.mark.asyncio
async def test_multi_chunk_answer_leaves_nothing_in_flight_on_the_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A long answer streamed over MANY frames still retires server-side.

    The production shape: agy grows ``modifiedResponse`` across many frames, so
    the message is several chunks. Every chunk carried ``index: 0``, and the
    server drops any chunk whose index does not exceed the last accepted one —
    so only the FIRST survived. The buffer kept a truncated prefix, the
    ``final`` chunk was discarded, and the message was replayed to every later
    subscriber as a cut-off duplicate of the answer just committed.

    A single-chunk answer hid this: its one chunk IS the whole text.
    """
    from omnigent.runtime import inflight_text

    growth = ["Kyoto ", "Kyoto Studio ", "Kyoto Studio is ", "Kyoto Studio is a studio app."]
    full = growth[-1]
    sink = _PostSink()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(
            [_frame([_generating_planner(t)]) for t in growth] + [_frame([_done_planner(full)])]
        ),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    # More than one chunk, and every index strictly increases.
    deltas = sink.deltas()
    assert len(deltas) > 1, "expected a multi-chunk stream"
    indices = [cast(int, d["index"]) for d in deltas]
    assert indices == sorted(set(indices)), f"indices must strictly increase, got {indices}"

    inflight_text.reset_for_tests()
    conv = "conv_multi_chunk"
    for event_type, data in sink.posts:
        if event_type == "external_output_text_delta":
            inflight_text.record_publish(conv, {"type": "response.output_text.delta", **data})
        elif event_type == "external_conversation_item":
            item = cast(dict[str, Any], data.get("item_data") or {})
            if item.get("role") == "assistant":
                inflight_text.record_publish(
                    conv,
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "id": "ap_1",
                            "type": "message",
                            "role": "assistant",
                            "content": item.get("content"),
                        },
                    },
                )

    leftover = [
        s
        for s in inflight_text.snapshot_for(conv)
        if s.get("type") == "response.output_text.delta"
    ]
    assert leftover == [], f"stale text would be replayed to the next subscriber: {leftover}"


@pytest.mark.asyncio
async def test_committed_turn_leaves_nothing_in_flight_on_the_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """After a committed turn the server has NOTHING left to replay.

    This is the invariant behind the duplicated/cut-off responses: the server
    replays any un-retired in-flight message to every new subscriber, so a
    leftover entry re-renders a stale copy of an earlier answer beside the
    current one on the next page load or turn.

    Asserted end-to-end by feeding the reader's own emitted events through
    ``inflight_text`` — the real consumer — rather than by counting deltas,
    because the reader commits from two paths (stream frames and the poll loop)
    and their interleaving is not fixed. Whatever the order, nothing may
    survive the turn.
    """
    from omnigent.runtime import inflight_text

    full = "apple banana cherry"
    sink = _PostSink()

    class _DeltaThenDrop:
        """Streams a partial, then drops — the reader falls back to polling,
        which is where the commit lands. This interleaving is the one that
        regressed: the close lived only in the stream handler."""

        def __call__(self, port: int, conversation_id: str) -> AsyncIterator[dict[str, object]]:
            async def _gen() -> AsyncIterator[dict[str, object]]:
                yield _frame([_generating_planner("apple banana")])
                raise httpx.ReadError("mid-stream drop")

            return _gen()

    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_DeltaThenDrop(),
        poll_steps=_StepScript([[_done_planner(full)], [_done_planner(full)]]),
        monkeypatch=monkeypatch,
        iterations=2,
    )

    inflight_text.reset_for_tests()
    conv = "conv_under_test"
    for event_type, data in sink.posts:
        if event_type == "external_output_text_delta":
            # Pass the emitted payload VERBATIM. Reconstructing it by hand is
            # what hid this bug: omitting ``index`` skipped the server's
            # strictly-increasing-index check, so the test retired messages the
            # real server would never retire.
            inflight_text.record_publish(
                conv,
                {"type": "response.output_text.delta", **data},
            )
        elif event_type == "external_conversation_item":
            item = cast(dict[str, Any], data.get("item_data") or {})
            if item.get("role") == "assistant":
                inflight_text.record_publish(
                    conv,
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "id": "ap_1",
                            "type": "message",
                            "role": "assistant",
                            "content": item.get("content"),
                        },
                    },
                )

    leftover = [
        s
        for s in inflight_text.snapshot_for(conv)
        if s.get("type") == "response.output_text.delta"
    ]
    assert leftover == [], f"stale text would be replayed to the next subscriber: {leftover}"


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

    # The pre-error delta was forwarded, and the poll-path commit CLOSED the
    # live block: it flushes the missing suffix and marks the stream final.
    # Without that the server holds a truncated entry in flight for the rest of
    # the session and replays it to every later subscriber.
    assert [d["delta"] for d in sink.deltas()] == [full, ", now complete."]
    assert sink.deltas()[-1]["final"] is True
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


@pytest.mark.asyncio
async def test_a_streamed_tool_turn_stays_running_until_the_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """
    REGRESSION: the spinner must not clear when agy dispatches a tool.

    The whole streamed turn, in the live order: the user's prompt, the planner
    that dispatches a tool, the tool's result, then the answering planner. IDLE
    belongs at the END of that sequence — one edge, after the answer.

    The predicate test below pins the cause; this pins the symptom the user
    actually saw, which is what the suite was missing: the dispatching planner
    fired IDLE, so the session showed idle while its tools kept running, and
    RUNNING could not re-open (it is gated on USER_INPUT).
    """
    sink = _PostSink()
    await _run_stream(
        bridge_dir=_bridge_dir(tmp_path),
        sink=sink,
        stream=_FrameScript(
            [
                _frame([_load("user_input")]),
                _frame([_load("stream_planner_tool_call")]),
                _frame([_load("stream_run_command_done")]),
                _frame([_load("stream_planner_text_done")]),
            ]
        ),
        poll_steps=_StepScript([[]]),
        monkeypatch=monkeypatch,
        iterations=1,
    )

    assert sink.statuses() == ["running", "idle"], (
        f"the turn must open once and close once; got {sink.statuses()}"
    )
    # WHERE the idle edge lands is the whole point — the count alone is
    # identical whether it fires at dispatch or after the answer, because the
    # second close is deduped by ``turn_active``. Assert its position.
    timeline = [
        f"status:{data['status']}"
        if event_type == "external_session_status"
        else f"item:{data.get('item_type')}"
        for event_type, data in sink.posts
        if event_type in ("external_session_status", "external_conversation_item")
    ]
    assert timeline.index("status:idle") > timeline.index("item:function_call_output"), (
        f"IDLE fired before the tool finished — the spinner cleared mid-turn: {timeline}"
    )


def test_turn_close_false_for_streamed_planner_that_dispatched_a_tool() -> None:
    """
    REGRESSION: a STREAMED tool dispatch must not fire the IDLE edge.

    The stream projection strips ``plannerResponse.toolCalls`` (the premise of
    this whole mapper rewrite), so a dispatching planner arrives DONE with
    neither tool calls nor text. Keying "did this dispatch?" on ``toolCalls``
    therefore read it as a degenerate close and fired IDLE the moment agy called
    a tool: the spinner cleared mid-turn and RUNNING could not re-open (it is
    gated on USER_INPUT), so the session read idle while the tools still ran.

    Uses the verbatim live stream frame rather than a hand-built dict — the
    hand-built poll shape below is exactly what let the bug through.
    """
    dispatched = _load("stream_planner_tool_call")
    # Pin the premise, so this test fails loudly if a future capture regains the
    # field rather than silently passing for the wrong reason.
    assert "toolCalls" not in dispatched["plannerResponse"], (
        "fixture no longer models the stripped stream projection"
    )
    assert reader._is_turn_close_step(dispatched) is False


def test_turn_close_true_for_streamed_planner_that_answered() -> None:
    """The streamed ANSWER still closes the turn — text is the close signal."""
    answered = _load("stream_planner_text_done")
    assert reader._is_turn_close_step(answered) is True


def test_turn_close_false_for_done_planner_no_text_no_tools() -> None:
    """
    A DONE planner with neither text nor tool calls does NOT close the turn.

    This shape is byte-identical to a streamed tool dispatch, so it cannot be
    read as a close without stranding every streamed tool turn (see above). A
    genuinely degenerate turn is instead reconciled by the idle backstop
    (``_close_turn_if_stranded``), which closes on agy's OWN cascade status —
    authoritative where this shape is ambiguous, at the cost of one detector
    interval.
    """
    empty_done = {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_DONE",
        "plannerResponse": {},
    }
    assert reader._is_turn_close_step(empty_done) is False


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


def _child_summary(
    *,
    parent_cascade_id: str,
    last_user_input_time: str = "2026-08-01T10:48:37.887431Z",
    include_parent_id: bool = True,
) -> dict[str, Any]:
    """Build a real agy SUBAGENT summary (live capture, agy 1.1.9).

    Captured from a live ``GetAllCascadeTrajectories`` while agy ran a subagent:
    the child is typed ``CORTEX_TRAJECTORY_TYPE_CASCADE`` — byte-identical to its
    parent — and is distinguishable ONLY by ``trajectoryMetadata``, which carries
    ``parentConversationId`` / a foreign ``rootConversationId`` / ``nestingDepth``
    / ``subagentSpec``.

    :param parent_cascade_id: The parent (root) conversation id this child hangs off.
    :param include_parent_id: When ``False``, omit ``parentConversationId`` so the
        foreign ``rootConversationId`` is the only remaining child signal.
    """
    metadata: dict[str, Any] = {
        "createdAt": "2026-08-01T09:34:51.639408Z",
        "initializationStateId": "11e484d0-0000-4000-8000-000000000000",
        "rootConversationId": parent_cascade_id,
        "nestingDepth": 1,
        "subagentSpec": {"typeName": "self", "role": "Task 4 Reviewer", "inherit": True},
    }
    if include_parent_id:
        metadata["parentConversationId"] = parent_cascade_id
    return {
        "trajectoryId": "82e9fb54-9f16-46eb-88d7-a84fd40deec3",
        "status": "CASCADE_RUN_STATUS_IDLE",
        # NOT a distinct type — the same value a real root cascade reports.
        "trajectoryType": "CORTEX_TRAJECTORY_TYPE_CASCADE",
        "lastUserInputTime": last_user_input_time,
        "summary": "MetricsStore Implementation Code Review",
        "trajectoryMetadata": metadata,
    }


def test_detect_rotation_subagent_child_is_never_a_rotation_target() -> None:
    """A running agy SUBAGENT must never be rotated onto.

    agy spawns each subagent as a child conversation that reports the SAME
    ``trajectoryType`` as a real root, and is always more recently active than the
    parent it is working for. Rotating onto it promotes a sub-conversation to a
    new top-level Omnigent session (and drags the tmux pane with it), which is
    what made a single subagent fan-out explode into a session per agent.
    """
    summaries = {
        _BOUND_CASCADE: _summary(last_user_input_time="2026-08-01T10:49:38.660037Z"),
        _OTHER_CASCADE: _child_summary(
            parent_cascade_id=_BOUND_CASCADE,
            # Strictly newer than the parent — the subagent is mid-turn while the
            # parent sits idle waiting for it, so activity alone always favours it.
            last_user_input_time="2026-08-01T10:50:00.000000Z",
        ),
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) is None


def test_detect_rotation_child_detected_without_explicit_parent_id() -> None:
    """A foreign ``rootConversationId`` alone marks a child, with no parent field."""
    summaries = {
        _BOUND_CASCADE: _summary(last_user_input_time="2026-08-01T10:49:38.660037Z"),
        _OTHER_CASCADE: _child_summary(
            parent_cascade_id=_BOUND_CASCADE,
            last_user_input_time="2026-08-01T10:50:00.000000Z",
            include_parent_id=False,
        ),
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) is None


def test_detect_rotation_child_of_a_third_cascade_is_also_excluded() -> None:
    """A subagent of some OTHER root is still a child — not a rotation target.

    The child test must not degenerate into "is it my own child": a subagent
    belonging to a different root is equally not the user's top-level conversation.
    """
    summaries = {
        _BOUND_CASCADE: _summary(last_user_input_time="2026-08-01T10:49:38.660037Z"),
        _OTHER_CASCADE: _child_summary(
            parent_cascade_id="ffffffff-1111-4222-8333-444444444444",
            last_user_input_time="2026-08-01T10:50:00.000000Z",
        ),
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) is None


def test_detect_rotation_real_clear_mint_still_rotates_with_self_root_metadata() -> None:
    """A genuine /clear rotation still fires when metadata is self-referential.

    The live capture shows a real root carries ``rootConversationId == <its own
    key>``, so the child test must not swallow the /clear case this detector
    exists for.
    """
    other = dict(_summary(last_user_input_time="2026-08-01T10:50:00.000000Z"))
    other["trajectoryMetadata"] = {
        "createdAt": "2026-08-01T10:49:34.181600Z",
        "initializationStateId": "87966e34-057b-4b63-afa2-a437cd67ea9d",
        "rootConversationId": _OTHER_CASCADE,
    }
    summaries = {
        _BOUND_CASCADE: _summary(last_user_input_time="2026-08-01T10:49:38.660037Z"),
        _OTHER_CASCADE: other,
    }
    assert reader._detect_rotated_cascade(summaries, _BOUND_CASCADE) == _OTHER_CASCADE


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


# ---------------------------------------------------------------------------
# Quiescence backstop (agy's own CASCADE_RUN_STATUS)
# ---------------------------------------------------------------------------


def test_cascade_is_idle_reads_agys_own_run_status() -> None:
    """
    The bound cascade's own ``CASCADE_RUN_STATUS`` is the quiescence signal.

    agy publishes this per cascade in ``GetAllCascadeTrajectories``. It reports
    ``RUNNING`` both while working AND while parked on a permission gate
    (live-verified against agy 1.1.8 over a 75s gate), so IDLE genuinely means the
    turn is over — never "waiting for the human".
    """
    idle = {_BOUND_CASCADE: {"status": "CASCADE_RUN_STATUS_IDLE"}}
    running = {_BOUND_CASCADE: {"status": "CASCADE_RUN_STATUS_RUNNING"}}
    assert reader._cascade_is_idle(idle, _BOUND_CASCADE)
    assert not reader._cascade_is_idle(running, _BOUND_CASCADE)
    # An absent / malformed entry is never treated as idle: closing a turn on
    # missing information would be worse than leaving the accelerator to do it.
    assert not reader._cascade_is_idle({}, _BOUND_CASCADE)
    assert not reader._cascade_is_idle({_BOUND_CASCADE: "nope"}, _BOUND_CASCADE)
    assert not reader._cascade_is_idle({_BOUND_CASCADE: {}}, _BOUND_CASCADE)


@pytest.mark.asyncio
async def test_watch_for_rotation_signals_quiescence_only_after_consecutive_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Quiescence is reported only after CONSECUTIVE idle observations.

    A single idle reading can catch the gap between a turn being delivered and
    agy starting it, which would close a turn that is about to run. Requiring two
    in a row makes the backstop conservative — it exists to guarantee no session
    is stranded, not to race the step-based close.
    """
    calls: list[int] = []

    async def _on_quiescent() -> None:
        calls.append(1)

    idle = {"trajectorySummaries": {_BOUND_CASCADE: {"status": "CASCADE_RUN_STATUS_IDLE"}}}
    busy = {"trajectorySummaries": {_BOUND_CASCADE: {"status": "CASCADE_RUN_STATUS_RUNNING"}}}
    # idle, busy (resets), idle, idle -> exactly one signal, on the 4th tick.
    script = [idle, busy, idle, idle]
    seen = {"n": 0}

    def _fetch(port: int) -> dict[str, object]:
        i = seen["n"]
        seen["n"] += 1
        if i >= len(script):
            raise asyncio.CancelledError()
        return script[i]

    monkeypatch.setattr(reader, "get_all_cascade_trajectories", _fetch)

    async def _noop_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(reader, "_sleep", _noop_sleep)

    with pytest.raises(asyncio.CancelledError):
        await reader._watch_for_rotation(
            port=_PORT,
            bound_cascade_id=_BOUND_CASCADE,
            interval_s=0.0,
            skip_cascade_ids=frozenset(),
            on_rotation=_fail_rotation,
            on_quiescent=_on_quiescent,
        )

    assert calls == [1], "expected exactly one quiescence signal, after two idle ticks"


# ---------------------------------------------------------------------------
# Sub-agents: agy runs each as its own cascade, mirrored into a child session
# ---------------------------------------------------------------------------


async def _no_sleep(_seconds: float) -> None:
    """Collapse the mirror's poll delay so a multi-poll test runs instantly."""
    return


async def _never_returns(**_kwargs: Any) -> None:
    """Stand in for the mirror loop: stays pending until cancelled."""
    await asyncio.Event().wait()


async def _cancel_mirrors(state: reader._ReaderState) -> None:
    """Cancel every mirror task a test started so none leaks past it."""
    for task in state.subagent_mirrors.values():
        task.cancel()
    for task in state.subagent_mirrors.values():
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _invoke_subagent_step(
    *,
    status: str = "CORTEX_STEP_STATUS_RUNNING",
    results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    An INVOKE_SUBAGENT step naming two sub-agents.

    Shaped after the live frame: ``results`` is filled positionally against
    ``subagents`` and a child's ``conversationId`` appears as soon as that child
    exists, while the step itself stays RUNNING until every child finishes.
    """
    if results is None:
        results = [{"conversationId": "child-aaa"}, {"conversationId": "child-bbb"}]
    return {
        "type": "CORTEX_STEP_TYPE_INVOKE_SUBAGENT",
        "status": status,
        "metadata": {
            "toolAction": "Dispatching reviewers",
            "toolSummary": "Review the codebase",
            "sourceTrajectoryStepInfo": {"trajectoryId": _CASCADE_ID, "stepIndex": 36},
        },
        "invokeSubagent": {
            "subagents": [
                {"typeName": "research", "role": "App Router Reviewer"},
                {"typeName": "research", "role": "Database Reviewer"},
            ],
            "results": results,
        },
    }


def _subagent_client(
    child_ids: list[str] | None = None,
) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    """A client whose registration POSTs answer with successive child ids."""
    captured: list[dict[str, Any]] = []
    remaining = list(child_ids if child_ids is not None else ["conv_child_a", "conv_child_b"])

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        captured.append(body)
        if body.get("type") == "external_antigravity_subagent_start":
            minted = remaining.pop(0) if remaining else "conv_child_x"
            return httpx.Response(200, json={"queued": False, "child_session_id": minted})
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(_handler))
    return client, captured


def _registrations(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the ``data`` of every sub-agent registration POST, in order."""
    return [
        body["data"]
        for body in captured
        if body.get("type") == "external_antigravity_subagent_start"
    ]


def test_subagent_pairs_skips_a_child_agy_has_not_named_yet() -> None:
    """
    A result slot with no ``conversationId`` is not yet mirrorable.

    agy fills the results list positionally as children spawn, so an early frame
    carries a placeholder slot; registering it would mint a child row keyed on
    nothing.
    """
    step = _invoke_subagent_step(results=[{"conversationId": "child-aaa"}, {}])
    pairs = reader._subagent_pairs(step)
    assert [child_id for _spec, child_id in pairs] == ["child-aaa"]


def test_subagent_pairs_ignores_a_non_subagent_step() -> None:
    """An ordinary tool step names no children."""
    assert reader._subagent_pairs(_load("run_command_done")) == []


async def test_each_subagent_is_registered_once_across_repeated_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    REGRESSION: the owning step is re-sent on every frame while sub-agents work.

    ``_maybe_mirror_subagents`` runs unconditionally (the step is not ``seen``
    until it settles), so without the ``subagent_mirrors`` guard each frame would
    register the same children again and stack duplicate mirror tasks against
    them.
    """
    monkeypatch.setattr(reader, "_mirror_subagent_cascade", _never_returns)
    client, captured = _subagent_client()
    state = reader._ReaderState(seen=set(), interacted=set(), port=4242)

    async with client:
        for _ in range(3):
            await reader._maybe_mirror_subagents(
                _invoke_subagent_step(),
                client=client,
                session_id=_SESSION_ID,
                cascade_id=_CASCADE_ID,
                state=state,
            )
        registrations = _registrations(captured)
        assert [r["cascade_id"] for r in registrations] == ["child-aaa", "child-bbb"], (
            f"Each child must register exactly once; got {registrations}"
        )
        assert sorted(state.subagent_mirrors) == ["child-aaa", "child-bbb"]
        await _cancel_mirrors(state)


async def test_registration_carries_the_role_type_and_spawning_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The rail names the sub-agent, and the row links back to its tool card.

    ``tool_call_id`` is the INVOKE_SUBAGENT step's own mirrored call id, so the
    child and the tool card that spawned it share one identity.
    """
    monkeypatch.setattr(reader, "_mirror_subagent_cascade", _never_returns)
    client, captured = _subagent_client()
    state = reader._ReaderState(seen=set(), interacted=set(), port=4242)

    async with client:
        await reader._maybe_mirror_subagents(
            _invoke_subagent_step(),
            client=client,
            session_id=_SESSION_ID,
            cascade_id=_CASCADE_ID,
            state=state,
        )
        first = _registrations(captured)[0]
        assert first["role"] == "App Router Reviewer"
        assert first["agent_type"] == "research"
        assert first["tool_call_id"] == f"agy_call_{_CASCADE_ID}_36"
        await _cancel_mirrors(state)


async def test_a_rejected_registration_starts_no_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A server that will not mint a child leaves nothing polling for it.

    Without this the mirror would poll a cascade forever with nowhere to post.
    """
    monkeypatch.setattr(reader, "_mirror_subagent_cascade", _never_returns)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "nope"})

    client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(_handler))
    state = reader._ReaderState(seen=set(), interacted=set(), port=4242)

    async with client:
        await reader._maybe_mirror_subagents(
            _invoke_subagent_step(),
            client=client,
            session_id=_SESSION_ID,
            cascade_id=_CASCADE_ID,
            state=state,
        )
    assert state.subagent_mirrors == {}


async def test_a_settled_invoke_step_does_not_stop_its_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    REGRESSION: ``invoke_subagent`` is fire-and-forget, not a wait.

    Its step reaches DONE the moment the child is spawned while the child runs on
    for minutes (live-verified: a child kept working 23s past its spawning step's
    ``completedAt`` and accumulated 35 steps). Treating that DONE as "the
    sub-agents finished" mirrored only each child's opening prompt and left every
    row spinning, so the handler must not read the step's status at all.
    """
    monkeypatch.setattr(reader, "_mirror_subagent_cascade", _never_returns)
    client, _captured = _subagent_client()
    state = reader._ReaderState(seen=set(), interacted=set(), port=4242)

    async with client:
        await reader._maybe_mirror_subagents(
            _invoke_subagent_step(status="CORTEX_STEP_STATUS_DONE"),
            client=client,
            session_id=_SESSION_ID,
            cascade_id=_CASCADE_ID,
            state=state,
        )
        mirrors = list(state.subagent_mirrors.values())
        assert len(mirrors) == 2, "both children must still be mirrored"
        assert not any(task.done() for task in mirrors), (
            "a DONE invoke step must not stop the mirrors it started"
        )
        await _cancel_mirrors(state)


# Hang guard for the mirror-loop tests. Deliberately far above any healthy run
# (these poll a patched transport — milliseconds locally): a shared CI runner can
# be an order of magnitude slower, and a budget tight enough to catch that is a
# flake, not a signal. Termination is asserted by the loop RETURNING; this only
# stops a genuine hang from wedging the suite.
_MIRROR_HANG_GUARD_S = 60

# Polls a "still working" child answers before it finishes, in the quiescence
# test. With the timer patched to 2, this is enough for the status veto to fire
# twice — the behaviour under test — without needing a long loop, which is what
# made the first version of this test time out on CI.
_QUIET_POLLS_BEFORE_FINISHING = 5


def _child_transcript() -> list[dict[str, Any]]:
    """A finished sub-agent's steps: its prompt, a tool call, its closing reply."""
    return [
        _load("user_input"),
        _load("run_command_done"),
        _done_planner("Task complete.", step_index=9),
    ]


async def test_mirror_posts_the_child_cascade_into_the_child_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A sub-agent's own steps reach its child session, and the mirror then stops.

    A sub-agent cascade is an ordinary cascade on the same agy port, so mirroring
    is the committed-only poll path pointed at the child. A child that finished
    between two polls delivers its whole transcript in ONE pass — opening and
    closing its turn before the poll returns — so the turn-open check has to run
    per step, not per poll, or the mirror never notices it ended.
    """
    monkeypatch.setattr(reader, "get_trajectory_steps", lambda _p, _c: _child_transcript())
    sink = _PostSink()
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    client, _captured = _subagent_client()
    async with client:
        await asyncio.wait_for(
            reader._mirror_subagent_cascade(
                port=4242,
                child_cascade_id="child-aaa",
                client=client,
                child_session_id="conv_child_a",
            ),
            timeout=_MIRROR_HANG_GUARD_S,
        )

    assert sink.item_types() == ["message", "function_call", "function_call_output", "message"], (
        f"the child's whole transcript must be mirrored; got {sink.item_types()}"
    )
    assert set(sink.urls) == {"/v1/sessions/conv_child_a/events"}, (
        f"the child's work must post to the CHILD session; got {set(sink.urls)}"
    )
    # The closing step fires the child's own IDLE edge, clearing the rail badge.
    assert sink.statuses() == ["running", "idle"], (
        f"Expected the child's turn to open then close; got {sink.statuses()}"
    )


async def test_mirror_keeps_polling_while_the_child_is_still_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    REGRESSION: the mirror must outlive the child's opening prompt.

    The bug that shipped stopped every mirror as soon as the spawning step went
    DONE, so each child recorded exactly its ``USER_INPUT`` prompt — one item
    against the 16-101 steps the sub-agents actually ran. Here the child's later
    steps only appear on the third poll; a mirror that gives up early misses
    them.
    """
    working = [_load("user_input")]  # turn open, still working
    finished = _child_transcript()
    polls = 0

    def _steps(_port: int, _cascade: str) -> list[dict[str, Any]]:
        nonlocal polls
        polls += 1
        return working if polls < 3 else finished

    monkeypatch.setattr(reader, "get_trajectory_steps", _steps)
    monkeypatch.setattr(reader, "_sleep", _no_sleep)
    sink = _PostSink()
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    client, _captured = _subagent_client()
    async with client:
        await asyncio.wait_for(
            reader._mirror_subagent_cascade(
                port=4242,
                child_cascade_id="child-aaa",
                client=client,
                child_session_id="conv_child_a",
            ),
            timeout=_MIRROR_HANG_GUARD_S,
        )

    assert polls >= 3, f"the mirror stopped after {polls} polls, before the child finished"
    assert "function_call" in sink.item_types(), (
        f"the child's later work must be mirrored; got {sink.item_types()}"
    )


async def test_a_child_that_goes_quiet_without_closing_is_closed_from_agy_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A sub-agent whose turn never closes still clears its rail badge.

    The child's own closing step is the ordinary signal, but a transcript that
    ends without a recognizable one would leave the mirror polling for the rest
    of the session and the row spinning forever. agy's run status is the
    authority for that stall.
    """
    monkeypatch.setattr(reader, "get_trajectory_steps", lambda _p, _c: [_load("user_input")])
    monkeypatch.setattr(reader, "_sleep", _no_sleep)
    monkeypatch.setattr(reader, "_SUBAGENT_QUIESCENT_POLLS", 2)
    monkeypatch.setattr(
        reader,
        "get_all_cascade_trajectories",
        lambda _port: {
            "trajectorySummaries": {"child-aaa": {"status": "CASCADE_RUN_STATUS_IDLE"}}
        },
    )
    sink = _PostSink()
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    client, _captured = _subagent_client()
    async with client:
        await asyncio.wait_for(
            reader._mirror_subagent_cascade(
                port=4242,
                child_cascade_id="child-aaa",
                client=client,
                child_session_id="conv_child_a",
            ),
            timeout=_MIRROR_HANG_GUARD_S,
        )

    assert sink.statuses() == ["running", "idle"], (
        f"a stalled child must still be closed out; got {sink.statuses()}"
    )


async def test_a_quiet_child_agy_still_reports_running_is_not_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Quiet is not finished: a sub-agent mid-build produces no steps for minutes.

    Closing on quiescence alone would truncate its transcript, so the status
    check must be able to veto — this pins that a non-idle cascade keeps the
    mirror alive rather than being closed on the timer.
    """
    working = [_load("user_input")]
    finished = _child_transcript()  # what it returns once the child is done
    polls = 0
    idle_checks = 0

    def _steps(_port: int, _cascade: str) -> list[dict[str, Any]]:
        nonlocal polls
        polls += 1
        return working if polls <= _QUIET_POLLS_BEFORE_FINISHING else finished

    def _still_running(_port: int) -> dict[str, Any]:
        nonlocal idle_checks
        idle_checks += 1
        return {"trajectorySummaries": {"child-aaa": {"status": "CASCADE_RUN_STATUS_RUNNING"}}}

    monkeypatch.setattr(reader, "get_trajectory_steps", _steps)
    monkeypatch.setattr(reader, "_sleep", _no_sleep)
    monkeypatch.setattr(reader, "_SUBAGENT_QUIESCENT_POLLS", 2)
    monkeypatch.setattr(reader, "get_all_cascade_trajectories", _still_running)
    sink = _PostSink()
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    client, _captured = _subagent_client()
    async with client:
        await asyncio.wait_for(
            reader._mirror_subagent_cascade(
                port=4242,
                child_cascade_id="child-aaa",
                client=client,
                child_session_id="conv_child_a",
            ),
            timeout=_MIRROR_HANG_GUARD_S,
        )

    # The veto is the point: the timer fired, agy's status overruled it, and the
    # mirror was still alive to see the child finish.
    assert idle_checks >= 1, f"the quiescence timer should have fired; got {idle_checks}"
    assert polls > _QUIET_POLLS_BEFORE_FINISHING, "the mirror gave up before the child finished"
    assert sink.statuses() == ["running", "idle"], (
        f"the child closed on its own step, not the timer; got {sink.statuses()}"
    )


async def test_the_quiescence_recheck_backs_off_after_agy_vetoes_a_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A child agy never calls idle is re-checked ever less often, not every window.

    agy answering "still running" can only veto the close, so the window is a
    repeating re-check rather than a one-shot grace period. Left flat it would
    re-ask at the same cadence for the life of the session; doubling keeps a
    long-lived quiet child cheap while a stalled one is still caught.
    """
    polls = 0
    idle_checks = 0
    reached_target = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _steps(_port: int, _cascade: str) -> list[dict[str, Any]]:
        nonlocal polls
        polls += 1
        if polls == 30:
            loop.call_soon_threadsafe(reached_target.set)
        return [_load("user_input")]  # turn open, never another step

    def _still_running(_port: int) -> dict[str, Any]:
        nonlocal idle_checks
        idle_checks += 1
        return {"trajectorySummaries": {"child-aaa": {"status": "CASCADE_RUN_STATUS_RUNNING"}}}

    monkeypatch.setattr(reader, "get_trajectory_steps", _steps)
    monkeypatch.setattr(reader, "_sleep", _no_sleep)
    monkeypatch.setattr(reader, "_SUBAGENT_QUIESCENT_POLLS", 2)
    monkeypatch.setattr(reader, "get_all_cascade_trajectories", _still_running)
    sink = _PostSink()
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    client, _captured = _subagent_client()
    async with client:
        mirror = asyncio.create_task(
            reader._mirror_subagent_cascade(
                port=4242,
                child_cascade_id="child-aaa",
                client=client,
                child_session_id="conv_child_a",
            )
        )
        try:
            await asyncio.wait_for(reached_target.wait(), timeout=_MIRROR_HANG_GUARD_S)
        finally:
            mirror.cancel()
            with pytest.raises(asyncio.CancelledError):
                await mirror

    assert polls >= 30, f"the mirror should have kept polling a vetoed child; got {polls}"
    # Windows of 2, 4, 8, 16 reach poll 30 in four checks; a flat window would
    # have asked ~15 times by now.
    assert idle_checks <= 5, (
        f"the re-check should back off, not repeat every window; got {idle_checks} "
        f"checks over {polls} polls"
    )
    assert sink.statuses() == ["running"], (
        f"a child agy still calls running must not be closed; got {sink.statuses()}"
    )


async def test_a_finished_child_takes_its_own_grandchild_mirrors_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A nested sub-agent's mirror is reachable at teardown, not orphaned.

    A child's steps run the same ``_process_committed_step`` path as the
    parent's, so a nested ``INVOKE_SUBAGENT`` registers a grandchild mirror into
    the CHILD's tracker. The reader's drain only walks the parent's, so without
    the child cleaning up its own, a depth-2 mirror holds no reachable reference
    and keeps polling a cascade whose agy is going away.
    """
    grandchild_polls = 0

    def _steps(_port: int, cascade: str) -> list[dict[str, Any]]:
        if cascade == "grand-ccc":
            nonlocal grandchild_polls
            grandchild_polls += 1
            return [_load("user_input")]  # never closes: polls until cancelled
        return [
            _load("user_input"),
            _invoke_subagent_step(results=[{"conversationId": "grand-ccc"}]),
            _done_planner("Child done.", step_index=9),
        ]

    monkeypatch.setattr(reader, "get_trajectory_steps", _steps)
    monkeypatch.setattr(reader, "_sleep", _no_sleep)
    # agy reports the grandchild still working, so nothing but cancellation can
    # end its mirror — the quiescence backstop must not be what stops it here.
    monkeypatch.setattr(
        reader,
        "get_all_cascade_trajectories",
        lambda _port: {
            "trajectorySummaries": {"grand-ccc": {"status": "CASCADE_RUN_STATUS_RUNNING"}}
        },
    )
    sink = _PostSink()
    monkeypatch.setattr(reader, "post_session_event_with_retry", sink)

    client, captured = _subagent_client(child_ids=["conv_grandchild"])
    async with client:
        await asyncio.wait_for(
            reader._mirror_subagent_cascade(
                port=4242,
                child_cascade_id="child-aaa",
                client=client,
                child_session_id="conv_child_a",
            ),
            timeout=_MIRROR_HANG_GUARD_S,
        )
        assert _registrations(captured), "the nested sub-agent should have been registered"
        settled = grandchild_polls
        # A live mirror polls in a tight loop (its delay is collapsed), so a real
        # pause is what makes an orphan visible; a cancelled one cannot advance.
        await asyncio.sleep(0.05)

    assert grandchild_polls == settled, (
        f"the grandchild mirror kept polling after its parent ended "
        f"({settled} → {grandchild_polls} polls)"
    )


async def test_subagent_idle_check_failure_keeps_mirroring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An RPC hiccup during the idle check must not truncate a sub-agent.

    Fail-safe: a wrong "idle" ends the mirror and loses the rest of the child's
    transcript, while a wrong "still running" costs only another poll.
    """

    def _raise(_port: int) -> dict[str, Any]:
        raise httpx.ConnectError("agy went away")

    monkeypatch.setattr(reader, "get_all_cascade_trajectories", _raise)
    assert await reader._subagent_cascade_is_idle(4242, "child-aaa") is False

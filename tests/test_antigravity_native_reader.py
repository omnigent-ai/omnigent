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

import copy
import json
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

    def deltas(self) -> list[dict[str, object]]:
        """Return the ``data`` payload of every ``external_output_text_delta``."""
        return [
            data for event_type, data in self.posts if event_type == "external_output_text_delta"
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
        return None

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
        return None

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
# USER_INPUT posts nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_input_posts_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_discovery: None,
) -> None:
    """A USER_INPUT step maps to [] — no conversation item is posted for it."""
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

    assert sink.item_types() == []


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
        return None

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
        iterations=1,
        on_pending=_on_pending,
    )

    assert len(captured) == 1
    cascade_id, port, pending = captured[0]
    assert cascade_id == _CASCADE_ID
    assert port == _PORT
    assert pending["kind"] == "ask_question"
    assert pending["trajectory_id"] == _CASCADE_ID


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
    # USER_INPUT still posts no conversation item; the assistant message commits.
    assert sink.item_types() == ["message"]


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
    """A USER_INPUT step with a specific requestedModel enum.

    :param model_enum: agy model enum string.
    :param step_index: Optional step index override; when provided it is written
        into ``metadata.sourceTrajectoryStepInfo.stepIndex`` so two consecutive
        turns can be assigned distinct dedup keys (the base fixture has no
        ``stepIndex``, so they would otherwise share the same ``(trajectory_id,
        None)`` key and the second turn would be silently de-duped).
    """
    step = copy.deepcopy(_load("user_input"))
    user_input = cast(dict[str, Any], step["userInput"])
    planner_cfg = cast(dict[str, Any], user_input["userConfig"]["plannerConfig"])
    planner_cfg["requestedModel"] = {"model": model_enum}
    if step_index is not None:
        traj_info = cast(dict[str, Any], step["metadata"])["sourceTrajectoryStepInfo"]
        traj_info["stepIndex"] = step_index
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
        return None

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
    # fallback to the poll loop), and every backoff recorded was the re-entry
    # backoff — proving the loop did NOT busy-spin re-POSTing at zero delay, and
    # that only the re-entry backoff (not the poll-interval sleep) ran.
    assert empty_stream.calls == target_entries
    assert backoff_sleeps and all(s == reader._STREAM_REENTRY_BACKOFF_S for s in backoff_sleeps)

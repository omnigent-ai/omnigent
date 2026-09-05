"""Tests for the native Antigravity (agy) executor bridge (web-turn injection).

These pin the write path: a web/mobile turn is delivered to the running agy by
TYPING IT INTO the agy TUI pane over tmux (``inject_user_message_via_tui``,
mocked here), which agy records as a real ``USER_INPUT`` step on the cascade the
TUI displays. Typing into the TUI (not headless ``SendUserCascadeMessage`` RPC)
is what unifies the agy TUI and the web mirror onto ONE cascade, giving
claude/codex-native parity (#1156/#1158).

They also pin the **completion gate**. Delivery is not completion: the executor
used to yield ``TurnComplete`` the instant the text had been typed and
submitted, so a dispatched implementation task returned an immediate empty
success before agy had run a single tool. The gate polls agy's own RPC
trajectory until the turn reaches a terminal state, and the ``agy`` fixture below
fakes that trajectory — a turn OPENS when the inject lands and only ever
completes when the faked trajectory says agy finished.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.inner.antigravity_native_executor as executor_mod
from omnigent.antigravity_native_bridge import (
    AntigravityNativeBridgeState,
    write_bridge_state,
)
from omnigent.inner.antigravity_native_executor import AntigravityNativeExecutor
from omnigent.inner.executor import ExecutorError, ExecutorEvent, TurnComplete

_CONVERSATION_ID = "90468e33-38c3-4e48-ae9f-03c843196227"
_PLACEHOLDER_ID = "agy_conv_placeholder123"
_PORT = 52548
_ECHOED_MODEL = "MODEL_PLACEHOLDER_M20"
_RECOMMENDED_MODEL = "MODEL_PLACEHOLDER_M132"
_DEFAULT_REPLY_TEXT = "all done"


def _user_step(text: str) -> dict[str, Any]:
    """
    Build the USER_INPUT step agy records for a delivered turn.

    :param text: The delivered text agy echoed back onto the cascade.
    :returns: One USER_INPUT step dict.
    """
    return {"type": "CORTEX_STEP_TYPE_USER_INPUT", "userInput": {"userResponse": text}}


def _planner_step(
    *, status: str, text: str | None = None, error: str | None = None
) -> dict[str, Any]:
    """
    Build a PLANNER_RESPONSE step at ``status``.

    :param status: ``CORTEX_STEP_STATUS_*`` value.
    :param text: Assistant text the planner carries, if any.
    :param error: agy error detail, for the ERROR shape.
    :returns: One PLANNER_RESPONSE step dict.
    """
    planner: dict[str, Any] = {}
    if text is not None:
        planner["response"] = text
    if error is not None:
        planner["error"] = error
    return {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": status,
        "plannerResponse": planner,
    }


def _tool_step(status: str) -> dict[str, Any]:
    """
    Build a RUN_COMMAND tool step at ``status``.

    :param status: ``CORTEX_STEP_STATUS_*`` value.
    :returns: One tool step dict.
    """
    return {
        "type": "CORTEX_STEP_TYPE_RUN_COMMAND",
        "status": status,
        "runCommand": {"command": "pytest"},
        # ``metadata.toolAction`` is what marks a step as a tool call on the live
        # wire, in both RPC shapes (see ``_is_tool_step``).
        "metadata": {"toolAction": "Running command"},
    }


def _executor(tmp_path: Path) -> AntigravityNativeExecutor:
    """
    Build an executor with an explicit bridge dir (no env needed).

    :param tmp_path: Pytest temporary directory used as the bridge dir.
    :returns: A configured :class:`AntigravityNativeExecutor`.
    """
    return AntigravityNativeExecutor(bridge_dir=tmp_path)


def _seed_state(tmp_path: Path, *, conversation_id: str = _CONVERSATION_ID) -> None:
    """
    Write bridge state the executor will read before delivering.

    :param tmp_path: Bridge directory.
    :param conversation_id: agy conversation id to record (a real id, or an
        ``agy_conv_*`` placeholder to model a fresh, not-yet-discovered session).
    :returns: None.
    """
    write_bridge_state(
        tmp_path,
        AntigravityNativeBridgeState(session_id="conv_test", conversation_id=conversation_id),
    )


def _steps_with_model(model: str) -> list[dict[str, object]]:
    """
    Build a trajectory-step list whose latest USER_INPUT step carries ``model``.

    Mirrors the live wire shape the executor reads to echo agy's current model:
    ``step.userInput.userConfig.plannerConfig.planModel`` (a string) on a
    ``CORTEX_STEP_TYPE_USER_INPUT`` step. Includes a trailing non-USER_INPUT
    step so the test exercises "find the latest USER_INPUT", not "take the last".

    :param model: agy model enum string to embed in the latest USER_INPUT step.
    :returns: A step list ending past the USER_INPUT step.
    """
    return [
        {
            "stepIndex": 0,
            "type": "CORTEX_STEP_TYPE_USER_INPUT",
            "userInput": {"userConfig": {"plannerConfig": {"planModel": "MODEL_PLACEHOLDER_OLD"}}},
        },
        {
            "stepIndex": 1,
            "type": "CORTEX_STEP_TYPE_USER_INPUT",
            "userInput": {"userConfig": {"plannerConfig": {"planModel": model}}},
        },
        {"stepIndex": 2, "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "plannerResponse": {}},
    ]


@pytest.fixture
def injected(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """
    Fake one running agy: the TUI inject AND the RPC trajectory the gate polls.

    The write path types the turn into the agy TUI pane via
    ``inject_user_message_via_tui``; this records every ``{bridge_dir, content}``
    call and, when ``rec["raise"]`` is set, raises it (modeling a dead/unavailable
    TUI pane) WITHOUT recording a turn — a failed delivery never opens one.

    A successful inject appends ``rec["reply"]`` to ``rec["steps"]``, which is
    what ``GetCascadeTrajectorySteps`` then returns, so the completion gate sees
    exactly the turn shape a test asks for. The default reply is a completed turn
    (USER_INPUT + a DONE planner carrying text) so tests that are not about the
    gate read as before.

    Knobs, all mutable by the test before it drives ``run_turn``:

    * ``steps`` — the trajectory BEFORE delivery (default empty).
    * ``reply`` — steps appended on a successful inject; ``None`` means agy
      records nothing at all.
    * ``cascade_status`` — agy's own run status for the idle backstop.
    * ``on_poll`` — ``callable(rec, poll_index)`` run before each trajectory read,
      for a trajectory that evolves across polls. Poll 0 is the executor's
      pre-delivery snapshot; poll 1 is the gate's first read.
    * ``read_error`` — ``callable(poll_index) -> Exception | None``, to fail
      specific reads.
    * ``port`` — resolved connect-RPC port, or ``None`` for "agy not found".

    :param monkeypatch: pytest monkeypatch fixture.
    :returns: The mutable fake-agy record described above.
    """
    rec: dict[str, object] = {
        "calls": [],
        "raise": None,
        "steps": [],
        "reply": None,
        "cascade_status": "CASCADE_RUN_STATUS_RUNNING",
        "on_poll": None,
        "read_error": None,
        "port": _PORT,
        "polls": 0,
        "idle_checks": 0,
    }

    def _inject(bridge_dir: Path, *, content: str, **_kw: object) -> None:
        calls = rec["calls"]
        assert isinstance(calls, list)
        calls.append({"bridge_dir": bridge_dir, "content": content})
        exc = rec["raise"]
        if exc is not None:
            assert isinstance(exc, BaseException)
            raise exc
        reply = rec["reply"]
        if reply is None:
            reply = [
                _user_step(content),
                _planner_step(status="CORTEX_STEP_STATUS_DONE", text=_DEFAULT_REPLY_TEXT),
            ]
        steps = rec["steps"]
        assert isinstance(steps, list) and isinstance(reply, list)
        steps.extend(reply)

    def _steps(_port: int, _cascade_id: str) -> list[dict[str, object]]:
        index = rec["polls"]
        assert isinstance(index, int)
        rec["polls"] = index + 1
        on_poll = rec["on_poll"]
        if callable(on_poll):
            on_poll(rec, index)
        read_error = rec["read_error"]
        if callable(read_error):
            exc = read_error(index)
            if exc is not None:
                raise exc
        steps = rec["steps"]
        assert isinstance(steps, list)
        return list(steps)

    def _trajectories(_port: int) -> dict[str, object]:
        checks = rec["idle_checks"]
        assert isinstance(checks, int)
        rec["idle_checks"] = checks + 1
        return {"trajectorySummaries": {_CONVERSATION_ID: {"status": rec["cascade_status"]}}}

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(executor_mod, "inject_user_message_via_tui", _inject)
    monkeypatch.setattr(executor_mod, "get_trajectory_steps", _steps)
    monkeypatch.setattr(executor_mod, "get_all_cascade_trajectories", _trajectories)
    monkeypatch.setattr(executor_mod, "resolve_language_server_port", lambda _c: rec["port"])
    monkeypatch.setattr(executor_mod, "_sleep", _no_sleep)
    return rec


def _injected(rec: dict[str, object]) -> list[dict[str, object]]:
    """Return the recorded TUI inject calls, in order."""
    calls = rec["calls"]
    assert isinstance(calls, list)
    return calls


async def _run(executor: AntigravityNativeExecutor, text: str) -> list[ExecutorEvent]:
    """
    Drive ``run_turn`` with a single user message and collect its events.

    :param executor: Executor under test.
    :param text: User message text.
    :returns: The yielded executor events.
    """
    return [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": text}],
            tools=[],
            system_prompt="",
        )
    ]


# ---------------------------------------------------------------------------
# capability flags
# ---------------------------------------------------------------------------


def test_does_not_support_streaming(tmp_path: Path) -> None:
    """
    ``supports_streaming`` is ``False``.

    Assistant output is posted by the read driver, not streamed by the executor,
    so it must report no streaming or the workflow would await chunks that never
    come.
    """
    assert _executor(tmp_path).supports_streaming() is False


def test_supports_live_message_queue(tmp_path: Path) -> None:
    """
    ``supports_live_message_queue`` is ``True``.

    The server routes mid-turn web messages to ``enqueue_session_message``; the
    executor advertises live steering so that wiring stays active under the RPC
    turn-send path.
    """
    assert _executor(tmp_path).supports_live_message_queue() is True


# ---------------------------------------------------------------------------
# run_turn — delivery (TUI injection)
# ---------------------------------------------------------------------------


def test_run_turn_delivers_via_tui_and_completes(
    tmp_path: Path, injected: dict[str, object]
) -> None:
    """
    ``run_turn`` types the user text into the agy TUI and completes on agy's DONE turn.

    The turn is injected into the TUI pane (#1156/#1158) — agy records it as a
    real USER_INPUT on the cascade the TUI displays — and the executor then waits
    for agy's terminal DONE planner before reporting completion, carrying that
    step's text so the caller receives the agent's actual answer.
    """
    _seed_state(tmp_path)
    events = asyncio.run(_run(_executor(tmp_path), "what is 2+2?"))
    calls = _injected(injected)
    assert len(calls) == 1
    assert calls[0]["content"] == "what is 2+2?"
    assert calls[0]["bridge_dir"] == tmp_path
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response == _DEFAULT_REPLY_TEXT


def test_run_turn_flattens_content_blocks(tmp_path: Path, injected: dict[str, object]) -> None:
    """Content-block user messages are flattened to text before injection."""
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "line one"},
                            # malformed data URI (no comma) -> not materialized
                            {"type": "input_image", "image_url": "data:image/png;base64"},
                            {"type": "input_text", "text": "line two"},
                        ],
                    }
                ],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_drive())
    # The unmaterializable image contributes a visible marker, not a
    # silent drop; attachment lines precede the text lines.
    assert _injected(injected)[0]["content"] == (
        "[Attachment attachment could not be loaded]\nline one\nline two"
    )
    assert isinstance(events[0], TurnComplete)


def test_run_turn_uses_latest_user_message(tmp_path: Path, injected: dict[str, object]) -> None:
    """Only the latest user message is delivered (history is not replayed)."""
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[
                    {"role": "user", "content": "old question"},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "new question"},
                ],
                tools=[],
                system_prompt="",
            )
        ]

    asyncio.run(_drive())
    assert _injected(injected)[0]["content"] == "new question"


def test_run_turn_no_user_text_errors(tmp_path: Path, injected: dict[str, object]) -> None:
    """A turn with no user text yields an ExecutorError without injecting."""
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[{"role": "assistant", "content": "only assistant"}],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_drive())
    assert _injected(injected) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)


# a tiny valid base64 PNG data URI (1x1 pixel), materialized to disk + referenced
_PNG_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_run_turn_image_attachment_materialized(
    tmp_path: Path, injected: dict[str, object]
) -> None:
    """An image block is written to the bridge dir and referenced by path."""
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_image", "image_url": _PNG_DATA_URI},
                            {"type": "input_text", "text": "describe this"},
                        ],
                    }
                ],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_drive())
    content = _injected(injected)[0]["content"]
    assert isinstance(content, str)
    # attachment marker is prepended ahead of the typed text
    assert content.startswith("[Attached: ")
    assert str(tmp_path) in content
    assert content.endswith("describe this")
    assert isinstance(events[0], TurnComplete)


def test_run_turn_attachment_only_no_longer_errors(
    tmp_path: Path, injected: dict[str, object]
) -> None:
    """An attachment-only turn injects the marker instead of hard-erroring."""
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[
                    {
                        "role": "user",
                        "content": [{"type": "input_image", "image_url": _PNG_DATA_URI}],
                    }
                ],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_drive())
    content = _injected(injected)[0]["content"]
    assert isinstance(content, str)
    assert content.startswith("[Attached: ")
    assert isinstance(events[0], TurnComplete)
    assert not any(isinstance(event, ExecutorError) for event in events)


# ---------------------------------------------------------------------------
# run_turn — failure mapping
# ---------------------------------------------------------------------------


def test_run_turn_missing_state_errors(tmp_path: Path, injected: dict[str, object]) -> None:
    """With no bridge state, ``run_turn`` yields an ExecutorError (no inject)."""
    events = asyncio.run(_run(_executor(tmp_path), "hi"))
    assert _injected(injected) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "bridge state is missing" in events[0].message


def test_run_turn_inactive_session_errors(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mismatched request session id blocks delivery with an ExecutorError."""
    _seed_state(tmp_path)
    executor = _executor(tmp_path)
    monkeypatch.setattr(executor, "_request_session_id", "conv_other")
    events = asyncio.run(_run(executor, "hi"))
    assert _injected(injected) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no longer active" in events[0].message


def test_run_turn_tui_inject_error_surfaces(tmp_path: Path, injected: dict[str, object]) -> None:
    """
    A ``RuntimeError`` from the TUI inject surfaces as an ExecutorError.

    The inject raises when the agy pane is gone / never advertised / the submit
    never started a turn; the executor must surface it (so the UI can prompt a
    restart) rather than report a fake success the mirror never fills. The
    completion gate must not swallow or soften that — a delivery failure is still
    a failure, reported without waiting on a turn that was never delivered.
    """
    _seed_state(tmp_path)
    injected["raise"] = RuntimeError("the agy terminal is no longer running (the TUI exited)")
    events = asyncio.run(_run(_executor(tmp_path), "hi"))
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "the agy TUI" in events[0].message
    # Only the pre-delivery snapshot ran; the gate was never entered.
    assert injected["polls"] == 1


# ---------------------------------------------------------------------------
# run_turn — the completion gate
#
# The defect these pin: the executor reported a turn complete as soon as the
# text was typed into the TUI, so an orchestrator dispatching an implementation
# task collected an immediate empty success before agy had done any work.
# ---------------------------------------------------------------------------


def _drive_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_timeout_s: float = 180.0,
    completion_timeout_s: float = 3600.0,
    idle_every: int = 5,
    idle_confirmations: int = 2,
    text: str = "implement the thing",
) -> list[ExecutorEvent]:
    """
    Run one gated turn with the gate's budgets pinned for the test.

    :param tmp_path: Bridge directory.
    :param monkeypatch: pytest monkeypatch fixture.
    :param start_timeout_s: Budget for agy to record the delivery as a turn.
    :param completion_timeout_s: Budget for the open turn to reach a terminal state.
    :param idle_every: Polls between idle-backstop checks.
    :param idle_confirmations: Consecutive idle readings required to close.
    :param text: User text to deliver.
    :returns: The yielded executor events.
    """
    monkeypatch.setattr(executor_mod, "_TURN_START_TIMEOUT_S", start_timeout_s)
    monkeypatch.setattr(executor_mod, "_TURN_COMPLETION_TIMEOUT_S", completion_timeout_s)
    monkeypatch.setattr(executor_mod, "_IDLE_CHECK_EVERY_N_POLLS", idle_every)
    monkeypatch.setattr(executor_mod, "_IDLE_CONFIRM_CHECKS", idle_confirmations)
    return asyncio.run(_run(_executor(tmp_path), text))


def test_gate_waits_for_a_terminal_done_state(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    REGRESSION: the turn completes only once agy's trajectory reaches DONE.

    agy is still running a tool on the first gate poll and only closes the turn
    on a later one. The old executor yielded ``TurnComplete`` straight off the
    successful inject, which here would mean completing while the tool was still
    running and returning no text at all.
    """
    _seed_state(tmp_path)
    injected["reply"] = [
        _user_step("implement the thing"),
        _tool_step("CORTEX_STEP_STATUS_RUNNING"),
    ]

    def _finish_on_third_read(rec: dict[str, object], index: int) -> None:
        if index == 3:
            steps = rec["steps"]
            assert isinstance(steps, list)
            steps.append(_planner_step(status="CORTEX_STEP_STATUS_DONE", text="refactor applied"))

    injected["on_poll"] = _finish_on_third_read

    events = _drive_gate(tmp_path, monkeypatch)
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response == "refactor applied"
    # Poll 0 was the pre-delivery snapshot, so the gate itself polled repeatedly
    # instead of completing off the inject.
    assert injected["polls"] == 4


def test_gate_does_not_complete_on_the_previous_turns_close(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    REGRESSION: an earlier turn's DONE planner never satisfies the new turn.

    The trajectory already ends in a completed turn when the new one is
    delivered. Scanning the whole trajectory (rather than only the steps after
    THIS turn's USER_INPUT) would report the new turn complete immediately, with
    the old turn's text.
    """
    _seed_state(tmp_path)
    injected["steps"] = [
        _user_step("previous question"),
        _planner_step(status="CORTEX_STEP_STATUS_DONE", text="previous answer"),
    ]
    injected["reply"] = [
        _user_step("implement the thing"),
        _tool_step("CORTEX_STEP_STATUS_RUNNING"),
    ]

    events = _drive_gate(tmp_path, monkeypatch, completion_timeout_s=0.0)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "did not reach a terminal state" in events[0].message


def test_gate_reports_an_agy_error_state_as_a_failure(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A turn whose planner ends in ERROR is a failure, distinguishable from success.

    agy's own error detail is carried through so the caller can tell a
    rate-limit from a safety block, and the failure is marked retryable because
    that class of error often survives a retry.
    """
    _seed_state(tmp_path)
    injected["reply"] = [
        _user_step("implement the thing"),
        _planner_step(status="CORTEX_STEP_STATUS_ERROR", error="resource exhausted"),
    ]

    events = _drive_gate(tmp_path, monkeypatch)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert not any(isinstance(event, TurnComplete) for event in events)
    assert "ERROR state" in events[0].message
    assert "resource exhausted" in events[0].message
    assert events[0].retryable is True


def test_gate_times_out_with_a_diagnosable_failure(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A turn that never reaches a terminal state fails, naming the budget and the cause.

    agy is parked on a WAITING permission gate that nobody answers. The result
    must be a bounded, explicit failure — never a hang, and never a success that
    claims unconfirmed work was done.
    """
    _seed_state(tmp_path)
    injected["reply"] = [
        _user_step("implement the thing"),
        _tool_step("CORTEX_STEP_STATUS_WAITING"),
    ]

    events = _drive_gate(tmp_path, monkeypatch, completion_timeout_s=0.0)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "did not reach a terminal state within 0s" in events[0].message
    assert "WAITING interaction" in events[0].message
    assert "NOT confirmed" in events[0].message
    assert events[0].retryable is False


def test_gate_fails_when_agy_never_records_the_turn(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A delivery agy never turns into a USER_INPUT step fails on the start budget.

    The submit was footer-verified, so nothing appearing on the cascade means the
    text did not open a turn. Reporting that promptly beats waiting out the full
    completion budget — and beats reporting success.
    """
    _seed_state(tmp_path)
    injected["reply"] = []

    events = _drive_gate(tmp_path, monkeypatch, start_timeout_s=0.0)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "never recorded it as a turn" in events[0].message


def test_gate_keeps_waiting_through_transient_rpc_failures(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An unreadable trajectory is "unknown", never "finished".

    The first gate reads fail (transport error, then no resolvable agy port).
    Neither may end the turn: the gate retries and completes only when agy's
    trajectory actually says DONE.
    """
    _seed_state(tmp_path)

    def _fail_first_gate_read(index: int) -> Exception | None:
        return httpx.ConnectError("connection refused") if index == 1 else None

    injected["read_error"] = _fail_first_gate_read

    events = _drive_gate(tmp_path, monkeypatch)
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response == _DEFAULT_REPLY_TEXT
    assert injected["polls"] >= 3


def test_gate_closes_a_degenerate_turn_on_agys_own_idle_status(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A turn that closes without a text planner is settled by agy's cascade status.

    No step-based rule can tell that shape from a streamed dispatch, so the gate
    falls back to agy's own run status — and only after consecutive idle
    readings, so a turn agy has not started yet can never pass as finished.
    """
    _seed_state(tmp_path)
    injected["reply"] = [
        _user_step("implement the thing"),
        _planner_step(status="CORTEX_STEP_STATUS_DONE"),
    ]
    injected["cascade_status"] = "CASCADE_RUN_STATUS_IDLE"

    events = _drive_gate(tmp_path, monkeypatch, idle_every=1, idle_confirmations=2)
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert injected["idle_checks"] == 2


def test_idle_status_alone_never_completes_an_unrecorded_turn(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An idle cascade with no recorded turn is not a completion.

    agy reports idle in the window between a delivery and the turn being
    recorded; treating that as terminal would reinstate the false success in its
    worst form.
    """
    _seed_state(tmp_path)
    injected["reply"] = []
    injected["cascade_status"] = "CASCADE_RUN_STATUS_IDLE"

    events = _drive_gate(tmp_path, monkeypatch, start_timeout_s=0.0, idle_every=1)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert injected["idle_checks"] == 0


def test_snapshot_is_retried_through_a_transient_failure(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The pre-delivery snapshot is retried rather than abandoned on a blip.

    The step count is the ONLY signal that identifies this turn, so a transient
    RPC failure must not cost the turn its verification. The first read fails and
    the retry succeeds, and the turn then completes normally.
    """
    _seed_state(tmp_path)
    injected["steps"] = [
        _user_step("previous question"),
        _planner_step(status="CORTEX_STEP_STATUS_DONE", text="previous answer"),
    ]

    def _fail_first_snapshot_read(index: int) -> Exception | None:
        return httpx.ConnectError("agy not up yet") if index == 0 else None

    injected["read_error"] = _fail_first_snapshot_read

    events = _drive_gate(tmp_path, monkeypatch)
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response == _DEFAULT_REPLY_TEXT


def test_repeated_text_with_no_snapshot_never_adopts_the_previous_turn(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    REGRESSION: identical consecutive text must not resolve to the finished turn.

    The previous turn used the SAME text and is already closed by its own DONE
    planner. With the pre-delivery snapshot failing for the whole budget there is
    no positional signal, and an earlier revision fell back to matching the
    delivered text — which selects that old turn and reports its completion as
    this one's. The gate must instead report the turn as unverifiable, and must
    never hand back the prior turn's reply.
    """
    _seed_state(tmp_path)
    repeated = "implement the thing"
    injected["steps"] = [
        _user_step(repeated),
        _planner_step(status="CORTEX_STEP_STATUS_DONE", text="stale answer from last turn"),
    ]
    # agy has not yet recorded the new turn when the gate would look.
    injected["reply"] = []
    injected["read_error"] = lambda _index: httpx.ConnectError("agy unreachable")

    monkeypatch.setattr(executor_mod, "_SNAPSHOT_TIMEOUT_S", 0.0)
    events = _drive_gate(tmp_path, monkeypatch, text=repeated)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert not any(isinstance(event, TurnComplete) for event in events)
    assert "could not be verified" in events[0].message
    assert "NOT confirmed" in events[0].message
    assert "stale answer from last turn" not in events[0].message
    # The turn was still delivered; only its completion is unverified.
    assert _injected(injected)[0]["content"] == repeated


def test_idle_backstop_waits_for_evidence_the_model_started(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    REGRESSION: a recorded USER_INPUT plus an idle cascade is NOT a completion.

    agy publishes the user step before it starts generating and reports the
    cascade idle throughout that window. Consecutive idle readings do not close
    that gap — they only require it to persist — so a delayed model start would
    otherwise be reported as a finished turn with no terminal evidence at all.
    Here the turn is recorded, the cascade is idle, and the model does not start
    until well past the consecutive-check window: no success may be reported.
    """
    _seed_state(tmp_path)
    # agy records the user step, then sits: no planner, no tool, cascade idle.
    injected["reply"] = [_user_step("implement the thing")]
    injected["cascade_status"] = "CASCADE_RUN_STATUS_IDLE"

    def _start_the_model_late(rec: dict[str, object], index: int) -> None:
        # Poll 0 is the snapshot and poll 1 identifies the turn, so by poll 6 the
        # backstop has had four opportunities — twice the confirmations it needs.
        if index == 6:
            rec["idle_checks_before_model_started"] = rec["idle_checks"]
            steps = rec["steps"]
            assert isinstance(steps, list)
            steps.append(
                _planner_step(status="CORTEX_STEP_STATUS_DONE", text="finally got started")
            )

    injected["on_poll"] = _start_the_model_late

    events = _drive_gate(tmp_path, monkeypatch, idle_every=1, idle_confirmations=2)
    # The backstop was never consulted while the turn had no model activity, so
    # no amount of idle could have closed it early.
    assert injected["idle_checks_before_model_started"] == 0
    # And the turn completes on real terminal evidence, not on the idle window.
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response == "finally got started"


def test_trajectory_mutation_under_the_gate_fails_rather_than_guesses(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Pin behaviour when agy's trajectory LIST changes shape mid-turn.

    The gate holds a list index across polls, which is only valid while agy
    appends. Whether agy ever prunes, compacts or reorders is unverified (the
    read driver keys identity on ``(trajectory_id, step_index)`` for exactly that
    reason). If the recorded position stops pointing at this turn's user input,
    the window after it may describe a different turn — so the gate reports a
    failure naming the mutation instead of classifying it. The failure direction
    is the point: never a success on a window the gate can no longer trust.
    """
    _seed_state(tmp_path)
    injected["steps"] = [
        _user_step("previous question"),
        _planner_step(status="CORTEX_STEP_STATUS_DONE", text="previous answer"),
    ]
    injected["reply"] = [
        _user_step("implement the thing"),
        _tool_step("CORTEX_STEP_STATUS_RUNNING"),
    ]

    def _prune_history_after_the_gate_locks_on(rec: dict[str, object], index: int) -> None:
        # Poll 0 = snapshot, poll 1 = the gate identifying the turn at index 2.
        # Drop the two history steps before poll 2, shifting every later index.
        if index == 2:
            steps = rec["steps"]
            assert isinstance(steps, list)
            del steps[0:2]

    injected["on_poll"] = _prune_history_after_the_gate_locks_on

    # A real (if tiny) poll delay so that, WITHOUT the re-validation, the gate
    # merely runs out its budget instead of spinning: the assertions below then
    # distinguish "detected the mutation" from "gave up eventually".
    async def _short_sleep(_seconds: float) -> None:
        await asyncio.sleep(0.01)

    monkeypatch.setattr(executor_mod, "_sleep", _short_sleep)

    events = _drive_gate(tmp_path, monkeypatch, completion_timeout_s=0.5)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert not any(isinstance(event, TurnComplete) for event in events)
    assert "changed shape under the completion gate" in events[0].message
    assert "NOT confirmed" in events[0].message


# ---------------------------------------------------------------------------
# enqueue_session_message (mid-turn steering)
# ---------------------------------------------------------------------------


def test_enqueue_session_message_delivers(tmp_path: Path, injected: dict[str, object]) -> None:
    """``enqueue_session_message`` injects the steer via the same TUI path and returns True."""
    _seed_state(tmp_path)
    result = asyncio.run(_executor(tmp_path).enqueue_session_message("main", "steer me"))
    assert result is True
    assert _injected(injected)[0]["content"] == "steer me"


def test_enqueue_session_message_empty_returns_false(
    tmp_path: Path, injected: dict[str, object]
) -> None:
    """Enqueuing empty content returns False without injecting."""
    _seed_state(tmp_path)
    result = asyncio.run(_executor(tmp_path).enqueue_session_message("main", ""))
    assert result is False
    assert _injected(injected) == []


def test_enqueue_session_message_inject_failure_returns_false(
    tmp_path: Path, injected: dict[str, object]
) -> None:
    """A failed TUI inject during enqueue returns False."""
    _seed_state(tmp_path)
    injected["raise"] = RuntimeError("boom")
    result = asyncio.run(_executor(tmp_path).enqueue_session_message("main", "steer"))
    assert result is False


# ---------------------------------------------------------------------------
# interrupt_session (real interrupt via CancelCascadeSteps)
# ---------------------------------------------------------------------------


def test_interrupt_session_cancels_and_returns_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``interrupt_session`` resolves the port + cascade id and cancels, returning True.

    A successful ``cancel_cascade_steps`` against the discovered agy means the
    running cascade was asked to stop, so the executor reports the interrupt
    succeeded.
    """
    _seed_state(tmp_path)
    seen: dict[str, object] = {}

    def _resolve_port(conversation_id: str) -> int | None:
        seen["resolved_for"] = conversation_id
        return _PORT

    def _cancel(port: int, cascade_id: str) -> bool:
        seen["cancel"] = {"port": port, "cascade_id": cascade_id}
        return True

    monkeypatch.setattr(executor_mod, "resolve_language_server_port", _resolve_port)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", _cancel)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is True
    assert seen["resolved_for"] == _CONVERSATION_ID
    assert seen["cancel"] == {"port": _PORT, "cascade_id": _CONVERSATION_ID}


def test_interrupt_session_rpc_failure_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A failed ``cancel_cascade_steps`` makes ``interrupt_session`` return False.

    ``cancel_cascade_steps`` fails open (returns False) on any RPC/transport
    error, and the executor must honestly relay that the interrupt did not land
    rather than claiming success.
    """
    _seed_state(tmp_path)
    monkeypatch.setattr(executor_mod, "resolve_language_server_port", lambda _conv: _PORT)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", lambda _port, _cid: False)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False


def test_interrupt_session_no_port_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    With no resolvable agy port, ``interrupt_session`` returns False without cancelling.

    A turn cannot be interrupted on an agy that cannot be located, so the
    executor reports failure and never calls cancel.
    """
    _seed_state(tmp_path)
    called = {"cancel": False}

    def _cancel(_port: int, _cid: str) -> bool:
        called["cancel"] = True
        return True

    monkeypatch.setattr(executor_mod, "resolve_language_server_port", lambda _conv: None)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", _cancel)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False
    assert called["cancel"] is False


def test_interrupt_session_placeholder_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    On a placeholder (no real conversation yet), interrupt returns False, no cancel.

    There is no live cascade to cancel before agy has minted its real id, so the
    executor must not RPC against the ``agy_conv_*`` placeholder.
    """
    _seed_state(tmp_path, conversation_id=_PLACEHOLDER_ID)
    called = {"resolve": False, "cancel": False}

    def _resolve_port(_conv: str) -> int | None:
        called["resolve"] = True
        return _PORT

    def _cancel(_port: int, _cid: str) -> bool:
        called["cancel"] = True
        return True

    monkeypatch.setattr(executor_mod, "resolve_language_server_port", _resolve_port)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", _cancel)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False
    assert called["cancel"] is False


def test_interrupt_session_missing_state_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    With no bridge state, ``interrupt_session`` returns False without cancelling.

    No bridge state means no cascade id to address, so the interrupt is a no-op
    reported as failure.
    """
    called = {"cancel": False}

    def _cancel(_port: int, _cid: str) -> bool:
        called["cancel"] = True
        return True

    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", _cancel)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False
    assert called["cancel"] is False


# ---------------------------------------------------------------------------
# model resolution helpers
# ---------------------------------------------------------------------------


def test_latest_requested_model_picks_latest_user_input() -> None:
    """
    ``_latest_requested_model`` returns the most recent USER_INPUT step's model.

    Echoing agy's CURRENT model means scanning for the LAST USER_INPUT step
    (a later turn may have switched models), not the first or the last step.
    """
    from omnigent.inner.antigravity_native_executor import _latest_requested_model

    assert _latest_requested_model(_steps_with_model(_ECHOED_MODEL)) == _ECHOED_MODEL


def test_latest_requested_model_none_when_absent() -> None:
    """
    ``_latest_requested_model`` returns ``None`` when no USER_INPUT model is present.

    An empty step list (first turn) or steps without a ``planModel`` must signal
    "nothing to echo" so the caller falls back to the recommended model.
    """
    from omnigent.inner.antigravity_native_executor import _latest_requested_model

    assert _latest_requested_model([]) is None
    assert (
        _latest_requested_model([{"stepIndex": 0, "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE"}])
        is None
    )


def test_latest_requested_model_falls_back_to_requested_model() -> None:
    """
    ``_latest_requested_model`` reads the legacy ``requestedModel.model`` shape.

    The live wire carries ``plannerConfig.planModel`` (a string), but a
    TUI-origin step may still use the older ``requestedModel.model`` dict shape.
    The executor must honor that fallback so such a turn's model still echoes.
    """
    from omnigent.inner.antigravity_native_executor import _latest_requested_model

    legacy_steps: list[dict[str, object]] = [
        {
            "stepIndex": 0,
            "type": "CORTEX_STEP_TYPE_USER_INPUT",
            "userInput": {
                "userConfig": {
                    "plannerConfig": {"requestedModel": {"model": "MODEL_PLACEHOLDER_M20"}}
                }
            },
        },
    ]
    assert _latest_requested_model(legacy_steps) == "MODEL_PLACEHOLDER_M20"


def test_recommended_model_picks_recommended_entry() -> None:
    """
    ``_recommended_model`` returns the ``recommended`` catalog entry's enum.

    The fallback model must be the one agy marks ``recommended`` so a first turn
    uses agy's own default rather than an arbitrary catalog entry.
    """
    from omnigent.inner.antigravity_native_executor import _recommended_model

    catalog: dict[str, object] = {
        "models": {
            "a": {"model": "MODEL_A", "recommended": False},
            "b": {"model": "MODEL_B", "recommended": True},
        }
    }
    assert _recommended_model(catalog) == "MODEL_B"


def test_recommended_model_none_when_absent() -> None:
    """
    ``_recommended_model`` returns ``None`` when no entry is recommended.

    A catalog with no ``recommended`` model (or a malformed one) must signal
    "no model" so the caller surfaces a clear error instead of guessing.
    """
    from omnigent.inner.antigravity_native_executor import _recommended_model

    assert _recommended_model({"models": {}}) is None
    assert _recommended_model({"models": {"a": {"model": "MODEL_A"}}}) is None
    assert _recommended_model({}) is None


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_init_requires_bridge_dir_env_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Constructing without a bridge dir or env var raises ``RuntimeError``.

    The harness always spawns with ``HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR``
    set; a missing value means the runner wiring is broken, which must fail loud
    rather than read a bogus path.
    """
    monkeypatch.delenv("HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR", raising=False)
    with pytest.raises(RuntimeError, match="HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR"):
        AntigravityNativeExecutor()


# ---------------------------------------------------------------------------
# reasoning_effort validation (F-M5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_run_turn_valid_effort_is_accepted(
    tmp_path: Path, injected: dict[str, object], effort: str
) -> None:
    """
    A valid Antigravity effort level (low/medium/high) does not block delivery.

    agy's Gemini backend supports these three levels. A valid effort in the
    config must not surface as an error — the executor validates it and proceeds
    to inject the turn into the TUI.

    :param tmp_path: Bridge directory (injected by pytest).
    :param injected: Stub recording TUI injects.
    :param effort: One valid effort level to test.
    :returns: None.
    """
    from omnigent.inner.executor import ExecutorConfig

    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="",
                config=ExecutorConfig(extra={"reasoning_effort": effort}),
            )
        ]

    events = asyncio.run(_drive())
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)


@pytest.mark.parametrize("bad_effort", ["xhigh", "max", "none", "minimal"])
def test_run_turn_unsupported_effort_surfaces_error(
    tmp_path: Path, injected: dict[str, object], bad_effort: str
) -> None:
    """
    An effort level unsupported by Antigravity/Gemini yields an ExecutorError.

    ``xhigh`` and ``max`` are OpenAI/Anthropic-only; ``none`` and ``minimal``
    are OpenAI-only. Passing them to an Antigravity turn should surface a
    clear non-retryable error so the caller does not silently ignore the
    mismatch.

    :param tmp_path: Bridge directory.
    :param injected: Stub recording TUI injects.
    :param bad_effort: An effort level that is invalid for Antigravity.
    :returns: None.
    """
    from omnigent.inner.executor import ExecutorConfig

    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="",
                config=ExecutorConfig(extra={"reasoning_effort": bad_effort}),
            )
        ]

    events = asyncio.run(_drive())
    assert _injected(injected) == [], "delivery must not happen on bad effort"
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert bad_effort in events[0].message


# ---------------------------------------------------------------------------
# _content_to_text flattening
# ---------------------------------------------------------------------------


def test_content_to_text_handles_string_blocks_none_and_other(tmp_path: Path) -> None:
    """
    Flattening covers every content shape the executor may receive.

    A plain string passes through; ``input_text``/``text`` blocks join by newline
    while an unmaterializable image/file block contributes a visible
    could-not-load marker; ``None`` yields ``""``; any other shape falls back to
    a JSON encoding rather than crashing.
    """
    from omnigent.inner.antigravity_native_executor import _content_to_text

    assert _content_to_text("  hello  ", tmp_path) == "hello"
    assert (
        _content_to_text(
            [
                {"type": "input_text", "text": "a"},
                # malformed data URI (no comma) -> not materialized
                {"type": "input_image", "image_url": "data:image/png;base64"},
                {"type": "text", "text": "b"},
            ],
            tmp_path,
        )
        == "[Attachment attachment could not be loaded]\na\nb"
    )
    assert _content_to_text(None, tmp_path) == ""
    # Defensive fallback for an unexpected shape: encoded, not crashed.
    assert _content_to_text(123, tmp_path) == "123"


def test_gate_releases_on_interrupt_without_claiming_completion(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Interrupting a gated turn releases the gate as CANCELLED, not as complete.

    Stop must not have to wait out the completion budget, and an interrupted
    turn must never be reported as finished work.
    """
    from omnigent.inner.executor import TurnCancelled

    _seed_state(tmp_path)
    injected["reply"] = [
        _user_step("implement the thing"),
        _tool_step("CORTEX_STEP_STATUS_RUNNING"),
    ]
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", lambda _p, _c: True)
    monkeypatch.setattr(executor_mod, "_TURN_COMPLETION_TIMEOUT_S", 3600.0)
    executor = _executor(tmp_path)

    # Interrupt between the gate's first and second polls, the way a user hitting
    # stop mid-turn does.
    async def _interrupt_instead_of_sleeping(_seconds: float) -> None:
        await executor.interrupt_session("main")

    monkeypatch.setattr(executor_mod, "_sleep", _interrupt_instead_of_sleeping)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in executor.run_turn(
                messages=[{"role": "user", "content": "implement the thing"}],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_drive())
    assert len(events) == 1
    assert isinstance(events[0], TurnCancelled)
    assert not any(isinstance(event, TurnComplete) for event in events)


def test_interrupt_arriving_before_the_turn_starts_is_not_lost(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A stop that lands before ``run_turn`` begins cancels that turn, and only it.

    The harness registers the turn context before it starts iterating
    ``run_turn``, so an interrupt can arrive in that window. Clearing the
    interrupt flag at turn entry dropped such a stop and left the gate waiting
    out its budget. The turn must be reported cancelled WITHOUT delivering text
    for a request the user already stopped — and the NEXT turn must be
    unaffected, or a consumed stop would poison every later turn.
    """
    from omnigent.inner.executor import TurnCancelled

    _seed_state(tmp_path)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", lambda _p, _c: True)
    executor = _executor(tmp_path)

    async def _stop_then_run() -> list[ExecutorEvent]:
        await executor.interrupt_session("main")
        return [
            event
            async for event in executor.run_turn(
                messages=[{"role": "user", "content": "cancelled before it started"}],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_stop_then_run())
    assert len(events) == 1
    assert isinstance(events[0], TurnCancelled)
    assert _injected(injected) == [], "a stopped turn must not be typed into the TUI"

    # The stop was consumed by that turn; a fresh turn runs normally.
    follow_up = asyncio.run(_run(executor, "the next turn"))
    assert len(follow_up) == 1
    assert isinstance(follow_up[0], TurnComplete)
    assert _injected(injected)[0]["content"] == "the next turn"

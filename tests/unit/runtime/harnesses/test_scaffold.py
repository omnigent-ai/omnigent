"""
Tests for :class:`omnigent.runtime.harnesses._scaffold.HarnessApp`.

End-to-end through real subprocesses spawned via the same
:class:`HarnessProcessManager` used in production. Each test
selects one of the fixture subclasses in
``_test_scaffold_harnesses.py`` by setting the
``HARNESS_TEST_FIXTURE`` env var before the subprocess starts;
this lets the registry stay a single string while parametrizing
behavior per test.

Tests intentionally use a short tmp parent dir under ``/tmp/``
because macOS's ``AF_UNIX`` socket path limit is ~104 chars and
pytest's default ``tmp_path`` exceeds that.

Tests that need a side-channel request (``tool_result``,
``interrupt``, or ``approval`` event) concurrent with an open
streaming response use a SEPARATE httpx client built via
:func:`_make_side_client`. Reusing the manager's stream client for
the side request blocks behind the keepalive of the held
streaming connection and surfaces as a ``httpx.ReadError``
mid-stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.errors import ErrorCode
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.runtime.harnesses._scaffold import HarnessApp, TurnContext
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.runtime.tool_output import MAX_TOOL_OUTPUT_BYTES

_TEST_HARNESS_NAME = "scaffold_fixture"
_TEST_HARNESS_MODULE = "tests.unit.runtime.harnesses._test_scaffold_harnesses"

# Unit tests for cap_tool_output itself live in tests/runtime/test_tool_output.py
# (mirroring its source module). The integration test below proves the cap is
# wired into the scaffold's dispatch_tool emit path.
_TRUNCATION_MARKER = "[output truncated by omnigent:"


@dataclass
class _ParsedSSEEvent:
    """
    Single parsed SSE event captured from a streaming response.

    :param event: The SSE event name from the ``event:`` line,
        e.g. ``"response.output_text.delta"``.
    :param data: The JSON-decoded payload from the ``data:`` line.
    """

    event: str
    data: dict[str, Any]


async def _stream_iter(
    response: httpx.Response,
) -> AsyncIterator[_ParsedSSEEvent]:
    """
    Yield parsed SSE events from an open streaming response.

    Splits on the blank-line terminator and re-buffers any
    trailing partial frame across chunks. Useful for tests that
    react to events as they arrive (e.g. PATCH after seeing
    action_required, then continue consuming).

    :param response: An open streaming response from
        ``client.stream("POST", ...)``.
    :yields: Parsed events one by one.
    """
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, _, buffer = buffer.partition("\n\n")
            event_line = next(
                (line for line in frame.splitlines() if line.startswith("event:")),
                None,
            )
            data_line = next(
                (line for line in frame.splitlines() if line.startswith("data:")),
                None,
            )
            if event_line is None or data_line is None:
                continue
            event_name = event_line[len("event:") :].strip()
            data_payload = json.loads(data_line[len("data:") :].strip())
            yield _ParsedSSEEvent(event=event_name, data=data_payload)


def _make_side_client(socket_path: str) -> httpx.AsyncClient:
    """
    Build a SEPARATE httpx client bound to the same Unix socket.

    Tests that need to issue a ``tool_result`` / ``interrupt`` /
    ``approval`` event concurrently with an open streaming
    response use this so the second request gets its own TCP
    connection — sharing one client across both ends of the
    simulated round-trip can block the second request behind the
    first's keepalive connection (the symptom is a httpx.ReadError
    on the streaming side mid-test).

    :param socket_path: Absolute Unix socket path the harness
        bound, fetched via :meth:`HarnessProcessManager.socket_path`.
    :returns: An :class:`httpx.AsyncClient` ready to issue
        side-channel requests.
    """
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=socket_path),
        base_url="http://harness.local",
    )


@pytest.mark.asyncio
async def test_build_terminal_event_waits_for_run_task_failure() -> None:
    """
    Terminal synthesis must wait for the run task to fully settle.

    The stream loop reaches ``_build_terminal_event`` after reading the
    sentinel queued from ``run_turn``'s ``finally`` block. At that point the
    task can still be in the last scheduling tick before its exception is
    visible. Treating that as success emits the wrong terminal event; letting
    the task exception escape makes the server surface a generic final-response
    failure.
    """

    async def _late_failure() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("terminal boom")

    ctx = TurnContext("resp_late_failure", asyncio.Queue(), asyncio.Event())
    task = asyncio.create_task(_late_failure())

    terminal = await HarnessApp()._build_terminal_event(
        ctx,
        model="test-agent",
        run_task=task,
        sequence=7,
    )

    assert terminal.type == "response.failed"
    assert terminal.sequence_number == 7
    assert terminal.response.status == "failed"
    assert terminal.response.error is not None
    assert terminal.response.error.message == "terminal boom"


@pytest.mark.asyncio
async def test_build_terminal_event_handles_pending_task_cancellation() -> None:
    """
    A cancellation racing terminal synthesis should become response.cancelled.

    This guards the cancel/terminal path: the builder should classify the
    cancellation and return a terminal event instead of raising while reading
    task state.
    """

    task_started = asyncio.Event()

    async def _wait_forever() -> None:
        task_started.set()
        await asyncio.sleep(60)

    ctx = TurnContext("resp_cancel_race", asyncio.Queue(), asyncio.Event())
    task = asyncio.create_task(_wait_forever())
    await task_started.wait()
    ctx.cancelled.set()
    task.cancel()

    terminal = await HarnessApp()._build_terminal_event(
        ctx,
        model="test-agent",
        run_task=task,
        sequence=3,
    )

    assert terminal.type == "response.cancelled"
    assert terminal.sequence_number == 3
    assert terminal.response.status == "cancelled"


@pytest.mark.asyncio
async def test_build_terminal_event_preserves_stream_task_cancellation() -> None:
    """
    Cancelling terminal synthesis itself must not become response.completed.

    ``asyncio.shield(run_task)`` keeps the harness run task alive when the
    stream task is cancelled, but the outer cancellation still needs to
    propagate so teardown can cancel the run task through the normal path.
    """

    task_started = asyncio.Event()

    async def _wait_forever() -> None:
        task_started.set()
        await asyncio.sleep(60)

    ctx = TurnContext("resp_stream_cancel", asyncio.Queue(), asyncio.Event())
    run_task = asyncio.create_task(_wait_forever())
    await task_started.wait()
    terminal_task = asyncio.create_task(
        HarnessApp()._build_terminal_event(
            ctx,
            model="test-agent",
            run_task=run_task,
            sequence=5,
        )
    )

    try:
        await asyncio.sleep(0)
        terminal_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await terminal_task
        assert not run_task.done()
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task


@pytest.fixture
def register_fixture_harness() -> Iterator[None]:
    """Register the scaffold fixture harness module for the test."""
    _HARNESS_MODULES[_TEST_HARNESS_NAME] = _TEST_HARNESS_MODULE
    try:
        yield
    finally:
        _HARNESS_MODULES.pop(_TEST_HARNESS_NAME, None)


@pytest.fixture
def short_tmp_parent() -> Iterator[Path]:
    """Per-test parent directory under /tmp with a short path."""
    parent = Path("/tmp") / f"omni-sc-{uuid.uuid4().hex[:8]}"
    parent.mkdir(mode=0o700)
    try:
        yield parent
    finally:
        shutil.rmtree(parent, ignore_errors=True)


@pytest.fixture
async def manager(
    short_tmp_parent: Path,
    register_fixture_harness: None,
) -> AsyncIterator[HarnessProcessManager]:
    """A started manager rooted in a short tmp dir."""
    mgr = HarnessProcessManager(
        idle_timeout_s=60.0,
        reaper_interval_s=60.0,
        tmp_parent=short_tmp_parent,
    )
    await mgr.start()
    try:
        yield mgr
    finally:
        await mgr.shutdown()


# ── Per-fixture-harness selectors ──────────────────────────────
#
# Each fixture below sets the ``HARNESS_TEST_FIXTURE`` env var
# the runner subprocess reads in
# ``tests/runtime/harnesses/_test_scaffold_harnesses.py:create_app``
# to pick which HarnessApp subclass to instantiate. Tests opt in
# by depending on the fixture they need (e.g.
# ``async def test_x(use_echo, manager): ...``) — declarative,
# self-documenting in the signature, impossible to forget.


@pytest.fixture
def use_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the echo fixture harness for this test."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "echo")


@pytest.fixture
def use_tool_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the tool-dispatch fixture harness for this test."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "tool_dispatch")


@pytest.fixture
def use_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the usage fixture harness for this test."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "usage")


@pytest.fixture
def use_elicitation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the elicitation fixture harness for this test."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "elicitation")


@pytest.fixture
def use_cancellable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the cancellable fixture harness for this test."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "cancellable")


@pytest.fixture
def use_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the injection fixture harness for this test."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "injection")


@pytest.fixture
def use_native_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the native-tool fixture harness for this test."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "native_tool")


@pytest.fixture
def use_fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the fast-heartbeat fixture harness for this test."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "fast_heartbeat")


@pytest.fixture
def use_unclassified_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the unclassified-exception fixture harness for this test."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "unclassified_exception")


@pytest.fixture
def use_shutdown_tracking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the shutdown-tracking fixture harness for this test."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "shutdown_tracking")


@pytest.fixture
def use_wedged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the wedged harness; 2s watchdog (scaffold reads the env at import)."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "wedged")
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "2")


@pytest.fixture
def use_busy_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the busy-progress harness; 2s idle watchdog (read at import)."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "busy_progress")
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "2")


@pytest.fixture
def use_wedged_fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the wedged-with-fast-heartbeats harness; 2s idle watchdog."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "wedged_fast_heartbeat")
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "2")


@pytest.fixture
def use_busy_absolute_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Busy harness, 2s absolute ceiling below its ~3s runtime; idle high so only the cap fires."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "busy_progress")
    # Idle high + reset on every emit → never trips; only the absolute cap can fire.
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "10")
    monkeypatch.setenv("HARNESS_TURN_ABSOLUTE_TIMEOUT_S", "2")


# ── Native function_call emission ──────────────────────────────


async def test_subclass_can_emit_paired_function_call_items(
    use_native_tool: None,
    manager: HarnessProcessManager,
) -> None:
    """A subclass can emit function_call + function_call_output directly.

    Verifies the §Sub-agent representation pattern: harness-native
    sub-agents (Claude Code Task, OpenAI Agents handoff) surface
    as a paired function_call (status: completed) +
    function_call_output emitted directly via ctx.emit, NOT going
    through ctx.dispatch_tool (which is for server-dispatched
    tools that need to park on a PATCH).
    """
    conv_id = "conv_native"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []
    async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
        async for event in _stream_iter(response):
            events.append(event)

    output_items = [e for e in events if e.event == "response.output_item.done"]
    # Exactly two output_item.done events: the function_call
    # (already-completed, not action_required) and the paired
    # function_call_output. AP-side resolver treats these as
    # "observed" and forwards client-bound; doesn't try to
    # dispatch.
    assert len(output_items) == 2
    fc_item = output_items[0].data["item"]
    fco_item = output_items[1].data["item"]
    assert fc_item["type"] == "function_call"
    assert fc_item["status"] == "completed"
    assert fc_item["name"] == "Task"
    assert fco_item["type"] == "function_call_output"
    # The pair correlates via call_id — if these don't match,
    # consumers can't render them as a single sub-agent
    # invocation.
    assert fc_item["call_id"] == fco_item["call_id"]
    assert fco_item["output"] == "subagent done"


# ── Heartbeat metadata ────────────────────────────────────────


async def test_heartbeat_carries_server_time_and_last_event_seq(
    use_fast_heartbeat: None,
    manager: HarnessProcessManager,
) -> None:
    """
    The streaming wrapper stamps ``server_time`` and
    ``last_event_seq`` on every emitted ``response.heartbeat``
    event per contract §Heartbeats.

    Uses the ``fast_heartbeat`` fixture (overrides
    ``_heartbeat_loop`` to fire every 0.2s, sleeps 0.6s in
    ``run_turn``) so at least one heartbeat is captured during a
    sub-second turn — production cadence is 15s which is too long
    for a unit test.

    What breaks if this fails: consumers can't detect clock drift
    (no server_time) or dropped events (no last_event_seq),
    regressing the contract's dead-detection promise. Step 5i's
    "AP raises retryable ExecutorError after ~50s of silence"
    behavior would still work without these fields, but richer
    diagnostics depend on them.
    """
    conv_id = "conv_heartbeat"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []
    async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
        async for event in _stream_iter(response):
            events.append(event)

    heartbeats = [e for e in events if e.event == "response.heartbeat"]
    # At least one heartbeat must fire during the 0.6s turn at
    # 0.2s cadence. Zero would mean either the heartbeat task
    # wasn't started or the turn returned before the first
    # interval — either way, regression in scaffold lifecycle.
    assert len(heartbeats) >= 1, (
        f"expected ≥1 heartbeat in a 0.6s turn at 0.2s cadence, "
        f"got {len(heartbeats)}. All event types received: "
        f"{[e.event for e in events]}"
    )

    # Find the first heartbeat that follows a non-heartbeat event;
    # the wrapper's ``last_event_seq`` should equal that prior
    # event's sequence number. The fast_heartbeat fixture emits
    # ``warmup`` text first so this is guaranteed to exist.
    warmup_index = next(i for i, e in enumerate(events) if e.event == "response.output_text.delta")
    warmup_seq = events[warmup_index].data["sequence_number"]
    later_heartbeat = next(
        e for i, e in enumerate(events) if i > warmup_index and e.event == "response.heartbeat"
    )

    # server_time: ISO 8601 UTC string with trailing 'Z'.
    # Wrapper-stamped at yield time, not heartbeat-construction
    # time, so format must match _utc_now_iso's output.
    server_time = later_heartbeat.data.get("server_time")
    assert server_time is not None, (
        "heartbeat after warmup is missing server_time — wrapper "
        "didn't stamp it (regression in _stream_response_body)"
    )
    assert server_time.endswith("Z"), f"server_time {server_time!r} not ISO-UTC"
    # Parsing back proves it's a real timestamp, not a placeholder
    # string. A future change that hardcodes "now" or returns a
    # dataclass repr would fail this.
    import datetime

    parsed = datetime.datetime.strptime(server_time, "%Y-%m-%dT%H:%M:%SZ")
    assert parsed.year >= 2026

    # last_event_seq: the sequence_number of the warmup event
    # (the most recent non-heartbeat at the moment the heartbeat
    # was yielded). If this is None, the wrapper didn't track
    # the prior event; if it's wrong, the wrapper used the
    # heartbeat's own seq (off-by-one bug).
    last_event_seq = later_heartbeat.data.get("last_event_seq")
    assert last_event_seq == warmup_seq, (
        f"heartbeat last_event_seq={last_event_seq}, expected "
        f"warmup's seq={warmup_seq}. If equal to the heartbeat's "
        f"own sequence_number, the wrapper stamped after "
        f"incrementing instead of before."
    )


# ── Unclassified-exception robustness ────────────────────────


async def test_run_turn_raise_synthesizes_response_failed_event(
    use_unclassified_exception: None,
    manager: HarnessProcessManager,
) -> None:
    """
    When ``run_turn`` raises an unclassified exception, the SSE
    stream MUST end with a synthesized ``response.failed`` event,
    not a bare connection close.

    Direct repro of the 2026-04-29 user-reported
    ``[llm] ReadError`` symptom. Pre-fix, an exception in
    ``run_turn`` (or any other point in the streaming loop) caused
    the response stream to close mid-flight without emitting the
    terminal event. AP-side ``the harness HTTP client.run_turn``
    saw ``httpx.ReadError`` reading a closed stream → workflow
    catch-all rendered the unhelpful ``[llm] ReadError`` to the
    REPL.

    Post-fix, the scaffold's last-line-of-defense except clause
    in ``_stream_turn`` synthesizes a ``response.failed`` carrying
    ``code="RuntimeError"`` (from the exception class name) and
    yields it before the generator exits. AP-side translates the
    failed event to a normal ``ExecutorError`` and the REPL
    renders ``[llm] RuntimeError: simulated mid-turn failure``.

    Failure mode this catches: any future regression that drops
    the synthesized failure path (e.g., a refactor that removes
    the except clause, or an exception in
    ``_synthesize_failed_event`` itself that propagates).
    """
    conv_id = "conv_raise"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []
    async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
        async for event in _stream_iter(response):
            events.append(event)

    event_types = [e.event for e in events]
    assert "response.failed" in event_types, (
        f"Expected a terminal response.failed event after run_turn "
        f"raised mid-flight; got {event_types!r}. If the list ends "
        f"with anything other than response.failed (or "
        f"response.completed when the harness recovered), the "
        f"scaffold's last-line-of-defense terminal-event guarantee "
        f"regressed — AP-side will see httpx.ReadError instead of "
        f"a meaningful semantic code."
    )
    # The terminal event must be the LAST event on the stream;
    # any frames after it would be dead frames the consumer
    # would either ignore or get confused by.
    assert event_types[-1] == "response.failed", (
        f"response.failed must be the final event; got "
        f"{event_types!r}. Trailing events after the terminal "
        f"break the contract."
    )

    failed_data = events[-1].data
    assert failed_data["response"]["status"] == "failed", (
        f"Synthesized terminal must carry status='failed', got "
        f"{failed_data['response']['status']!r}."
    )
    error = failed_data["response"]["error"]
    assert error is not None, (
        f"Synthesized response.failed must populate error; got "
        f"{failed_data!r}. Without the error envelope, AP's "
        f"retry-classification has nothing to read and falls back "
        f"to permanent failure."
    )
    # Default base-class error_detail uses ``type(exc).__name__``
    # as the code. The fixture raises ``RuntimeError`` so we
    # expect that exact string.
    assert error["code"] == "RuntimeError", (
        f"Expected error.code='RuntimeError' (from "
        f"type(exception).__name__); got {error['code']!r}. If "
        f"the code is something else, the synthesizer is using a "
        f"different classifier than _build_error_detail."
    )
    assert "simulated mid-turn failure" in error["message"], (
        f"Error message should propagate str(exception); got "
        f"{error['message']!r}. Without the original message in "
        f"the envelope, operators have to dig through harness "
        f"logs to identify the failure cause."
    )

    # The warmup delta the fixture emitted before raising MUST
    # have made it through too. Proves the synthesized failure
    # path doesn't suppress prior in-flight events; it just adds
    # the missing terminal.
    assert "response.output_text.delta" in event_types, (
        f"Expected the warmup delta to land before the synthesized "
        f"failure; got {event_types!r}. If the delta is missing, "
        f"the except clause is somehow dropping prior queue events "
        f"on the way out."
    )


async def test_per_turn_watchdog_fails_wedged_turn(
    use_wedged: None,
    manager: HarnessProcessManager,
) -> None:
    """
    A wedged ``run_turn`` must terminate with ``response.failed`` once
    the watchdog fires, not stream heartbeats forever.

    The test completing at all is half the assertion: a regressed
    watchdog would hang the stream drain to the suite pytest-timeout.
    """
    conv_id = "conv_wedged"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []
    # Above the 2s watchdog, below the suite timeout: a regression fails here.
    async with asyncio.timeout(30):
        async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
            async for event in _stream_iter(response):
                events.append(event)

    event_types = [e.event for e in events]
    assert event_types[-1] == "response.failed", (
        f"Wedged turn must terminate with response.failed once the "
        f"watchdog fires; got {event_types!r}."
    )
    error = events[-1].data["response"]["error"]
    assert error is not None and "watchdog" in error["message"], (
        f"Watchdog failure must carry an explanatory error message; got {error!r}."
    )


async def test_per_turn_watchdog_allows_active_turn_past_window(
    use_busy_progress: None,
    manager: HarnessProcessManager,
) -> None:
    """
    A turn that keeps emitting progress must complete even when its
    total duration exceeds the watchdog window.

    The watchdog is an *idle* timeout: each non-heartbeat event resets
    the deadline, so an orchestrator turn that legitimately runs for
    minutes (tests + build + many tool calls) isn't killed mid-turn. The
    ``busy_progress`` harness emits a delta every 0.1s for ~3s against a
    2s watchdog. This test FAILS on the old fixed-cumulative watchdog
    (which fired at 2s → ``response.failed``), which is exactly the
    nessie bug being fixed.
    """
    conv_id = "conv_busy_progress"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []
    # The ~3s turn must outlast the 2s watchdog; 20s caps a regression
    # (cumulative watchdog still fails fast at 2s, so this is only a
    # guard against an unrelated hang).
    async with asyncio.timeout(20):
        async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
            async for event in _stream_iter(response):
                events.append(event)

    event_types = [e.event for e in events]
    # response.completed proves the idle watchdog let the active turn run
    # past its 2s window. response.failed here means the cumulative
    # watchdog fired mid-stream (the regression / unfixed behavior).
    assert event_types[-1] == "response.completed", (
        f"An actively-streaming turn must complete, not be killed by the "
        f"watchdog; got terminal {event_types[-1]!r} (full: {event_types!r})."
    )
    # The turn ran its full cadence: ~30 deltas. A much smaller count
    # would mean the stream was cut short before the turn finished.
    delta_count = sum(1 for t in event_types if t == "response.output_text.delta")
    assert delta_count >= 25, (
        f"Expected ~30 progress deltas from the full cadence; got "
        f"{delta_count}. A low count means the turn was truncated."
    )


async def test_per_turn_watchdog_ignores_heartbeats(
    use_wedged_fast_heartbeat: None,
    manager: HarnessProcessManager,
) -> None:
    """
    Heartbeats must NOT reset the idle watchdog.

    ``response.heartbeat`` is keep-alive, not progress. If it reset the
    deadline, a wedged turn emitting heartbeats every 15s would never
    fail. The ``wedged_fast_heartbeat`` harness hangs forever while
    firing a 0.2s heartbeat against a 2s watchdog (~10 heartbeats inside
    the window); the watchdog must still fire and terminate the turn.
    """
    conv_id = "conv_wedged_fast_heartbeat"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []
    # Above the 2s watchdog, below the suite timeout. If heartbeats wrongly
    # reset the deadline the stream never terminates and this timeout trips
    # — a clean failure rather than a silent pass.
    async with asyncio.timeout(20):
        async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
            async for event in _stream_iter(response):
                events.append(event)

    event_types = [e.event for e in events]
    # Heartbeats fired throughout, but the wedged turn made no real
    # progress — the idle watchdog must ignore the heartbeats and fail.
    assert event_types[-1] == "response.failed", (
        f"Wedged turn must fail despite ongoing heartbeats; got "
        f"{event_types[-1]!r}. If response.completed/no-terminal, "
        f"heartbeats wrongly reset the idle watchdog."
    )
    # At least one heartbeat must have actually fired inside the window —
    # otherwise this test would pass even if heartbeat-reset were broken.
    assert "response.heartbeat" in event_types, (
        f"Expected heartbeats during the wedged window; got {event_types!r}. "
        f"Without them this test wouldn't exercise heartbeat-vs-watchdog."
    )
    error = events[-1].data["response"]["error"]
    assert error is not None and "watchdog" in error["message"], (
        f"Watchdog failure must carry an explanatory error message; got {error!r}."
    )


async def test_per_turn_absolute_watchdog_caps_runaway_active_turn(
    use_busy_absolute_cap: None,
    manager: HarnessProcessManager,
) -> None:
    """
    The absolute ceiling fails a turn that keeps emitting but never ends.

    The idle watchdog never trips an actively-streaming turn (each emit
    resets it), so a runaway-but-active loop needs the absolute backstop.
    The ``busy_progress`` harness emits every 0.1s for ~3s; with idle=10s
    (reset on every emit → never trips) and absolute=2s, the absolute cap
    must fire ~1s before the harness's natural completion.
    """
    conv_id = "conv_busy_absolute_cap"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []
    # Above the 2s cap, below the ~3s natural completion's headroom and the
    # suite timeout; 20s only guards an unrelated hang.
    async with asyncio.timeout(20):
        async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
            async for event in _stream_iter(response):
                events.append(event)

    event_types = [e.event for e in events]
    # response.failed (not completed) proves the absolute ceiling fired
    # before the harness's ~3s natural completion. response.completed would
    # mean the cap didn't engage.
    assert event_types[-1] == "response.failed", (
        f"Absolute ceiling must fail a runaway-but-active turn; got "
        f"{event_types[-1]!r} (full: {event_types!r})."
    )
    # The error must name the ABSOLUTE watchdog, not the idle one — proves
    # the right ceiling tripped (idle was set high and reset by every emit).
    error = events[-1].data["response"]["error"]
    assert error is not None and "absolute" in error["message"], (
        f"Failure must come from the absolute ceiling; got {error!r}. An "
        f"'idle'-only message would mean the idle watchdog fired instead."
    )
    # Deltas streamed before the cap fired (it was actively emitting, not
    # wedged) — distinguishes this from the idle/wedged path.
    assert "response.output_text.delta" in event_types, (
        f"Expected progress deltas before the absolute cap; got {event_types!r}."
    )


# ── Health probe ──────────────────────────────────────────────


async def test_health_endpoint_returns_status_ok(
    use_echo: None,
    manager: HarnessProcessManager,
) -> None:
    """GET /health returns {"status": "ok"} at root, NOT /v1.

    Per the design's API table: the health probe is the one
    endpoint mounted at root rather than under /v1, matching AP's
    own /health route.
    """
    client = await manager.get_client("conv_health", _TEST_HARNESS_NAME)
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    # /v1/health should NOT exist — that would be a misconfiguration.
    bad_resp = await client.get("/v1/health")
    assert bad_resp.status_code == 404


async def test_scaffold_response_completed_preserves_context_tokens(
    use_usage: None,
    manager: HarnessProcessManager,
) -> None:
    """Terminal usage preserves ``context_tokens`` from the inner executor.

    Billing totals (``total_tokens`` summed across sub-calls) are
    split from context-fill totals (``context_tokens`` = last
    sub-call total for multi-call executors like openai-agents).
    The harness scaffold converts the inner executor's usage dict
    into the wire ``Usage`` model before emitting
    ``response.completed``. If that conversion drops
    ``context_tokens``, the REPL/web context ring falls back to the
    inflated billing sum and tool-call turns look like they compacted.
    """
    conv_id = "conv_usage"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    events: list[_ParsedSSEEvent] = []
    async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
        async for event in _stream_iter(response):
            events.append(event)

    completed = next(e for e in events if e.event == "response.completed")
    usage = completed.data["response"]["usage"]
    assert usage["total_tokens"] == 10_800
    assert usage["context_tokens"] == 5_700, (
        "context_tokens must survive scaffold serialization so clients can "
        "render context fill from the last sub-call instead of the billing sum."
    )


async def test_scaffold_response_completed_preserves_cache_tokens(
    use_usage: None,
    manager: HarnessProcessManager,
) -> None:
    """Terminal usage preserves the cache-read / cache-creation counts.

    The harness scaffold converts the inner executor's usage dict
    into the wire :class:`Usage` model before emitting
    ``response.completed``. Anthropic-style executors (e.g.
    claude-sdk) report ``input_tokens`` as the *non-cached* portion
    plus separate ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens`` counts. If that conversion drops
    the cache counts (the bug this guards), the server-side cost path
    never sees them and ``total_cost_usd`` silently reverts to the
    cache-blind ``input*price + output*price`` formula — a large,
    systematic undercount in an agent loop where the system prompt
    and history are cached almost every turn.
    """
    conv_id = "conv_usage_cache"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    events: list[_ParsedSSEEvent] = []
    async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
        async for event in _stream_iter(response):
            events.append(event)

    completed = next(e for e in events if e.event == "response.completed")
    usage = completed.data["response"]["usage"]
    # The fixture's provider_usage carries 8_000 cache-read + 2_000
    # cache-creation tokens. A missing key (KeyError) or a 0 would mean
    # the scaffold's Usage construction dropped the cache breakdown,
    # which is exactly what made the cache-aware cost computation inert.
    assert usage["cache_read_input_tokens"] == 8_000, (
        "cache_read_input_tokens must survive scaffold serialization so "
        "the server cost path can price cache hits at the reduced rate."
    )
    assert usage["cache_creation_input_tokens"] == 2_000, (
        "cache_creation_input_tokens must survive scaffold serialization so "
        "the server cost path can price cache writes at the premium rate."
    )


# ── Session-keyed surface (POST /events) ──────────────────────
#
# Exercises the discriminated
# /v1/sessions/{conversation_id}/events endpoint — the harness
# scaffold's only downward-event surface after the legacy
# /v1/responses routes were dropped. Each test verifies that a
# downward event variant (message / interrupt / tool_result /
# approval) dispatches into the same in-flight machinery
# (TurnContext / Future registries). Endpoint shape matches
# ``designs/session_rearchitecture.md`` §3 "Endpoints" /
# "Event types and direction".


async def test_session_message_event_streams_initial_then_text_then_completes(
    use_echo: None,
    manager: HarnessProcessManager,
) -> None:
    """A ``message`` event starts a turn and streams the standard envelope.

    Verifies the session-keyed equivalent of
    ``test_echo_streams_initial_then_text_then_completes``: the
    initial envelope, the subclass's text emit, and the terminal
    ``response.completed`` all flow through identically. If the
    discriminator dispatch regressed (e.g. silently no-op'd on
    ``message``), the streaming shape would diverge.
    """
    conv_id = "conv_session_echo"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    events: list[_ParsedSSEEvent] = []
    async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
        async for event in _stream_iter(response):
            events.append(event)

    assert [e.event for e in events] == [
        "response.created",
        "response.in_progress",
        "response.output_text.delta",
        "response.completed",
    ]
    # The same allocation rule applies — turn id is the harness's
    # response_id from the underlying _post_responses.
    turn_id = events[0].data["response"]["id"]
    assert turn_id.startswith("resp_")
    assert events[-1].data["response"]["status"] == "completed"
    # Echo payload survives — proves the body actually reached
    # ``run_turn`` through the new route, not just an empty
    # delegation that lost the request.
    assert "hi" in events[2].data["delta"]


async def test_session_events_404s_on_conversation_id_mismatch(
    use_echo: None,
    manager: HarnessProcessManager,
) -> None:
    """``POST /v1/sessions/<wrong>/events`` returns 404, not 200.

    The harness scaffold is per-conversation. A request addressed
    to the wrong conversation indicates the caller routed to the
    wrong subprocess — fail loud rather than silently start a
    turn under a mismatched id (which would produce a turn that
    Omnigent could never correlate).
    """
    conv_id = "conv_session_mismatch"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    resp = await client.post(
        "/v1/sessions/conv_does_not_match/events",
        json={
            "type": "message",
            "role": "user",
            "model": "test-agent",
            "content": [],
        },
    )
    assert resp.status_code == 404
    body = resp.json()
    # Error envelope shape matches OmnigentError's serialization
    # (see _handle_omnigent_error). Without this, AP-side error
    # handling would have to special-case session 404s.
    assert "error" in body
    assert body["error"]["code"] == ErrorCode.NOT_FOUND


async def test_session_tool_result_event_resolves_parked_dispatch(
    use_tool_dispatch: None,
    manager: HarnessProcessManager,
) -> None:
    """A ``tool_result`` event resolves the in-flight turn's parked Future.

    Starts a turn via a ``message`` event, observes
    ``action_required``, then POSTs a ``tool_result`` event to
    the same ``/events`` endpoint with the matching ``call_id``.
    Verifies the parked Future resolves and the subclass receives
    the output — proves the discriminated surface dispatches
    into the right entry of the in-flight registry.
    """
    conv_id = "conv_session_tool"
    stream_client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    side_client = _make_side_client(str(manager.socket_path(conv_id)))
    events: list[_ParsedSSEEvent] = []
    start_body = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [],
    }
    try:
        async with stream_client.stream(
            "POST",
            f"/v1/sessions/{conv_id}/events",
            json=start_body,
        ) as response:
            patched = False
            async for event in _stream_iter(response):
                events.append(event)
                if (
                    not patched
                    and event.event == "response.output_item.done"
                    and event.data["item"].get("status") == "action_required"
                ):
                    patch_resp = await side_client.post(
                        f"/v1/sessions/{conv_id}/events",
                        json={
                            "type": "tool_result",
                            "call_id": "call_test_1",
                            "output": "ok",
                        },
                    )
                    assert patch_resp.status_code == 204
                    patched = True
        # Subclass received the event-delivered output through the
        # session-keyed surface — proves shared in-flight state.
        deltas = [e.data["delta"] for e in events if e.event == "response.output_text.delta"]
        assert any("got:ok" in d for d in deltas), (
            f"expected the tool_result-delivered output to reach run_turn; got deltas={deltas!r}"
        )
        # A small output flows through the cap untouched — guards against an
        # over-eager cap that truncates everything, which the large-input cap
        # test alone would not catch.
        streamed = [
            e.data["item"]["output"]
            for e in events
            if e.event == "response.output_item.done"
            and e.data["item"].get("type") == "function_call_output"
        ]
        assert streamed == ["ok"]
        assert _TRUNCATION_MARKER not in streamed[0]
        assert events[-1].event == "response.completed"
    finally:
        await side_client.aclose()


async def test_session_tool_result_over_cap_streams_truncated_but_returns_full(
    use_tool_dispatch: None,
    manager: HarnessProcessManager,
) -> None:
    """A multi-MB tool_result is streamed truncated, but run_turn gets it full.

    Proves the output cap end-to-end: the function_call_output the scaffold
    streams + persists is bounded (so the client never receives one giant SSE
    frame), while ``ctx.dispatch_tool`` still returns the FULL result to the
    harness's ``run_turn`` — the live model's view is unaffected.
    ``_ToolDispatchHarness`` echoes the returned value as a ``got:<result>``
    text delta, so the delta length proves the return was not truncated.
    """
    conv_id = "conv_session_tool_cap"
    stream_client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    side_client = _make_side_client(str(manager.socket_path(conv_id)))
    # 2 MiB — comfortably over the 1 MiB cap so truncation is unambiguous.
    big_output = "x" * (2 * 1024 * 1024)
    start_body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []
    try:
        async with stream_client.stream(
            "POST",
            f"/v1/sessions/{conv_id}/events",
            json=start_body,
        ) as response:
            patched = False
            async for event in _stream_iter(response):
                events.append(event)
                if (
                    not patched
                    and event.event == "response.output_item.done"
                    and event.data["item"].get("status") == "action_required"
                ):
                    patch_resp = await side_client.post(
                        f"/v1/sessions/{conv_id}/events",
                        json={
                            "type": "tool_result",
                            "call_id": "call_test_1",
                            "output": big_output,
                        },
                    )
                    assert patch_resp.status_code == 204
                    patched = True
        assert events[-1].event == "response.completed"

        # The streamed function_call_output is capped: it carries the truncation
        # notice and is far smaller than the 2 MiB input. Without the cap wiring
        # this would be the full 2 MiB payload — one giant client frame.
        streamed_outputs = [
            e.data["item"]["output"]
            for e in events
            if e.event == "response.output_item.done"
            and e.data["item"].get("type") == "function_call_output"
        ]
        assert len(streamed_outputs) == 1, (
            f"expected exactly one function_call_output; got {len(streamed_outputs)}"
        )
        streamed = streamed_outputs[0]
        assert _TRUNCATION_MARKER in streamed, "streamed output was not truncated (cap not wired)"
        # Capped size = kept bytes + the short notice (< ~100 bytes), never the
        # multi-MB original. If this fails, truncation produced the wrong size.
        assert len(streamed.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES + 200
        assert len(streamed.encode("utf-8")) < len(big_output)

        # ...but run_turn received the FULL result via dispatch_tool's return:
        # the echoed got:<result> delta is the whole 2 MiB. This is the load-
        # bearing guarantee — capping the mirror must not shrink the model's view.
        got_deltas = [
            e.data["delta"]
            for e in events
            if e.event == "response.output_text.delta" and e.data["delta"].startswith("got:")
        ]
        assert len(got_deltas) == 1, f"expected one got: delta; got {len(got_deltas)}"
        assert got_deltas[0] == f"got:{big_output}", "dispatch_tool return was truncated"
    finally:
        await side_client.aclose()


async def test_session_tool_result_event_404s_on_conversation_id_mismatch(
    use_tool_dispatch: None,
    manager: HarnessProcessManager,
) -> None:
    """A ``tool_result`` posted to a wrong conversation_id 404s.

    Verifies the conversation_id check fires before any
    tool-result resolution: a stray event addressed to the wrong
    conversation must NOT silently succeed. Stale ``call_id``
    entries within the correct conversation are allowed to
    no-op (loose-by-default), but a mismatched conversation_id
    must fail loud.
    """
    conv_id = "conv_session_patch_mismatch"
    stream_client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    side_client = _make_side_client(str(manager.socket_path(conv_id)))
    start_body = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [],
    }
    try:
        async with stream_client.stream(
            "POST",
            f"/v1/sessions/{conv_id}/events",
            json=start_body,
        ) as response:
            handled = False
            async for event in _stream_iter(response):
                if (
                    not handled
                    and event.event == "response.output_item.done"
                    and event.data["item"].get("status") == "action_required"
                ):
                    bad = await side_client.post(
                        "/v1/sessions/conv_wrong/events",
                        json={
                            "type": "tool_result",
                            "call_id": "call_test_1",
                            "output": "ok",
                        },
                    )
                    assert bad.status_code == 404
                    # Resolve via the correctly-addressed event so
                    # the stream finalizes cleanly.
                    good = await side_client.post(
                        f"/v1/sessions/{conv_id}/events",
                        json={
                            "type": "tool_result",
                            "call_id": "call_test_1",
                            "output": "ok",
                        },
                    )
                    assert good.status_code == 204
                    handled = True
            assert handled, "action_required never arrived"
    finally:
        await side_client.aclose()


async def test_session_interrupt_event_cancels_in_flight_turn(
    use_cancellable: None,
    manager: HarnessProcessManager,
) -> None:
    """An ``interrupt`` event cancels the in-flight turn.

    Verifies that an interrupt sets ``ctx.cancelled``. The
    fixture subclass observes it and emits ``"cancelled"`` before
    returning — terminal event is ``response.cancelled``.
    """
    conv_id = "conv_session_cancel"
    stream_client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    side_client = _make_side_client(str(manager.socket_path(conv_id)))
    events: list[_ParsedSSEEvent] = []
    start_body = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [],
    }
    try:
        async with stream_client.stream(
            "POST",
            f"/v1/sessions/{conv_id}/events",
            json=start_body,
        ) as response:
            cancelled = False
            async for event in _stream_iter(response):
                events.append(event)
                if not cancelled and event.event == "response.in_progress":
                    cancel_resp = await side_client.post(
                        f"/v1/sessions/{conv_id}/events",
                        json={"type": "interrupt"},
                    )
                    assert cancel_resp.status_code == 204
                    cancelled = True
        text_deltas = [e for e in events if e.event == "response.output_text.delta"]
        assert text_deltas, "expected a delta from the fixture before terminate"
        assert text_deltas[0].data["delta"] == "cancelled"
        assert events[-1].event == "response.cancelled"
    finally:
        await side_client.aclose()


async def test_session_interrupt_event_404s_on_conversation_id_mismatch(
    use_echo: None,
    manager: HarnessProcessManager,
) -> None:
    """An ``interrupt`` event with a wrong conversation_id 404s.

    The interrupt path is one of the most likely places for a
    stale routing decision to surface (cancel can arrive after
    a turn ended); the conversation_id check must fire even when
    no turn is in flight.
    """
    conv_id = "conv_session_cancel_mismatch"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    resp = await client.post(
        "/v1/sessions/conv_other/events",
        json={"type": "interrupt"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == ErrorCode.NOT_FOUND


async def test_session_interrupt_event_404s_when_no_turn_in_flight(
    use_echo: None,
    manager: HarnessProcessManager,
) -> None:
    """Correct conversation_id + no in-flight turn returns 404.

    The harness has no concept of an idle interrupt — if no turn
    is in flight, there is nothing to cancel. Fail loud rather
    than silently no-op so a stray interrupt from Omnigent after a turn
    already ended surfaces as an obvious operator error.
    """
    conv_id = "conv_session_interrupt_idle"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    resp = await client.post(
        f"/v1/sessions/{conv_id}/events",
        json={"type": "interrupt"},
    )
    assert resp.status_code == 404


async def test_session_approval_event_resolves_elicitation(
    use_elicitation: None,
    manager: HarnessProcessManager,
) -> None:
    """An ``approval`` event resolves the parked elicitation Future.

    Verifies the request/reply correlation: the elicitation
    reply lands on the matching in-flight context's parked
    Future via ``POST /v1/sessions/{id}/events`` with
    ``type=approval``.
    """
    conv_id = "conv_session_elicit"
    stream_client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    side_client = _make_side_client(str(manager.socket_path(conv_id)))
    events: list[_ParsedSSEEvent] = []
    start_body = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [],
    }
    try:
        async with stream_client.stream(
            "POST",
            f"/v1/sessions/{conv_id}/events",
            json=start_body,
        ) as response:
            replied = False
            async for event in _stream_iter(response):
                events.append(event)
                if not replied and event.event == "response.elicitation_request":
                    reply = await side_client.post(
                        f"/v1/sessions/{conv_id}/events",
                        json={
                            "type": "approval",
                            "elicitation_id": "elicit_test_1",
                            "action": "accept",
                        },
                    )
                    assert reply.status_code == 204
                    replied = True
        text_deltas = [e for e in events if e.event == "response.output_text.delta"]
        assert len(text_deltas) == 1
        assert text_deltas[0].data["delta"] == "action:accept"
    finally:
        await side_client.aclose()


async def test_session_approval_event_404s_on_conversation_id_mismatch(
    use_elicitation: None,
    manager: HarnessProcessManager,
) -> None:
    """An ``approval`` event with a wrong conversation_id 404s.

    The conversation_id check must fire even when the
    elicitation_id is real (i.e. would resolve the Future on the
    correct surface). Otherwise a misrouted reply could silently
    resolve another conversation's elicitation in a multi-tenant
    misconfiguration.
    """
    conv_id = "conv_session_elicit_mismatch"
    stream_client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    side_client = _make_side_client(str(manager.socket_path(conv_id)))
    start_body = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [],
    }
    try:
        async with stream_client.stream(
            "POST",
            f"/v1/sessions/{conv_id}/events",
            json=start_body,
        ) as response:
            handled = False
            async for event in _stream_iter(response):
                if not handled and event.event == "response.elicitation_request":
                    bad = await side_client.post(
                        "/v1/sessions/conv_other/events",
                        json={
                            "type": "approval",
                            "elicitation_id": "elicit_test_1",
                            "action": "decline",
                        },
                    )
                    assert bad.status_code == 404
                    # Unblock the parked Future so the stream
                    # completes cleanly.
                    good = await side_client.post(
                        f"/v1/sessions/{conv_id}/events",
                        json={
                            "type": "approval",
                            "elicitation_id": "elicit_test_1",
                            "action": "decline",
                        },
                    )
                    assert good.status_code == 204
                    handled = True
            assert handled, "elicitation never arrived"
    finally:
        await side_client.aclose()


async def test_session_message_event_without_previous_response_id_injects_active_turn(
    use_injection: None,
    manager: HarnessProcessManager,
) -> None:
    """A second sessions-native message injects into the active turn.

    Verifies the sessions-native steering path where the caller
    does not know or provide ``previous_response_id``. While a turn
    is actively streaming, a second ``message`` event for the same
    conversation should be treated as steering and return 204, not
    start a competing streaming response. If this regresses, REPL
    mid-turn steering opens a second turn that races the original
    turn instead of reaching ``ctx.next_injection``.
    """
    conv_id = "conv_session_native_inject"
    stream_client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    side_client = _make_side_client(str(manager.socket_path(conv_id)))
    events: list[_ParsedSSEEvent] = []
    start_body = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [],
    }
    try:
        async with stream_client.stream(
            "POST",
            f"/v1/sessions/{conv_id}/events",
            json=start_body,
        ) as response:
            injected = False
            async for event in _stream_iter(response):
                events.append(event)
                if (
                    not injected
                    and event.event == "response.output_text.delta"
                    and event.data.get("delta") == "ready:"
                ):
                    inject_resp = await side_client.post(
                        f"/v1/sessions/{conv_id}/events",
                        json={
                            "type": "message",
                            "role": "user",
                            "model": "test-agent",
                            "content": [
                                {"type": "input_text", "text": "x"},
                                {"type": "input_text", "text": "y"},
                            ],
                        },
                    )
                    assert inject_resp.status_code == 204
                    injected = True
        text_deltas = [e.data["delta"] for e in events if e.event == "response.output_text.delta"]
        assert text_deltas == ["ready:", "got_2"]
    finally:
        await side_client.aclose()


async def test_interrupt_then_message_without_prev_id_starts_fresh_turn(
    use_cancellable: None,
    manager: HarnessProcessManager,
) -> None:
    """After an interrupt, a follow-up (no previous_response_id) starts a FRESH turn.

    Production bug: the follow-up was injected into the just-cancelled turn —
    whose session is mid-teardown — resuming the abandoned generation and
    leaving the agent one message behind. ``_handle_interrupt_event`` now clears
    ``_active_turn_ctx``, so a follow-up with no ``previous_response_id`` must
    start a new turn (HTTP 200 stream with its own ``response.created``), NOT a
    204 in-band injection into the dying turn.
    """
    conv_id = "conv_interrupt_fresh"
    stream_client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    side_client = _make_side_client(str(manager.socket_path(conv_id)))
    followup_client = _make_side_client(str(manager.socket_path(conv_id)))
    start_body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    followup_body = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [{"type": "input_text", "text": "new question"}],
    }
    followup_status: int | None = None
    followup_events: list[_ParsedSSEEvent] = []
    try:
        async with stream_client.stream(
            "POST", f"/v1/sessions/{conv_id}/events", json=start_body
        ) as response:
            interrupted = False
            async for event in _stream_iter(response):
                if not interrupted and event.event == "response.in_progress":
                    cancel_resp = await side_client.post(
                        f"/v1/sessions/{conv_id}/events", json={"type": "interrupt"}
                    )
                    assert cancel_resp.status_code == 204
                    interrupted = True
                    # Send the follow-up while the cancelled turn is still
                    # tearing down — it must start a fresh turn, not inject.
                    async with followup_client.stream(
                        "POST", f"/v1/sessions/{conv_id}/events", json=followup_body
                    ) as r2:
                        followup_status = r2.status_code
                        if r2.status_code == 200:
                            async for e2 in _stream_iter(r2):
                                followup_events.append(e2)
                                if e2.event == "response.created":
                                    break
                    break
        # 200 (fresh streaming turn), NOT 204 (in-band injection into the
        # cancelled turn — the bug). If 204, the follow-up resumed the dying
        # session and the agent would answer one message behind.
        assert followup_status == 200, (
            f"follow-up after interrupt must start a fresh turn (200), got {followup_status}"
        )
        assert any(e.event == "response.created" for e in followup_events), (
            "the fresh turn must emit its own response.created"
        )
    finally:
        await side_client.aclose()
        await followup_client.aclose()


async def test_interrupt_then_message_with_prev_id_starts_fresh_turn(
    use_cancellable: None,
    manager: HarnessProcessManager,
) -> None:
    """A follow-up that steers via the interrupted turn's id also starts fresh.

    A client may follow up using the in-progress response_id. After Stop, that
    id points at a cancelled turn whose session is mid-teardown — injecting into
    it (then dropping the injection) loses the message. The explicit
    previous_response_id branch must also skip a cancelled turn and start a fresh
    turn (HTTP 200 with a NEW response_id), not return 204.
    """
    conv_id = "conv_interrupt_fresh_previd"
    stream_client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    side_client = _make_side_client(str(manager.socket_path(conv_id)))
    followup_client = _make_side_client(str(manager.socket_path(conv_id)))
    start_body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    followup_status: int | None = None
    followup_events: list[_ParsedSSEEvent] = []
    turn_id: str | None = None
    try:
        async with stream_client.stream(
            "POST", f"/v1/sessions/{conv_id}/events", json=start_body
        ) as response:
            interrupted = False
            async for event in _stream_iter(response):
                if event.event == "response.created":
                    turn_id = event.data["response"]["id"]
                if not interrupted and event.event == "response.in_progress":
                    cancel_resp = await side_client.post(
                        f"/v1/sessions/{conv_id}/events", json={"type": "interrupt"}
                    )
                    assert cancel_resp.status_code == 204
                    interrupted = True
                    assert turn_id is not None
                    followup_body = {
                        "type": "message",
                        "role": "user",
                        "model": "test-agent",
                        "previous_response_id": turn_id,
                        "content": [{"type": "input_text", "text": "new question"}],
                    }
                    async with followup_client.stream(
                        "POST", f"/v1/sessions/{conv_id}/events", json=followup_body
                    ) as r2:
                        followup_status = r2.status_code
                        if r2.status_code == 200:
                            async for e2 in _stream_iter(r2):
                                followup_events.append(e2)
                                if e2.event == "response.created":
                                    break
                    break
        # 200 fresh turn, NOT 204 in-band injection into the cancelled turn.
        assert followup_status == 200, (
            f"follow-up steering the interrupted turn must start a fresh turn (200), "
            f"got {followup_status}"
        )
        fresh_ids = [
            e.data["response"]["id"] for e in followup_events if e.event == "response.created"
        ]
        assert fresh_ids and fresh_ids[0] != turn_id, "the fresh turn must have a new response id"
    finally:
        await side_client.aclose()
        await followup_client.aclose()


async def test_session_message_event_in_band_injection(
    use_injection: None,
    manager: HarnessProcessManager,
) -> None:
    """A ``message`` with ``previous_response_id`` injects in-band.

    Verifies the in-band POST routing works through the new
    surface — the second message event with
    ``previous_response_id == in-flight turn_id`` returns 204
    (not a streaming 200) and the subclass sees the injection.
    Without this, the new surface would silently start a second
    concurrent turn.
    """
    conv_id = "conv_session_inject"
    stream_client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    side_client = _make_side_client(str(manager.socket_path(conv_id)))
    events: list[_ParsedSSEEvent] = []
    start_body = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [],
    }
    try:
        async with stream_client.stream(
            "POST",
            f"/v1/sessions/{conv_id}/events",
            json=start_body,
        ) as response:
            injected = False
            async for event in _stream_iter(response):
                events.append(event)
                if (
                    not injected
                    and event.event == "response.output_text.delta"
                    and event.data.get("delta") == "ready:"
                ):
                    turn_id = next(
                        e.data["response"]["id"] for e in events if e.event == "response.created"
                    )
                    inject_resp = await side_client.post(
                        f"/v1/sessions/{conv_id}/events",
                        json={
                            "type": "message",
                            "role": "user",
                            "model": "test-agent",
                            "previous_response_id": turn_id,
                            "content": [
                                {"type": "input_text", "text": "x"},
                                {"type": "input_text", "text": "y"},
                            ],
                        },
                    )
                    assert inject_resp.status_code == 204
                    injected = True
        text_deltas = [e.data["delta"] for e in events if e.event == "response.output_text.delta"]
        assert text_deltas == ["ready:", "got_2"]
    finally:
        await side_client.aclose()


async def test_session_events_unknown_type_returns_422(
    use_echo: None,
    manager: HarnessProcessManager,
) -> None:
    """An unknown ``type`` field on the events body returns 422.

    Verifies the discriminated-union dispatcher rejects unknown
    variants at request-validation time (Pydantic). Without this,
    a typo in the runner's outgoing event body would silently
    no-op rather than failing loud per
    ``designs/DESIGN_PRINCIPLES.md``.
    """
    conv_id = "conv_session_422"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    resp = await client.post(
        f"/v1/sessions/{conv_id}/events",
        json={"type": "frobnicate", "frob": "nicate"},
    )
    assert resp.status_code == 422


# ── Lifespan shutdown hook ─────────────────────────


async def test_on_shutdown_hook_called_during_lifespan_teardown(
    use_shutdown_tracking: None,
    short_tmp_parent: Path,
    register_fixture_harness: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scaffold's ``on_shutdown`` hook fires during lifespan
    teardown, even when uvicorn overwrites the signal handlers.

    Spawns a ``_ShutdownTrackingHarness`` subprocess that writes a
    sentinel file in ``on_shutdown``. After a normal streaming turn,
    the manager releases the subprocess (SIGTERM → graceful shutdown
    → lifespan finally → ``on_shutdown``). The test checks the
    sentinel file exists.

    Before the fix (Fix A + E): uvicorn's signal
    handler overwrote the scaffold's, so ``_on_shutdown_signal`` was
    never called, ``on_shutdown`` was never invoked, and inner
    executor resources (e.g. child ``claude`` processes) leaked.

    :meta: Exercises Fix A (lifespan finally calls
        ``_on_shutdown_signal``) and Fix E (``on_shutdown`` hook).
    """
    import tempfile

    marker_fd, marker_path = tempfile.mkstemp(prefix="shutdown-marker-", dir=str(short_tmp_parent))
    os.close(marker_fd)
    os.unlink(marker_path)

    monkeypatch.setenv("HARNESS_SHUTDOWN_MARKER", marker_path)

    mgr = HarnessProcessManager(
        idle_timeout_s=60.0,
        reaper_interval_s=60.0,
        tmp_parent=short_tmp_parent,
    )
    await mgr.start()
    try:
        conv_id = "conv_shutdown_hook"
        client = await mgr.get_client(conv_id, _TEST_HARNESS_NAME)

        body = {
            "type": "message",
            "role": "user",
            "model": "test-agent",
            "content": "test",
        }
        events: list[_ParsedSSEEvent] = []
        async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
            async for event in _stream_iter(response):
                events.append(event)

        # Turn completed — the scaffold should have emitted
        # response.completed.
        terminal_events = [e for e in events if e.event == "response.completed"]
        assert len(terminal_events) == 1, (
            "Expected exactly one response.completed event; "
            "without it, the turn didn't finish cleanly."
        )

        # Release triggers SIGTERM → graceful shutdown → lifespan
        # finally → on_shutdown → sentinel write.
        await mgr.release(conv_id)

        import asyncio

        # Give the subprocess a moment to finish writing the
        # sentinel and exit.
        for _ in range(30):
            if Path(marker_path).exists():
                break
            await asyncio.sleep(0.1)

        # The sentinel file should exist, proving on_shutdown was
        # called. If missing, the lifespan's finally block didn't
        # invoke the hook — the bug this guards against.
        assert Path(marker_path).exists(), (
            f"Sentinel file {marker_path} was not written. "
            f"on_shutdown() was not called during lifespan teardown. "
            f"This is the root cause of child-process leaks."
        )
        content = Path(marker_path).read_text(encoding="utf-8")
        assert content == "shutdown_called"
    finally:
        await mgr.shutdown()

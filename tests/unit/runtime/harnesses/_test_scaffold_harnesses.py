"""
HarnessApp subclass fixtures for scaffold tests.

Each fixture exercises one slice of the scaffold contract — echo,
tool dispatch, elicitation, cancellation, injection. They share
the convention that ``create_app`` in this module reads an
environment variable (``HARNESS_TEST_FIXTURE``) to pick which
subclass to instantiate so the tests can register a single
module path in ``_HARNESS_MODULES`` and parametrize the fixture
selection per test.

Lives under ``tests/`` so it doesn't ship as production code.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI

from omnigent.runtime.harnesses._scaffold import HarnessApp, TurnContext
from omnigent.server.schemas import (
    CreateResponseRequest,
    ElicitationRequestParams,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
)

# Environment variable read at ``create_app`` time to select which
# fixture subclass to spawn. Each test sets this before triggering
# a process-manager spawn.
_FIXTURE_ENV_VAR = "HARNESS_TEST_FIXTURE"


class _EchoHarness(HarnessApp):
    """
    Trivial harness: emits a single ``response.output_text.delta``
    with the request input echoed back, then returns.

    Verifies the basic streaming + terminal-event path: SSE
    connection, sequence numbering, ``response.completed`` close.
    """

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        # Stringify the input list — tests assert the echoed text
        # contains a marker they injected.
        echoed = json.dumps(request.input or [])
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta=echoed))


class _UsageHarness(HarnessApp):
    """
    Emits provider usage with ``context_tokens`` and the cache
    breakdown set.

    Verifies the scaffold preserves both the context-fill field and
    the Anthropic-style cache-read / cache-creation token counts in
    the terminal ``response.completed`` event instead of dropping
    them while converting the inner executor usage dict to the wire
    :class:`Usage` model. The cache counts are what the server-side
    cost path prices at their own rates, so dropping them silently
    reverts cost to the cache-blind ``input+output`` formula.
    """

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        ctx.provider_usage = {
            "input_tokens": 10_300,
            "output_tokens": 500,
            "total_tokens": 10_800,
            "context_tokens": 5_700,
            "cache_read_input_tokens": 8_000,
            "cache_creation_input_tokens": 2_000,
        }
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="usage"))


class _ToolDispatchHarness(HarnessApp):
    """
    Emits a ``function_call`` (action_required), parks on the
    PATCH-delivered result, then echoes the result back as a text
    delta.

    Verifies: action_required emit, Future parking on
    ``ctx.dispatch_tool``, PATCH route resolves the Future,
    subclass receives the output string.
    """

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        result = await ctx.dispatch_tool(
            call_id="call_test_1",
            name="echo_tool",
            arguments='{"x": 1}',
            agent="test-agent",
        )
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta=f"got:{result}"))


class _ElicitationHarness(HarnessApp):
    """
    Emits an elicitation request, parks on the event-delivered
    reply, then emits the reply's action as a text delta.

    Verifies: elicitation_request emit, Future parking on
    ``ctx.elicit``, ``approval`` event on
    ``POST /v1/sessions/{id}/events`` resolves the Future, and
    the subclass receives the :class:`ElicitationResult`.
    """

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        result = await ctx.elicit(
            elicitation_id="elicit_test_1",
            params=ElicitationRequestParams(mode="form", message="approve?"),
        )
        ctx.emit(
            OutputTextDeltaEvent(
                type="response.output_text.delta", delta=f"action:{result.action}"
            )
        )


class _CancellableHarness(HarnessApp):
    """
    Sleeps in a poll loop checking ``ctx.cancelled`` every 50ms,
    then either emits ``"timeout"`` (if it sleeps the full 5s) or
    emits ``"cancelled"`` (if cancellation arrives mid-sleep).

    Verifies: cancel route sets the event, the subclass observes
    it, the terminal event becomes ``response.cancelled``.
    """

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        for _ in range(100):
            if ctx.cancelled.is_set():
                ctx.emit(
                    OutputTextDeltaEvent(type="response.output_text.delta", delta="cancelled")
                )
                return
            await asyncio.sleep(0.05)
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="timeout"))


class _InjectionHarness(HarnessApp):
    """
    Emits a starter delta, then waits up to 2 seconds for an
    injection. Echoes the injection's input length, then completes.

    Verifies: in-band injection routing — a ``message`` event
    on ``POST /v1/sessions/{id}/events`` whose
    ``previous_response_id`` matches the in-flight turn lands on
    the injection queue rather than starting a new turn, and the
    subclass observes it via ``ctx.next_injection``.
    """

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="ready:"))
        injection = await ctx.next_injection(timeout=2.0)
        if injection is None:
            ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="none"))
            return
        n = len(injection.input or [])
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta=f"got_{n}"))


class _NativeToolEmittingHarness(HarnessApp):
    """
    Emits a ``function_call`` + paired ``function_call_output``
    (both ``status: "completed"``) representing a harness-native
    tool call (e.g., what Claude Code's Task tool would surface
    per §Sub-agent representation).

    Verifies the subclass can emit the function_call and
    function_call_output items directly without going through
    ``dispatch_tool`` (which is for server-dispatched tools that
    park on a PATCH).
    """

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        # function_call item — already-completed (not action_required).
        ctx.emit(
            OutputItemDoneEvent(
                type="response.output_item.done",
                item={
                    "id": "fc_native",
                    "type": "function_call",
                    "status": "completed",
                    "name": "Task",
                    "arguments": '{"prompt": "subagent task"}',
                    "call_id": "call_native_1",
                    "agent": "test-agent",
                },
            )
        )
        # paired function_call_output.
        ctx.emit(
            OutputItemDoneEvent(
                type="response.output_item.done",
                item={
                    "id": "fco_native",
                    "type": "function_call_output",
                    "call_id": "call_native_1",
                    "output": "subagent done",
                },
            )
        )


class _FastHeartbeatHarness(HarnessApp):
    """
    Heartbeat-cadence override: fires every 0.2s instead of the
    production 15s, so an integration test can observe at least
    one heartbeat in a sub-second turn and assert its
    ``server_time`` + ``last_event_seq`` are populated by the
    streaming wrapper.

    The turn emits a single text delta (so the heartbeat that
    follows has a non-None ``last_event_seq``), then sleeps
    long enough for one heartbeat interval to fire, then
    returns.

    Overrides ``_heartbeat_loop`` rather than poking the module
    constant: the constant is read once at module import in the
    subprocess and a test-process monkeypatch wouldn't propagate.
    """

    async def _heartbeat_loop(self, ctx: TurnContext) -> None:
        # 0.2s gives ~3 heartbeats per 0.6s sleep below — enough
        # margin that a slow CI box still sees at least one before
        # ``run_turn`` returns and ``_teardown_turn`` cancels the
        # heartbeat task.
        from omnigent.server.schemas import HeartbeatEvent

        while True:
            await asyncio.sleep(0.2)
            ctx.emit(HeartbeatEvent(type="response.heartbeat"))

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        # Emit a non-heartbeat event first so the subsequent
        # heartbeat's ``last_event_seq`` is non-None — proves the
        # wrapper tracks the previous user-visible event correctly.
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="warmup"))
        # Sleep through ~4 heartbeat intervals so at least one
        # fires before the turn returns. 0.8s at 0.2s cadence
        # gives margin against asyncio scheduler jitter on slow
        # CI boxes — a tighter 0.6s budget would mean a single
        # 100ms hiccup could miss the third interval and leave
        # the test seeing only one heartbeat (still passes the
        # ``>= 1`` assertion, but loses the "after warmup"
        # heartbeat the test specifically looks for).
        await asyncio.sleep(0.8)


class _UnclassifiedExceptionHarness(HarnessApp):
    """
    Emits one warmup delta, then raises a bare ``RuntimeError``
    mid-turn.

    Verifies the scaffold's last-line-of-defense robustness:
    when ``run_turn`` raises an unfamiliar exception, the
    streaming response MUST still terminate with a synthesized
    ``response.failed`` event so consumers (the AP-side
    ``the harness HTTP client``) get a clean
    ``[llm] <code>: <message>`` instead of a bare
    ``httpx.ReadError``. See the 2026-04-29 12-shell user repro
    where Databricks gateway 429s broke the inner SDK's stream
    and the terminal event emission failed silently.
    """

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        ctx.emit(
            OutputTextDeltaEvent(
                type="response.output_text.delta",
                delta="warmup-before-raise",
            ),
        )
        # Tiny sleep so the emit lands before the raise — the
        # streaming wrapper picks up the delta from the queue
        # before reading the sentinel pushed by the
        # ``_guarded_run_turn`` finally on raise.
        await asyncio.sleep(0.05)
        raise RuntimeError("simulated mid-turn failure")


class _SlowStreamHarness(HarnessApp):
    """
    Slow-streaming harness: emits ``response.output_text.delta``
    events on a steady cadence for several seconds, polling
    ``ctx.cancelled`` between emits so an interrupt can stop the
    turn mid-stream.

    Used by the session-interrupt integration test to verify the
    Omnigent server → runner → harness interrupt path actually cancels
    the in-flight turn. ``_EchoHarness`` completes synchronously
    and is useless for this purpose because there is no turn left
    to interrupt by the time the test POSTs the interrupt event.

    Cadence: a delta every ~50ms for up to ~5 seconds (100 ticks).
    On cancel, returns early without emitting a final delta — the
    streaming wrapper synthesizes ``response.cancelled`` as the
    terminal event.

    :ivar _tick_seconds: Per-iteration sleep, exposed for tests
        that want to assert on cadence.
    """

    _tick_seconds: float = 0.05
    _max_ticks: int = 100

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        for i in range(self._max_ticks):
            if ctx.cancelled.is_set():
                return
            ctx.emit(
                OutputTextDeltaEvent(
                    type="response.output_text.delta",
                    delta=f"chunk-{i} ",
                )
            )
            # Sleep in small steps so the cancel event is observed
            # promptly even mid-tick.
            await asyncio.sleep(self._tick_seconds)


class _ShutdownTrackingHarness(HarnessApp):
    """
    Tracks whether :meth:`on_shutdown` was called during lifespan
    teardown.

    Writes a sentinel file at the path from the
    ``HARNESS_SHUTDOWN_MARKER`` env var when ``on_shutdown`` fires.
    The test checks for this file after the subprocess exits to
    verify the scaffold's lifespan ``finally`` block invokes the
    subclass hook.

    Also verifies Fix A: the ``_on_shutdown_signal`` call in the
    ``finally`` block happens unconditionally, even when uvicorn's
    own signal handler overwrote the scaffold's.
    """

    async def on_shutdown(self) -> None:
        """Write a sentinel file proving this method was called."""
        marker_path = os.environ.get("HARNESS_SHUTDOWN_MARKER")
        if marker_path:
            from pathlib import Path

            Path(marker_path).write_text("shutdown_called", encoding="utf-8")

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="hello"))


class _WedgedHarness(HarnessApp):
    """Hangs forever in ``run_turn`` — exercises the per-turn watchdog."""

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request, ctx
        await asyncio.Event().wait()  # never set; hang until cancelled


class _BusyProgressHarness(HarnessApp):
    """
    Emits ``response.output_text.delta`` on a steady sub-second cadence
    for longer than the per-turn watchdog window, then completes.

    Exercises the *idle-reset* watchdog: a turn that keeps emitting real
    progress events must reach ``response.completed`` even though its
    total duration exceeds ``HARNESS_TURN_TIMEOUT_S``. With the old
    fixed-*cumulative* watchdog this turn was cut to ``response.failed``
    mid-stream — that is the nessie "long orchestration turn killed
    before it finished" bug this fixture reproduces.

    Cadence: a delta every 0.1s for ~3s (30 ticks). Against the 2s
    watchdog the fixture sets, each gap (0.1s) is a 20x margin under the
    idle deadline, so a slow CI box can't spuriously trip it; yet the
    ~3s cumulative duration comfortably exceeds the 2s window the old
    cumulative watchdog enforced.
    """

    _tick_seconds: float = 0.1
    _max_ticks: int = 30

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request
        for i in range(self._max_ticks):
            ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta=f"tick-{i} "))
            await asyncio.sleep(self._tick_seconds)


class _WedgedFastHeartbeatHarness(HarnessApp):
    """
    Hangs forever in ``run_turn`` while emitting fast heartbeats.

    Exercises the load-bearing detail of the idle-reset watchdog:
    ``response.heartbeat`` is keep-alive, NOT progress, so it must NOT
    reset the idle deadline. With a 0.2s heartbeat against a 2s watchdog,
    ~10 heartbeats fire inside the window — if heartbeats reset the
    watchdog, the turn would never fail; the watchdog must still fire and
    terminate the wedged turn with ``response.failed``.

    Overrides ``_heartbeat_loop`` (not the module constant) because the
    constant is read once at subprocess import and a test-process
    monkeypatch wouldn't propagate.
    """

    async def _heartbeat_loop(self, ctx: TurnContext) -> None:
        from omnigent.server.schemas import HeartbeatEvent

        while True:
            await asyncio.sleep(0.2)
            ctx.emit(HeartbeatEvent(type="response.heartbeat"))

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        del request, ctx
        await asyncio.Event().wait()  # never set; hang until the watchdog fires


_FIXTURES: dict[str, type[HarnessApp]] = {
    "echo": _EchoHarness,
    "wedged": _WedgedHarness,
    "busy_progress": _BusyProgressHarness,
    "wedged_fast_heartbeat": _WedgedFastHeartbeatHarness,
    "usage": _UsageHarness,
    "tool_dispatch": _ToolDispatchHarness,
    "elicitation": _ElicitationHarness,
    "cancellable": _CancellableHarness,
    "injection": _InjectionHarness,
    "native_tool": _NativeToolEmittingHarness,
    "fast_heartbeat": _FastHeartbeatHarness,
    "unclassified_exception": _UnclassifiedExceptionHarness,
    "slow_stream": _SlowStreamHarness,
    "shutdown_tracking": _ShutdownTrackingHarness,
}


def create_app() -> FastAPI:
    """
    Build a fixture FastAPI app for whichever subclass the
    ``HARNESS_TEST_FIXTURE`` env var selects.

    :returns: The fixture's :class:`FastAPI` instance.
    :raises ValueError: If the env var is unset or names an
        unknown fixture (programming error in the test).
    """
    fixture_name = os.environ.get(_FIXTURE_ENV_VAR)
    if fixture_name is None:
        raise ValueError(
            f"{_FIXTURE_ENV_VAR} env var not set; tests must select a fixture "
            f"before spawning the runner"
        )
    fixture_cls = _FIXTURES.get(fixture_name)
    if fixture_cls is None:
        raise ValueError(f"unknown fixture {fixture_name!r}; available: {sorted(_FIXTURES)}")
    return fixture_cls().build()

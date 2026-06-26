"""Faithful Tier-B fault-injection regression for the #1026 turn-context desync.

Issue #1026 wedges a session **permanently** through a three-factor race:

1. An active turn has a tool dispatch IN FLIGHT (a parked dispatch future).
2. The harness/upstream connection drops on the policy-verdict-delivery POST —
   ``httpx.RemoteProtocolError: Server disconnected without sending a response``
   raised inside :func:`_evaluate_policy_via_omnigent` at the
   ``harness_client.post(...)`` callsite (``omnigent/runner/app.py``).
3. A NEW user message is buffered into the already-active turn (the
   ``post_session_events`` "buffering message for active turn" branch) racing
   with #2.

On the **pre-fix** code the active-turn binding was cleared but never rebound,
so every later inner-SDK callback orphaned, and the policy gate fell back to a
silent fail-OPEN (``ALLOW``) on the authoritative ``PHASE_TOOL_CALL`` gate. The
session then stayed wedged until a host-daemon restart.

PR #1077 fixes this with (a) a fail-CLOSED policy verdict for ``PHASE_TOOL_CALL``
when no turn context is bound, (b) a Tier-1 per-conversation SDK self-heal
(:meth:`ExecutorAdapter._maybe_resync_on_orphan`) once N consecutive orphans
accumulate, and (c) a runner-side verdict-delivery retry-then-signal that drives
``_resync_turn_state`` to publish a single ``runner_turn_context_desync``
terminal status (or hand off to a buffered continuation).

This module proves BOTH directions of the faithfulness gate, and — per
cross-review — does so **without manufacturing the symptom**:

* The negative control flips the fix OFF with the SMALLEST REAL toggles the
  production code reads — ``FAIL_CLOSED_PHASES`` (the constant the real policy
  evaluator consults) set to ``()`` and ``_ORPHAN_RESYNC_THRESHOLD`` set out of
  reach — and then exercises the REAL ``_stable_policy_evaluator`` /
  ``_stable_tool_executor`` / ``_maybe_resync_on_orphan``. The fail-OPEN +
  unbounded-orphan symptom therefore comes OUT OF production code; nothing is
  hand-rolled. With the constants at their real values the SAME real calls fail
  CLOSED and self-heal — that contrast is the proof.

* The headline test chains all three factors into ONE causal scenario:
  - factor #1 — a REAL in-flight harness turn started through the production
    scaffold entry (``_start_or_inject_turn``, which registers ``_in_flight`` /
    ``_active_turn_ctx``) whose executor parks a REAL tool dispatch through the
    production ``_stable_tool_executor`` → ``_bridge_one_dispatch`` →
    ``dispatch_tool`` path (no hand-inserted future);
  - factor #2 — the REAL verdict-POST drop firing the REAL retry-then-signal
    into the REAL ``_resync_turn_state``;
  - factor #3 — a REAL buffered message via the REAL ``post_session_events``
    route.
  The orphan callbacks then fire because the REAL harness turn teardown
  (``_stream_turn`` finally → ``_teardown_turn`` → ``run_turn`` finally, unwound
  by the REAL interrupt forward) cleared the context — NOT because a test poked
  ``_current_ctx = None``. Recovery is proved by a SUBSEQUENT REAL harness turn
  that rebinds a live ctx and dispatches a tool to a terminal ``TurnComplete``.

The full cross-process harness↔runner socket cannot be scripted deterministically
in one process (``omnigent/runner/app.py`` says as much where it exposes
``app.state.resync_turn_state`` as a test seam). This test stands in for that one
socket hop — the runner's interrupt forward closes the harness stream (cancels
the task draining the real ``_start_or_inject_turn`` StreamingResponse) — and
otherwise drives only real code at each callsite.

HONEST BOUNDARY (Tier-A): one thing is intentionally NOT asserted here — that the
RUNNER itself drives the buffered continuation to a terminal outcome (drain the
buffer → start a continuation turn → ``proxy_stream`` it to the harness → consume
its SSE → terminalize). That round-trip is the runner→harness streaming-dispatch
path, which the project exercises only via a REAL harness subprocess over a UDS
(see ``tests/runner/test_runner_dispatch.py`` /
``tests/runner/test_app_sessions_native.py``, which spawn real uvicorn harness
subprocesses). Reproducing it deterministically in-process would require a full
Tier-A integration harness (runner app + harness app + a StreamingResponse↔httpx
streaming-transport bridge + spec/MCP/content-resolution setup + multi-background-
task polling) — not a Tier-B unit test. Rather than re-stage it with fakes, this
module asserts the deterministic REAL recovery facts (the resync hands off to the
P1.7 continuation path, and a real recovered harness turn dispatches to terminal)
and leaves the runner-internal buffer→continuation→terminal stream to a Tier-A
integration test. The no-buffer runner recovery (``_resync_turn_state`` publishing
exactly one ``runner_turn_context_desync`` terminal status) IS proved here, since
that path needs no harness stream.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi.responses import StreamingResponse

import omnigent.runtime.harnesses._executor_adapter as adapter_mod
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorEvent,
    Message,
    ToolSpec,
    TurnComplete,
)
from omnigent.runner import create_runner_app
from omnigent.runner.app import (
    _RUNNER_TURN_CONTEXT_DESYNC_CODE,
    _evaluate_policy_via_omnigent,
)
from omnigent.runtime.harnesses._executor_adapter import (
    _ORPHAN_RESYNC_THRESHOLD,
    ExecutorAdapter,
)
from omnigent.runtime.harnesses._scaffold import ToolResultEvent
from omnigent.server.schemas import CreateResponseRequest
from tests.runner.helpers import NullServerClient

_ADAPTER_LOGGER = "omnigent.runtime.harnesses._executor_adapter"
_APP_LOGGER = "omnigent.runner.app"

# Capture the real default threshold at import (before any monkeypatch) so the
# negative control can size its orphan burst relative to the production value.
_ORPHAN_RESYNC_THRESHOLD_DEFAULT = _ORPHAN_RESYNC_THRESHOLD


# ── Real-object stubs (boundary fakes only — never the logic under test) ──


class _DispatchParkingExecutor(Executor):
    """Inner executor that parks a REAL tool dispatch through production code.

    Models only the inner-SDK boundary. Its ``run_turn`` invokes
    ``self._tool_executor`` — the bound :meth:`ExecutorAdapter._stable_tool_executor`
    the adapter installs on the executor instance — exactly as a real inner SDK
    does when the model calls a tool. That drives the real ``_bridge_one_dispatch``
    → ``TurnContext.dispatch_tool`` parking path, so the in-flight dispatch future
    is created by production code (not inserted by hand). The call awaits the
    result, so the turn stays in-flight until the dispatch is resolved or the
    turn is torn down; after it resolves, any configured terminal events flush.
    """

    def __init__(self, events: list[ExecutorEvent] | None = None) -> None:
        self._events = events or []
        self.interrupt_calls: list[str] = []
        self.close_calls = 0
        self.close_session_calls = 0

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        # ``_tool_executor`` is installed on this instance by the adapter before
        # it iterates run_turn (see ExecutorAdapter.run_turn). Real parking path.
        await self._tool_executor("Bash", {"command": "ls"})  # type: ignore[attr-defined]
        for event in self._events:
            yield event

    async def interrupt_session(self, session_key: str) -> bool:
        self.interrupt_calls.append(session_key)
        return True

    async def close(self) -> None:
        self.close_calls += 1

    async def close_session(self, session_key: str) -> None:
        del session_key
        self.close_session_calls += 1

    async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
        del session_key, content
        return True


class _OkServerClient:
    """Omnigent-server client whose ``/policies/evaluate`` returns ALLOW.

    Lets :func:`_evaluate_policy_via_omnigent` proceed to the *verdict-delivery*
    POST — the real callsite where factor #2 is injected.
    """

    async def post(self, _url: str, *, json: dict[str, Any], timeout: Any) -> httpx.Response:
        del json, timeout
        return httpx.Response(200, json={"result": "POLICY_ACTION_ALLOW", "reason": None})


class _DeadChannelHarnessClient:
    """Harness client whose verdict POST raises a real dead-channel error.

    Factor #2: the ``Server disconnected without sending a response`` drop on
    the policy-verdict-delivery POST, raised at the real ``harness_client.post``
    callsite inside :func:`_evaluate_policy_via_omnigent`.
    """

    def __init__(self, exc: BaseException) -> None:
        self.attempts = 0
        self._exc = exc

    async def post(self, _url: str, *, json: dict[str, Any], timeout: Any) -> httpx.Response:
        del json, timeout
        self.attempts += 1
        raise self._exc


class _HarnessInterruptClient:
    """Harness HTTP client modelling the runner→harness interrupt hop.

    The runner's ``_forward_harness_interrupt`` POSTs ``{"type": "interrupt"}``
    to the harness. In production the interrupt closes the runner's harness SSE
    stream, which makes the harness's ``_stream_turn`` tear the turn down. Here
    the harness turn is driven in-process by ``consume_task`` (the task draining
    the real :meth:`HarnessApp._start_or_inject_turn` StreamingResponse), so the
    interrupt cancels THAT — triggering the REAL ``_stream_turn`` finally →
    ``_teardown_turn`` → ``run_turn`` finally, which clears the turn context.

    ``stream`` is a benign empty SSE response: the runner's buffered-continuation
    kick (``_check_and_start_next_turn``) calls it, but driving that continuation
    to a real terminal outcome needs the full runner→harness streaming-dispatch
    path (Tier-A; see the module docstring's HONEST BOUNDARY). Nothing in these
    tests asserts on this stream — the recovery proof is the real recovered
    harness turn (F3), not this no-op.
    """

    def __init__(self, pm: _ChainProcessManager) -> None:
        self._pm = pm

    async def post(
        self, _url: str, *, json: dict[str, Any], timeout: Any = None
    ) -> httpx.Response:
        del timeout
        if json.get("type") == "interrupt":
            task = self._pm.consume_task
            if task is not None and not task.done():
                task.cancel()
        return httpx.Response(200, json={})

    def stream(self, _method: str, _url: str, **_kwargs: Any) -> _EmptyStream:
        return _EmptyStream()


class _EmptyStream:
    """Async-context-manager stub yielding a 200 response with no SSE frames."""

    status_code = 200
    headers: dict[str, str] = {}

    async def __aenter__(self) -> _EmptyStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def aiter_text(self) -> AsyncIterator[str]:
        return
        yield  # pragma: no cover - marks this an async generator


class _ChainProcessManager:
    """Process-manager stub for the three-factor chain.

    Holds the ``consume_task`` driving the real harness turn so the interrupt
    client can cancel it (the real teardown trigger).
    """

    handles_tool_dispatch = True

    def __init__(self) -> None:
        self._sessions: set[str] = set()
        self.consume_task: asyncio.Task[None] | None = None

    async def get_client(self, conversation_id: str, harness: str, env: Any = None) -> Any:
        del harness, env
        self._sessions.add(conversation_id)
        return _HarnessInterruptClient(self)

    def has_session(self, conversation_id: str) -> bool:
        return conversation_id in self._sessions

    def has_active_turn(self, conversation_id: str) -> bool:
        del conversation_id
        return False

    async def forward_cancel(self, conversation_id: str) -> bool:
        del conversation_id
        return True

    async def release(self, conversation_id: str) -> None:
        self._sessions.discard(conversation_id)


def _request(text: str = "hi") -> CreateResponseRequest:
    return CreateResponseRequest(model="agent", input=text)


def _tool_result(call_id: str, output: str) -> ToolResultEvent:
    return ToolResultEvent(type="tool_result", call_id=call_id, output=output)


def _drain_status_events(queues: dict[str, Any], conv_id: str) -> list[dict[str, Any]]:
    """Pop every queued ``session.status`` event for *conv_id*.

    :param queues: The app's per-session event-queue dict, i.e.
        ``app.state.session_event_queues``.
    """
    queue = queues.get(conv_id)
    out: list[dict[str, Any]] = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, dict) and event.get("type") == "session.status":
            out.append(event)
    return out


async def _start_turn_stream(
    adapter: ExecutorAdapter, request: CreateResponseRequest
) -> StreamingResponse:
    """Start a real harness turn and return its SSE StreamingResponse.

    A fresh ``message`` (no ``previous_response_id``) always starts a turn, so
    the production entry returns a :class:`StreamingResponse`, never a 204.
    """
    resp = await adapter._start_or_inject_turn(request)
    assert isinstance(resp, StreamingResponse), resp
    return resp


async def _drain_stream(body_iterator: Any) -> None:
    """Consume a harness StreamingResponse body so its real ``run_turn`` runs.

    Parks on the event queue once the turn parks on its dispatch — i.e. it stays
    alive as the in-flight turn until cancelled (the real interrupt teardown).
    """
    async for _chunk in body_iterator:
        pass


async def _spin_until(predicate: Any, *, limit: int = 200) -> None:
    """Yield to the loop until *predicate* holds (bounded)."""
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


# ── Isolated factor tests (each factor at its real callsite) ──────────────


async def test_factor2_verdict_post_remoteprotocolerror_retries_then_signals() -> None:
    """Factor #2: the verdict POST raising ``RemoteProtocolError`` signals desync.

    Drives the REAL :func:`_evaluate_policy_via_omnigent`. The dead-channel drop
    is raised at the real ``harness_client.post(...)`` callsite; the real
    retry-once-then-signal logic runs and fires ``on_delivery_failure`` exactly
    once with the conversation id.
    """
    signaled: list[str] = []

    async def _on_delivery_failure(conv_id: str) -> None:
        signaled.append(conv_id)

    harness = _DeadChannelHarnessClient(
        httpx.RemoteProtocolError("Server disconnected without sending a response")
    )
    await _evaluate_policy_via_omnigent(
        server_client=_OkServerClient(),
        harness_client=harness,
        conversation_id="conv_factor2",
        evaluation_id="poleval_factor2",
        phase="PHASE_TOOL_CALL",
        data={"name": "mcp__github__merge_pull_request", "arguments": {}},
        on_delivery_failure=_on_delivery_failure,
    )

    # Original attempt + one fresh-connection retry, then the desync signal.
    assert harness.attempts == 2
    assert signaled == ["conv_factor2"]


async def test_factor3_new_message_lands_in_real_buffer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Factor #3: a new message lands in the real ``buffering ...`` branch.

    Drives the REAL ``POST /v1/sessions/{conv}/events`` route while a turn is in
    flight (``conv in _active_turns``). The request reaches the real
    buffer-into-active-turn branch, returns 202 ``buffered``, and the message is
    parked in the runner's REAL ``_session_message_buffers`` (inspected via the
    ``app.state.session_message_buffers`` seam — not just status/log text).
    """
    conv = "conv_factor3"
    pm = _ChainProcessManager()
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    # A turn is active for this conversation (runner-side factor #1).
    app.state.active_turns[conv] = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        with caplog.at_level(logging.INFO, logger=_APP_LOGGER):
            resp = await client.post(
                f"/v1/sessions/{conv}/events",
                json={"type": "message", "role": "user", "content": "second message"},
            )

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "buffered"
    assert "buffering message for active turn" in caplog.text
    # The REAL buffer structure actually holds the message.
    buffered = app.state.session_message_buffers.get(conv)
    assert buffered, "the message must be parked in the real buffer"
    assert buffered[-1]["content"] == "second message"
    assert buffered[-1]["conversation_id"] == conv


# ── The three-factor causal chain (shared driver) ─────────────────────────


def _wedged_dispatch_call_id(adapter: ExecutorAdapter) -> str | None:
    """Return the call_id of the single parked dispatch, if any."""
    for ctx in adapter._in_flight.values():
        if ctx._pending_tool_calls:
            return next(iter(ctx._pending_tool_calls))
    return None


async def _run_three_factor_chain(adapter: ExecutorAdapter) -> tuple[Any, str]:
    """Wire factors 1→2→3 through real callsites and return the wedged state.

    Returns ``(app, conv)``. On return, the REAL harness turn teardown has run —
    triggered by the runner's REAL interrupt forward closing the harness stream
    (``_stream_turn`` finally → ``_teardown_turn`` → ``run_turn`` finally) — so
    ``adapter._current_ctx is None``. Any callback that fires now is genuinely
    orphaned by the chain, not by a test poking the slot.
    """
    conv = "conv_chain"
    pm = _ChainProcessManager()
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    app.state.session_event_queues.pop(conv, None)

    # Factor #1: a REAL in-flight harness turn that parks a REAL tool dispatch.
    # The turn is started through the production scaffold entry
    # (``_start_or_inject_turn`` registers ``_in_flight`` + ``_active_turn_ctx``
    # and spawns ``run_turn``); driving its StreamingResponse runs ``run_turn``,
    # whose executor invokes the installed ``_stable_tool_executor`` →
    # ``_bridge_one_dispatch`` → ``dispatch_tool``, parking the future in
    # production code.
    resp = await _start_turn_stream(adapter, _request("primary turn"))
    consume_task: asyncio.Task[None] = asyncio.create_task(_drain_stream(resp.body_iterator))
    pm.consume_task = consume_task
    await _spin_until(lambda: _wedged_dispatch_call_id(adapter) is not None)
    # Runner-side view: a stream-mode turn is in flight (the None sentinel).
    app.state.active_turns[conv] = None

    # Factor #3: a NEW user message is buffered into the active turn via the
    # REAL route — racing the verdict failure.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        buffered = await client.post(
            f"/v1/sessions/{conv}/events",
            json={"type": "message", "role": "user", "content": "queued during the drop"},
        )
    assert buffered.status_code == 202
    assert app.state.session_message_buffers.get(conv), "factor #3 must populate the real buffer"

    # Factor #2: the verdict-delivery POST drops at the real callsite; the real
    # retry-then-signal fires on_delivery_failure → the REAL _resync_turn_state,
    # whose interrupt forward closes the harness stream (cancels consume_task).
    async def _on_delivery_failure(cid: str) -> None:
        await app.state.resync_turn_state(cid, "verdict_delivery_channel_dead")

    harness = _DeadChannelHarnessClient(
        httpx.RemoteProtocolError("Server disconnected without sending a response")
    )
    await _evaluate_policy_via_omnigent(
        server_client=_OkServerClient(),
        harness_client=harness,
        conversation_id=conv,
        evaluation_id="poleval_chain",
        phase="PHASE_TOOL_CALL",
        data={"name": "mcp__github__merge_pull_request", "arguments": {}},
        on_delivery_failure=_on_delivery_failure,
    )
    assert harness.attempts == 2

    # Let the cancelled harness turn unwind its REAL teardown (clears ctx).
    await asyncio.gather(consume_task, return_exceptions=True)
    await _spin_until(lambda: adapter._current_ctx is None)
    return app, conv


async def test_three_factor_chain_self_heals_and_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fix LIVE: the chained desync fails CLOSED, self-heals, and recovers.

    Runs the connected three-factor chain (factor #1 parks a REAL dispatch via
    production code; factor #2's REAL verdict-POST drop signals the REAL resync
    whose interrupt tears the REAL harness turn down; factor #3 buffers a REAL
    message), then exercises the orphan callbacks the chain's REAL teardown made
    possible. With the fix live:

    * the policy no-ctx path returns DENY for ``PHASE_TOOL_CALL`` (fail-CLOSED,
      no silent "ALLOW");
    * consecutive orphan tool callbacks are BOUNDED — the Tier-1 watchdog fires
      and zeroes the counter (no unbounded pile-up);
    * the recovery hands off to the buffered continuation (P1.7): NO desync
      ``failed`` is published (the continuation will own the terminal edge);
    * a SUBSEQUENT REAL harness turn rebinds a live ctx and a tool callback
      during it actually DISPATCHES to a result through production code (not
      orphaned), reaching a terminal ``TurnComplete`` — the session recovered
      with no restart.

    HONEST BOUNDARY: that the RUNNER itself drives the buffered message to a
    terminal outcome (drain → stream a continuation turn → terminalize) is NOT
    asserted here — it needs the full runner→harness streaming-dispatch path
    (Tier-A; see module docstring). The recovery is proved by the real recovered
    HARNESS turn below.
    """
    executor = _DispatchParkingExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)

    with caplog.at_level(logging.ERROR, logger=_ADAPTER_LOGGER):
        app, conv = await _run_three_factor_chain(adapter)

        # The orphan callbacks now fire on a context the REAL teardown cleared.
        # Fail-CLOSED: the authoritative tool-call gate DENIES with no ctx.
        verdict = await adapter._stable_policy_evaluator("PHASE_TOOL_CALL", {})
        assert verdict.action == "POLICY_ACTION_DENY"

        # Consecutive orphan tool callbacks are bounded by the real watchdog.
        for _ in range(_ORPHAN_RESYNC_THRESHOLD):
            result = await adapter._stable_tool_executor("Bash", {"command": "ls"})
            assert result["code"] == _RUNNER_TURN_CONTEXT_DESYNC_CODE

    text = caplog.text
    # Fix direction: DENY, never the silent fail-OPEN, and the watchdog escalated.
    assert "defaulting to POLICY_ACTION_DENY" in text
    assert "returning ALLOW by default" not in text
    assert "defaulting to POLICY_ACTION_ALLOW" not in text
    assert "forcing Tier-1 SDK reset" in text
    # Counter did not pile up unbounded — the watchdog reset it.
    assert adapter._orphan_callback_count < _ORPHAN_RESYNC_THRESHOLD

    # The buffered continuation owns the terminal edge: the recovery handed off
    # to P1.7 and did NOT publish a desync failure (released the token).
    statuses = _drain_status_events(app.state.session_event_queues, conv)
    assert all(
        s.get("error", {}).get("code") != _RUNNER_TURN_CONTEXT_DESYNC_CODE for s in statuses
    ), statuses
    assert conv not in app.state.desync_terminalized

    # ── A SUBSEQUENT REAL harness turn rebinds and DISPATCHES a tool to terminal. ──
    # Driven through the production scaffold entry on the SAME adapter (the
    # executor was detached by the real teardown; a fresh one is built).
    cont_executor = _DispatchParkingExecutor(events=[TurnComplete(response="ok")])
    adapter._executor_factory = lambda: cont_executor
    cont_resp = await _start_turn_stream(adapter, _request("continuation work"))
    cont_consume: asyncio.Task[None] = asyncio.create_task(_drain_stream(cont_resp.body_iterator))

    # The continuation's tool callback must see a LIVE bound ctx and PARK a real
    # dispatch (an orphaned callback would instead return the desync error and
    # park nothing). Parking proves the ctx rebound through production code.
    await _spin_until(lambda: _wedged_dispatch_call_id(adapter) is not None)
    cont_call_id = _wedged_dispatch_call_id(adapter)
    assert cont_call_id is not None and cont_call_id != "call_inflight"

    # Resolve the dispatch the REAL way (the scaffold tool_result handler) and
    # let the continuation turn run to a terminal TurnComplete.
    await adapter._handle_tool_result_event(_tool_result(cont_call_id, "dispatched-live"))
    await asyncio.gather(cont_consume, return_exceptions=True)
    await _spin_until(lambda: adapter._current_ctx is None)
    assert adapter._orphan_callback_count == 0


async def test_three_factor_chain_fix_disabled_reproduces_wedge(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEGATIVE CONTROL: with the fix toggled OFF, the chain WEDGES (from real code).

    The fix is disabled with the smallest REAL toggles the production code reads
    — no hand-rolled symptom:

    * ``FAIL_CLOSED_PHASES`` (omnigent/policies/types.py:55, imported into the
      executor adapter and read by the REAL ``_stable_policy_evaluator``) → ``()``
      so the SAME real evaluator falls back to fail-OPEN ALLOW;
    * ``_ORPHAN_RESYNC_THRESHOLD`` (the constant the REAL
      ``_maybe_resync_on_orphan`` consults) → unreachable, so the REAL watchdog
      is still invoked but never escalates.

    Then the REAL callbacks reproduce the exact #1026 symptom from production
    code: the tool-call gate fails OPEN (ALLOW), and orphan callbacks pile up
    unbounded with no self-heal — the permanent wedge.
    """
    monkeypatch.setattr(adapter_mod, "FAIL_CLOSED_PHASES", ())
    monkeypatch.setattr(adapter_mod, "_ORPHAN_RESYNC_THRESHOLD", 10**9)

    executor = _DispatchParkingExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)

    with caplog.at_level(logging.ERROR, logger=_ADAPTER_LOGGER):
        await _run_three_factor_chain(adapter)

        # SYMPTOM 1 (from REAL code): the authoritative tool-call gate fails OPEN.
        verdict = await adapter._stable_policy_evaluator("PHASE_TOOL_CALL", {})
        assert verdict.action == "POLICY_ACTION_ALLOW"

        # SYMPTOM 2 (from REAL code): orphan callbacks pile up with no self-heal.
        n_orphans = _ORPHAN_RESYNC_THRESHOLD_DEFAULT * 2
        for _ in range(n_orphans):
            result = await adapter._stable_tool_executor("Bash", {"command": "ls"})
            assert result["code"] == _RUNNER_TURN_CONTEXT_DESYNC_CODE

    text = caplog.text
    # The fail-OPEN symptom is emitted BY the real evaluator (not a test copy).
    assert "policy evaluator fired with no active turn context (phase=PHASE_TOOL_CALL" in text
    assert "defaulting to POLICY_ACTION_ALLOW" in text
    # The real orphan tool-callback symptom string is present too.
    assert "tool callback fired with no active turn context (tool=" in text
    assert "returning error" in text
    # WEDGE: the watchdog never escalated, so no Tier-1 reset happened and the
    # cached executor was never dropped by a self-heal; orphans are unbounded.
    assert "forcing Tier-1 SDK reset" not in text
    assert adapter._orphan_callback_count >= n_orphans


# ── Runner-half recovery, no-buffer terminal status (fix LIVE) ────────────


async def test_runner_recovery_publishes_single_desync_terminal_status() -> None:
    """Fix LIVE (runner half): a desync signal with NO buffer yields ONE status.

    Same real recovery entry as the chain, but with no buffered continuation:
    the real ``_resync_turn_state`` clears the active-turn gate and publishes
    exactly one ``session.status: failed`` carrying ``runner_turn_context_desync``
    — and a subsequent turn would be accepted (the gate is clear, no restart).
    """
    conv = "conv_runner_recovery"
    pm = _ChainProcessManager()
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    app.state.session_event_queues.pop(conv, None)
    app.state.active_turns[conv] = None

    async def _on_delivery_failure(cid: str) -> None:
        await app.state.resync_turn_state(cid, "verdict_delivery_channel_dead")

    harness = _DeadChannelHarnessClient(
        httpx.RemoteProtocolError("Server disconnected without sending a response")
    )
    await _evaluate_policy_via_omnigent(
        server_client=_OkServerClient(),
        harness_client=harness,
        conversation_id=conv,
        evaluation_id="poleval_runner",
        phase="PHASE_TOOL_CALL",
        data={},
        on_delivery_failure=_on_delivery_failure,
    )

    assert harness.attempts == 2
    # The wedged turn's gate was cleared — a subsequent turn is accepted.
    assert conv not in app.state.active_turns
    # Exactly one terminal status, and it surfaces the desync code.
    statuses = _drain_status_events(app.state.session_event_queues, conv)
    assert len(statuses) == 1, statuses
    assert statuses[0]["status"] == "failed"
    assert statuses[0]["error"]["code"] == _RUNNER_TURN_CONTEXT_DESYNC_CODE
    assert conv in app.state.desync_terminalized


async def test_negative_control_runner_legacy_swallow_leaves_turn_wedged() -> None:
    """NEGATIVE CONTROL (runner half): legacy log-and-swallow leaves the wedge.

    The pre-fix runner had NO ``on_delivery_failure`` signal — a dead verdict
    channel was logged and swallowed, so the active-turn gate was never cleared
    and no terminal status was published. Passing ``on_delivery_failure=None``
    exercises that exact legacy path: the turn stays wedged in ``_active_turns``.
    """
    conv = "conv_legacy_wedge"
    pm = _ChainProcessManager()
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    app.state.session_event_queues.pop(conv, None)
    app.state.active_turns[conv] = None

    harness = _DeadChannelHarnessClient(
        httpx.RemoteProtocolError("Server disconnected without sending a response")
    )
    await _evaluate_policy_via_omnigent(
        server_client=_OkServerClient(),
        harness_client=harness,
        conversation_id=conv,
        evaluation_id="poleval_legacy",
        phase="PHASE_TOOL_CALL",
        data={},
        on_delivery_failure=None,
    )

    # WEDGE: the gate is never cleared and no terminal status is published.
    assert conv in app.state.active_turns
    assert _drain_status_events(app.state.session_event_queues, conv) == []
    assert conv not in app.state.desync_terminalized

"""Tests for harness↔runner turn-context desync recovery (``_resync_turn_state``).

A desync leaves the runner's active-turn slot bound to a turn the harness has
already torn down (or parked on a policy future whose verdict channel is dead).
Recovery must release the wedged turn in milliseconds rather than at
``_POLICY_EVAL_TIMEOUT_S``, clear the active-turn gate deterministically, and
publish exactly one terminal status.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_desynced_session_releases_and_recovers() -> None:
    """A desync with a buffered message releases the wedged turn promptly.

    Simulates a turn parked on the 24h policy-evaluation future (its verdict-
    delivery channel died). ``_resync_turn_state`` must release it in
    milliseconds, flag the conversation desynced, and let the buffered
    continuation bind a fresh turn (which clears the flag). With a continuation
    queued it stays silent: no user-visible ``runner_turn_context_desync``
    ``failed`` edge is published.
    """
    conv = "conv_desync_recover"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    forever = asyncio.Event()

    async def _wedged_turn() -> None:
        # Stand in for a turn parked on the 24h policy-evaluation future.
        await forever.wait()

    async with _runner_client(app) as http:
        # Bind a live (wedged) turn into the single active-turn slot.
        task = asyncio.create_task(_wedged_turn())
        app.state.active_turns[conv] = task
        try:
            # A message arriving mid-turn buffers (single-active-turn gate).
            resp = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag",
                    "model": "x",
                    "content": [{"role": "user", "content": "follow up"}],
                },
            )
            assert resp.status_code == 202, resp.text

            loop = asyncio.get_running_loop()
            t0 = loop.time()
            await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")
            elapsed = loop.time() - t0

            # Released in ms — nowhere near the 24h policy-eval timeout.
            assert elapsed < 2.0, elapsed
            # The wedged turn was cancelled, not left parked.
            assert task.cancelled() or task.done()
            # Flagged desynced; the buffered continuation has not bound yet.
            assert conv in app.state.desynced_sessions

            # The buffered continuation binds a fresh turn, which clears the
            # desynced flag at turn start (recovery).
            deadline = loop.time() + 3.0
            while loop.time() < deadline and conv in app.state.desynced_sessions:
                await asyncio.sleep(0.02)
            assert conv not in app.state.desynced_sessions
        finally:
            forever.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # Silent recovery: no user-visible desync `failed` edge was published while
    # a continuation was queued (that surfaces only when nothing will continue).
    queue = app.state.session_event_queues.get(conv)
    desync_failed = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if (
            isinstance(event, dict)
            and event.get("type") == "session.status"
            and event.get("status") == "failed"
            and isinstance(event.get("error"), dict)
            and event["error"].get("code") == "runner_turn_context_desync"
        ):
            desync_failed.append(event)
    assert desync_failed == []


@pytest.mark.asyncio
async def test_desync_drains_buffer_when_turn_pops_active_slot() -> None:
    """A desync drains the buffer even when the cancelled turn self-pops.

    A background turn cancelled while blocked in ``_drain_streaming_response``
    pops ``_active_turns`` itself and re-raises WITHOUT routing through
    ``_on_proxy_stream_end`` — so ``_cancel_active_turn``'s identity guard fails
    and no continuation drain is scheduled. ``_resync_turn_state`` must kick the
    drain explicitly, or the buffered message strands while the session shows
    idle.
    """
    conv = "conv_desync_selfpop"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    forever = asyncio.Event()

    async def _wedged_turn() -> None:
        # Mimic _drain_streaming_response: on cancel, pop _active_turns and
        # re-raise WITHOUT _on_proxy_stream_end (so no continuation is kicked).
        try:
            await forever.wait()
        except asyncio.CancelledError:
            app.state.active_turns.pop(conv, None)
            raise

    async with _runner_client(app) as http:
        task = asyncio.create_task(_wedged_turn())
        app.state.active_turns[conv] = task
        try:
            resp = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag",
                    "model": "x",
                    "content": [{"role": "user", "content": "follow up"}],
                },
            )
            assert resp.status_code == 202, resp.text

            loop = asyncio.get_running_loop()
            await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")
            assert task.cancelled() or task.done()

            # The explicit continuation kick started a fresh turn, which clears
            # the desynced flag at ``_run_turn_bg`` start. Without the kick this
            # would never clear (the buffer would strand and the session idle).
            deadline = loop.time() + 3.0
            while loop.time() < deadline and conv in app.state.desynced_sessions:
                await asyncio.sleep(0.02)
            assert conv not in app.state.desynced_sessions
        finally:
            forever.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_desync_stream_mode_forwards_interrupt() -> None:
    """Stream-mode (None-sentinel) desync clears the gate and forwards interrupt.

    A ``stream=true`` turn parks ``_active_turns[conv] = None`` (no runner Task
    — the AP request drives ``proxy_stream``). Recovery must guarantee the
    sentinel is cleared so the active-turn gate cannot stay stuck, and forward a
    best-effort interrupt so the harness unwinds.
    """
    conv = "conv_desync_streammode"
    harness = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app):
        # Stream-mode active turn: the None sentinel, not an asyncio.Task.
        app.state.active_turns[conv] = None
        await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")

    # The sentinel was popped deterministically (gate cleared) and the interrupt
    # was forwarded — neither happened before the fix.
    assert conv not in app.state.active_turns
    assert {"type": "interrupt"} in harness.patched_events


class _DeadInterruptHarnessClient(_ScriptedHarnessClient):
    """Scripted harness whose interrupt POST always raises (wedged/dead harness)."""

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        # Model a dead/wedged harness: the interrupt forward never lands and
        # never ends proxy_stream.
        raise httpx.ConnectError("harness gone")


@pytest.mark.asyncio
async def test_desync_stream_mode_clears_gate_even_when_interrupt_fails() -> None:
    """Stream-mode sentinel clears and the buffer drains on a dead interrupt.

    The pathological wedge: ``stream=true`` (None sentinel) + dead verdict
    channel + a failing interrupt that never ends ``proxy_stream``. Recovery
    must STILL pop the sentinel — not contingent on the interrupt or on
    proxy_stream — and drain a buffered follow-up, or the session is stuck idle
    in the active-turn gate forever.
    """
    conv = "conv_desync_streammode_dead"
    harness = _DeadInterruptHarnessClient([])
    pm = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as http:
        # Stream-mode active turn parked as the None sentinel.
        app.state.active_turns[conv] = None
        # A follow-up arrives mid-turn and buffers behind the active-turn gate.
        resp = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag",
                "model": "x",
                "content": [{"role": "user", "content": "follow up"}],
            },
        )
        assert resp.status_code == 202, resp.text

        loop = asyncio.get_running_loop()
        # Recovery runs even though the interrupt forward raises (dead harness).
        await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")

        # The buffered follow-up drained: the sentinel was popped
        # deterministically and the continuation kick bound a fresh turn, which
        # clears the desynced flag at ``_run_turn_bg`` start. Without the
        # deterministic pop the gate stays stuck and the flag never clears.
        deadline = loop.time() + 3.0
        while loop.time() < deadline and conv in app.state.desynced_sessions:
            await asyncio.sleep(0.02)
        assert conv not in app.state.desynced_sessions


class _InterruptEndsStreamHarnessClient(_ScriptedHarnessClient):
    """Interrupt POST succeeds AND drives proxy_stream's terminal bookkeeping.

    Reproduces the race the publish-once guard must close: a successful
    interrupt ends ``proxy_stream``, so its ``_on_proxy_stream_end`` runs during
    ``_resync_turn_state``, competing with the desync ``failed`` publish.
    """

    app: Any = None
    conv: str = ""

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        if isinstance(json, dict) and json.get("type") == "interrupt" and self.app is not None:
            # Successful interrupt → proxy_stream reaches its terminal
            # bookkeeping right here, mid-resync.
            self.app.state.on_proxy_stream_end(self.conv)
        return await super().post(url, json=json, timeout=timeout)


@pytest.mark.asyncio
async def test_desync_stream_mode_publishes_single_terminal_status() -> None:
    """Stream-mode no-buffer desync publishes exactly ONE terminal status.

    Stream-mode None sentinel + no buffer + interrupt SUCCEEDS, so
    ``proxy_stream`` reaches ``_on_proxy_stream_end`` while ``_resync_turn_state``
    also runs the no-buffer desync ``failed``. The publish-once token must
    dedupe: the client sees exactly one terminal status — the desync ``failed``
    — never an ``idle``+``failed`` pair, since ``failed`` is terminal and
    non-retryable.
    """
    conv = "conv_desync_single_terminal"
    harness = _InterruptEndsStreamHarnessClient([])
    pm = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    harness.app = app
    harness.conv = conv

    async with _runner_client(app):
        # Stream-mode sentinel, no buffered follow-up.
        app.state.active_turns[conv] = None
        await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")

    # Collect every session.status edge published for the conv.
    queue = app.state.session_event_queues.get(conv)
    statuses: list[dict[str, Any]] = []
    while queue is not None and not queue.empty():
        ev = queue.get_nowait()
        if isinstance(ev, dict) and ev.get("type") == "session.status":
            statuses.append(ev)

    # Exactly ONE terminal status, and it is the desync failed — no idle.
    assert len(statuses) == 1, statuses
    assert statuses[0]["status"] == "failed"
    assert statuses[0]["error"]["code"] == "runner_turn_context_desync"
    # The interrupt was actually forwarded (so the race window was real).
    assert {"type": "interrupt"} in harness.patched_events


class _SlotSwapBaseException(BaseException):
    """A non-``Exception`` raised in setup to exercise the finally floor.

    Inherits ``BaseException`` (NOT ``Exception``) so it bypasses every
    ``except`` clause in ``_run_turn_bg`` and the ONLY exit is the ``finally``
    floor — exactly the abnormal-exit path the identity guard protects.
    """


@pytest.mark.asyncio
async def test_run_turn_bg_finalizer_identity_guard_spares_foreign_slot() -> None:
    """F2: the ``_run_turn_bg`` finally floor must identity-compare before popping.

    When turn A's body exits abnormally while ``_active_turns[conv]`` already
    holds a DIFFERENT (newer) turn's task — the stale-finalizer clobber class the
    ExecutorAdapter identity CAS also fixes — the floor must NOT finalize the
    foreign slot. A bare ``conv in _active_turns`` check would pop the newer turn.

    Driven deterministically: a ``spec_resolver`` swaps the slot to a foreign
    task, then raises a ``BaseException`` that bypasses every ``except`` clause,
    so the finally floor is the sole exit and runs with the foreign task in the
    slot. With the identity guard the foreign task survives; with the bare check
    it is clobbered.
    """
    conv = "conv_finalizer_identity"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    foreign_task = asyncio.create_task(asyncio.Event().wait())

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        # Simulate a newer turn having bound the slot mid-setup, then force the
        # body out through a non-Exception so only the finally floor runs.
        app.state.active_turns[conv] = foreign_task
        raise _SlotSwapBaseException

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as http:
            resp = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag",
                    "model": "x",
                    "content": [{"role": "user", "content": "turn A"}],
                },
            )
            assert resp.status_code == 202, resp.text

            task = app.state.active_turns.get(conv)
            assert isinstance(task, asyncio.Task)

            # Let A's task run to its abnormal exit; the BaseException propagates.
            with contextlib.suppress(BaseException):
                await task

        # The foreign (newer) turn's slot survived A's finalizer — identity guard.
        assert app.state.active_turns.get(conv) is foreign_task
    finally:
        foreign_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await foreign_task
        app.state.active_turns.pop(conv, None)


@pytest.mark.asyncio
async def test_on_proxy_stream_end_spares_superseded_response() -> None:
    """BLOCKING-1: a stale stream terminal must not clobber a newer turn's state.

    Stream-mode recovery pops the old sentinel and binds a continuation (new
    response id + in-flight marker). If the OLD stream terminates AFTER that, its
    terminal callback must NOT remove the newer turn's slot, response id, or
    in-flight marker. ``_on_proxy_stream_end`` is generation-aware: given the old
    stream's ``owner_response_id`` it no-ops when a newer response owns the
    conversation.
    """
    conv = "conv_stream_supersede"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    newer = asyncio.create_task(asyncio.Event().wait())
    try:
        # A newer turn owns the conversation.
        app.state.active_turns[conv] = newer
        app.state.live_response_id[conv] = "resp_new"

        # The OLD stream's terminal callback fires late, for a superseded id.
        app.state.on_proxy_stream_end(conv, owner_response_id="resp_old")

        # The newer turn's state survived — no clobber.
        assert app.state.active_turns.get(conv) is newer
        assert app.state.live_response_id.get(conv) == "resp_new"
        assert conv not in pm.cleared_in_flight

        # Positive control: the terminal callback for the CURRENT response DOES
        # finalize (clears the slot, response id, and in-flight marker).
        app.state.on_proxy_stream_end(conv, owner_response_id="resp_new")
        assert conv not in app.state.active_turns
        assert conv not in app.state.live_response_id
        assert conv in pm.cleared_in_flight
    finally:
        newer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await newer
        app.state.active_turns.pop(conv, None)


@pytest.mark.asyncio
async def test_resync_ownership_gate_ignores_superseded_delivery_failure() -> None:
    """BLOCKING-2: a delayed verdict-delivery failure must not cancel a newer turn.

    ``_evaluate_policy_via_omnigent`` signals ``on_delivery_failure`` for a
    specific turn's response. A delayed or duplicate failure from an OLD response
    must not tear down whichever newer turn is now active — ``_resync_turn_state``
    ignores a signal whose ``owner_response_id`` no longer matches the live one.
    """
    conv = "conv_delivery_supersede"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app):
        # A newer turn is active (stream-mode sentinel), owning a fresh response.
        app.state.active_turns[conv] = None
        app.state.live_response_id[conv] = "resp_new"

        # A stale delivery failure from the OLD response signals recovery.
        await app.state.resync_turn_state(
            conv, "verdict_delivery_channel_dead", owner_response_id="resp_old"
        )

    # The newer turn survived: still bound, no desync `failed` published.
    assert conv in app.state.active_turns
    assert app.state.live_response_id.get(conv) == "resp_new"
    queue = app.state.session_event_queues.get(conv)
    while queue is not None and not queue.empty():
        ev = queue.get_nowait()
        assert not (
            isinstance(ev, dict)
            and ev.get("type") == "session.status"
            and ev.get("status") == "failed"
            and isinstance(ev.get("error"), dict)
            and ev["error"].get("code") == "runner_turn_context_desync"
        ), ev
    app.state.active_turns.pop(conv, None)


@pytest.mark.asyncio
async def test_resync_clears_in_flight_marker_no_buffer() -> None:
    """B1: an accepted no-buffer resync clears the process-manager in-flight marker.

    ``_resync_turn_state`` pops ``_live_response_id`` up front, which makes the
    cancelled turn's own ``_on_proxy_stream_end`` skip ``clear_in_flight`` (its
    owner check fails on the now-absent id). If the resync doesn't clear the
    marker itself, it leaks — the idle reaper then skips the harness FOREVER
    (``process_manager`` reaper: ``if conv_id in _in_flight_response_ids:
    continue``). Assert the marker is cleared for a real marked response.
    """
    conv = "conv_resync_inflight"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app):
        # A live stream-mode turn with a REAL marked in-flight response, no buffer.
        app.state.active_turns[conv] = None
        app.state.live_response_id[conv] = "resp_live"
        pm.mark_in_flight(conv, "resp_live")
        assert conv not in pm.cleared_in_flight

        await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")

    # The in-flight marker was cleared, so the idle reaper will NOT skip the
    # harness forever.
    assert conv in pm.cleared_in_flight
    # No buffer, no continuation → a single terminal desync `failed` was owned.
    queue = app.state.session_event_queues.get(conv)
    failed = []
    while queue is not None and not queue.empty():
        ev = queue.get_nowait()
        if (
            isinstance(ev, dict)
            and ev.get("type") == "session.status"
            and ev.get("status") == "failed"
            and isinstance(ev.get("error"), dict)
            and ev["error"].get("code") == "runner_turn_context_desync"
        ):
            failed.append(ev)
    assert len(failed) == 1, failed
    app.state.active_turns.pop(conv, None)


class _InterruptBindsContinuationClient(_ScriptedHarnessClient):
    """On the recovery interrupt, bind a continuation AND drain the buffer.

    Reproduces the NB3 race deterministically: a continuation binds a fresh turn
    and consumes the buffer DURING ``_resync_turn_state``'s teardown await (the
    interrupt forward is the await), so the post-await buffer is empty while a
    healthy continuation is live.
    """

    app: Any = None
    conv: str = ""
    continuation: asyncio.Task[None] | None = None

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        if isinstance(json, dict) and json.get("type") == "interrupt" and self.app is not None:
            self.app.state.session_message_buffers.pop(self.conv, None)
            self.continuation = asyncio.create_task(asyncio.Event().wait())
            # A real bind bumps the turn epoch; mirror that so recovery detects it.
            self.app.state.turn_bind_epoch[self.conv] = (
                self.app.state.turn_bind_epoch.get(self.conv, 0) + 1
            )
            self.app.state.active_turns[self.conv] = self.continuation
        return await super().post(url, json=json, timeout=timeout)


@pytest.mark.asyncio
async def test_resync_does_not_publish_failed_over_drained_continuation() -> None:
    """NB3: a continuation that binds AND drains the buffer during teardown wins.

    While ``_resync_turn_state`` awaits the teardown (the interrupt forward), a
    continuation binds a fresh turn AND drains the buffer. Re-reading only the
    buffer post-await sees it empty and publishes desync `failed` — clobbering
    the healthy continuation. The fix reserves ownership on the active-turn slot:
    a bound continuation means we do NOT publish `failed`.
    """
    conv = "conv_resync_cont_race"
    harness = _InterruptBindsContinuationClient([])
    pm = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    harness.app = app
    harness.conv = conv

    async with _runner_client(app):
        # Stream-mode sentinel with a buffer present (so the pre-claim does NOT
        # fire); the teardown takes the sentinel path whose interrupt forward is
        # the injection point.
        app.state.active_turns[conv] = None
        app.state.session_message_buffers[conv] = [{"content": "follow up"}]
        try:
            await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")

            # The continuation bound during teardown owns the slot; recovery did
            # NOT publish a desync `failed` over it.
            assert app.state.active_turns.get(conv) is harness.continuation
            queue = app.state.session_event_queues.get(conv)
            while queue is not None and not queue.empty():
                ev = queue.get_nowait()
                assert not (
                    isinstance(ev, dict)
                    and ev.get("type") == "session.status"
                    and ev.get("status") == "failed"
                    and isinstance(ev.get("error"), dict)
                    and ev["error"].get("code") == "runner_turn_context_desync"
                ), ev
        finally:
            if harness.continuation is not None:
                harness.continuation.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await harness.continuation
            app.state.active_turns.pop(conv, None)


@pytest.mark.asyncio
async def test_resync_removes_completed_stale_task_and_publishes_terminal() -> None:
    """BLOCKING (round 5): a COMPLETED task in the slot is a corpse, not a continuation.

    The #1026 abnormal-exit path can leave a turn's OWN task in ``_active_turns``
    after it completed (its teardown never routed through ``_on_proxy_stream_end``).
    ``_cancel_inprocess_turn`` used to ``return`` on a done task WITHOUT removing
    it, and ``_resync_turn_state`` then read mere slot membership as a healthy
    continuation — so it suppressed the terminal and left the corpse. Subsequent
    messages buffer forever behind it (the original wedge). Recovery must
    compare-and-remove the completed generation and publish the terminal desync
    ``failed``.
    """
    conv = "conv_resync_corpse"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    # A COMPLETED task left stale in the slot; a real marked in-flight response.
    done_task = asyncio.create_task(asyncio.sleep(0))
    await done_task
    assert done_task.done()

    async with _runner_client(app):
        app.state.active_turns[conv] = done_task
        app.state.live_response_id[conv] = "resp_dead"
        pm.mark_in_flight(conv, "resp_dead")
        # No buffered message.

        await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")

    # The corpse was removed — NOT mistaken for a live continuation.
    assert conv not in app.state.active_turns
    # Its in-flight marker was cleared, so the reaper isn't blocked.
    assert conv in pm.cleared_in_flight
    # A single terminal desync `failed` was published (the wedge is resolved, not
    # silently suppressed).
    queue = app.state.session_event_queues.get(conv)
    failed = []
    while queue is not None and not queue.empty():
        ev = queue.get_nowait()
        if (
            isinstance(ev, dict)
            and ev.get("type") == "session.status"
            and ev.get("status") == "failed"
            and isinstance(ev.get("error"), dict)
            and ev["error"].get("code") == "runner_turn_context_desync"
        ):
            failed.append(ev)
    assert len(failed) == 1, failed

    # And a subsequent message would START a turn, not buffer forever behind the
    # corpse: the active-turn gate is clear.
    assert conv not in app.state.active_turns


class _CompleteTaskOnInterruptClient(_ScriptedHarnessClient):
    """On the recovery interrupt, complete the wedged task so it arrives DONE.

    Reproduces the race where a cancel-forwarded live turn COMPLETES during the
    ``_forward_harness_interrupt`` await, so ``_cancel_active_turn`` sees it done
    and must sweep its stale ``_interrupted_sessions`` token (else the next turn
    is tainted).
    """

    task: asyncio.Task[None] | None = None
    release: asyncio.Event | None = None

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        if isinstance(json, dict) and json.get("type") == "interrupt":
            if self.release is not None:
                self.release.set()
            if self.task is not None:
                with contextlib.suppress(BaseException):
                    await self.task  # ensure it is DONE before we return
        return await super().post(url, json=json, timeout=timeout)


@pytest.mark.asyncio
async def test_resync_clears_interrupt_token_when_task_completes_during_teardown() -> None:
    """Round-6 pre-empt: a task that completes during teardown must not leak its token.

    ``_cancel_inprocess_turn`` sets ``_interrupted_sessions`` then forwards the
    interrupt; if the live turn COMPLETES during that await, ``_cancel_active_turn``
    hits the done-task sweep. The sweep must clear ``_interrupted_sessions`` (it is
    NOT cleared at the next ``_run_turn_bg`` start), or the next turn's
    ``_on_proxy_stream_end`` reads a stale ``was_interrupted`` and publishes a
    spurious ``idle``/``cancelled``.
    """
    conv = "conv_resync_token_leak"
    harness = _CompleteTaskOnInterruptClient([])
    pm = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    release = asyncio.Event()

    async def _turn() -> None:
        await release.wait()  # completes when the interrupt forward fires

    task = asyncio.create_task(_turn())
    harness.task = task
    harness.release = release

    async with _runner_client(app):
        app.state.active_turns[conv] = task
        app.state.live_response_id[conv] = "resp_live"
        pm.mark_in_flight(conv, "resp_live")

        await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")

    # The task completed during teardown and was swept; its interrupt token was
    # cleared so the NEXT turn is not tainted.
    assert task.done()
    assert conv not in app.state.interrupted_sessions
    assert conv not in app.state.active_turns
    app.state.active_turns.pop(conv, None)


class _InterruptBindsStreamContinuationClient(_ScriptedHarnessClient):
    """On the recovery interrupt, bind a NEW stream=true turn (None sentinel).

    Reproduces the round-6 clobber: both the old wedged stream turn and the new
    continuation use ``None`` as the slot value, so an identity check
    (``_slot_now is _original_slot``) mistakes the fresh turn for the old corpse.
    """

    app: Any = None
    conv: str = ""

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        if isinstance(json, dict) and json.get("type") == "interrupt" and self.app is not None:
            # A fresh stream=true turn binds the None sentinel + a new response;
            # a real bind bumps the turn epoch, so mirror that.
            self.app.state.turn_bind_epoch[self.conv] = (
                self.app.state.turn_bind_epoch.get(self.conv, 0) + 1
            )
            self.app.state.active_turns[self.conv] = None
            self.app.state.live_response_id[self.conv] = "resp_new"
        return await super().post(url, json=json, timeout=timeout)


@pytest.mark.asyncio
async def test_resync_does_not_clobber_stream_continuation_reusing_none_sentinel() -> None:
    """BLOCKING (round 6): a new stream=true turn (None sentinel) is not a corpse.

    The old wedged stream turn and a freshly bound ``stream=true`` continuation
    both park ``_active_turns[conv] = None``. Recovery must NOT mistake the new
    None sentinel for the old one and erase it — it relies on "teardown removed
    the original", so any occupant present after teardown is a distinct
    continuation. The new turn's slot and its ``resp_new`` marker must survive,
    and no desync ``failed`` is published over it.
    """
    conv = "conv_resync_stream_reuse"
    harness = _InterruptBindsStreamContinuationClient([])
    pm = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    harness.app = app
    harness.conv = conv

    async with _runner_client(app):
        # The OLD wedged stream turn: None sentinel + its own response, no buffer.
        app.state.active_turns[conv] = None
        app.state.live_response_id[conv] = "resp_old"
        pm.mark_in_flight(conv, "resp_old")

        await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")

    # The freshly bound continuation survived: its slot and marker intact ...
    assert conv in app.state.active_turns
    assert app.state.live_response_id.get(conv) == "resp_new"
    # ... and no desync `failed` was published over it.
    queue = app.state.session_event_queues.get(conv)
    while queue is not None and not queue.empty():
        ev = queue.get_nowait()
        assert not (
            isinstance(ev, dict)
            and ev.get("type") == "session.status"
            and ev.get("status") == "failed"
            and isinstance(ev.get("error"), dict)
            and ev["error"].get("code") == "runner_turn_context_desync"
        ), ev
    app.state.active_turns.pop(conv, None)


class _ReplacementRunsToCompletionClient(_ScriptedHarnessClient):
    """On the recovery interrupt, run a replacement turn to COMPLETION.

    Reproduces the round-7 clobber: a stream=true replacement starts AND finishes
    during the interrupt await — it binds (bumping the epoch), publishes its own
    terminal, and pops its slot — so a post-teardown slot check sees an empty slot
    and old recovery would publish desync `failed` over the already-finished
    replacement, whose terminal was swallowed by the conversation-wide token.
    """

    app: Any = None
    conv: str = ""

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        if isinstance(json, dict) and json.get("type") == "interrupt" and self.app is not None:
            st = self.app.state
            # 1) Replacement BINDS (bump epoch like a real turn start).
            st.turn_bind_epoch[self.conv] = st.turn_bind_epoch.get(self.conv, 0) + 1
            st.active_turns[self.conv] = None
            st.live_response_id[self.conv] = "resp_new"
            # 2) Replacement FINISHES: its own terminal pops the slot + publishes.
            st.on_proxy_stream_end(self.conv, owner_response_id="resp_new")
        return await super().post(url, json=json, timeout=timeout)


@pytest.mark.asyncio
async def test_resync_does_not_clobber_replacement_that_finished_during_interrupt() -> None:
    """BLOCKING (round 7): a replacement that starts AND finishes during teardown wins.

    The replacement leaves an EMPTY slot (it finished), so a post-teardown slot
    check misses it entirely — but its bind bumped the monotonic epoch, so
    recovery detects the continuation and does NOT publish a stale desync
    ``failed``. Its own terminal (``idle``) is published, not swallowed by the
    conversation-wide token (the token is epoch-scoped).
    """
    conv = "conv_resync_replacement_done"
    harness = _ReplacementRunsToCompletionClient([])
    pm = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    harness.app = app
    harness.conv = conv

    async with _runner_client(app):
        # The OLD wedged stream turn: None sentinel + its own response, no buffer.
        app.state.active_turns[conv] = None
        app.state.live_response_id[conv] = "resp_old"
        pm.mark_in_flight(conv, "resp_old")

        await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")

    # Collect terminal statuses.
    queue = app.state.session_event_queues.get(conv)
    statuses: list[dict[str, Any]] = []
    while queue is not None and not queue.empty():
        ev = queue.get_nowait()
        if isinstance(ev, dict) and ev.get("type") == "session.status":
            statuses.append(ev)

    # The replacement's terminal survived and NO stale desync `failed` was published.
    desync_failed = [
        s
        for s in statuses
        if s.get("status") == "failed"
        and isinstance(s.get("error"), dict)
        and s["error"].get("code") == "runner_turn_context_desync"
    ]
    assert desync_failed == [], statuses
    # The replacement published its own idle (not swallowed).
    assert any(s.get("status") == "idle" for s in statuses), statuses
    app.state.active_turns.pop(conv, None)


@pytest.mark.asyncio
async def test_delete_session_clears_all_paired_desync_state() -> None:
    """A deleted session must not leave desync state that a same-id recreate inherits.

    Epochs come from a non-repeating sequence, so a recreated session never
    collides on epoch — but a leftover ``_desync_terminalized`` claim or a stale
    ``_desynced`` flag would still carry into the new lifetime and could suppress
    its terminal or misclassify a later interruption. Deletion must clear ALL of it.
    """
    conv = "conv_delete_recreate"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as http:
        # Seed the state a desync recovery leaves behind (a real turn bind stamps
        # the epoch; the token/flag are what a recovery would have claimed).
        app.state.begin_turn_slot(conv)
        app.state.desync_terminalized[conv] = app.state.turn_bind_epoch[conv]
        app.state.desynced_sessions.add(conv)

        resp = await http.request("DELETE", f"/v1/sessions/{conv}")
        assert resp.status_code in (200, 204), resp.text

    # All paired desync/turn state is gone — a recreated same-id session starts clean.
    assert conv not in app.state.turn_bind_epoch
    assert conv not in app.state.desync_terminalized
    assert conv not in app.state.desynced_sessions

    # And a fresh turn's terminal for the recreated id is NOT suppressed by a stale
    # epoch claim: with the token cleared, _on_proxy_stream_end publishes normally.
    app.state.begin_turn_slot(conv)  # a recreated session's first bind
    app.state.on_proxy_stream_end(conv)
    queue = app.state.session_event_queues.get(conv)
    statuses = []
    while queue is not None and not queue.empty():
        ev = queue.get_nowait()
        if isinstance(ev, dict) and ev.get("type") == "session.status":
            statuses.append(ev)
    assert any(s.get("status") == "idle" for s in statuses), statuses


class _DeleteRecreateDuringInterruptClient(_ScriptedHarnessClient):
    """On the recovery interrupt, simulate a same-id delete → recreate mid-await.

    Reproduces the round-8 lifecycle race: while the OLD recovery is blocked in
    ``_forward_harness_interrupt``, the session is deleted (clearing its epoch)
    and recreated with the same id, binding a NEW turn. With a per-conversation
    epoch that resets on delete, the recreate returns to the same epoch the OLD
    recovery captured, so it clobbers the new lifetime's turn. A non-repeating
    (global) epoch keeps them distinct.
    """

    app: Any = None
    conv: str = ""

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        if isinstance(json, dict) and json.get("type") == "interrupt" and self.app is not None:
            st = self.app.state
            # DELETE: clear the old lifetime's paired state (as delete_session does).
            st.active_turns.pop(self.conv, None)
            st.turn_bind_epoch.pop(self.conv, None)
            st.desync_terminalized.pop(self.conv, None)
            st.desynced_sessions.discard(self.conv)
            # RECREATE same id: a new turn binds with a FRESH (non-repeating) epoch.
            st.begin_turn_slot(self.conv)
            st.live_response_id[self.conv] = "resp_new"
        return await super().post(url, json=json, timeout=timeout)


@pytest.mark.asyncio
async def test_resync_does_not_clobber_recreated_session_after_delete_mid_interrupt() -> None:
    """BLOCKING-class (round 8): a same-id recreate during the interrupt await is not clobbered.

    The OLD stream recovery is inside ``_forward_harness_interrupt`` when the
    session is deleted and recreated. The recreated turn must NOT receive a stale
    ``runner_turn_context_desync`` from the old recovery — the non-repeating epoch
    lets recovery see the new lifetime as a distinct generation.
    """
    conv = "conv_delete_mid_interrupt"
    harness = _DeleteRecreateDuringInterruptClient([])
    pm = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    harness.app = app
    harness.conv = conv

    async with _runner_client(app):
        # OLD wedged stream turn with its own bind epoch + response, no buffer.
        app.state.begin_turn_slot(conv)
        app.state.live_response_id[conv] = "resp_old"
        pm.mark_in_flight(conv, "resp_old")

        # Recovery pops the sentinel, then blocks in _forward_harness_interrupt,
        # where the client injects the delete + recreate.
        await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")

    # The recreated turn survived: its slot + resp_new marker intact ...
    assert conv in app.state.active_turns
    assert app.state.live_response_id.get(conv) == "resp_new"
    # ... and NO stale desync `failed` was published over the new lifetime.
    queue = app.state.session_event_queues.get(conv)
    while queue is not None and not queue.empty():
        ev = queue.get_nowait()
        assert not (
            isinstance(ev, dict)
            and ev.get("type") == "session.status"
            and ev.get("status") == "failed"
            and isinstance(ev.get("error"), dict)
            and ev["error"].get("code") == "runner_turn_context_desync"
        ), ev
    app.state.active_turns.pop(conv, None)


class _NestedRecoveryDuringInterruptClient(_ScriptedHarnessClient):
    """On the OLD recovery's interrupt, bind a replacement AND claim a nested token.

    Reproduces the round-9 bug: the replacement itself desyncs and its own
    (nested) recovery claims the epoch-scoped token BEFORE the old recovery
    returns from its interrupt await. The old recovery must NOT strip that nested
    token — it may only release its OWN claim (equal to its entry epoch).
    """

    app: Any = None
    conv: str = ""
    nested_epoch: int = 0

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        if isinstance(json, dict) and json.get("type") == "interrupt" and self.app is not None:
            st = self.app.state
            # A replacement binds (fresh epoch) ...
            st.begin_turn_slot(self.conv)
            st.live_response_id[self.conv] = "resp_new"
            # ... and its own nested recovery claims the epoch-scoped token.
            self.nested_epoch = st.turn_bind_epoch[self.conv]
            st.desync_terminalized[self.conv] = self.nested_epoch
        return await super().post(url, json=json, timeout=timeout)


@pytest.mark.asyncio
async def test_old_recovery_does_not_strip_nested_recovery_token() -> None:
    """BLOCKING (round 9): the old recovery must not pop a nested recovery's token.

    Old recovery detects a continuation, but the replacement's own recovery has
    re-claimed ``_desync_terminalized`` under a HIGHER epoch. A compare-and-pop
    keyed on the old recovery's entry epoch leaves the nested token intact, so the
    replacement's competing terminal is still suppressed (no idle→failed pair).
    """
    conv = "conv_nested_recovery"
    harness = _NestedRecoveryDuringInterruptClient([])
    pm = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    harness.app = app
    harness.conv = conv

    async with _runner_client(app):
        # OLD wedged stream turn, no buffer (so the old recovery pre-claims).
        app.state.begin_turn_slot(conv)
        app.state.live_response_id[conv] = "resp_old"
        pm.mark_in_flight(conv, "resp_old")

        await app.state.resync_turn_state(conv, "verdict_delivery_channel_dead")

    # The nested recovery's token survived the old recovery's release.
    assert app.state.desync_terminalized.get(conv) == harness.nested_epoch

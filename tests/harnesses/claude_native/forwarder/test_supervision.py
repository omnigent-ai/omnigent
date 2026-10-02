"""Supervision tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    record_hook_event,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_item_request,
    _start_recording_server,
)


@pytest.mark.asyncio
async def test_forwarder_survives_unhandled_loop_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A non-HTTP loop exception is logged and the next poll continues.

    This fails if a disk or parsing exception tears down the
    background forwarder task, which leaves the browser mirror frozen
    without surfacing a session event.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "survives-loop-error",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "after loop error"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    original_forward = forwarder._forward_available_items
    fail_once = True

    async def _fail_once_forward_available_items(
        **kwargs: Any,
    ) -> forwarder.TranscriptForwardState:
        """
        Raise once, then delegate to the real forwarder.

        :param kwargs: Keyword arguments passed by
            :func:`forward_claude_transcript_to_session`.
        :returns: Updated transcript forward state.
        """
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise PermissionError("state write failed")
        return await original_forward(**kwargs)

    monkeypatch.setattr(
        forwarder,
        "_forward_available_items",
        _fail_once_forward_available_items,
    )
    caplog.set_level(logging.ERROR, logger="omnigent.harnesses.claude_native.forwarder")

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        request = await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert "Claude transcript forwarder loop failed" in caplog.text
    assert "session=conv_abc" in caplog.text
    assert str(bridge_dir) not in caplog.text
    assert request["body"]["type"] == "external_conversation_item"
    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "after loop error"}],
    }


# ── supervise_forwarder ────────────────────────────────────────────


def _supervisor_kwargs(tmp_path: Path) -> dict[str, Any]:
    """
    Build the kwargs used to invoke :func:`supervise_forwarder` in tests.

    The supervisor passes these through to the (stubbed) forwarder
    coroutine; nothing here has to be a real running service since
    every test patches the forwarder.

    :param tmp_path: Pytest-provided temp directory used as the
        bridge dir argument.
    :returns: Dict of keyword arguments suitable for
        ``supervise_forwarder(**kwargs)``.
    """
    return {
        "base_url": "http://localhost:0",
        "headers": {},
        "session_id": "conv_abc",
        "bridge_dir": tmp_path,
        "agent_name": "claude",
        "start_at_end": False,
    }


@pytest.mark.asyncio
async def test_supervise_forwarder_restarts_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A non-cancellation exception in the forwarder restarts it.

    This is the case that left the chat view permanently desynced
    overnight: the forwarder task died inside its own
    ``async with httpx.AsyncClient`` block, the parent's
    ``await _attach_with_reconnect`` kept running, and no one
    restarted the forwarder. With the supervisor we expect a second
    call after the first one raises.
    """
    call_count = 0

    async def fake_forwarder(**_: Any) -> None:
        """Fake forwarder: crash on call 1, signal stop on call 2."""
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # RuntimeError stands in for the kinds of errors that can
            # escape the forwarder's inner ``except Exception`` (e.g.
            # something raised during the ``async with`` setup before
            # the per-iteration try block). The supervisor catches
            # Exception and restarts.
            raise RuntimeError("simulated unrecoverable crash")
        # CancelledError is the ONLY thing the supervisor re-raises,
        # so use it as the test's exit signal once we've verified
        # the restart happened.
        raise asyncio.CancelledError()

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record sleeps without waiting."""
        sleeps.append(seconds)

    monkeypatch.setattr(forwarder, "forward_claude_transcript_to_session", fake_forwarder)
    monkeypatch.setattr(forwarder, "_supervisor_sleep", fake_sleep)

    with caplog.at_level(logging.WARNING, logger=forwarder.__name__):
        with pytest.raises(asyncio.CancelledError):
            await forwarder.supervise_forwarder(**_supervisor_kwargs(tmp_path))

    # 2 = first crash + restart. If the supervisor exited after the
    # first crash (the pre-fix behavior), call_count would be 1.
    assert call_count == 2, (
        f"Forwarder should have been called twice (initial + restart), "
        f"got {call_count}. If 1, the supervisor exited on crash "
        f"instead of restarting."
    )
    # One sleep ran — between the crash and the restart. The second
    # call raised CancelledError, which propagates immediately and
    # skips the post-iteration sleep.
    assert sleeps == [forwarder._SUPERVISOR_INITIAL_BACKOFF_S]
    assert "Claude transcript forwarder crashed" in caplog.text
    assert "session=conv_abc" in caplog.text
    assert str(tmp_path) not in caplog.text


@pytest.mark.asyncio
async def test_supervise_forwarder_propagates_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    :class:`asyncio.CancelledError` exits the supervisor without restarting.

    The parent's ``finally`` block relies on this: ``forwarder.cancel()``
    followed by ``await forwarder`` must complete promptly with a
    single CancelledError, not loop forever on restart.
    """
    call_count = 0
    forwarder_running = asyncio.Event()

    async def fake_forwarder(**_: Any) -> None:
        """Fake forwarder that announces it's running and then blocks."""
        nonlocal call_count
        call_count += 1
        forwarder_running.set()
        # Wait forever — let the test cancel us.
        await asyncio.Event().wait()

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record sleeps; cancellation must NOT route through here."""
        sleeps.append(seconds)

    monkeypatch.setattr(forwarder, "forward_claude_transcript_to_session", fake_forwarder)
    monkeypatch.setattr(forwarder, "_supervisor_sleep", fake_sleep)

    supervisor_task = asyncio.create_task(
        forwarder.supervise_forwarder(**_supervisor_kwargs(tmp_path)),
    )
    # Wait until the fake forwarder is actually executing before
    # cancelling, so the cancellation hits inside the forwarder
    # call (the realistic path), not before it even starts.
    await asyncio.wait_for(forwarder_running.wait(), timeout=1.0)
    supervisor_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await supervisor_task

    # Forwarder ran exactly once and no backoff sleep happened —
    # cancellation skipped the restart path entirely.
    assert call_count == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_supervise_forwarder_backoff_grows_on_repeated_crashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Consecutive crashes use exponentially growing backoff, capped at the max.

    Prevents a fast-failing forwarder from POST-storming the Omnigent server
    or burning CPU on tight-loop restarts.
    """
    # 6 crashes is enough to walk past the cap: 1, 2, 4, 8, 16, 30
    # (the 6th would naively be 32 but the cap clamps it to 30).
    crash_budget = 6
    call_count = 0

    async def fake_forwarder(**_: Any) -> None:
        """Crash ``crash_budget`` times, then signal stop."""
        nonlocal call_count
        call_count += 1
        if call_count <= crash_budget:
            raise RuntimeError(f"simulated crash {call_count}")
        raise asyncio.CancelledError()

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record sleep durations without waiting."""
        sleeps.append(seconds)

    # Pin monotonic so every run looks instantaneous and the
    # healthy-uptime reset branch never fires.
    monkeypatch.setattr(forwarder, "_supervisor_monotonic", lambda: 1000.0)
    monkeypatch.setattr(forwarder, "forward_claude_transcript_to_session", fake_forwarder)
    monkeypatch.setattr(forwarder, "_supervisor_sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await forwarder.supervise_forwarder(**_supervisor_kwargs(tmp_path))

    # 6 crashes → 6 sleeps with doubling backoff, last clamped to max.
    # If the cap isn't being applied, the 6th entry would be 32.0
    # instead of _SUPERVISOR_MAX_BACKOFF_S (30.0).
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, forwarder._SUPERVISOR_MAX_BACKOFF_S], (
        f"Backoff should double up to the {forwarder._SUPERVISOR_MAX_BACKOFF_S}s "
        f"cap; got {sleeps}."
    )


@pytest.mark.asyncio
async def test_supervise_forwarder_resets_backoff_after_healthy_uptime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A long-running forwarder that eventually crashes resets backoff.

    Without this, a forwarder that ran healthy for hours and then
    hit a transient blip would still wait the full 30s before
    restarting — penalizing successful long runs as if they were
    a crash-loop.
    """
    healthy_threshold = forwarder._SUPERVISOR_HEALTHY_UPTIME_S
    call_count = 0
    # The supervisor calls _supervisor_monotonic() twice per
    # iteration: once at run_started_at, once at run_duration_s.
    # We feed 4 iterations × 2 readings = 8 values, with run 3
    # crossing the healthy threshold.
    monotonic_values = iter(
        [
            # Run 1: short-lived (1s uptime). Backoff stays at initial.
            0.0,
            1.0,
            # Run 2: short-lived (1s uptime). Backoff doubles.
            10.0,
            11.0,
            # Run 3: long-lived (>= threshold). Backoff resets after
            # this iteration completes.
            20.0,
            20.0 + healthy_threshold + 1.0,
            # Run 4: short-lived. Should sleep the post-reset initial
            # value, not the doubled-from-run-3 value.
            200.0,
            201.0,
        ],
    )

    async def fake_forwarder(**_: Any) -> None:
        """Crash 3 times to drive the reset, then signal stop on call 4."""
        nonlocal call_count
        call_count += 1
        if call_count >= 4:
            raise asyncio.CancelledError()
        raise RuntimeError(f"simulated crash {call_count}")

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record backoff durations."""
        sleeps.append(seconds)

    monkeypatch.setattr(forwarder, "_supervisor_monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(forwarder, "forward_claude_transcript_to_session", fake_forwarder)
    monkeypatch.setattr(forwarder, "_supervisor_sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await forwarder.supervise_forwarder(**_supervisor_kwargs(tmp_path))

    # Run 1 → sleep 1 (initial). Backoff grows to 2.
    # Run 2 → sleep 2. Backoff grows to 4.
    # Run 3 → healthy, backoff resets to initial BEFORE sleep, then
    #         doubles to 2 after sleep — so sleep value is the initial.
    # Run 4 (CancelledError) → propagates, no further sleep.
    # If the reset branch didn't fire, run 3's sleep would be 4.0
    # instead of 1.0.
    assert sleeps == [
        1.0,
        2.0,
        forwarder._SUPERVISOR_INITIAL_BACKOFF_S,
    ], (
        f"Healthy uptime should reset backoff before the post-iteration sleep; "
        f"got {sleeps}. If the third entry is 4.0, the reset branch is not firing."
    )


@pytest.mark.parametrize(
    "raised_exc",
    [SystemExit("shutdown"), KeyboardInterrupt()],
    ids=["SystemExit", "KeyboardInterrupt"],
)
@pytest.mark.asyncio
async def test_supervise_forwarder_propagates_process_shutdown_signals(
    raised_exc: BaseException,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    :class:`BaseException` subclasses used for shutdown are not swallowed.

    The supervisor only restarts on :class:`Exception`. Process-level
    signals (``KeyboardInterrupt`` from Ctrl-C, ``SystemExit`` from
    ``sys.exit()``) must propagate so the wrapper CLI shuts down
    promptly instead of looping inside an "unkillable" supervisor.
    """
    call_count = 0

    async def fake_forwarder(**_: Any) -> None:
        """Raise the shutdown signal under test on the first call."""
        nonlocal call_count
        call_count += 1
        raise raised_exc

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record sleeps; this path should never be reached."""
        sleeps.append(seconds)

    monkeypatch.setattr(forwarder, "forward_claude_transcript_to_session", fake_forwarder)
    monkeypatch.setattr(forwarder, "_supervisor_sleep", fake_sleep)

    with pytest.raises(type(raised_exc)):
        await forwarder.supervise_forwarder(**_supervisor_kwargs(tmp_path))

    # Forwarder ran exactly once and no backoff sleep happened — the
    # shutdown signal propagated through the supervisor without a
    # restart attempt. If call_count is 2+, the supervisor swallowed
    # the signal (the regression).
    assert call_count == 1
    assert sleeps == []


# _PostRetryTracker: bounded subagent_delivery_not_confirmed retries (L2)
# ---------------------------------------------------------------------------


def _http_status_error(status_code: int, body: object) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError whose response.json() returns `body`."""
    request = httpx.Request("POST", "http://omnigent/v1/sessions/conv_x/events")
    content = json.dumps(body).encode() if body is not None else b""
    response = httpx.Response(status_code, request=request, content=content)
    return httpx.HTTPStatusError("rejected", request=request, response=response)


def test_subagent_delivery_not_confirmed_503_exhausts_after_budget() -> None:
    tracker = forwarder._PostRetryTracker(max_not_confirmed_attempts=3)
    exc = _http_status_error(
        503, {"error": "subagent_delivery_not_confirmed", "reason": "missing_work_entry"}
    )
    # Attempts 1 and 2 keep retrying...
    assert tracker.record_failure("k", exc, session_id="conv_retry").exhausted is False
    assert tracker.record_failure("k", exc, session_id="conv_retry").exhausted is False
    # ...attempt 3 hits the not-confirmed budget and gives up.
    assert tracker.record_failure("k", exc, session_id="conv_retry").exhausted is True


def test_generic_503_without_not_confirmed_body_never_exhausts() -> None:
    tracker = forwarder._PostRetryTracker(max_not_confirmed_attempts=3)
    exc = _http_status_error(503, {"error": "internal_error"})
    for _ in range(10):
        assert tracker.record_failure("k", exc, session_id="conv_retry").exhausted is False


def test_unbounded_transient_retries_keep_delay_capped_without_overflow() -> None:
    # A transport-level failure is neither permanent nor not-confirmed, so it
    # retries with no give-up budget and `attempts` grows without bound. The
    # backoff exponent must be clamped before `2 ** n` is evaluated: min()
    # computes both operands, so an unclamped exponent overflows float at
    # attempt ~1025 and raises OverflowError out of record_failure.
    tracker = forwarder._PostRetryTracker(base_delay_s=1.0, max_delay_s=30.0)
    exc = httpx.RequestError("Databricks token refresh returned no token")
    for _ in range(2000):
        decision = tracker.record_failure("k", exc, session_id="conv_retry")
        assert decision.exhausted is False
        assert decision.delay_s <= 30.0
    # The schedule still saturates at the cap instead of decaying.
    assert decision.delay_s == 30.0


def test_backoff_schedule_unchanged_below_the_cap() -> None:
    tracker = forwarder._PostRetryTracker(base_delay_s=1.0, max_delay_s=30.0)
    exc = httpx.RequestError("boom")
    delays = [tracker.record_failure("k", exc, session_id="conv_retry").delay_s for _ in range(6)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]


def test_permanent_4xx_still_exhausts_at_three() -> None:
    tracker = forwarder._PostRetryTracker(max_permanent_attempts=3)
    exc = _http_status_error(400, {"error": "bad_request"})
    assert tracker.record_failure("k", exc, session_id="conv_retry").exhausted is False
    assert tracker.record_failure("k", exc, session_id="conv_retry").exhausted is False
    assert tracker.record_failure("k", exc, session_id="conv_retry").exhausted is True


def test_is_subagent_delivery_not_confirmed_classifier() -> None:
    yes = _http_status_error(503, {"error": "subagent_delivery_not_confirmed"})
    no_status = _http_status_error(500, {"error": "subagent_delivery_not_confirmed"})
    no_body = _http_status_error(503, {"error": "something_else"})
    assert forwarder._is_subagent_delivery_not_confirmed(yes) is True
    assert forwarder._is_subagent_delivery_not_confirmed(no_status) is False
    assert forwarder._is_subagent_delivery_not_confirmed(no_body) is False
    assert forwarder._is_subagent_delivery_not_confirmed(httpx.ConnectError("boom")) is False


@pytest.mark.asyncio
async def test_forward_progress_timeout_resets_after_each_response() -> None:
    """A healthy long drain is not cancelled while responses keep arriving."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.06)
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        async with forwarder._forward_progress_timeout(client, 0.1):
            for _ in range(3):
                await client.post("/events", json={})


@pytest.mark.asyncio
async def test_forward_loop_deadline_unsticks_a_stalled_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A stalled await inside one poll iteration is cancelled and the loop resumes.

    A silent stall in any forwarding stage used to stop mirroring, status
    events and the pane busy signal forever — with zero log output — and
    the pane reaper then killed the live session an hour later. The
    iteration deadline converts such a stall into a logged, bounded
    hiccup: the stuck await is cancelled (the warning's traceback names
    it) and the next iteration proceeds.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    monkeypatch.setattr(forwarder, "_FORWARD_LOOP_STALL_DEADLINE_S", 0.2)
    ensure_calls: list[int] = []
    real_ensure = forwarder._ensure_hook_state

    async def _stalls_on_first_call(*args: Any, **kwargs: Any) -> Any:
        ensure_calls.append(len(ensure_calls) + 1)
        if len(ensure_calls) == 1:
            await asyncio.Event().wait()
        return await real_ensure(*args, **kwargs)

    monkeypatch.setattr(forwarder, "_ensure_hook_state", _stalls_on_first_call)
    monkeypatch.setattr(forwarder._logger, "handlers", [caplog.handler])
    monkeypatch.setattr(forwarder._logger, "propagate", False)

    with caplog.at_level(logging.WARNING, logger="omnigent.harnesses.claude_native.forwarder"):
        task = asyncio.create_task(
            forwarder.forward_claude_transcript_to_session(
                base_url="http://127.0.0.1:9",
                headers={},
                session_id="conv_stall",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.01,
            )
        )
        try:

            async def _second_iteration_ran() -> None:
                while len(ensure_calls) < 2:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(_second_iteration_ran(), timeout=5.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    stall_warnings = [r for r in caplog.records if "made no live progress" in r.getMessage()]
    assert stall_warnings, "the deadline trip must be loudly logged, never silent"
    # The warning's traceback names the stalled await for next-time forensics.
    assert stall_warnings[0].exc_info is not None

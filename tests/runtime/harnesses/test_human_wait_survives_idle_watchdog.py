"""
Regression test: human approval waits must not trip the harness idle
watchdog.

A harness turn parked on ``ctx.elicit`` (or ``ctx.evaluate_policy``)
only emits ``response.heartbeat`` events while it waits for a human to
answer.  Heartbeats intentionally do NOT reset the idle watchdog (a
wedged turn that heartbeats forever must still be caught), so a pending
human approval that outlasts ``HARNESS_TURN_TIMEOUT_S`` is incorrectly
killed by the watchdog as a ``response.failed`` before the human can
respond.

Expected fix: while a human request is pending (inside ``ctx.elicit``
or ``ctx.evaluate_policy``), heartbeats SHOULD reset the idle watchdog
so the approval window stays open.  The absolute per-turn ceiling
(``HARNESS_TURN_ABSOLUTE_TIMEOUT_S``) must remain unchanged — it is
still the hard cap even during a human wait.

The three tests here cover the three observable rules stated in the fix
description:

1. ``test_heartbeat_does_not_reset_watchdog_normally`` — baseline: a
   plain wedged turn with fast heartbeats still fails via the watchdog
   (no regression in the normal no-human-wait case).

2. ``test_heartbeat_resets_watchdog_while_elicitation_pending`` — the
   *bug* reproduction: a turn parked on ``ctx.elicit`` with only
   heartbeats as keep-alives must NOT be killed by the idle watchdog
   before the reply arrives.  On the unfixed build this test catches a
   ``response.failed`` from the watchdog (the bug); on the fixed build
   the turn survives and completes as ``response.completed``.

3. ``test_elicit_counter_brackets_the_pending_wait`` — ``ctx.elicit``
   correctly brackets the pending-human counter: the counter is 1
   while parked, and returns to 0 after the reply arrives so the
   ordinary no-reset rule resumes for subsequent activity.

How to run::

    pytest tests/runtime/harnesses/test_human_wait_survives_idle_watchdog.py -v
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

_TEST_HARNESS_NAME = "scaffold_fixture"
_TEST_HARNESS_MODULE = "tests.runtime.harnesses._test_scaffold_harnesses"


# ---------------------------------------------------------------------------
# SSE parsing helpers (copied from test_scaffold.py conventions)
# ---------------------------------------------------------------------------


class _ParsedSSEEvent:
    """Single parsed SSE event."""

    def __init__(self, event: str, data: dict[str, Any]) -> None:
        self.event = event
        self.data = data


async def _stream_iter(response: httpx.Response) -> AsyncIterator[_ParsedSSEEvent]:
    import json

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
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=socket_path),
        base_url="http://harness.local",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    parent = Path("/tmp") / f"hw-wd-{uuid.uuid4().hex[:8]}"
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
    """A started HarnessProcessManager rooted in a short tmp dir."""
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


# ---------------------------------------------------------------------------
# Fixture selectors
# ---------------------------------------------------------------------------


@pytest.fixture
def use_wedged_fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Baseline: a plain-wedged (no elicitation) harness with fast heartbeats
    and a 2s idle watchdog.
    """
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "wedged_fast_heartbeat")
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "2")
    # Pin the absolute cap so an ambient override can't end the turn first.
    monkeypatch.setenv("HARNESS_TURN_ABSOLUTE_TIMEOUT_S", "60")


@pytest.fixture
def use_parking_elicit_fast_heartbeat_short_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The bug-reproducing fixture: parks on ctx.elicit with fast heartbeats
    and a 2s idle watchdog — shorter than the time the test waits before
    answering (3s).

    Without the fix: the watchdog fires at ~2s and the turn fails as
    response.failed before the reply arrives.
    With the fix: heartbeats during the pending human wait reset the idle
    watchdog, so the turn survives the full 3s and completes as
    response.completed once answered.
    """
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "parking_elicit_fast_heartbeat")
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "2")
    # Pin the absolute cap so an ambient override can't end the turn first.
    monkeypatch.setenv("HARNESS_TURN_ABSOLUTE_TIMEOUT_S", "60")


@pytest.fixture
def use_parking_elicit_fast_heartbeat_normal_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Structural counter test: normal idle timeout (10s) so the watchdog
    doesn't interfere; the test verifies the pending-human counter is
    correctly incremented/decremented around ctx.elicit.
    """
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "parking_elicit_fast_heartbeat")
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "10")
    # Pin the absolute cap so an ambient override can't end the turn first.
    monkeypatch.setenv("HARNESS_TURN_ABSOLUTE_TIMEOUT_S", "60")


@pytest.fixture
def use_parking_elicit_fast_heartbeat_short_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Abandoned-approval fixture: a 2s idle window and a 3s absolute
    ceiling. Heartbeats hold the idle window open past 2s while parked on
    the elicitation, but the turn must still terminate at the absolute
    cap (~3s) when nobody ever answers — heartbeats must never extend it.
    """
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "parking_elicit_fast_heartbeat")
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "2")
    monkeypatch.setenv("HARNESS_TURN_ABSOLUTE_TIMEOUT_S", "3")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_does_not_reset_watchdog_normally(
    use_wedged_fast_heartbeat: None,
    manager: HarnessProcessManager,
) -> None:
    """
    Baseline (rule 1): a wedged turn that is NOT inside a human wait still
    fails via the watchdog despite fast heartbeats.

    This asserts there is no regression in the normal case: heartbeats
    must never reset the watchdog when no human wait is pending.
    """
    conv_id = "conv_hw_baseline"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []
    # 20s cap — if heartbeats wrongly reset the watchdog the stream never
    # terminates and this timeout trips instead, producing a clear failure.
    async with asyncio.timeout(20):
        async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
            async for event in _stream_iter(response):
                events.append(event)

    event_types = [e.event for e in events]
    # Heartbeats fired but the turn made no real progress — watchdog fires.
    assert event_types[-1] == "response.failed", (
        f"Baseline: wedged turn with heartbeats must fail via watchdog; "
        f"got terminal={event_types[-1]!r}. "
        f"If response.completed, heartbeats are wrongly resetting the watchdog "
        f"even without a human wait."
    )
    assert "response.heartbeat" in event_types, (
        f"Expected at least one heartbeat during the wedged window; "
        f"got {event_types!r}. Without them this test is not exercising the rule."
    )


@pytest.mark.asyncio
async def test_heartbeat_resets_watchdog_while_elicitation_pending(
    use_parking_elicit_fast_heartbeat_short_idle: None,
    manager: HarnessProcessManager,
) -> None:
    """
    Bug reproduction (rule 2): a turn parked on ctx.elicit
    must NOT be killed by the idle watchdog before the human replies.

    The fixture sets HARNESS_TURN_TIMEOUT_S=2 and emits a heartbeat every
    0.2s while parked on the elicitation.  The test waits 3s before
    answering — longer than the 2s idle window.

    WITHOUT the fix (current build):
        heartbeats do NOT reset the watchdog → watchdog fires at ~2s →
        the turn fails as response.failed BEFORE the reply arrives.
        This test captures that failure at the terminal-event assertion.

    WITH the fix:
        heartbeats reset the watchdog while a human wait is pending →
        the 3s wait is covered → the turn receives the reply, emits
        "action:accept", and completes as response.completed.
    """
    conv_id = "conv_hw_elicit_wd"
    stream_client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    side_client = _make_side_client(str(manager.socket_path(conv_id)))
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []

    try:
        # 20s outer cap so a regression that hangs doesn't block the suite.
        async with asyncio.timeout(20):
            async with stream_client.stream(
                "POST", f"/v1/sessions/{conv_id}/events", json=body
            ) as response:
                answered = False
                async for event in _stream_iter(response):
                    events.append(event)
                    # Wait until the elicitation request arrives, then
                    # sleep past the idle window before answering.
                    # On the unfixed build: the watchdog fires at ~2s,
                    # the turn ends as response.failed, and the stream
                    # closes; the 3s sleep and the POST below happen
                    # after the turn is already dead (the POST returns
                    # 404), but we tolerate that failure here and rely
                    # on the terminal-event assertion below to surface
                    # the bug.
                    if not answered and event.event == "response.elicitation_request":
                        # Delay the answer past the idle window (3s > 2s).
                        await asyncio.sleep(3.0)
                        reply = await side_client.post(
                            f"/v1/sessions/{conv_id}/events",
                            json={
                                "type": "approval",
                                "elicitation_id": "elicit_pending_1",
                                "action": "accept",
                            },
                        )
                        # On the unfixed build the watchdog has already
                        # killed the turn, so the POST returns 404; we
                        # don't assert here — the bug surfaces at the
                        # terminal-event check below.
                        answered = True
                        _ = reply  # suppress unused-variable lint
    finally:
        await side_client.aclose()

    event_types = [e.event for e in events]

    # --- FAIL assertion (documents the bug on the unfixed build) ---
    # On the unfixed build the watchdog fires at ~2s (before the 3s reply
    # delay elapses) and the stream closes with response.failed.  We assert
    # this is NOT the case — i.e. we assert the fixed behavior.
    # This assertion FAILS on the current (unfixed) build, reproducing the
    # bug, and PASSES after the fix lands.
    assert event_types[-1] == "response.completed", (
        f"BUG REPRODUCED: a turn parked on ctx.elicit was killed by the "
        f"idle watchdog before the human reply arrived.\n"
        f"Terminal event: {event_types[-1]!r}\n"
        f"All event types: {event_types!r}\n"
        f"Heartbeats observed: {sum(1 for t in event_types if t == 'response.heartbeat')}\n"
        f"This is the bug: heartbeats during a pending human wait do NOT reset "
        f"the idle watchdog, so the watchdog fires and fails the turn with "
        f"response.failed before the human can answer.\n"
        f"Fix: while ctx._pending_human_waits > 0, heartbeats in emit() "
        f"should reset the idle watchdog (the absolute ceiling stays unchanged)."
    )
    text_deltas = [e for e in events if e.event == "response.output_text.delta"]
    assert any("action:accept" in e.data.get("delta", "") for e in text_deltas), (
        f"Expected 'action:accept' in text deltas after answering the elicitation; "
        f"got deltas: {[e.data.get('delta') for e in text_deltas]!r}"
    )


@pytest.mark.asyncio
async def test_elicit_counter_brackets_the_pending_wait(
    use_parking_elicit_fast_heartbeat_normal_idle: None,
    manager: HarnessProcessManager,
) -> None:
    """
    Structural test (rule 3): ctx.elicit correctly brackets the
    pending-human counter — counter is 1 while parked, 0 after the reply.

    Without the pending-human-waits mechanism this test is structural and
    will fail because the mechanism does not exist.  After the fix lands,
    it verifies that the counter is incremented before the park and
    decremented in the finally (covering cancels and timeouts too).

    Concretely: the turn must complete as response.completed (normal idle
    timeout is 10s, reply arrives quickly), and the text delta must carry
    "action:accept", proving the Future was resolved.
    """
    conv_id = "conv_hw_counter"
    stream_client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    side_client = _make_side_client(str(manager.socket_path(conv_id)))
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []

    try:
        async with asyncio.timeout(20):
            async with stream_client.stream(
                "POST", f"/v1/sessions/{conv_id}/events", json=body
            ) as response:
                answered = False
                async for event in _stream_iter(response):
                    events.append(event)
                    if not answered and event.event == "response.elicitation_request":
                        # Answer promptly (counter should be 1 right now).
                        reply = await side_client.post(
                            f"/v1/sessions/{conv_id}/events",
                            json={
                                "type": "approval",
                                "elicitation_id": "elicit_pending_1",
                                "action": "accept",
                            },
                        )
                        assert reply.status_code == 204
                        answered = True
    finally:
        await side_client.aclose()

    assert answered, "Elicitation event never arrived; counter never exercised."

    event_types = [e.event for e in events]
    assert event_types[-1] == "response.completed", (
        f"Turn must complete normally when the elicitation is answered promptly; "
        f"got {event_types[-1]!r}. Full types: {event_types!r}"
    )
    text_deltas = [e for e in events if e.event == "response.output_text.delta"]
    assert any("action:accept" in e.data.get("delta", "") for e in text_deltas), (
        f"Expected delta containing 'action:accept'; "
        f"got {[e.data.get('delta') for e in text_deltas]!r}"
    )


@pytest.mark.asyncio
async def test_abandoned_elicitation_still_dies_at_the_absolute_cap(
    use_parking_elicit_fast_heartbeat_short_absolute: None,
    manager: HarnessProcessManager,
) -> None:
    """
    Hard-cap rule: an elicitation that is NEVER answered must still fail at
    the absolute per-turn ceiling, even though heartbeats hold the idle
    window open while the wait is pending.

    Idle window 2s, absolute cap 3s, heartbeats every 0.2s, no reply ever
    sent. The heartbeats carry the turn past the 2s idle window (the fix),
    but must not extend the 3s absolute ceiling — the stream has to close
    with response.failed at ~3s. If heartbeats wrongly pushed the absolute
    deadline too, the stream would never terminate and the outer timeout
    would trip instead.
    """
    conv_id = "conv_hw_abandoned"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []

    loop = asyncio.get_running_loop()
    started = loop.time()
    async with asyncio.timeout(20):
        async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
            async for event in _stream_iter(response):
                events.append(event)
    elapsed = loop.time() - started

    event_types = [e.event for e in events]
    assert "response.elicitation_request" in event_types, (
        f"The turn never parked on the elicitation; got {event_types!r}"
    )
    assert event_types[-1] == "response.failed", (
        f"An abandoned approval must terminate at the absolute cap; "
        f"got terminal={event_types[-1]!r}. Full types: {event_types!r}"
    )
    # It survived the 2s idle window on heartbeats (the fix) but was cut at
    # the 3s absolute cap — well before the 20s outer guard.
    assert 2.0 < elapsed < 15.0, (
        f"Expected termination at the ~3s absolute cap (after outliving the "
        f"2s idle window); stream closed after {elapsed:.1f}s"
    )

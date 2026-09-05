"""A transiently wedged turn is recovered by the scaffold, not hard-stopped.

An idle-watchdog expiry means one call (typically an LLM request that opened
a stream and then emitted nothing) wedged mid-turn. That is usually
transient, so ``HarnessApp._guarded_run_turn`` must abandon the wedged
``run_turn`` invocation and re-run the turn (announcing the recovery with a
``response.retry`` event) instead of failing the whole run on the first
expiry. Only when every attempt wedges — or the absolute ceiling leaves no
room for another attempt — does the turn surface as ``response.failed``.

The tests cover the rules of that recovery:

1. a turn whose first invocation wedges and whose retry completes must end
   ``response.completed``, with a ``response.retry`` announcing the recovery;
2. a turn that wedges on every attempt still fails, and its terminal error
   says recovery was attempted;
3. recovery must not retry past the absolute ceiling — with less than one
   idle window of absolute budget left, the expiry fails immediately;
4. a cancelled turn is not retried.

How to run::

    pytest tests/runtime/harnesses/test_wedged_turn_recovery.py -v
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
from omnigent.runtime.harnesses._scaffold import HarnessApp, TurnContext
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
    parent = Path("/tmp") / f"wedge-rec-{uuid.uuid4().hex[:8]}"
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


@pytest.fixture
def use_wedge_once_then_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the wedge-once harness; 2s idle window, high absolute cap."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "wedge_once_then_complete")
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "2")
    # Pin the absolute cap high so only the idle window governs the test.
    monkeypatch.setenv("HARNESS_TURN_ABSOLUTE_TIMEOUT_S", "60")


@pytest.fixture
def use_wedged_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn the always-wedged harness; 2s idle window, high absolute cap."""
    monkeypatch.setenv("HARNESS_TEST_FIXTURE", "wedged")
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "2")
    monkeypatch.setenv("HARNESS_TURN_ABSOLUTE_TIMEOUT_S", "60")


# ---------------------------------------------------------------------------
# End-to-end through a real harness subprocess
# ---------------------------------------------------------------------------


async def test_turn_wedged_once_recovers_and_completes(
    use_wedge_once_then_complete: None,
    manager: HarnessProcessManager,
) -> None:
    """A single wedged invocation is abandoned and the retried turn completes.

    On the unfixed scaffold the first idle expiry fails the whole turn as
    ``response.failed`` — this test then fails at the terminal-event
    assertion. With recovery, the wedged invocation is cancelled, a
    ``response.retry`` announces the re-run, and the retry completes.
    """
    conv_id = "conv_wedge_recover"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []
    # One wedged window (~2s) + the retried turn; 30s guards only a hang.
    async with asyncio.timeout(30):
        async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
            async for event in _stream_iter(response):
                events.append(event)

    event_types = [e.event for e in events]
    assert event_types[-1] == "response.completed", (
        f"A turn whose wedged call was recovered must complete; got terminal "
        f"{event_types[-1]!r} (full: {event_types!r}). response.failed means "
        f"the first idle expiry still hard-stops the run."
    )
    retries = [e for e in events if e.event == "response.retry"]
    assert len(retries) == 1, (
        f"Exactly one response.retry must announce the recovery; got "
        f"{len(retries)} (full: {event_types!r})."
    )
    retry = retries[0].data
    assert retry["source"] == "llm" and retry["error"]["code"] == "timeout", (
        f"The retry event must classify the wedge as a retryable llm timeout; got {retry!r}."
    )
    deltas = [e.data.get("delta", "") for e in events if e.event == "response.output_text.delta"]
    assert any("recovered-done" in d for d in deltas), (
        f"The retried turn's output must reach the stream; deltas: {deltas!r}."
    )


async def test_turn_wedged_on_every_attempt_still_fails(
    use_wedged_forever: None,
    manager: HarnessProcessManager,
) -> None:
    """When the retry wedges too, the turn fails and the error says so.

    Recovery is bounded: a call that wedges twice in a row is genuinely
    stuck, so the turn must still surface ``response.failed`` — with the
    idle-watchdog message noting that recovery was attempted, so the user
    knows the failure survived a retry.
    """
    conv_id = "conv_wedge_exhausted"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    body = {"type": "message", "role": "user", "model": "test-agent", "content": []}
    events: list[_ParsedSSEEvent] = []
    # Two wedged windows (~4s) then the terminal; 30s guards only a hang.
    async with asyncio.timeout(30):
        async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
            async for event in _stream_iter(response):
                events.append(event)

    event_types = [e.event for e in events]
    assert event_types[-1] == "response.failed", (
        f"A turn wedged on every attempt must still fail; got {event_types!r}."
    )
    assert "response.retry" in event_types, (
        f"The bounded recovery retry must have been announced before the "
        f"failure; got {event_types!r}."
    )
    error = events[-1].data["response"]["error"]
    assert error is not None and "idle watchdog" in error["message"], (
        f"The terminal error must name the idle watchdog; got {error!r}."
    )
    assert "recovery" in error["message"], (
        f"The terminal error must say recovery was attempted so the user "
        f"knows the failure survived a retry; got {error!r}."
    )


# ---------------------------------------------------------------------------
# In-process rules (fast, no subprocess)
# ---------------------------------------------------------------------------


async def test_no_retry_when_absolute_budget_below_one_idle_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No retry starts when the absolute ceiling can't fit another window.

    A retry that could not even wedge again before the hard cap kills it
    buys nothing; the expiry must fail immediately via the idle error so a
    post-ceiling stall keeps dying promptly.
    """
    from omnigent.runtime.harnesses import _scaffold

    attempts = 0

    class _AlwaysWedgedApp(HarnessApp):
        async def run_turn(self, request: Any, ctx: TurnContext) -> None:
            nonlocal attempts
            del request, ctx
            attempts += 1
            await asyncio.Event().wait()

    # Absolute cap (0.5s) leaves less than one idle window (0.4s) after the
    # first expiry, so the recovery gate must refuse to start a retry.
    monkeypatch.setattr(_scaffold, "_TURN_IDLE_TIMEOUT_S", 0.4)
    monkeypatch.setattr(_scaffold, "_TURN_ABSOLUTE_TIMEOUT_S", 0.5)

    app = _AlwaysWedgedApp()
    ctx = TurnContext(
        response_id="resp_no_budget",
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )
    async with asyncio.timeout(5):
        with pytest.raises(RuntimeError) as excinfo:
            await app._guarded_run_turn(None, ctx)  # type: ignore[arg-type]

    assert attempts == 1, (
        f"run_turn must not be retried when the absolute budget can't fit "
        f"another idle window; it ran {attempts} times."
    )
    assert "idle watchdog" in str(excinfo.value), str(excinfo.value)


async def test_cancelled_turn_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn cancelled while wedged must not be re-run by recovery."""
    from omnigent.runtime.harnesses import _scaffold

    attempts = 0

    class _WedgedThenCancelledApp(HarnessApp):
        async def run_turn(self, request: Any, ctx: TurnContext) -> None:
            nonlocal attempts
            del request
            attempts += 1
            ctx.cancelled.set()  # an interrupt landed while the call wedged
            await asyncio.Event().wait()

    monkeypatch.setattr(_scaffold, "_TURN_IDLE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(_scaffold, "_TURN_ABSOLUTE_TIMEOUT_S", 3600.0)

    app = _WedgedThenCancelledApp()
    ctx = TurnContext(
        response_id="resp_cancelled_no_retry",
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )
    async with asyncio.timeout(5):
        with pytest.raises(RuntimeError):
            await app._guarded_run_turn(None, ctx)  # type: ignore[arg-type]

    assert attempts == 1, (
        f"A cancelled turn must not be retried by wedge recovery; run_turn ran {attempts} times."
    )


async def test_recovered_turn_failure_counts_only_final_attempt_in_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exhausted-recovery error reports the retry count accurately."""
    from omnigent.runtime.harnesses import _scaffold

    class _AlwaysWedgedApp(HarnessApp):
        async def run_turn(self, request: Any, ctx: TurnContext) -> None:
            del request, ctx
            await asyncio.Event().wait()

    monkeypatch.setattr(_scaffold, "_TURN_IDLE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(_scaffold, "_TURN_ABSOLUTE_TIMEOUT_S", 3600.0)

    app = _AlwaysWedgedApp()
    ctx = TurnContext(
        response_id="resp_retry_count",
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )
    async with asyncio.timeout(5):
        with pytest.raises(RuntimeError) as excinfo:
            await app._guarded_run_turn(None, ctx)  # type: ignore[arg-type]

    message = str(excinfo.value)
    assert "idle watchdog" in message, message
    assert "1 recovery retry was attempted and also wedged" in message, message

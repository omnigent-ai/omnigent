"""Backoff and log throttling while the forwarder loop keeps failing."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import record_hook_event
from omnigent.inner.databricks_executor import DatabricksAuthError
from tests.harnesses.claude_native.forwarder._support import _start_recording_server

_LOGGER_NAME = "omnigent.harnesses.claude_native.forwarder"


def test_failure_streak_delay_grows_caps_and_resets() -> None:
    """Delay doubles per failure, caps, and returns to the poll interval."""
    streak = forwarder._FailureStreak()
    assert streak.delay_s(3.5) == 3.5
    delays = []
    for i in range(8):
        streak.record(float(i))
        delays.append(streak.delay_s(3.5))
    assert delays == [3.5, 7.0, 14.0, 28.0, 56.0, 60.0, 60.0, 60.0]
    assert streak.recover(100.0) == (8, 100.0)
    assert streak.recover(101.0) is None
    assert streak.delay_s(3.5) == 3.5


def test_failure_streak_logs_first_then_periodically() -> None:
    """Only the first failure and one per interval are loggable."""
    interval = forwarder._LOOP_FAILURE_LOG_INTERVAL_S
    streak = forwarder._FailureStreak()
    assert streak.record(0.0) == "first"
    assert streak.record(1.0) == "quiet"
    assert streak.record(interval - 1) == "quiet"
    assert streak.record(interval) == "periodic"
    assert streak.record(interval + 1) == "quiet"
    assert streak.record(2 * interval) == "periodic"


@pytest.mark.asyncio
async def test_forwarder_backs_off_and_throttles_logs_while_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A persistent auth failure backs off, logs one stack, then recovers."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    original_forward = forwarder._forward_available_items
    failures_left = 8

    async def _flaky_forward(**kwargs: Any) -> forwarder.TranscriptForwardState:
        nonlocal failures_left
        if failures_left > 0:
            failures_left -= 1
            raise DatabricksAuthError("Host credential service could not supply credentials")
        return await original_forward(**kwargs)

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _recording_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 11:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(forwarder, "_forward_available_items", _flaky_forward)
    monkeypatch.setattr(forwarder.asyncio, "sleep", _recording_sleep)
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    server, thread, base_url = _start_recording_server()
    try:
        with pytest.raises(asyncio.CancelledError):
            await forwarder.forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id="conv_abc",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=1.0,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 1.0, 1.0, 1.0]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    recovered = [r for r in caplog.records if "recovered" in r.getMessage()]
    assert len(recovered) == 1
    assert recovered[0].levelno == logging.INFO
    assert "after 8 failed attempts" in recovered[0].getMessage()


@pytest.mark.asyncio
async def test_subagent_worker_failure_logs_are_throttled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated child worker failures log one stack, then a recovery line."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    monkeypatch.setattr(forwarder, "_subagents_dir_for_transcript", lambda _path: subagents_dir)
    worker_failures: dict[str, forwarder._FailureStreak] = {}
    failing = True

    async def _one(**_kwargs: Any) -> None:
        if failing:
            raise DatabricksAuthError("no credentials")

    monkeypatch.setattr(forwarder, "_forward_one_subagent", _one)
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    state = forwarder.SubagentForwardState(
        subagents={
            "agent-1": forwarder.SubagentEntry(
                subagent_id="agent-1", child_conversation_id="child-1"
            )
        }
    )

    async def _tick() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200)), base_url="http://ap"
        ) as client:
            await forwarder._forward_available_subagents(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                agent_name="claude-native-ui",
                start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                worker_failures=worker_failures,
            )

    for _ in range(5):
        await _tick()
    failing = False
    await _tick()

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    recovered = [r for r in caplog.records if "recovered" in r.getMessage()]
    assert len(recovered) == 1
    assert "after 5 failed attempts" in recovered[0].getMessage()
    assert not worker_failures

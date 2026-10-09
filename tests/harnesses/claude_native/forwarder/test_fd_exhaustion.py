"""The forwarder treats fd exhaustion as one paused outage, not a per-poll ERROR."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import forwarder
from omnigent.harnesses.claude_native.bridge import record_hook_event
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_item_request,
    _start_recording_server,
)


def test_fd_exhaustion_poll_delay_doubles_to_the_cap() -> None:
    delay = 0.25
    seen = []
    for _ in range(7):
        delay = forwarder._fd_exhaustion_poll_delay(delay, 0.25)
        seen.append(delay)

    assert seen == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0]
    # A poll interval already above the cap is kept rather than shortened.
    assert forwarder._fd_exhaustion_poll_delay(30.0, 30.0) == 30.0


@pytest.mark.asyncio
async def test_fd_exhaustion_warns_once_then_mirroring_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """EMFILE polls log one WARNING then DEBUG, never the per-poll ERROR; mirroring resumes."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "assistant-1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "after the outage"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    original_read = forwarder.read_active_session_id
    remaining_failures = {"n": 5}

    def exhausted_read(path: Path) -> str | None:
        if remaining_failures["n"] > 0:
            remaining_failures["n"] -= 1
            raise OSError(errno.EMFILE, "Too many open files", str(path / "bridge.json"))
        return original_read(path)

    monkeypatch.setattr(forwarder, "read_active_session_id", exhausted_read)
    sleeps: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(seconds)

    monkeypatch.setattr(forwarder, "_poll_sleep", recording_sleep)
    caplog.set_level(logging.DEBUG, logger=forwarder._logger.name)

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
        # The item posts mid-poll; the recovery line lands once that poll completes.
        deadline = asyncio.get_running_loop().time() + 5.0
        while not any("recovered after fd exhaustion" in r.getMessage() for r in caplog.records):
            assert asyncio.get_running_loop().time() < deadline, "forwarder never logged recovery"
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"]["data"]["item_data"]["content"] == [
        {"type": "output_text", "text": "after the outage"}
    ]
    assert remaining_failures["n"] == 0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    exhausted = [r for r in caplog.records if "hit fd exhaustion" in r.getMessage()]
    assert [r.levelno for r in exhausted] == [logging.WARNING] + [logging.DEBUG] * 4
    assert "EMFILE" in exhausted[0].getMessage()
    assert "session=conv_abc" in exhausted[0].getMessage()
    recovered = [r for r in caplog.records if "recovered after fd exhaustion" in r.getMessage()]
    assert [r.levelno for r in recovered] == [logging.INFO]
    # Each failing poll doubled the delay; the first healthy poll reset it.
    assert sleeps[:6] == pytest.approx([0.02, 0.04, 0.08, 0.16, 0.32, 0.01])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rewarn_s", "expected_warnings", "expected_recoveries"),
    [
        pytest.param(60.0, 1, 1, id="second-outage-inside-window-stays-quiet"),
        pytest.param(0.0, 5, 2, id="outage-past-window-rewarns-and-logs-recovery"),
    ],
)
async def test_rewarn_window_spans_outages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    rewarn_s: float,
    expected_warnings: int,
    expected_recoveries: int,
) -> None:
    """The last-warned stamp survives recovery: flapping stays within one WARNING per window."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "assistant-1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    monkeypatch.setattr(forwarder, "_FD_EXHAUSTION_REWARN_S", rewarn_s)
    original_read = forwarder.read_active_session_id
    # Two outages separated by one healthy poll, then steady recovery.
    script = iter(["fail", "fail", "fail", "ok", "fail", "fail", "ok", "ok"])
    settled = asyncio.Event()

    def scripted_read(path: Path) -> str | None:
        step = next(script, None)
        if step is None:
            settled.set()
        elif step == "fail":
            raise OSError(errno.EMFILE, "Too many open files", str(path / "bridge.json"))
        return original_read(path)

    monkeypatch.setattr(forwarder, "read_active_session_id", scripted_read)
    caplog.set_level(logging.DEBUG, logger=forwarder._logger.name)

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
        await _get_recorded_item_request(server)
        await asyncio.wait_for(settled.wait(), timeout=10)
        # Let the poll that drained the script finish its recovery bookkeeping.
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    exhausted = [r for r in caplog.records if "hit fd exhaustion" in r.getMessage()]
    assert len(exhausted) == 5
    assert sum(r.levelno == logging.WARNING for r in exhausted) == expected_warnings
    recovered = [r for r in caplog.records if "recovered after fd exhaustion" in r.getMessage()]
    assert len(recovered) == expected_recoveries


@pytest.mark.asyncio
async def test_non_fd_failure_ends_the_fd_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A poll failing past the descriptor-dependent reads ends the outage and resets the delay."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "assistant-1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    original_read = forwarder.read_active_session_id
    script = iter(["fail", "fail", "boom", "ok"])

    def scripted_read(path: Path) -> str | None:
        step = next(script, None)
        if step == "fail":
            raise OSError(errno.EMFILE, "Too many open files", str(path / "bridge.json"))
        if step == "boom":
            raise PermissionError("bridge state unreadable")
        return original_read(path)

    monkeypatch.setattr(forwarder, "read_active_session_id", scripted_read)
    sleeps: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(seconds)

    monkeypatch.setattr(forwarder, "_poll_sleep", recording_sleep)
    caplog.set_level(logging.DEBUG, logger=forwarder._logger.name)

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
        await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [
        r.getMessage().startswith("Claude transcript forwarder loop failed") for r in errors
    ] == [True]
    recovered = [r for r in caplog.records if "recovered after fd exhaustion" in r.getMessage()]
    assert len(recovered) == 1
    # Two fd failures doubled the delay; the unrelated failure reset it.
    assert sleeps[:3] == pytest.approx([0.02, 0.04, 0.01])

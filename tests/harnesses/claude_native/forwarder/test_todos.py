"""Todos tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native.bridge import (
    record_hook_event,
)
from omnigent.harnesses.claude_native.forwarder import (
    forward_claude_transcript_to_session,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _RecordingHTTPServer,
    _start_recording_server,
)

# ── Native task state accumulation ───────────────────────────────────────────


async def _drain_todos_request(server: _RecordingHTTPServer) -> dict[str, Any]:
    """
    Await the first ``external_session_todos`` POST from the forwarder.

    :param server: Recording HTTP server.
    :returns: The ``data`` payload of the matching POST body.
    """
    while True:
        req = await _get_recorded_request(server)
        if req["body"].get("type") == "external_session_todos":
            return req["body"]["data"]


def _record_session_start(bridge_dir: Path, transcript_path: Path) -> None:
    """
    Write a ``SessionStart`` hook event so the forwarder enters its main loop.

    :param bridge_dir: Bridge directory for ``record_hook_event``.
    :param transcript_path: Transcript file path carried in the payload.
    :returns: None.
    """
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )


@pytest.mark.asyncio
async def test_forwarder_posts_todos_on_task_created(tmp_path: Path) -> None:
    """
    A ``TaskCreated`` hook event causes the forwarder to POST an
    ``external_session_todos`` event with the new task at status ``"pending"``.

    This fails if the ``TaskCreated`` branch in the forwarder's hook loop
    fails to set ``native_todos_changed = True`` or if the ``todos_to_post``
    list is not built from the accumulation maps.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    _record_session_start(bridge_dir, transcript_path)
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "TaskCreated",
            "session_id": "claude-session",
            "task_id": "1",
            "task_subject": "Write integration tests",
        },
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
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
        data = await _drain_todos_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert data["todos"] == [
        {
            "content": "Write integration tests",
            "status": "pending",
            # activeForm equals content for native tasks (suppresses
            # duplicate rendering in the panel).
            "activeForm": "Write integration tests",
        }
    ]


@pytest.mark.asyncio
async def test_forwarder_posts_todos_on_task_completed(tmp_path: Path) -> None:
    """
    A ``TaskCreated`` followed by ``TaskCompleted`` causes a final POST
    where the task has status ``"completed"``.

    This fails if ``TaskCompleted`` does not update ``task_statuses`` or
    if ``native_todos_changed`` is not set when it should be.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    _record_session_start(bridge_dir, transcript_path)
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "TaskCreated",
            "session_id": "claude-session",
            "task_id": "1",
            "task_subject": "Fix the bug",
        },
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "TaskCompleted",
            "session_id": "claude-session",
            "task_id": "1",
        },
    )

    server, thread, base_url = _start_recording_server()
    # Drain two consecutive todos POSTs: one for TaskCreated, one for TaskCompleted.
    posted: list[dict[str, Any]] = []
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
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
        posted.append(await _drain_todos_request(server))
        posted.append(await _drain_todos_request(server))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # First POST: task is still pending (from TaskCreated).
    assert posted[0]["todos"][0]["status"] == "pending"
    # Second POST: task is completed (from TaskCompleted).
    assert posted[1]["todos"][0]["status"] == "completed"
    assert posted[1]["todos"][0]["content"] == "Fix the bug"


@pytest.mark.asyncio
async def test_forwarder_posts_raw_todos_on_todo_write(tmp_path: Path) -> None:
    """
    A ``PostToolUse/TodoWrite`` hook event causes the forwarder to POST
    the raw ``tool_input.todos`` list verbatim, bypassing accumulation.

    This fails if the ``record.todos is not None`` branch is not taken
    ahead of the native-task path, or if the list is modified before posting.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    raw_todos = [
        {"content": "Write tests", "status": "in_progress", "activeForm": "Writing tests"},
        {"content": "Review PR", "status": "pending", "activeForm": "Reviewing PR"},
    ]
    _record_session_start(bridge_dir, transcript_path)
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "claude-session",
            "tool_name": "TodoWrite",
            "tool_input": {"todos": raw_todos},
        },
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
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
        data = await _drain_todos_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # The raw list is forwarded verbatim — no accumulation or transformation.
    assert data["todos"] == raw_todos

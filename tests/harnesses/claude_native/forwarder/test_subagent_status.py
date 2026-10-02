"""Subagent status tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    ClaudeHookRecord,
    record_hook_event,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _seed_subagent_on_disk,
    _start_recording_server,
    _subagent_drop_row,
)


@pytest.mark.asyncio
async def test_forwarder_ignores_subagent_stop_failure_hook(
    tmp_path: Path,
) -> None:
    """
    A subagent's ``StopFailure`` must not flip the parent session failed.

    Claude Code subagents (spawned via the Agent tool for e.g. Explore)
    inherit the parent's hook settings and write to the same
    ``hooks.jsonl``. A subagent failing must not mark the *parent* turn
    failed — the parent is still running while it awaits the Agent tool
    result. Subagent transcripts live under a ``subagents/`` directory,
    which the forwarder uses to distinguish them from parent events.
    (Running/idle are no longer hook-derived; ``StopFailure`` →
    ``failed`` is the only mapped status left, so this is the surviving
    subagent-skip case.)
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_transcript = tmp_path / "session" / "subagents" / "agent-abc.jsonl"
    subagent_transcript.parent.mkdir(parents=True, exist_ok=True)
    subagent_transcript.write_text("", encoding="utf-8")

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "parent-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Subagent fails first — this must NOT surface as the parent failing.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "subagent-session",
            "transcript_path": str(subagent_transcript),
        },
    )
    # Parent turn fails — this SHOULD surface as the one failed edge.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "parent-session",
            "transcript_path": str(transcript_path),
        },
    )

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
        # Exactly one status POST: the parent's failed. The subagent
        # StopFailure (recorded first) must be skipped, so no second
        # status POST ever arrives — the bounded wait below must time out.
        first = await _get_recorded_request(server)
        with pytest.raises(AssertionError):
            await _get_recorded_request(server, timeout_s=0.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert first["body"] == {
        "type": "external_session_status",
        "data": {"status": "failed"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subagent_session_id", "subagent_transcript_name", "subagent_fields"),
    [
        # Background subagent: foreign session id, non-``subagents/`` path.
        pytest.param("bg-agent-session", "bg-agent.jsonl", {}, id="background-by-session-id"),
        # In-process subagent: parent's session id and path; only ``agent_id`` marks it.
        pytest.param(
            "parent-session",
            "session.jsonl",
            {
                "agent_id": "a4892977eed616593",
                "agent_type": "general-purpose",
                "error": "unknown",
                "last_assistant_message": 'API Error: 499 {"error_code":"CANCELLED","message":""}',
            },
            id="in-process-by-agent-id",
        ),
    ],
)
async def test_forwarder_ignores_subagent_stop_failure_without_subagents_path(
    tmp_path: Path,
    subagent_session_id: str,
    subagent_transcript_name: str,
    subagent_fields: dict[str, str],
) -> None:
    """
    A subagent's ``StopFailure`` must not flip the parent to ``failed``.

    The subsequent parent ``Stop`` acts as an anchor: the forwarder must
    emit exactly one POST (``idle``), proving it ran and that the
    subagent's ``StopFailure`` was silently skipped.
    """
    bridge_dir = tmp_path / "bridge"
    parent_transcript = tmp_path / "session.jsonl"
    parent_transcript.write_text("", encoding="utf-8")
    subagent_transcript = tmp_path / subagent_transcript_name
    subagent_transcript.write_text("", encoding="utf-8")

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "parent-session",
            "transcript_path": str(parent_transcript),
        },
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": subagent_session_id,
            "transcript_path": str(subagent_transcript),
            **subagent_fields,
        },
    )
    # Parent turn ends normally — anchor that proves the forwarder ran.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "parent-session",
            "transcript_path": str(parent_transcript),
        },
    )

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
        first = await _get_recorded_request(server)
        with pytest.raises(AssertionError):
            await _get_recorded_request(server, timeout_s=0.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # If the subagent StopFailure was wrongly forwarded, first would be
    # ``failed``; only the parent's ``idle`` should arrive.
    assert first["body"] == {
        "type": "external_session_status",
        "data": {"status": "idle", "background_task_count": 0, "turn_completed": True},
    }


@pytest.mark.asyncio
async def test_forwarder_parent_stop_failure_not_affected_by_background_session_check(
    tmp_path: Path,
) -> None:
    """
    A ``StopFailure`` carrying the parent's own session id is still
    forwarded as ``failed`` when the session id check is active.
    """
    bridge_dir = tmp_path / "bridge"
    parent_transcript = tmp_path / "session.jsonl"
    parent_transcript.write_text("", encoding="utf-8")

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "parent-session",
            "transcript_path": str(parent_transcript),
        },
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "parent-session",
            "transcript_path": str(parent_transcript),
        },
    )

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
        first = await _get_recorded_request(server)
        with pytest.raises(AssertionError):
            await _get_recorded_request(server, timeout_s=0.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert first["body"] == {
        "type": "external_session_status",
        "data": {"status": "failed"},
    }


def test_is_subagent_hook_record_rotation_race(tmp_path: Path) -> None:
    """
    A StopFailure with an old (pre-rotation) parent session id must NOT be
    classified as a subagent when the seen set includes that old id.
    """
    record = ClaudeHookRecord(
        event_cursor=1,
        byte_offset=100,
        event_name="StopFailure",
        claude_session_id="parent-old",
        transcript_path=tmp_path / "session.jsonl",
    )
    # Both old and new ids are seen — old is still a parent id.
    assert not forwarder._is_subagent_hook_record(
        record, parent_claude_session_ids={"parent-old", "parent-new"}
    )
    # Only the new id is seen — old id would be wrongly dropped without
    # the seen set.
    assert forwarder._is_subagent_hook_record(record, parent_claude_session_ids={"parent-new"})


def test_is_subagent_hook_record_empty_seen_set_uses_path(tmp_path: Path) -> None:
    """
    When the seen set is empty (no pin yet), the path check alone decides.
    """
    subagent_path = tmp_path / "session" / "subagents" / "agent-abc.jsonl"
    parent_path = tmp_path / "session.jsonl"

    # Subagent path → True (path check catches it).
    assert forwarder._is_subagent_hook_record(
        ClaudeHookRecord(
            event_cursor=1,
            byte_offset=50,
            event_name="StopFailure",
            claude_session_id="any",
            transcript_path=subagent_path,
        ),
        parent_claude_session_ids=set(),
    )
    # Non-subagent path → False (conservative).
    assert not forwarder._is_subagent_hook_record(
        ClaudeHookRecord(
            event_cursor=2,
            byte_offset=100,
            event_name="StopFailure",
            claude_session_id="any",
            transcript_path=parent_path,
        ),
        parent_claude_session_ids=set(),
    )
    # No path → False (conservative).
    assert not forwarder._is_subagent_hook_record(
        ClaudeHookRecord(
            event_cursor=3,
            byte_offset=150,
            event_name="StopFailure",
            claude_session_id="any",
            transcript_path=None,
        ),
        parent_claude_session_ids=set(),
    )


@pytest.mark.asyncio
async def test_forwarder_ignores_subagent_stop_hook(
    tmp_path: Path,
) -> None:
    """
    A subagent's ``Stop`` must not deliver the parent session as idle.

    Claude Code Task subagents inherit the parent's hook settings and write to
    the same ``hooks.jsonl``. A subagent finishing must NOT post ``idle`` for
    the parent — the parent turn is still running while it awaits the Agent
    tool result, and a parent ``idle`` triggers terminal sub-agent delivery.
    Subagent transcripts live under a ``subagents/`` directory, which the
    forwarder uses to skip them. We record a subagent ``Stop`` ahead of the
    parent ``Stop``: the one and only idle POST must be the parent's.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_transcript = tmp_path / "session" / "subagents" / "agent-abc.jsonl"
    subagent_transcript.parent.mkdir(parents=True, exist_ok=True)
    subagent_transcript.write_text("", encoding="utf-8")

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "parent-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Subagent stops first — this must NOT surface as the parent going idle.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "subagent-session",
            "transcript_path": str(subagent_transcript),
        },
    )
    # Parent turn ends — this SHOULD surface as the one idle edge.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "parent-session",
            "transcript_path": str(transcript_path),
        },
    )

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
        # Exactly one status POST: the parent's idle. The subagent ``Stop``
        # (recorded first) must be skipped, so no second status POST arrives —
        # the bounded wait below must time out.
        first = await _get_recorded_request(server)
        with pytest.raises(AssertionError):
            await _get_recorded_request(server, timeout_s=0.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert first["body"] == {
        "type": "external_session_status",
        "data": {"status": "idle", "background_task_count": 0, "turn_completed": True},
    }


@pytest.mark.parametrize("supports_idle", [True, False], ids=["new-server", "old-server"])
async def test_subagent_idle_observation_preserves_timing_and_deduplication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    supports_idle: bool,
) -> None:
    """The same five-second gap emits once, resets on activity, and survives restart."""
    now = 1000.0
    monkeypatch.setattr(
        forwarder, "time", SimpleNamespace(time=lambda: now, monotonic=time.monotonic)
    )
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.touch()
    child_path = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="idle-worker",
        agent_type="Explore",
        description="long-running task",
        tool_use_id="toolu_idle",
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "idle-worker": forwarder.SubagentEntry(
                subagent_id="idle-worker", child_conversation_id="conv_child"
            )
        }
    )
    status_events: list[dict[str, Any]] = []
    capability = forwarder._SubagentStatusCapability()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/conv_child/events"
        body = json.loads(request.content)
        if isinstance(body, list):
            return httpx.Response(202, json=[{"item_id": "item"} for _ in body])
        status_events.append(body)
        if body["type"] == "subagent.status" and not supports_idle:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "invalid_input",
                        "message": "Unknown event type: 'subagent.status'. Allowed types: []",
                    }
                },
            )
        return httpx.Response(202, json={"queued": False})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:

        async def tick() -> None:
            nonlocal state
            state = await forwarder._forward_available_subagents(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                agent_name="claude-native-ui",
                start_retry_tracker=forwarder._PostRetryTracker(),
                item_retry_tracker=forwarder._PostRetryTracker(),
                status_retry_tracker=forwarder._PostRetryTracker(),
                status_capability=capability,
            )

        await tick()
        assert status_events == []  # No idle observation before the first activity.
        for cycle in range(2):
            with child_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "isSidechain": True,
                            "type": "assistant",
                            "uuid": f"message-{cycle}",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": f"working {cycle}"}],
                            },
                        }
                    )
                    + "\n"
                )
            await tick()
            assert status_events[-1] == {
                "type": "external_session_status",
                "data": {"status": "running"},
            }
            now += forwarder._SUBAGENT_IDLE_THRESHOLD_S
            await tick()
            count = len(status_events)
            assert count == cycle * 2 + 1
            now += 0.001
            await tick()
            if supports_idle or cycle == 0:
                count += 1
                assert status_events[-1] == {"type": "subagent.status", "data": {"idle": True}}
            await tick()
            state = forwarder._read_subagent_forward_state(bridge_dir)
            await tick()
            assert len(status_events) == count


async def test_subagent_idle_unsupported_cache_covers_other_children(tmp_path: Path) -> None:
    """One old-server rejection suppresses other children's idle events, not failures."""
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.touch()
    bridge_dir = tmp_path / "bridge"
    capability = forwarder._SubagentStatusCapability()
    state = forwarder.SubagentForwardState(subagents={})
    events: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        events.append(body)
        if body["type"] == "subagent.status":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "invalid_input",
                        "message": "Unknown event type: 'subagent.status'. Allowed types: []",
                    }
                },
            )
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for child_id in ("first", "second", "failed"):
            _seed_subagent_on_disk(
                transcript_path=transcript_path,
                subagent_id=child_id,
                agent_type="Explore",
                description="worker",
                tool_use_id=f"toolu_{child_id}",
            )
            state.subagents[child_id] = forwarder.SubagentEntry(
                subagent_id=child_id,
                child_conversation_id=f"conv_{child_id}",
                last_activity_ts=time.time() - 60,
                last_status="running",
                delivery_error="lost output" if child_id == "failed" else None,
            )
            state = await forwarder._forward_available_subagents(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                agent_name="claude-native-ui",
                start_retry_tracker=forwarder._PostRetryTracker(),
                item_retry_tracker=forwarder._PostRetryTracker(),
                status_retry_tracker=forwarder._PostRetryTracker(),
                status_capability=capability,
            )
    assert events == [
        {"type": "subagent.status", "data": {"idle": True}},
        {"type": "external_session_status", "data": {"status": "failed", "output": "lost output"}},
    ]


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (400, {"error": {"code": "invalid_input", "message": "Invalid idle payload"}}),
        (400, {"error": {"code": "invalid_input", "message": "Unknown event type: 'other'."}}),
        (400, {"error": {"code": "invalid_input", "message": None}}),
        (400, ["malformed error"]),
        (400, "not json"),
        (401, {"error": {"code": "unauthorized"}}),
        (403, {"error": {"code": "forbidden"}}),
        (503, {"error": {"code": "unavailable"}}),
    ],
)
async def test_subagent_idle_other_errors_are_not_suppressed(status: int, payload: Any) -> None:
    """A malformed request, auth failure, or outage must still reach normal retry handling."""
    events: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(json.loads(request.content))
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)

    capability = forwarder._SubagentStatusCapability()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                await capability.post_idle(client, session_id="conv_child")
    assert events == [{"type": "subagent.status", "data": {"idle": True}}] * 2


@pytest.mark.parametrize("previous_status", ["running", "idle", "failed"])
async def test_subagent_idle_observation_retries_and_resumes_existing_checkpoint(
    tmp_path: Path, previous_status: str
) -> None:
    """Only successful delivery advances dedupe; an idle checkpoint stays deduped."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.touch()
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="idle-worker",
        agent_type="Explore",
        description="long-running task",
        tool_use_id="toolu_idle",
    )
    forwarder._write_subagent_forward_state(
        bridge_dir,
        forwarder.SubagentForwardState(
            subagents={
                "idle-worker": forwarder.SubagentEntry(
                    subagent_id="idle-worker",
                    child_conversation_id="conv_child",
                    last_activity_ts=time.time() - 60,
                    last_status=previous_status,
                )
            }
        ),
    )
    state = forwarder._read_subagent_forward_state(bridge_dir)
    attempts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(json.loads(request.content))
        return httpx.Response(503 if len(attempts) == 1 else 202, json={})

    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for tick in range(3):
            state = await forwarder._forward_available_subagents(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                agent_name="claude-native-ui",
                start_retry_tracker=forwarder._PostRetryTracker(),
                item_retry_tracker=forwarder._PostRetryTracker(),
                status_retry_tracker=tracker,
            )
            if tick == 0 and previous_status != "idle":
                assert state.subagents["idle-worker"].last_status == previous_status
                assert forwarder._read_subagent_forward_state(bridge_dir) == state

    assert attempts == (
        []
        if previous_status == "idle"
        else [{"type": "subagent.status", "data": {"idle": True}}] * 2
    )
    assert state.subagents["idle-worker"].last_status == "idle"


@pytest.mark.asyncio
async def test_persistent_subagent_502_ends_as_explicit_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A child that exhausts 502 retries fails with recoverable dead letters."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_id = "persistent-502"
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id=subagent_id,
        agent_type="Explore",
        description="persistent outage",
        tool_use_id="toolu_persistent_502",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "persistent-message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "lost output"}],
                },
            }
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            subagent_id: forwarder.SubagentEntry(
                subagent_id=subagent_id,
                child_conversation_id="conv_persistent_502",
            )
        }
    )
    statuses: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if isinstance(body, list):
            return httpx.Response(502, text="bad gateway")
        if body.get("type") == "external_session_status":
            statuses.append(body["data"])
        return httpx.Response(202, json={})

    tracker = forwarder._PostRetryTracker(
        base_delay_s=0.0,
        max_transient_attempts=2,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for _ in range(2):
            state = await forwarder._forward_available_subagents(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=state,
                agent_name="claude-native-ui",
                start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                item_retry_tracker=tracker,
                status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            )
        failed_entry = replace(
            state.subagents[subagent_id],
            last_activity_ts=time.time() - forwarder._SUBAGENT_IDLE_THRESHOLD_S - 60,
        )
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={subagent_id: failed_entry}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert statuses == [{"status": "failed", "output": forwarder._SUBAGENT_DROPPED_ITEM_REASON}]
    assert state.subagents[subagent_id].delivery_error == forwarder._SUBAGENT_DROPPED_ITEM_REASON
    persisted = forwarder._read_subagent_forward_state(bridge_dir)
    assert (
        persisted.subagents[subagent_id].delivery_error == forwarder._SUBAGENT_DROPPED_ITEM_REASON
    )
    dead_letters = (bridge_dir / "dead_letter.jsonl").read_text("utf-8").splitlines()
    assert len(dead_letters) == 1
    assert json.loads(dead_letters[0])["http_status"] == 502

    row = _subagent_drop_row(caplog)
    assert row["session_id"] == "conv_persistent_502"
    assert row["attributes"]["parent_session_id"] == "conv_parent"
    assert row["attributes"]["drop_reason"] == "transient_retries_exhausted"
    assert row["attributes"]["http_status"] == "502"
    assert row["attributes"]["attempts"] == "2"
    assert row["attributes"]["item_count"] == "1"
    assert row["attributes"]["exception_type"] == "HTTPStatusError"
    assert "lost output" not in json.dumps(row["attributes"])


@pytest.mark.asyncio
async def test_parent_output_forwards_while_child_history_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck child request cannot prevent the next live parent poll."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="blocked-child",
        agent_type="Explore",
        description="blocked history",
        tool_use_id="toolu_blocked_child",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "blocked-child-item",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "old output"}],
                },
            }
        ],
    )
    forwarder._write_subagent_forward_state(
        bridge_dir,
        forwarder.SubagentForwardState(
            subagents={
                "blocked-child": forwarder.SubagentEntry(
                    subagent_id="blocked-child",
                    child_conversation_id="conv_blocked_child",
                )
            }
        ),
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    child_request_started = asyncio.Event()
    release_child = asyncio.Event()
    parent_item_forwarded = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if isinstance(body, list):
            child_request_started.set()
            await release_child.wait()
            return httpx.Response(
                202,
                json=[
                    {"queued": False, "item_id": f"item-{index}"} for index, _ in enumerate(body)
                ],
            )
        if (
            request.url.path == "/v1/sessions/conv_parent/events"
            and isinstance(body, dict)
            and body.get("type") == "external_conversation_item"
        ):
            parent_item_forwarded.set()
        return httpx.Response(202, json={})

    @contextlib.asynccontextmanager
    async def open_mock_client(*_args: Any, **_kwargs: Any) -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://ap"
        ) as client:
            yield client

    monkeypatch.setattr("omnigent.cli_auth.open_server_client", open_mock_client)
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url="http://ap",
            headers={},
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        await asyncio.wait_for(child_request_started.wait(), timeout=2.0)
        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "fresh-parent-item",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "fresh output"}],
                        },
                    }
                )
                + "\n"
            )
        await asyncio.wait_for(parent_item_forwarded.wait(), timeout=1.0)
    finally:
        release_child.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

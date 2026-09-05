from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from omnigent import claude_native_bridge as bridge
from omnigent import claude_native_forwarder as forwarder


def _append(path: Path, record: dict[str, object], *, complete: bool = True) -> None:
    with path.open("ab") as handle:
        payload = json.dumps(record).encode("utf-8")
        handle.write(payload + (b"\n" if complete else b""))


def test_goal_snapshot_uses_latest_complete_structured_event(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _append(transcript, {"type": "active_goal", "value": {"title": "old"}})
    _append(transcript, {"type": "assistant", "message": {"role": "assistant"}})
    _append(transcript, {"type": "active_goal", "value": None})
    _append(transcript, {"type": "active_goal", "value": {"title": "partial"}}, complete=False)

    snapshot = bridge.read_latest_transcript_goal_state(transcript)

    assert snapshot.goal_state_observed is True
    assert snapshot.latest_goal_state is None
    assert snapshot.byte_offset < transcript.stat().st_size


@pytest.mark.asyncio
async def test_reattach_recovers_goal_without_replaying_or_moving_cursor(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _append(
        transcript,
        {"type": "user", "uuid": "old-message", "message": {"role": "user", "content": "old"}},
    )
    _append(transcript, {"type": "active_goal", "value": {"title": "ship"}})
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript,
        line_cursor=2,
        byte_offset=transcript.stat().st_size,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(
            transcript, transcript.stat().st_size
        ),
    )
    dedupe = forwarder._ForwardDedupeState()
    posted: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await forwarder._recover_goal_state_from_transcript(
            transcript_path=transcript,
            dedupe=dedupe,
        )
        updated = await forwarder._forward_available_items(
            client=client,
            session_id="conv",
            bridge_dir=tmp_path,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            dedupe=dedupe,
        )

    assert updated == state
    assert posted == [{"type": "external_goal_state", "data": {"state": "active"}}]


@pytest.mark.asyncio
async def test_failed_recovery_retries_and_new_incremental_clear_wins(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _append(transcript, {"type": "active_goal", "value": {"title": "ship"}})
    active_end = transcript.stat().st_size
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript,
        line_cursor=1,
        byte_offset=active_end,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript, active_end),
    )
    dedupe = forwarder._ForwardDedupeState()
    requests: list[dict[str, object]] = []
    request_states: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        data = body.get("data")
        assert isinstance(data, dict)
        state_value = data.get("state")
        assert state_value is None or isinstance(state_value, str)
        request_states.append(state_value)
        if len(requests) == 1:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(202, json={})

    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await forwarder._recover_goal_state_from_transcript(
            transcript_path=transcript,
            dedupe=dedupe,
        )
        first = await forwarder._forward_available_items(
            client=client,
            session_id="conv",
            bridge_dir=tmp_path,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=tracker,
            dedupe=dedupe,
        )
        _append(transcript, {"type": "active_goal", "value": None})
        second = await forwarder._forward_available_items(
            client=client,
            session_id="conv",
            bridge_dir=tmp_path,
            agent_name="claude-native-ui",
            state=first,
            retry_tracker=tracker,
            dedupe=dedupe,
        )
        await forwarder._forward_available_items(
            client=client,
            session_id="conv",
            bridge_dir=tmp_path,
            agent_name="claude-native-ui",
            state=second,
            retry_tracker=tracker,
            dedupe=dedupe,
        )

    assert request_states == ["active", None]


@pytest.mark.asyncio
async def test_failed_goal_post_retries_on_quiet_poll(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _append(transcript, {"type": "active_goal", "value": {"title": "ship"}})
    end = transcript.stat().st_size
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript,
        line_cursor=1,
        byte_offset=end,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript, end),
    )
    dedupe = forwarder._ForwardDedupeState()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(202, json={})

    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await forwarder._recover_goal_state_from_transcript(
            transcript_path=transcript,
            dedupe=dedupe,
        )
        state = await forwarder._forward_available_items(
            client=client,
            session_id="conv",
            bridge_dir=tmp_path,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=tracker,
            dedupe=dedupe,
        )
        await forwarder._forward_available_items(
            client=client,
            session_id="conv",
            bridge_dir=tmp_path,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=tracker,
            dedupe=dedupe,
        )

    assert attempts == 2
    assert dedupe.posted_goal_state_known is True
    assert dedupe.posted_goal_state == "active"


@pytest.mark.asyncio
async def test_goal_recovery_scans_once_until_file_generation_changes(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _append(transcript, {"type": "active_goal", "value": {"title": "ship"}})
    dedupe = forwarder._ForwardDedupeState()

    with patch(
        "omnigent.claude_native_forwarder.read_latest_transcript_goal_state",
        wraps=bridge.read_latest_transcript_goal_state,
    ) as scan:
        await forwarder._recover_goal_state_from_transcript(
            transcript_path=transcript,
            dedupe=dedupe,
        )
        await forwarder._recover_goal_state_from_transcript(
            transcript_path=transcript,
            dedupe=dedupe,
        )
        replacement = tmp_path / "replacement.jsonl"
        _append(replacement, {"type": "active_goal", "value": None})
        replacement.replace(transcript)
        await forwarder._recover_goal_state_from_transcript(
            transcript_path=transcript,
            dedupe=dedupe,
        )

    assert scan.call_count == 2
    assert dedupe.observed_goal_state_known is True
    assert dedupe.observed_goal_state is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_value,new_value", [(None, {"title": "new"}), ({"title": "old"}, None)]
)
async def test_new_goal_generation_drops_old_pending_post_and_offset(
    tmp_path: Path, old_value: dict[str, str] | None, new_value: dict[str, str] | None
) -> None:
    transcript = tmp_path / "session.jsonl"
    _append(transcript, {"type": "padding", "data": "x" * 4096})
    _append(transcript, {"type": "active_goal", "value": old_value})
    dedupe = forwarder._ForwardDedupeState()
    await forwarder._recover_goal_state_from_transcript(transcript_path=transcript, dedupe=dedupe)
    old_offset = dedupe.observed_goal_byte_offset
    replacement = tmp_path / "new.jsonl"
    _append(replacement, {"type": "metadata"})
    replacement.replace(transcript)
    await forwarder._recover_goal_state_from_transcript(transcript_path=transcript, dedupe=dedupe)
    assert dedupe.observed_goal_state_known is False
    assert dedupe.observed_goal_byte_offset == -1
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript, line_cursor=1, byte_offset=transcript.stat().st_size
    )
    posts: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://ap"
    ) as client:
        tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
        state = await forwarder._forward_available_items(
            client=client,
            session_id="conv",
            bridge_dir=tmp_path,
            agent_name="claude-native-ui",
            dedupe=dedupe,
            retry_tracker=tracker,
            state=state,
        )
        assert posts == []
        _append(transcript, {"type": "active_goal", "value": new_value})
        assert transcript.stat().st_size < old_offset
        await forwarder._forward_available_items(
            client=client,
            session_id="conv",
            bridge_dir=tmp_path,
            agent_name="claude-native-ui",
            dedupe=dedupe,
            retry_tracker=tracker,
            state=state,
        )
    expected = "active" if new_value is not None else None
    assert posts == [{"type": "external_goal_state", "data": {"state": expected}}]

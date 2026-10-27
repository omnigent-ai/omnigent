"""Exercise retained Claude context through snapshot persistence and cold resume."""

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.conversation import CompactionData
from omnigent.harnesses.claude_native import forwarder
from omnigent.harnesses.claude_native.bridge import (
    _HOOKS_FILE,
    ClaudeTranscriptItem,
    read_transcript_items_since,
    record_hook_event,
)
from omnigent.harnesses.claude_native.forwarder import (
    _CompactionSnapshotError,
    _forward_available_status_events,
    _handle_compact_summary_item,
    _persist_native_compaction_item,
    _PostRetryTracker,
)
from omnigent.harnesses.claude_native.main import (
    _claude_transcript_records_from_session_items,
)

_EXTERNAL_SESSION_ID = "00000000-0000-4000-8000-000000000001"


def _records(
    compacted_messages: list[dict[str, Any]],
    later_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return _claude_transcript_records_from_session_items(
        [
            {
                "id": "compaction",
                "type": "compaction",
                "token_count": 987,
                "compacted_messages": compacted_messages,
            }
        ]
        + (later_items or []),
        session_id="conversation-test",
        external_session_id=_EXTERNAL_SESSION_ID,
        cwd=Path("test-workdir"),
        bridge_dir=Path("test-bridge"),
    )


def test_native_string_summary_content_survives_resume() -> None:
    records = _records(
        [
            {
                "type": "message",
                "role": "user",
                "content": "summary retained across cold resume",
            }
        ]
    )

    boundary, summary = records
    assert summary["parentUuid"] == boundary["uuid"]
    assert summary["message"]["content"] == "summary retained across cold resume"


def test_structured_user_message_is_not_promoted_to_summary() -> None:
    records = _records(
        [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "ordinary prompt"}],
            }
        ]
    )

    user_record = records[1]
    assert "isCompactSummary" not in user_record
    assert "isVisibleInTranscriptOnly" not in user_record


def _transcript(*, segment_only: bool = False) -> list[dict[str, Any]]:
    metadata: dict[str, Any] = {
        "trigger": "manual",
        "preservedSegment": {
            "headUuid": "request",
            "tailUuid": "result",
            "anchorUuid": "summary",
        },
    }
    if not segment_only:
        metadata["preservedMessages"] = {
            "anchorUuid": "summary",
            "uuids": ["request", "call", "result"],
        }
    return [
        _claude_record("user", "old", None, "older context already summarized"),
        _claude_record("user", "request", "old", "continue the retained request"),
        _claude_record(
            "assistant",
            "call",
            "request",
            [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "Read",
                    "input": {"file_path": "test.txt"},
                }
            ],
        ),
        _claude_record(
            "user",
            "result",
            "call",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "retained tool result",
                }
            ],
        ),
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "parentUuid": None,
            "compactMetadata": metadata,
        },
        {
            **_claude_record("user", "summary", "boundary", "summary of older context"),
            "isCompactSummary": True,
            "isVisibleInTranscriptOnly": True,
        },
    ]


def _second_compaction() -> list[dict[str, Any]]:
    """A later turn and the compaction that retained it, appended after ``_transcript()``."""
    return [
        {
            "type": "user",
            "uuid": "recent",
            "parentUuid": "summary",
            "message": {"role": "user", "content": "new retained request"},
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary-2",
            "parentUuid": None,
            "compactMetadata": {
                "preservedMessages": {
                    "anchorUuid": "summary-2",
                    "uuids": ["recent"],
                }
            },
        },
        {
            "type": "user",
            "uuid": "summary-2",
            "parentUuid": "boundary-2",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "second summary"},
        },
    ]


def _claude_timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any]]
) -> tuple[Path, Path]:
    config = tmp_path / "claude"
    project = config / "projects" / "local-test"
    project.mkdir(parents=True)
    transcript = project / f"{_EXTERNAL_SESSION_ID}.jsonl"
    transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "state.json").write_text(
        json.dumps(
            {
                "claude_session_id": _EXTERNAL_SESSION_ID,
                "transcript_path": str(transcript),
            }
        ),
        encoding="utf-8",
    )
    return bridge, transcript


def _persist(
    bridge: Path,
    posted: list[dict[str, Any]],
    *,
    summary_override: str | None = None,
    summary_source_id: str | None = None,
    summary_not_before: float | None = None,
    allow_snapshot_fallback: bool = False,
) -> None:
    # Only the external HTTP service is replaced; the SDK and production
    # snapshot/resume paths read the fixture from disk.
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "last-item"}]})
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={})

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="https://fixture.invalid"
        ) as client:
            await _persist_native_compaction_item(
                client,
                session_id="conversation-test",
                bridge_dir=bridge,
                summary_override=summary_override,
                summary_source_id=summary_source_id,
                summary_not_before=summary_not_before,
                allow_snapshot_fallback=allow_snapshot_fallback,
            )

    asyncio.run(run())


@pytest.mark.parametrize("segment_only", [False, True])
@pytest.mark.parametrize("continuation_parent", [None, "summary", "result"])
def test_retained_turn_and_tool_pair_survive_save_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    segment_only: bool,
    continuation_parent: str | None,
) -> None:
    entries = _transcript(segment_only=segment_only)
    if continuation_parent:
        entries.append(
            {
                "type": "user",
                "uuid": "next",
                "parentUuid": continuation_parent,
                "message": {"role": "user", "content": "post-compaction request"},
            }
        )
    bridge, _ = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []
    _persist(bridge, posted)
    messages = posted[0]["data"]["compacted_messages"]
    by_uuid = {
        entry["uuid"]: entry["message"]["content"] for entry in entries if "message" in entry
    }
    expected = [by_uuid[uid] for uid in ("summary", "request", "call", "result")]
    if continuation_parent:
        expected.append("post-compaction request")
    assert [message["content"] for message in messages] == expected
    later_items = []
    if continuation_parent:
        later_items.append(
            {
                "id": uuid.uuid5(
                    uuid.NAMESPACE_URL, "omnigent-external-item:conversation-test:next:0:message"
                ).hex,
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "post-compaction request"}],
            }
        )
    records = _records(messages, later_items)
    assert [record["message"]["content"] for record in records[1:]] == expected
    assert all(record["parentUuid"] == parent["uuid"] for parent, record in pairwise(records))


@pytest.mark.parametrize(
    "summary_source_id", [None, "summary:0:compact_summary"], ids=["hook", "transcript"]
)
def test_attachment_chain_preserves_compaction_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_source_id: str | None,
) -> None:
    entries = _transcript()
    summary = entries.pop()
    parent_uuid = "boundary"
    for attachment_type in ("instructions", "session_context", "date"):
        attachment_uuid = f"attachment-{attachment_type}"
        entries.append(
            {
                "type": "attachment",
                "uuid": attachment_uuid,
                "parentUuid": parent_uuid,
                "attachment": {"type": attachment_type},
            }
        )
        parent_uuid = attachment_uuid
    summary["parentUuid"] = parent_uuid
    entries.append(summary)
    bridge, _ = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []

    _persist(bridge, posted, summary_source_id=summary_source_id)

    assert len(posted) == 1
    assert posted[0]["type"] == "compaction"
    messages = posted[0]["data"]["compacted_messages"]
    assert [message["content"] for message in messages] == [
        summary["message"]["content"],
        entries[1]["message"]["content"],
        entries[2]["message"]["content"],
        entries[3]["message"]["content"],
    ]
    resumed = _records(messages)
    assert [record["message"]["content"] for record in resumed[1:]] == [
        message["content"] for message in messages
    ]
    assert all(record["parentUuid"] == parent["uuid"] for parent, record in pairwise(resumed))


@pytest.mark.parametrize(
    "invalid_ancestor",
    [
        None,
        {"type": "attachment", "parentUuid": "attachment"},
        {"type": "attachment", "parentUuid": 123},
        {"type": "user", "parentUuid": "boundary"},
        {"type": "system", "subtype": "other", "parentUuid": "boundary"},
    ],
    ids=["missing", "cycle", "invalid-parent", "message", "other-system"],
)
def test_summary_boundary_rejects_invalid_attachment_chain(
    invalid_ancestor: dict[str, Any] | None,
) -> None:
    records = {entry["uuid"]: entry for entry in _transcript()}
    records["summary"]["parentUuid"] = "attachment"
    if invalid_ancestor is not None:
        records["attachment"] = {"uuid": "attachment", **invalid_ancestor}

    assert forwarder._compaction_summary_boundary(records, records["summary"]) is None


def test_hook_snapshot_rejects_attachment_chain_to_an_older_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _transcript()
    entries.insert(0, {"uuid": "older-boundary", "type": "system", "subtype": "compact_boundary"})
    entries[-1]["parentUuid"] = "attachment"
    entries.append({"uuid": "attachment", "type": "attachment", "parentUuid": "older-boundary"})
    _, transcript = _write_transcript(tmp_path, monkeypatch, entries)

    with pytest.raises(ValueError, match="Claude compaction summary is not available yet"):
        forwarder._latest_compact_summary_uuid(transcript)


@pytest.mark.parametrize("missing", ["request", "summary"])
def test_incomplete_snapshot_is_not_persisted_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    entries = _transcript()
    bridge, transcript = _write_transcript(
        tmp_path, monkeypatch, [entry for entry in entries if entry["uuid"] != missing]
    )
    posted: list[dict[str, Any]] = []
    with pytest.raises(
        _CompactionSnapshotError, match="Failed to read Claude compaction snapshot"
    ):
        _persist(bridge, posted)
    assert posted == []
    transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    _persist(bridge, posted)
    assert len(posted[0]["data"]["compacted_messages"]) == 4


def test_hook_completion_before_summary_is_written_is_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SessionStart source=compact can be processed before Claude writes the
    # compact_boundary/isCompactSummary records, so the transcript still holds
    # only the pre-compaction turns.
    entries = [
        entry
        for entry in _transcript()
        if entry.get("subtype") != "compact_boundary" and not entry.get("isCompactSummary")
    ]
    bridge, _ = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []

    with pytest.raises(_CompactionSnapshotError):
        _persist(bridge, posted)

    assert posted == []


def test_hook_completion_waits_for_the_summary_of_a_boundary_without_retained_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A boundary without preserved-message metadata keeps the SDK messages as
    # the snapshot; until its summary record is on disk those are still the
    # pre-compaction turns.
    entries = _transcript()
    next(entry for entry in entries if entry["uuid"] == "boundary")["compactMetadata"] = {
        "trigger": "manual"
    }
    summary = entries.pop()
    bridge, transcript = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []

    with pytest.raises(_CompactionSnapshotError):
        _persist(bridge, posted)
    assert posted == []

    entries.append(summary)
    transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    _persist(bridge, posted)

    assert [message["content"] for message in posted[0]["data"]["compacted_messages"]] == [
        "summary of older context"
    ]


@pytest.mark.parametrize("stale_timestamp", ["2026-05-27T22:53:13.245Z", None])
def test_hook_completion_ignores_a_boundary_written_before_its_precompact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_timestamp: str | None
) -> None:
    precompact_seen_at = datetime(2026, 5, 27, 22, 53, 20, tzinfo=UTC)
    entries = _transcript()
    if stale_timestamp is not None:
        next(entry for entry in entries if entry["uuid"] == "boundary")["timestamp"] = (
            stale_timestamp
        )
    entries.append(_second_compaction()[0])
    bridge, transcript = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []

    # The only boundary on disk belongs to an earlier compaction (or cannot be
    # dated), so the hook path must wait rather than snapshot everything since it.
    with pytest.raises(_CompactionSnapshotError):
        _persist(bridge, posted, summary_not_before=precompact_seen_at.timestamp())
    assert posted == []

    recent, boundary, summary = _second_compaction()
    boundary["timestamp"] = _claude_timestamp(precompact_seen_at + timedelta(seconds=2))
    entries.extend([boundary, summary])
    transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    _persist(bridge, posted, summary_not_before=precompact_seen_at.timestamp())

    assert [message["content"] for message in posted[0]["data"]["compacted_messages"]] == [
        "second summary",
        recent["message"]["content"],
    ]


async def _poll_completion_hooks(
    bridge: Path,
    posted: list[dict[str, Any]],
    hook_state: forwarder.HookForwardState,
    tracker: _PostRetryTracker,
) -> forwarder.HookForwardState:
    """Run one hook-forwarder poll against a recording Omnigent stub."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "last-item"}]})
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="https://fixture.invalid"
    ) as client:
        return await _forward_available_status_events(
            client=client,
            session_id="conversation-test",
            bridge_dir=bridge,
            state=hook_state,
            retry_tracker=tracker,
            compaction_retry_tracker=tracker,
            dedupe=forwarder._ForwardDedupeState(),
            task_subjects={},
            task_statuses={},
            task_order=[],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token", ["precompact", "undated_precompact", "none", "legacy_standalone"]
)
async def test_completion_hook_binds_to_the_boundary_of_its_own_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
    entries = _transcript()
    next(entry for entry in entries if entry["uuid"] == "boundary")["timestamp"] = (
        "2026-05-27T22:53:13.245Z"
    )
    recent, boundary, summary = _second_compaction()
    entries.append(recent)
    bridge, transcript = _write_transcript(tmp_path, monkeypatch, entries)
    if token in ("precompact", "undated_precompact"):
        await forwarder._note_precompact(
            bridge,
            claude_session_id=_EXTERNAL_SESSION_ID,
            transcript_path=str(transcript),
            recorded_at=time.time() if token == "precompact" else None,
        )
    elif token == "legacy_standalone":
        # Older forwarders stamped standalone tokens with the poll time, which
        # postdates the boundary they complete.
        forwarder._write_compaction_state(
            bridge,
            forwarder.CompactionForwardState(
                pending=forwarder._PendingCompaction(seq=1, seen_at=time.time()),
                last_seq=1,
            ),
        )
    record_hook_event(
        bridge,
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": _EXTERNAL_SESSION_ID,
            "transcript_path": str(transcript),
        },
    )
    posted: list[dict[str, Any]] = []
    tracker = _PostRetryTracker(base_delay_s=0, max_delay_s=0)

    hook_state = await _poll_completion_hooks(
        bridge, posted, forwarder.HookForwardState(event_cursor=0, byte_offset=0), tracker
    )
    boundaries = [event for event in posted if event["type"] == "compaction"]
    if token == "precompact":
        # The only boundary on disk predates the PreCompact: hold the hook
        # until Claude writes this compaction's boundary and summary.
        assert hook_state.event_cursor == 0
        assert boundaries == []
        boundary["timestamp"] = _claude_timestamp(datetime.now(UTC) + timedelta(seconds=5))
        entries.extend([boundary, summary])
        transcript.write_text(
            "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
        )
        hook_state = await _poll_completion_hooks(bridge, posted, hook_state, tracker)
        boundaries = [event for event in posted if event["type"] == "compaction"]
        expected = ["second summary", recent["message"]["content"]]
    else:
        # Undated/standalone tokens (even legacy poll-time stamps) have no lower
        # bound: bind to the latest boundary and keep its retained messages.
        expected = [
            "summary of older context",
            "continue the retained request",
            entries[2]["message"]["content"],
            entries[3]["message"]["content"],
            recent["message"]["content"],
        ]

    assert hook_state.event_cursor > 0
    assert len(boundaries) == 1
    assert [message["content"] for message in boundaries[0]["data"]["compacted_messages"]] == (
        expected
    )
    state = forwarder._read_compaction_state(bridge)
    assert state.persisted_seqs == (1,)
    assert state.pending is None


@pytest.mark.asyncio
async def test_completion_hook_dates_the_boundary_from_when_precompact_fired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
    fired_at = datetime.now(UTC) - timedelta(minutes=5)
    entries = _transcript()
    next(entry for entry in entries if entry["uuid"] == "boundary")["timestamp"] = (
        _claude_timestamp(fired_at + timedelta(seconds=30))
    )
    bridge, transcript = _write_transcript(tmp_path, monkeypatch, entries)
    # The forwarder polls this PreCompact only now, in the same batch as the
    # completion hook, after Claude already wrote the boundary.
    with (bridge / _HOOKS_FILE).open("a", encoding="utf-8") as hooks:
        hooks.write(
            json.dumps(
                {
                    "recorded_at": fired_at.timestamp(),
                    "payload": {
                        "hook_event_name": "PreCompact",
                        "session_id": _EXTERNAL_SESSION_ID,
                        "transcript_path": str(transcript),
                    },
                }
            )
            + "\n"
        )
    record_hook_event(
        bridge,
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": _EXTERNAL_SESSION_ID,
            "transcript_path": str(transcript),
        },
    )
    posted: list[dict[str, Any]] = []

    hook_state = await _poll_completion_hooks(
        bridge,
        posted,
        forwarder.HookForwardState(event_cursor=0, byte_offset=0),
        _PostRetryTracker(base_delay_s=0, max_delay_s=0),
    )

    assert hook_state.event_cursor > 0
    boundaries = [event for event in posted if event["type"] == "compaction"]
    assert [message["content"] for message in boundaries[0]["data"]["compacted_messages"]] == [
        entries[i]["message"]["content"] for i in (5, 1, 2, 3)
    ]
    assert forwarder._read_compaction_state(bridge).persisted_seqs == (1,)


@pytest.mark.parametrize("empty_content", [[], ""])
def test_valid_empty_retained_content_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty_content: Any
) -> None:
    entries = _transcript()
    next(entry for entry in entries if entry["uuid"] == "request")["message"]["content"] = (
        empty_content
    )
    bridge, _ = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []

    _persist(bridge, posted)

    assert len(posted[0]["data"]["compacted_messages"]) == 3
    assert all(message["content"] for message in posted[0]["data"]["compacted_messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_path", ["transcript", "hook", "alternating"])
async def test_permanently_missing_uuid_persists_sdk_fallback_and_unblocks_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion_path: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
    entries = [entry for entry in _transcript() if entry["uuid"] != "request"]
    bridge, transcript = _write_transcript(tmp_path, monkeypatch, entries)
    monkeypatch.setattr(forwarder, "_COMPACTION_SNAPSHOT_MAX_FAILURES", 3)
    await forwarder._note_precompact(
        bridge, claude_session_id=_EXTERNAL_SESSION_ID, transcript_path=str(transcript)
    )
    record_hook_event(
        bridge,
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": _EXTERNAL_SESSION_ID,
            "transcript_path": str(transcript),
        },
    )
    record_hook_event(
        bridge,
        {
            "hook_event_name": "TaskCreated",
            "session_id": _EXTERNAL_SESSION_ID,
            "task_id": "followup",
            "task_subject": "Forward after compaction",
        },
    )
    posted: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "last-item"}]})
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={})

    snapshot_tracker = _PostRetryTracker(base_delay_s=0, max_delay_s=0)
    transcript_tracker = _PostRetryTracker(base_delay_s=0, max_delay_s=0)
    hook_tracker = _PostRetryTracker(base_delay_s=0, max_delay_s=0)
    hook_state = forwarder.HookForwardState(event_cursor=0, byte_offset=0)
    summary_item = ClaudeTranscriptItem(
        source_id="summary:0:compact_summary",
        item_type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "summary"}]},
        response_id=None,
        is_compact_summary=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="https://fixture.invalid"
    ) as client:

        async def poll_hooks() -> forwarder.HookForwardState:
            return await _forward_available_status_events(
                client=client,
                session_id="conversation-test",
                bridge_dir=bridge,
                state=hook_state,
                retry_tracker=hook_tracker,
                compaction_retry_tracker=snapshot_tracker,
                dedupe=forwarder._ForwardDedupeState(),
                task_subjects={},
                task_statuses={},
                task_order=[],
            )

        for attempt in range(3):
            if completion_path == "transcript" or (
                completion_path == "alternating" and attempt % 2 == 0
            ):
                handled = await _handle_compact_summary_item(
                    client,
                    session_id="conversation-test",
                    bridge_dir=bridge,
                    item=summary_item,
                    retry_tracker=transcript_tracker,
                    compaction_retry_tracker=snapshot_tracker,
                )
                assert handled is (attempt == 2)
            else:
                hook_state = await poll_hooks()
                assert (hook_state.event_cursor > 0) is (attempt == 2)
            boundaries = [event for event in posted if event["type"] == "compaction"]
            assert len(boundaries) == (1 if attempt == 2 else 0)
        hook_state = await poll_hooks()
        assert hook_state.event_cursor > 0

    assert forwarder._read_compaction_state(bridge).persisted_seqs == (1,)
    assert forwarder._read_compaction_state(bridge).pending is None
    boundaries = [event for event in posted if event["type"] == "compaction"]
    assert len(boundaries) == 1
    assert [message["content"] for message in boundaries[0]["data"]["compacted_messages"]] == [
        "summary of older context"
    ]
    assert any(event["type"] == "external_session_todos" for event in posted)
    assert [
        event["data"]["status"]
        for event in posted
        if event["type"] == "external_compaction_status"
    ] == ["completed"]
    assert not snapshot_tracker.has_retry_state("compaction-snapshot:1")
    [fallback_log] = [
        record for record in caplog.records if "snapshot retry budget exhausted" in record.message
    ]
    assert fallback_log.exc_info is not None
    assert str(fallback_log.exc_info[1]) == "Claude retained message is not available yet"


@pytest.mark.parametrize("failure", ["missing_file", "invalid_utf8", "sdk_error", "mismatch"])
def test_snapshot_read_failure_can_persist_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from claude_agent_sdk import get_session_messages

    bridge, transcript = _write_transcript(tmp_path, monkeypatch, _transcript())
    sdk_messages = get_session_messages(_EXTERNAL_SESSION_ID)
    monkeypatch.setattr("claude_agent_sdk.get_session_messages", lambda _session_id: sdk_messages)
    if failure == "missing_file":
        transcript.unlink()
    elif failure == "invalid_utf8":
        transcript.write_bytes(b"\xff\n")
    elif failure == "sdk_error":

        def fail_reader(_session_id: str) -> list[object]:
            raise OSError("SDK read failed")

        monkeypatch.setattr("claude_agent_sdk.get_session_messages", fail_reader)
    else:
        sdk_messages[0].uuid = "different-summary"
    posted: list[dict[str, Any]] = []
    _persist(bridge, posted, allow_snapshot_fallback=True)
    assert len(posted) == 1
    if failure == "sdk_error":
        assert "compacted_messages" not in posted[0]["data"]
    else:
        assert [message["content"] for message in posted[0]["data"]["compacted_messages"]] == [
            "summary of older context"
        ]


def test_snapshot_overlap_deduplicates_tool_items_but_preserves_repeated_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _transcript()
    bridge, transcript = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []
    _persist(bridge, posted)
    snapshot = CompactionData(**posted[0]["data"]).compacted_messages
    assert snapshot is not None
    _, _, forwarded = read_transcript_items_since(
        transcript, start_line=0, agent_name="claude-native"
    )
    later_items = [
        {
            "id": uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"omnigent-external-item:conversation-test:{item.source_id}",
            ).hex,
            "type": item.item_type,
            **item.data,
        }
        for item in forwarded
        if item.source_id.split(":")[0] in ("request", "call", "result")
    ]
    later_items.append(
        {
            "id": "genuinely-repeated-prompt",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "continue the retained request"}],
        }
    )
    records = _records(snapshot, later_items)
    contents = [record["message"]["content"] for record in records[1:]]
    assert len(contents) == 5
    assert contents[-1] == "continue the retained request"
    assert (
        sum(
            block.get("type") == "tool_use"
            for content in contents
            if isinstance(content, list)
            for block in content
        )
        == 1
    )
    assert (
        sum(
            block.get("type") == "tool_result"
            for content in contents
            if isinstance(content, list)
            for block in content
        )
        == 1
    )


def test_malformed_retained_content_is_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _transcript()
    entries[1]["message"]["content"] = {"unexpected": "content"}
    bridge, _ = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []

    with pytest.raises(_CompactionSnapshotError):
        _persist(bridge, posted)

    assert posted == []


def test_invalid_preserved_messages_falls_back_to_valid_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _transcript()
    entries[4]["compactMetadata"]["preservedMessages"]["uuids"] = ["request", 7]
    bridge, _ = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []

    _persist(bridge, posted)

    assert [message["content"] for message in posted[0]["data"]["compacted_messages"]] == [
        "summary of older context",
        "continue the retained request",
        entries[2]["message"]["content"],
        entries[3]["message"]["content"],
    ]


def test_empty_preserved_messages_falls_back_to_valid_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _transcript()
    entries[4]["compactMetadata"]["preservedMessages"] = {}
    bridge, _ = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []

    _persist(bridge, posted)

    assert [message["content"] for message in posted[0]["data"]["compacted_messages"]] == [
        entries[i]["message"]["content"] for i in (5, 1, 2, 3)
    ]


@pytest.mark.parametrize(
    "preserved_messages",
    [{}, {"anchorUuid": "summary", "uuids": ["request", 7]}, {"anchorUuid": 7}],
)
def test_invalid_preserved_metadata_is_not_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preserved_messages: dict[str, Any],
) -> None:
    entries = _transcript()
    entries[4]["compactMetadata"] = {"preservedMessages": preserved_messages}
    bridge, _ = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []

    with pytest.raises(_CompactionSnapshotError):
        _persist(bridge, posted)

    assert posted == []


def test_empty_sdk_snapshot_retries_without_an_initial_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, transcript = _write_transcript(tmp_path, monkeypatch, [])
    posted: list[dict[str, Any]] = []

    with pytest.raises(_CompactionSnapshotError):
        _persist(bridge, posted)
    assert posted == []

    transcript.write_text(
        "".join(json.dumps(entry) + "\n" for entry in _transcript()), encoding="utf-8"
    )
    _persist(bridge, posted)

    assert posted[0]["type"] == "compaction"


def test_sdk_read_error_is_snapshot_failure_without_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _ = _write_transcript(tmp_path, monkeypatch, _transcript())

    def fail_reader(_session_id: str) -> list[object]:
        raise httpx.ReadError("session reader failed")

    monkeypatch.setattr("claude_agent_sdk.get_session_messages", fail_reader)
    posted: list[dict[str, Any]] = []

    with pytest.raises(_CompactionSnapshotError) as exc_info:
        _persist(bridge, posted)

    assert isinstance(exc_info.value.__cause__, httpx.ReadError)
    assert posted == []


def test_missing_sdk_snapshot_is_not_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, transcript = _write_transcript(tmp_path, monkeypatch, _transcript())
    transcript.unlink()
    posted: list[dict[str, Any]] = []

    with pytest.raises(_CompactionSnapshotError):
        _persist(bridge, posted)

    assert posted == []


def test_transcript_snapshot_is_bound_to_compact_summary_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _transcript() + _second_compaction()
    bridge, _ = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []

    with pytest.raises(_CompactionSnapshotError):
        _persist(
            bridge,
            posted,
            summary_override="first summary",
            summary_source_id="summary:0:compact_summary",
        )

    assert posted == []


def test_latest_compaction_replaces_older_retained_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _transcript() + _second_compaction()
    bridge, _ = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []
    _persist(bridge, posted)
    assert [message["content"] for message in posted[0]["data"]["compacted_messages"]] == [
        "second summary",
        "new retained request",
    ]


def test_snapshot_log_reports_restored_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bridge, _ = _write_transcript(tmp_path, monkeypatch, _transcript())
    with caplog.at_level(logging.INFO, logger="omnigent.harnesses.claude_native.forwarder"):
        _persist(bridge, [])
    (record,) = [r for r in caplog.records if "compaction snapshot" in r.getMessage()]
    message = record.getMessage()
    assert "sdk_messages=1 snapshot_messages=4 transcript_found=True" in message
    assert "retained" not in message


def test_summary_without_retained_metadata_keeps_sdk_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _transcript()
    next(entry for entry in entries if entry["uuid"] == "boundary")["compactMetadata"] = {
        "trigger": "manual"
    }
    bridge, _ = _write_transcript(tmp_path, monkeypatch, entries)
    posted: list[dict[str, Any]] = []
    _persist(bridge, posted)
    assert [message["content"] for message in posted[0]["data"]["compacted_messages"]] == [
        "summary of older context"
    ]


def test_later_native_string_is_not_promoted_to_summary() -> None:
    records = _records(
        [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "leading assistant"}],
            },
            {
                "type": "message",
                "role": "user",
                "content": "ordinary later user prompt",
            },
        ]
    )

    user_record = records[2]
    assert "isCompactSummary" not in user_record
    assert "isVisibleInTranscriptOnly" not in user_record


_SERVER_TOOL_CALL_ID = "srvtoolu_01retained"


def _server_tool_transcript(
    tool_name: str, result_type: str, result_content: Any
) -> list[dict[str, Any]]:
    # Claude Code writes one record per content block; blocks of one API
    # response share message.id, which is how it rejoins them on resume.
    return [
        _claude_record("user", "old", None, "older context already summarized"),
        _claude_record("user", "request", "old", "ask the advisor about the retry policy"),
        _claude_record(
            "assistant", "asking", "request", [{"type": "text", "text": "Asking."}], "msg_asking"
        ),
        _claude_record(
            "assistant",
            "call",
            "asking",
            [
                {
                    "type": "server_tool_use",
                    "id": _SERVER_TOOL_CALL_ID,
                    "name": tool_name,
                    "input": {"query": "retry policy"},
                }
            ],
            "msg_server_tool",
        ),
        _claude_record(
            "assistant",
            "result",
            "call",
            [
                {
                    "type": result_type,
                    "tool_use_id": _SERVER_TOOL_CALL_ID,
                    "content": result_content,
                }
            ],
            "msg_server_tool",
        ),
        _claude_record(
            "assistant", "done", "result", [{"type": "text", "text": "Consulted."}], "msg_done"
        ),
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "parentUuid": None,
            "compactMetadata": {
                "trigger": "manual",
                "preservedMessages": {
                    "anchorUuid": "summary",
                    "uuids": ["request", "asking", "call", "result", "done"],
                },
            },
        },
        {
            **_claude_record("user", "summary", "boundary", "summary of older context"),
            "isCompactSummary": True,
            "isVisibleInTranscriptOnly": True,
        },
    ]


def _records_holding_server_tool_pair(
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    call_record = result_record = None
    for record in records:
        content = record.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "server_tool_use" and block.get("id") == _SERVER_TOOL_CALL_ID:
                call_record = record
            elif block.get("tool_use_id") == _SERVER_TOOL_CALL_ID:
                result_record = record
    return call_record, result_record


@pytest.mark.parametrize(
    ("tool_name", "result_type", "result_content"),
    [
        (
            "advisor",
            "advisor_tool_result",
            {"type": "advisor_result", "content": [{"type": "text", "text": "Use backoff."}]},
        ),
        (
            "web_search",
            "web_search_tool_result",
            [
                {
                    "type": "web_search_result",
                    "url": "https://example.test/retries",
                    "title": "Retries",
                }
            ],
        ),
    ],
)
def test_server_tool_pair_stays_rejoinable_across_save_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    result_type: str,
    result_content: Any,
) -> None:
    bridge, _ = _write_transcript(
        tmp_path, monkeypatch, _server_tool_transcript(tool_name, result_type, result_content)
    )
    posted: list[dict[str, Any]] = []
    _persist(bridge, posted)

    records = _records(posted[0]["data"]["compacted_messages"])

    call_record, result_record = _records_holding_server_tool_pair(records)
    assert call_record is not None and result_record is not None
    # A server_tool_use whose result is not in the same resumed message is replayed
    # as "[Tool use interrupted]" and the orphaned result is rejected by the API.
    call_message_id = call_record["message"].get("id")
    assert call_record is result_record or (
        call_message_id is not None and call_message_id == result_record["message"].get("id")
    ), (
        f"{tool_name} server_tool_use and {result_type} were resumed as separate records "
        f"with message ids {call_message_id!r} / {result_record['message'].get('id')!r}"
    )


_ADVISOR_RESULT = {"type": "advisor_result", "content": [{"type": "text", "text": "Use backoff."}]}


def _claude_record(
    kind: str, uid: str, parent: str | None, content: Any, message_id: str | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": kind, "content": content}
    if message_id is not None:
        message["id"] = message_id
    return {
        "type": kind,
        "uuid": uid,
        "parentUuid": parent,
        "sessionId": _EXTERNAL_SESSION_ID,
        "message": message,
    }


def test_snapshot_keeps_claude_message_ids_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _ = _write_transcript(
        tmp_path,
        monkeypatch,
        _server_tool_transcript("advisor", "advisor_tool_result", _ADVISOR_RESULT),
    )
    posted: list[dict[str, Any]] = []
    _persist(bridge, posted)

    snapshot = CompactionData(**posted[0]["data"]).compacted_messages
    assert snapshot is not None
    assert [message.get("message_id") for message in snapshot] == [
        None,
        None,
        "msg_asking",
        "msg_server_tool",
        "msg_server_tool",
        "msg_done",
    ]
    records = _records(snapshot)
    assert [record["message"].get("id") for record in records[1:]] == [
        None,
        None,
        "msg_asking",
        "msg_server_tool",
        "msg_done",
    ]


def test_sdk_fallback_snapshot_keeps_claude_message_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_agent_sdk import get_session_messages

    call = {"type": "server_tool_use", "id": "srvtoolu_02", "name": "advisor", "input": {}}
    result = {
        "type": "advisor_tool_result",
        "tool_use_id": "srvtoolu_02",
        "content": _ADVISOR_RESULT,
    }
    # Only the summary's chain reaches the SDK reader; a post-compaction turn
    # gives that fallback snapshot an advisor pair to keep ids for.
    entries = [
        *_server_tool_transcript("advisor", "advisor_tool_result", _ADVISOR_RESULT),
        _claude_record("user", "next", "summary", "ask again"),
        _claude_record("assistant", "call-2", "next", [call], "msg_post"),
        _claude_record("assistant", "result-2", "call-2", [result], "msg_post"),
    ]
    bridge, transcript = _write_transcript(tmp_path, monkeypatch, entries)
    sdk_messages = get_session_messages(_EXTERNAL_SESSION_ID)
    monkeypatch.setattr("claude_agent_sdk.get_session_messages", lambda _session_id: sdk_messages)
    transcript.unlink()
    posted: list[dict[str, Any]] = []

    _persist(bridge, posted, allow_snapshot_fallback=True)

    snapshot = posted[0]["data"]["compacted_messages"]
    assert [message.get("message_id") for message in snapshot] == [
        None,
        None,
        "msg_post",
        "msg_post",
    ]
    assert [record["message"]["content"] for record in _records(snapshot)[1:]] == [
        "summary of older context",
        "ask again",
        [call, result],
    ]


def _legacy_assistant_entry(content: list[dict[str, Any]], *source_ids: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "message", "role": "assistant", "content": content}
    if source_ids:
        entry["forwarded_source_ids"] = list(source_ids)
    return entry


def _later_assistant_item(source_id: str, text: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid5(
            uuid.NAMESPACE_URL, f"omnigent-external-item:conversation-test:{source_id}"
        ).hex,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


@pytest.mark.parametrize(
    ("call_type", "result_type"),
    [("server_tool_use", "advisor_tool_result"), ("mcp_tool_use", "mcp_tool_result")],
)
def test_legacy_snapshot_rejoins_split_server_tool_pair_on_resume(
    call_type: str, result_type: str
) -> None:
    call = {"type": call_type, "id": _SERVER_TOOL_CALL_ID, "name": "advisor", "input": {}}
    note = {"type": "text", "text": "Checking."}
    result = {"type": result_type, "tool_use_id": _SERVER_TOOL_CALL_ID, "content": _ADVISOR_RESULT}

    records = _records(
        [
            {"type": "message", "role": "user", "content": "summary of older context"},
            _legacy_assistant_entry([{"type": "text", "text": "Asking."}], "asking:0:message"),
            _legacy_assistant_entry([call]),
            _legacy_assistant_entry([note], "note:0:message"),
            _legacy_assistant_entry([result], "result:0:message"),
            _legacy_assistant_entry([{"type": "text", "text": "Consulted."}], "done:0:message"),
        ],
        # Items the forwarder posted for the merged records must still be deduplicated.
        [
            _later_assistant_item("note:0:message", "Checking."),
            _later_assistant_item("result:0:message", "[Advisor response]"),
        ],
    )

    assert [record["message"]["content"] for record in records[1:]] == [
        "summary of older context",
        [{"type": "text", "text": "Asking."}],
        [call, note, result],
        [{"type": "text", "text": "Consulted."}],
    ]
    assert all(record["parentUuid"] == parent["uuid"] for parent, record in pairwise(records))


def test_orphaned_server_tool_result_is_dropped_on_resume(
    caplog: pytest.LogCaptureFixture,
) -> None:
    result = {
        "type": "advisor_tool_result",
        "tool_use_id": _SERVER_TOOL_CALL_ID,
        "content": _ADVISOR_RESULT,
    }

    with caplog.at_level(logging.WARNING):
        records = _records(
            [
                {"type": "message", "role": "user", "content": "summary of older context"},
                _legacy_assistant_entry([result], "orphan:0:message"),
                _legacy_assistant_entry([{"type": "text", "text": "Consulted."}, result]),
            ],
            # The dropped entry's items stay covered by the snapshot.
            [_later_assistant_item("orphan:0:message", "[Advisor response]")],
        )

    # The dropped results are diagnosable by call id; their content is not logged.
    assert caplog.text.count(f"tool_use_ids=['{_SERVER_TOOL_CALL_ID}']") == 2
    assert "Use backoff." not in caplog.text

    assert [record["message"]["content"] for record in records[1:]] == [
        "summary of older context",
        [{"type": "text", "text": "Consulted."}],
    ]
    assert all(record["parentUuid"] == parent["uuid"] for parent, record in pairwise(records))


def test_snapshot_of_only_orphaned_results_still_covers_its_items() -> None:
    result = {
        "type": "advisor_tool_result",
        "tool_use_id": _SERVER_TOOL_CALL_ID,
        "content": _ADVISOR_RESULT,
    }

    records = _records(
        [_legacy_assistant_entry([result], "orphan:0:message")],
        [_later_assistant_item("orphan:0:message", "[Advisor response]")],
    )

    assert [record["type"] for record in records] == ["system"]


@pytest.mark.parametrize("result_message_id", ["msg_call", None])
def test_server_tool_result_is_not_folded_across_another_response(
    result_message_id: str | None,
) -> None:
    call = {"type": "server_tool_use", "id": _SERVER_TOOL_CALL_ID, "name": "advisor", "input": {}}
    result = {
        "type": "advisor_tool_result",
        "tool_use_id": _SERVER_TOOL_CALL_ID,
        "content": _ADVISOR_RESULT,
    }
    other_reply = {"type": "text", "text": "Unrelated reply."}
    result_entry = _legacy_assistant_entry([result])
    if result_message_id is not None:
        result_entry["message_id"] = result_message_id

    records = _records(
        [
            {"type": "message", "role": "user", "content": "summary of older context"},
            {**_legacy_assistant_entry([call]), "message_id": "msg_call"},
            {**_legacy_assistant_entry([other_reply]), "message_id": "msg_other"},
            result_entry,
        ]
    )

    assert [
        (record["message"]["content"], record["message"].get("id")) for record in records[1:]
    ] == [
        ("summary of older context", None),
        ([call], "msg_call"),
        ([other_reply], "msg_other"),
    ]


def test_already_paired_server_tool_entry_is_left_unchanged() -> None:
    call = {"type": "server_tool_use", "id": _SERVER_TOOL_CALL_ID, "name": "advisor", "input": {}}
    result = {
        "type": "advisor_tool_result",
        "tool_use_id": _SERVER_TOOL_CALL_ID,
        "content": _ADVISOR_RESULT,
    }
    # A snapshot taken after a cold resume reads the rebuilt record holding the pair.
    paired = [{"type": "text", "text": "Asking."}, call, result, {"type": "text", "text": "Done."}]

    records = _records(
        [
            {"type": "message", "role": "user", "content": "summary of older context"},
            {**_legacy_assistant_entry(paired, "paired:0:message"), "message_id": "msg_pair"},
        ],
        [_later_assistant_item("paired:0:message", "Asking.")],
    )

    assert [record["message"] for record in records[1:]] == [
        {"role": "user", "content": "summary of older context"},
        {"role": "assistant", "content": paired, "id": "msg_pair"},
    ]


def test_rejoined_server_tool_pair_keeps_its_response_message_id() -> None:
    call = {"type": "server_tool_use", "id": _SERVER_TOOL_CALL_ID, "name": "advisor", "input": {}}
    result = {
        "type": "advisor_tool_result",
        "tool_use_id": _SERVER_TOOL_CALL_ID,
        "content": _ADVISOR_RESULT,
    }

    records = _records(
        [
            {"type": "message", "role": "user", "content": "summary of older context"},
            _legacy_assistant_entry([call]),
            {**_legacy_assistant_entry([result]), "message_id": "msg_pair"},
        ]
    )

    assert records[2]["message"] == {
        "role": "assistant",
        "content": [call, result],
        "id": "msg_pair",
    }

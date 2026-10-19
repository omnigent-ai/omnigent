"""Exercise retained Claude context through snapshot persistence and cold resume."""

import asyncio
import json
import logging
import uuid
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.conversation import CompactionData
from omnigent.harnesses.claude_native import forwarder
from omnigent.harnesses.claude_native.bridge import (
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
    def message(kind: str, uid: str, parent: str | None, content: Any) -> dict[str, Any]:
        return {
            "type": kind,
            "uuid": uid,
            "parentUuid": parent,
            "sessionId": _EXTERNAL_SESSION_ID,
            "message": {"role": kind, "content": content},
        }

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
        message("user", "old", None, "older context already summarized"),
        message("user", "request", "old", "continue the retained request"),
        message(
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
        message(
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
            **message("user", "summary", "boundary", "summary of older context"),
            "isCompactSummary": True,
            "isVisibleInTranscriptOnly": True,
        },
    ]


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completion_path: str
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
    assert not snapshot_tracker.has_retry_state("compaction-snapshot:1")


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
    entries = _transcript()
    entries.extend(
        [
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
    )
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
    entries = _transcript()
    entries.extend(
        [
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
    )
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

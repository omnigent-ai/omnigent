"""Exercise retained Claude context through snapshot persistence and cold resume."""

import asyncio
import json
import logging
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.claude_native.forwarder import _persist_native_compaction_item
from omnigent.harnesses.claude_native.main import (
    _claude_transcript_records_from_session_items,
)

_EXTERNAL_SESSION_ID = "00000000-0000-4000-8000-000000000001"


def _records(compacted_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _claude_transcript_records_from_session_items(
        [
            {
                "id": "compaction",
                "type": "compaction",
                "token_count": 987,
                "compacted_messages": compacted_messages,
            }
        ],
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


def _persist(bridge: Path, posted: list[dict[str, Any]]) -> None:
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
                client, session_id="conversation-test", bridge_dir=bridge
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
    expected = [entries[i]["message"]["content"] for i in (5, 1, 2, 3)]
    if continuation_parent:
        expected.append("post-compaction request")
    assert [message["content"] for message in messages] == expected
    records = _records(messages)
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
    with pytest.raises(ValueError):
        _persist(bridge, posted)
    assert posted == []
    transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    _persist(bridge, posted)
    assert len(posted[0]["data"]["compacted_messages"]) == 4


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
    entries[4]["compactMetadata"] = {"trigger": "manual"}
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

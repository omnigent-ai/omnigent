"""Claude Code's queue log is followed to the prompts it may not have taken yet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.submit_verification import (
    PromptQueue,
    advance_prompt_queue,
    start_prompt_queue,
)
from omnigent.util.json_types import JsonObject

_SESSION = "11111111-2222-4333-8444-555555555555"


def _queue(operation: str, content: str | None = None, **fields: str) -> JsonObject:
    record: JsonObject = {"type": "queue-operation", "operation": operation, "sessionId": _SESSION}
    if content is not None:
        record["content"] = content
    return {**record, **fields}


def _append(transcript: Path, *records: JsonObject) -> None:
    with transcript.open("a", encoding="utf-8") as handle:
        handle.writelines(json.dumps(record) + "\n" for record in records)


def _advance(queue: PromptQueue) -> PromptQueue:
    advanced = advance_prompt_queue(queue)
    assert advanced is not None
    return advanced


def _texts(queue: PromptQueue) -> list[str | None]:
    return [prompt.text for prompt in queue.prompts]


@pytest.mark.parametrize(
    "before,after,expected",
    [
        pytest.param(
            [],
            [_queue("enqueue", "/a"), _queue("enqueue", "b"), _queue("remove", "b")],
            ["/a"],
            id="absorbed",
        ),
        pytest.param([], [_queue("enqueue", "/a"), _queue("dequeue")], [], id="dequeued"),
        pytest.param(
            [],
            [_queue("enqueue", "/a"), _queue("enqueue", "b"), _queue("dequeue")],
            ["/a", "b"],
            id="dequeue-by-priority-names-neither",
        ),
        pytest.param(
            [],
            [
                _queue("enqueue", "/a"),
                _queue("enqueue", "b"),
                _queue("dequeue"),
                _queue("remove", "b"),
            ],
            [],
            id="removal-shows-what-was-dequeued",
        ),
        pytest.param(
            [],
            [
                _queue("enqueue", "/a"),
                _queue("enqueue", "b"),
                _queue("popAll", "/a"),
                _queue("popAll", "b"),
            ],
            [],
            id="pop-all",
        ),
        pytest.param(
            [],
            [_queue("enqueue", "/a"), _queue("enqueue", "b"), _queue("popOne", "/a")],
            ["b"],
            id="pop-one",
        ),
        pytest.param(
            [],
            [_queue("enqueue", "/a"), _queue("enqueue", "/a"), _queue("remove", "/a")],
            ["/a"],
            id="duplicate-text",
        ),
        pytest.param(
            [],
            [_queue("enqueue", "/a"), {**_queue("enqueue", "/b"), "sessionId": "other"}],
            ["/a"],
            id="other-session",
        ),
        pytest.param(
            [_queue("enqueue", "x")],
            [_queue("remove", "x"), _queue("enqueue", "/b")],
            ["/b"],
            id="removal-from-before-the-boundary",
        ),
        pytest.param(
            [_queue("enqueue", "x")],
            [_queue("dequeue"), _queue("enqueue", "/b"), _queue("dequeue")],
            [],
            id="dequeue-from-before-the-boundary",
        ),
        pytest.param(
            [],
            [_queue("enqueue"), _queue("enqueue", "/b"), _queue("remove")],
            ["/b"],
            id="non-text",
        ),
    ],
)
def test_following_keeps_every_prompt_the_log_cannot_rule_out(
    tmp_path: Path,
    before: list[JsonObject],
    after: list[JsonObject],
    expected: list[str],
) -> None:
    transcript = tmp_path / "native.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in before), encoding="utf-8")
    queue = start_prompt_queue(transcript, _SESSION)
    _append(transcript, *after)

    assert _texts(_advance(queue)) == expected


def test_following_resumes_after_the_last_complete_record(tmp_path: Path) -> None:
    """Each read starts where the last stopped, and a record still being written waits."""
    transcript = tmp_path / "native.jsonl"
    transcript.write_text("", encoding="utf-8")
    queue = start_prompt_queue(transcript, _SESSION)
    _append(transcript, _queue("enqueue", "/a"))
    partial = json.dumps(_queue("dequeue"))
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(partial[:10])

    queue = _advance(queue)
    assert _texts(queue) == ["/a"]
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(partial[10:] + "\n")

    assert _texts(_advance(queue)) == []


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(None, id="no-transcript-yet"),
        pytest.param(json.dumps(_queue("enqueue", "x")) + "\n", id="partial-record"),
    ],
)
def test_following_starts_before_a_record_still_being_written(
    tmp_path: Path, existing: str | None
) -> None:
    transcript = tmp_path / "native.jsonl"
    record = json.dumps(_queue("enqueue", "/a"))
    if existing is not None:
        transcript.write_text(existing + record[:10], encoding="utf-8")
    queue = start_prompt_queue(transcript, _SESSION)
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write((record[10:] if existing is not None else record) + "\n")

    assert _texts(_advance(queue)) == ["/a"]


def test_following_reads_past_large_records(tmp_path: Path) -> None:
    """Tool output larger than the submit check's read limit does not hide the queue."""
    transcript = tmp_path / "native.jsonl"
    transcript.write_text("", encoding="utf-8")
    queue = start_prompt_queue(transcript, _SESSION)
    output: JsonObject = {
        "type": "user",
        "sessionId": _SESSION,
        "content": "x" * (17 * 1024 * 1024),
    }
    _append(transcript, _queue("enqueue", "/a"), output)

    queue = _advance(queue)
    assert _texts(queue) == ["/a"]
    _append(transcript, output, _queue("dequeue"))

    assert _texts(_advance(queue)) == []


def test_following_stops_once_the_transcript_is_replaced(tmp_path: Path) -> None:
    transcript = tmp_path / "native.jsonl"
    _append(transcript, _queue("enqueue", "x"))
    queue = start_prompt_queue(transcript, _SESSION)
    transcript.write_text(json.dumps(_queue("enqueue", "/a")) + "\n", encoding="utf-8")

    assert advance_prompt_queue(queue) is None

"""Tests for the native Antigravity (agy) transcript forwarder.

These exercise the parse/map layer with fixture transcript lines based on the
REAL shapes captured from live agy transcripts (Step 0 of the F2 unit), plus
the dedup, partial/malformed-line, truncation, and file-appears-polling
behaviors. No live agy is launched: the pure mapping is driven directly and the
async tail loop runs against a temp transcript file with a mock Omnigent event
sink.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.antigravity_native_forwarder as forwarder
from omnigent.antigravity_native_audit import DEGRADE_NOTICE_TEXT
from omnigent.antigravity_native_bridge import (
    AntigravityNativeBridgeState,
    prepare_bridge_dir,
    read_bridge_state,
    write_bridge_state,
)
from omnigent.antigravity_native_forwarder import (
    AmbiguousDiscoveryError,
    OutboundEvent,
    PaneTarget,
    TranscriptParser,
    _audit_batch,
    _audit_tool_calls_from_events,
    _AuditOutcome,
    _highest_step_below,
    _ToolCallIdAllocator,
    forward_antigravity_transcript_to_session,
    step_to_events,
    transcript_path_for_conversation,
    unwrap_user_request,
)

_CID = "8ca97c49-4711-4f1c-a4f5-c8d8e4979687"


# ── Real fixture step shapes (captured from live agy transcripts) ──────────


def _user_input_step(text: str = "status?", step_index: int = 0) -> dict[str, Any]:
    """
    Build a USER_INPUT step matching agy's real wrapped-content shape.

    :param text: The unwrapped user prompt to embed.
    :param step_index: The step index to stamp.
    :returns: A USER_INPUT step dict.
    """
    content = (
        f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\nThe current local time is: 2026-06-07T01:17:43-07:00.\n"
        "</ADDITIONAL_METADATA>\n"
        "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection`.\n"
        "</USER_SETTINGS_CHANGE>"
    )
    return {
        "step_index": step_index,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": "2026-06-07T01:17:43Z",
        "content": content,
    }


def _planner_text_step(text: str = "OK", step_index: int = 2) -> dict[str, Any]:
    """
    Build a PLANNER_RESPONSE step with assistant text and no tool calls.

    :param text: Assistant message text.
    :param step_index: The step index to stamp.
    :returns: A PLANNER_RESPONSE step dict.
    """
    return {
        "step_index": step_index,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-06-07T01:17:44Z",
        "content": text,
    }


def _planner_tool_step(step_index: int = 2) -> dict[str, Any]:
    """
    Build a PLANNER_RESPONSE step that initiates a single tool call.

    :param step_index: The step index to stamp.
    :returns: A PLANNER_RESPONSE step dict with ``tool_calls``.
    """
    return {
        "step_index": step_index,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-06-07T01:17:44Z",
        "content": "I will list the directory structure.",
        "thinking": "**Prioritizing Tool Usage** I'm focusing on tool selection.",
        "tool_calls": [
            {
                "name": "list_dir",
                "args": {
                    "DirectoryPath": "/Users/bryanli/Projects/askcv.ai",
                    "toolAction": "Listing root directory",
                    "toolSummary": "List root directory",
                },
            }
        ],
    }


def _planner_two_tool_step(step_index: int = 2) -> dict[str, Any]:
    """
    Build a PLANNER_RESPONSE step that initiates TWO tool calls in one step.

    Used to exercise the per-call ordinal: two violations from the same step must
    get distinct warning response ids.

    :param step_index: The step index to stamp.
    :returns: A PLANNER_RESPONSE step dict with two ``tool_calls`` entries.
    """
    return {
        "step_index": step_index,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-06-07T01:17:44Z",
        "content": "Running two commands.",
        "tool_calls": [
            {"name": "run_command", "args": {"CommandLine": "rm -rf /a"}},
            {"name": "run_command", "args": {"CommandLine": "rm -rf /b"}},
        ],
    }


def _list_directory_result_step(step_index: int = 3) -> dict[str, Any]:
    """
    Build a LIST_DIRECTORY tool-result step.

    :param step_index: The step index to stamp.
    :returns: A LIST_DIRECTORY step dict.
    """
    return {
        "step_index": step_index,
        "source": "MODEL",
        "type": "LIST_DIRECTORY",
        "status": "DONE",
        "created_at": "2026-06-07T01:17:45Z",
        "content": (
            "Created At: 2026-06-07T01:17:45Z\nCompleted At: 2026-06-07T01:17:45Z\n"
            '{"name":".claude", "isDir":true}\n{"name":"README.md", "sizeBytes":"4807"}'
        ),
    }


def _run_command_result_step(step_index: int = 14) -> dict[str, Any]:
    """
    Build a RUN_COMMAND tool-result step.

    :param step_index: The step index to stamp.
    :returns: A RUN_COMMAND step dict.
    """
    return {
        "step_index": step_index,
        "source": "MODEL",
        "type": "RUN_COMMAND",
        "status": "DONE",
        "created_at": "2026-06-07T01:17:52Z",
        "content": (
            "Created At: 2026-06-07T01:17:52Z\nCompleted At: 2026-06-07T01:18:00Z\n\n"
            "\t\t\t\tThe command completed successfully.\n\t\t\t\tOutput:\n\t\t\t\thi\n"
        ),
    }


def _conversation_history_step(step_index: int = 1) -> dict[str, Any]:
    """
    Build a SYSTEM/CONVERSATION_HISTORY step (skipped by the mapper).

    :param step_index: The step index to stamp.
    :returns: A CONVERSATION_HISTORY step dict.
    """
    return {
        "step_index": step_index,
        "source": "SYSTEM",
        "type": "CONVERSATION_HISTORY",
        "status": "DONE",
        "created_at": "2026-06-07T01:17:43Z",
        "content": "# Conversation History\nHere are the conversation IDs...",
    }


def _system_message_step(step_index: int = 5) -> dict[str, Any]:
    """
    Build a SYSTEM/SYSTEM_MESSAGE step (skipped by the mapper).

    :param step_index: The step index to stamp.
    :returns: A SYSTEM_MESSAGE step dict.
    """
    return {
        "step_index": step_index,
        "source": "SYSTEM",
        "type": "SYSTEM_MESSAGE",
        "status": "DONE",
        "created_at": "2026-06-07T15:02:13Z",
        "content": "The following is a <SYSTEM_MESSAGE> not actually sent by the user.",
    }


def _allocator() -> _ToolCallIdAllocator:
    """
    Build a fresh tool-call id allocator for the fixture conversation.

    :returns: A new allocator bound to ``_CID``.
    """
    return _ToolCallIdAllocator(conversation_id=_CID)


# ── unwrap_user_request ────────────────────────────────────────────────────


def test_unwrap_user_request_strips_metadata() -> None:
    """The user prompt is extracted from the wrapped USER_INPUT content."""
    content = _user_input_step(text="deploy the app")["content"]
    assert isinstance(content, str)
    assert unwrap_user_request(content) == "deploy the app"


def test_unwrap_user_request_falls_back_to_whole_content() -> None:
    """Content without the wrapper falls back to the stripped whole string."""
    assert unwrap_user_request("  raw prompt  ") == "raw prompt"


# ── step_to_events: user input ─────────────────────────────────────────────


def test_user_input_maps_to_user_message_item() -> None:
    """A USER_INPUT step becomes a role=user message item with unwrapped text."""
    events = step_to_events(
        _user_input_step(text="hi there", step_index=0),
        conversation_id=_CID,
        allocator=_allocator(),
    )
    assert events == [
        OutboundEvent(
            event_type="external_conversation_item",
            data={
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi there"}],
                },
                "response_id": f"agy_{_CID}_0",
            },
            step_index=0,
        )
    ]


def test_empty_user_input_is_skipped() -> None:
    """A USER_INPUT step whose unwrapped prompt is empty yields no events."""
    step = _user_input_step(text="", step_index=0)
    assert step_to_events(step, conversation_id=_CID, allocator=_allocator()) == []


# ── step_to_events: planner / assistant ────────────────────────────────────


def test_planner_text_maps_to_delta_then_assistant_message() -> None:
    """A text-only PLANNER_RESPONSE emits a delta then a durable message."""
    events = step_to_events(
        _planner_text_step(text="Done.", step_index=2),
        conversation_id=_CID,
        allocator=_allocator(),
    )
    assert [e.event_type for e in events] == [
        "external_output_text_delta",
        "external_conversation_item",
    ]
    delta = events[0]
    assert delta.data == {
        "delta": "Done.",
        "message_id": f"antigravity:{_CID}:2:planner",
        "index": 0,
        "final": True,
    }
    message = events[1]
    assert message.data == {
        "item_type": "message",
        "item_data": {
            "role": "assistant",
            "agent": "antigravity-native-ui",
            "content": [{"type": "output_text", "text": "Done."}],
        },
        "response_id": f"agy_{_CID}_2",
    }


def test_planner_tool_call_maps_to_function_call_item() -> None:
    """A PLANNER_RESPONSE with tool_calls emits text plus a function_call."""
    events = step_to_events(
        _planner_tool_step(step_index=2),
        conversation_id=_CID,
        allocator=_allocator(),
    )
    # delta + assistant message + function_call
    assert [e.event_type for e in events] == [
        "external_output_text_delta",
        "external_conversation_item",
        "external_conversation_item",
    ]
    function_call = events[2]
    assert function_call.data["item_type"] == "function_call"
    item_data = function_call.data["item_data"]
    assert isinstance(item_data, dict)
    assert item_data["name"] == "list_dir"
    assert item_data["agent"] == "antigravity-native-ui"
    assert item_data["call_id"] == f"agy_call_{_CID}_0"
    # Display-only keys are stripped from mirrored arguments.
    arguments = json.loads(item_data["arguments"])
    assert arguments == {"DirectoryPath": "/Users/bryanli/Projects/askcv.ai"}
    assert "toolAction" not in arguments
    assert "toolSummary" not in arguments


def test_thinking_is_not_emitted() -> None:
    """The ``thinking`` field never produces an event (skipped per scope)."""
    events = step_to_events(
        _planner_tool_step(step_index=2),
        conversation_id=_CID,
        allocator=_allocator(),
    )
    for event in events:
        assert "thinking" not in json.dumps(event.data)
        if event.event_type == "external_output_text_delta":
            # The delta carries the visible content, never the reasoning text.
            assert event.data["delta"] == "I will list the directory structure."


# ── step_to_events: tool results ───────────────────────────────────────────


def test_tool_result_maps_to_function_call_output() -> None:
    """A non-PLANNER MODEL step with content becomes a function_call_output."""
    events = step_to_events(
        _list_directory_result_step(step_index=3),
        conversation_id=_CID,
        allocator=_allocator(),
    )
    assert len(events) == 1
    output = events[0]
    assert output.data["item_type"] == "function_call_output"
    item_data = output.data["item_data"]
    assert isinstance(item_data, dict)
    assert item_data["call_id"] == f"agy_call_{_CID}_orphan_0"
    assert "README.md" in item_data["output"]


def test_run_command_result_maps_to_function_call_output() -> None:
    """A RUN_COMMAND result step is mirrored as a function_call_output."""
    events = step_to_events(
        _run_command_result_step(step_index=14),
        conversation_id=_CID,
        allocator=_allocator(),
    )
    assert len(events) == 1
    assert events[0].data["item_type"] == "function_call_output"
    item_data = events[0].data["item_data"]
    assert isinstance(item_data, dict)
    assert "The command completed successfully." in item_data["output"]


def test_invocation_and_result_share_call_id() -> None:
    """The function_call and its following function_call_output pair by id."""
    allocator = _allocator()
    invocation_events = step_to_events(
        _planner_tool_step(step_index=2),
        conversation_id=_CID,
        allocator=allocator,
    )
    result_events = step_to_events(
        _list_directory_result_step(step_index=3),
        conversation_id=_CID,
        allocator=allocator,
    )
    invocation_call_id = next(
        e.data["item_data"]["call_id"]  # type: ignore[index]
        for e in invocation_events
        if e.event_type == "external_conversation_item"
        and e.data.get("item_type") == "function_call"
    )
    output_call_id = result_events[0].data["item_data"]["call_id"]  # type: ignore[index]
    assert invocation_call_id == output_call_id == f"agy_call_{_CID}_0"


# ── step_to_events: system / skipped ───────────────────────────────────────


@pytest.mark.parametrize(
    "step",
    [
        _conversation_history_step(),
        _system_message_step(),
        {"step_index": 9, "source": "SYSTEM", "type": "EPHEMERAL_MESSAGE", "content": "x"},
    ],
)
def test_system_steps_are_skipped(step: dict[str, Any]) -> None:
    """SYSTEM-sourced steps (history / system / ephemeral) yield no events."""
    assert step_to_events(step, conversation_id=_CID, allocator=_allocator()) == []


def test_step_without_step_index_is_skipped() -> None:
    """A step missing an integer ``step_index`` yields no events."""
    step = {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "hi"}
    assert step_to_events(step, conversation_id=_CID, allocator=_allocator()) == []


# ── TranscriptParser: status edges + ordering ──────────────────────────────


def _feed_jsonl(parser: TranscriptParser, steps: list[dict[str, Any]]) -> list[OutboundEvent]:
    """
    Feed a list of steps as one JSONL blob through the parser.

    :param parser: The parser under test.
    :param steps: Steps to serialize and feed.
    :returns: All events emitted across the fed lines.
    """
    blob = "".join(json.dumps(step) + "\n" for step in steps)
    return parser.feed(blob)


def test_parser_emits_running_then_idle_status_edges() -> None:
    """A user turn opens ``running`` and the closing assistant text emits ``idle``."""
    parser = TranscriptParser(conversation_id=_CID)
    events = _feed_jsonl(
        parser,
        [
            _user_input_step(text="hi", step_index=0),
            _conversation_history_step(step_index=1),
            _planner_text_step(text="Hello!", step_index=2),
        ],
    )
    status_events = [e for e in events if e.event_type == "external_session_status"]
    assert [e.data["status"] for e in status_events] == ["running", "idle"]
    # The running edge precedes the user message; idle follows the assistant text.
    types = [e.event_type for e in events]
    assert types[0] == "external_session_status"
    assert events[0].data == {"status": "running"}
    assert events[-1].data == {"status": "idle"}


def test_parser_no_idle_until_assistant_text_without_tools() -> None:
    """A turn stays running through tool calls/results until plain assistant text."""
    parser = TranscriptParser(conversation_id=_CID)
    events = _feed_jsonl(
        parser,
        [
            _user_input_step(text="list", step_index=0),
            _planner_tool_step(step_index=2),
            _list_directory_result_step(step_index=3),
        ],
    )
    statuses = [e.data["status"] for e in events if e.event_type == "external_session_status"]
    assert statuses == ["running"]
    assert parser.turn_active is True
    # The closing plain-text answer ends the turn.
    closing = parser.feed(json.dumps(_planner_text_step(text="Here it is.", step_index=4)) + "\n")
    statuses2 = [e.data["status"] for e in closing if e.event_type == "external_session_status"]
    assert statuses2 == ["idle"]
    assert parser.turn_active is False


def test_parser_status_can_be_disabled() -> None:
    """With ``emit_status=False`` no status edges are produced."""
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)
    events = _feed_jsonl(
        parser,
        [_user_input_step(step_index=0), _planner_text_step(step_index=2)],
    )
    assert all(e.event_type != "external_session_status" for e in events)


# ── TranscriptParser: dedup ────────────────────────────────────────────────


def test_parser_dedups_by_step_index() -> None:
    """Re-feeding a line with a seen step_index posts it only once."""
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)
    line = json.dumps(_planner_text_step(text="Once.", step_index=7)) + "\n"
    first = parser.feed(line)
    second = parser.feed(line)
    assert len([e for e in first if e.event_type == "external_conversation_item"]) == 1
    assert second == []


def test_parser_dedup_survives_full_replay() -> None:
    """Replaying the entire transcript prefix emits nothing the second time."""
    steps = [
        _user_input_step(step_index=0),
        _conversation_history_step(step_index=1),
        _planner_tool_step(step_index=2),
        _list_directory_result_step(step_index=3),
        _planner_text_step(text="Done.", step_index=4),
    ]
    parser = TranscriptParser(conversation_id=_CID)
    first = _feed_jsonl(parser, steps)
    assert first  # produced events on first pass
    second = _feed_jsonl(parser, steps)
    assert second == []


# ── TranscriptParser: partial / malformed lines ────────────────────────────


def test_parser_buffers_partial_trailing_line() -> None:
    """An incomplete trailing line is buffered until its newline arrives."""
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)
    full = json.dumps(_planner_text_step(text="Buffered.", step_index=2))
    head, tail = full[:20], full[20:]
    # Feed the line in two chunks with the newline only in the second.
    assert parser.feed(head) == []
    events = parser.feed(tail + "\n")
    item_events = [e for e in events if e.event_type == "external_conversation_item"]
    assert len(item_events) == 1
    item_data = item_events[0].data["item_data"]
    assert isinstance(item_data, dict)
    assert item_data["content"] == [{"type": "output_text", "text": "Buffered."}]


def test_parser_skips_malformed_line_but_keeps_going() -> None:
    """A malformed JSON line is skipped; surrounding valid lines still post."""
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)
    blob = (
        json.dumps(_planner_text_step(text="A", step_index=2))
        + "\n"
        + "{not valid json at all\n"
        + json.dumps(_planner_text_step(text="B", step_index=3))
        + "\n"
    )
    events = parser.feed(blob)
    texts = [
        e.data["item_data"]["content"][0]["text"]  # type: ignore[index]
        for e in events
        if e.event_type == "external_conversation_item"
    ]
    assert texts == ["A", "B"]


def test_parser_skips_blank_and_non_object_lines() -> None:
    """Blank lines and JSON non-objects are skipped without error."""
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)
    blob = "\n" + "[1, 2, 3]\n" + "  \n" + json.dumps(_planner_text_step(step_index=2)) + "\n"
    events = parser.feed(blob)
    assert len([e for e in events if e.event_type == "external_conversation_item"]) == 1


# ── _read_transcript_from_offset: truncation / rotation ────────────────────


def test_offset_reader_tails_appended_lines(tmp_path: Path) -> None:
    """The offset reader returns only newly appended steps on each call."""
    path = tmp_path / "transcript_full.jsonl"
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)
    path.write_text(json.dumps(_planner_text_step(text="first", step_index=2)) + "\n")
    events, offset = forwarder._read_transcript_from_offset(path, 0, parser)
    assert len([e for e in events if e.event_type == "external_conversation_item"]) == 1
    assert offset == path.stat().st_size
    # Append a second line; only it is returned.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_planner_text_step(text="second", step_index=3)) + "\n")
    events2, offset2 = forwarder._read_transcript_from_offset(path, offset, parser)
    texts = [
        e.data["item_data"]["content"][0]["text"]  # type: ignore[index]
        for e in events2
        if e.event_type == "external_conversation_item"
    ]
    assert texts == ["second"]
    assert offset2 == path.stat().st_size


def test_offset_reader_handles_truncation(tmp_path: Path) -> None:
    """A file rewritten smaller than the offset restarts from 0 without dup."""
    path = tmp_path / "transcript_full.jsonl"
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)
    path.write_text(json.dumps(_planner_text_step(text="orig", step_index=2)) + "\n")
    _events, offset = forwarder._read_transcript_from_offset(path, 0, parser)
    assert offset > 0
    # Rewrite the file smaller (truncation/rotation in place). The already-seen
    # step_index=2 must NOT be re-posted; a new step_index=3 must be.
    path.write_text(json.dumps(_planner_text_step(text="new", step_index=3)) + "\n")
    assert path.stat().st_size < offset
    events2, _offset2 = forwarder._read_transcript_from_offset(path, offset, parser)
    texts = [
        e.data["item_data"]["content"][0]["text"]  # type: ignore[index]
        for e in events2
        if e.event_type == "external_conversation_item"
    ]
    assert texts == ["new"]


def test_offset_reader_missing_file_is_noop(tmp_path: Path) -> None:
    """A missing transcript file yields no events and preserves the offset."""
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)
    events, offset = forwarder._read_transcript_from_offset(tmp_path / "absent.jsonl", 123, parser)
    assert events == []
    assert offset == 123


# ── async tail loop: file-appears polling + discovery + posting ────────────


class _EventSink:
    """
    Collects Omnigent session events + external-id PATCHes via a MockTransport.

    :param events: Accumulated ``(type, data)`` tuples from ``/events`` POSTs in
        order.
    :param external_id_patches: ``external_session_id`` values from PATCHes to
        ``/v1/sessions/{id}`` in order (the forwarder persists agy's discovered
        id this way).
    """

    def __init__(self) -> None:
        """Initialize an empty sink."""
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.external_id_patches: list[str] = []

    def transport(self) -> httpx.MockTransport:
        """
        Return a MockTransport that records event POSTs and id PATCHes.

        :returns: A MockTransport recording ``/events`` POSTs and session
            ``external_session_id`` PATCHes.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            if request.method == "PATCH":
                external_id = body.get("external_session_id")
                if isinstance(external_id, str):
                    self.external_id_patches.append(external_id)
                return httpx.Response(200, json={"ok": True})
            self.events.append((body["type"], body["data"]))
            return httpx.Response(200, json={"ok": True})

        return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _isolate_bridge_and_brain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """
    Point the bridge root and agy brain root at per-test temp dirs.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp directory.
    :returns: The temp brain root path.
    """
    monkeypatch.setattr("omnigent.antigravity_native_bridge._BRIDGE_ROOT", tmp_path / "bridge")
    brain = tmp_path / "brain"
    brain.mkdir()
    monkeypatch.setattr(forwarder, "_DEFAULT_AGY_APP_DATA_DIR", brain.parent)
    monkeypatch.setattr(forwarder, "_BRAIN_SUBDIR", "brain")
    return brain


def _write_transcript(brain: Path, conversation_id: str, steps: list[dict[str, Any]]) -> Path:
    """
    Write a transcript file for a conversation under the brain root.

    :param brain: The temp brain root.
    :param conversation_id: agy conversation id (dir name).
    :param steps: Steps to serialize into the transcript.
    :returns: The written transcript path.
    """
    path = brain / conversation_id / ".system_generated" / "logs" / "transcript_full.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(step) + "\n" for step in steps))
    return path


async def test_forwarder_waits_for_missing_file_then_posts(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """The forwarder polls for a not-yet-created transcript, then mirrors it."""
    brain = _isolate_bridge_and_brain
    # No live agy in a unit test, so stub the connect-RPC ownership confirmation
    # used by fresh discovery (see ``_conversation_is_owned_by_live_agy``).
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)
    bridge_dir = prepare_bridge_dir("bridge-1")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_1",
            conversation_id="agy_conv_minted",  # the launcher value agy ignores
        ),
    )
    sink = _EventSink()

    # Speed up polling.
    sleeps: list[float] = []

    async def _fast_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    async def _create_transcript_after_delay() -> None:
        # Let the forwarder poll a few times against the empty brain root first.
        await asyncio.sleep(0.05)
        _write_transcript(
            brain,
            _CID,
            [
                _user_input_step(text="hi", step_index=0),
                _planner_text_step(text="Hello!", step_index=2),
            ],
        )

    async def _run_forwarder() -> None:
        await forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_1",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=sink.transport(),
            transcript_discovery_timeout_s=5.0,
        )

    task = asyncio.create_task(_run_forwarder())
    await _create_transcript_after_delay()
    # Give the tail loop time to read and post, then stop it.
    for _ in range(200):
        if any(t == "external_conversation_item" for t, _ in sink.events):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    item_events = [(t, d) for t, d in sink.events if t == "external_conversation_item"]
    roles = [d["item_data"].get("role") for _t, d in item_events]
    assert "user" in roles
    assert "assistant" in roles
    # The discovered agy conversation id was persisted back to bridge state.
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.conversation_id == _CID
    # ...and PATCHed onto the Omnigent session as external_session_id so a
    # later resume targets agy's real id (correction C).
    assert _CID in sink.external_id_patches


async def test_forwarder_discovers_conversation_and_emits_status(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """A pre-existing transcript is discovered and mirrored with status edges."""
    brain = _isolate_bridge_and_brain
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)
    bridge_dir = prepare_bridge_dir("bridge-2")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_2",
            conversation_id="agy_conv_minted",
        ),
    )
    _write_transcript(
        brain,
        _CID,
        [
            _user_input_step(text="hi", step_index=0),
            _conversation_history_step(step_index=1),
            _planner_text_step(text="Hello!", step_index=2),
        ],
    )
    sink = _EventSink()

    async def _fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    task = asyncio.create_task(
        forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_2",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=sink.transport(),
            transcript_discovery_timeout_s=5.0,
        )
    )
    for _ in range(200):
        statuses = [d["status"] for t, d in sink.events if t == "external_session_status"]
        if "idle" in statuses:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    statuses = [d["status"] for t, d in sink.events if t == "external_session_status"]
    assert statuses[0] == "running"
    assert "idle" in statuses


async def test_forwarder_times_out_when_no_transcript_appears(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """With no transcript ever created, the run returns on the discovery timeout."""
    bridge_dir = prepare_bridge_dir("bridge-3")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_3",
            conversation_id="agy_conv_minted",
        ),
    )
    sink = _EventSink()

    async def _instant_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _instant_sleep)

    # Returns (does not hang) because the discovery deadline elapses.
    await asyncio.wait_for(
        forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_3",
            bridge_dir=bridge_dir,
            poll_interval_s=0.0,
            discovery_floor=0.0,
            ap_transport=sink.transport(),
            transcript_discovery_timeout_s=0.01,
        ),
        timeout=5.0,
    )
    assert sink.events == []


def test_transcript_path_for_conversation_uses_brain_root(
    _isolate_bridge_and_brain: Path,
) -> None:
    """The transcript path is rooted under the (patched) agy brain root."""
    path = transcript_path_for_conversation(_CID)
    assert path == (
        _isolate_bridge_and_brain / _CID / ".system_generated" / "logs" / "transcript_full.jsonl"
    )


# ── verifiable cross-session discovery (Fix 1) ─────────────────────────────


def _make_brain_dir(brain: Path, conversation_id: str) -> None:
    """
    Create an (empty) brain conversation dir under the brain root.

    :param brain: The temp brain root.
    :param conversation_id: agy conversation id (dir name) to create.
    :returns: None.
    """
    (brain / conversation_id).mkdir(parents=True, exist_ok=True)


def test_discover_skips_dir_claimed_by_another_bridge(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    Discovery binds the unclaimed brain dir, never one another bridge claims.

    The brain root is shared by every agy on the host. Under two near-
    simultaneous launches, both brain dirs appear in the window; if forwarder A
    bound the dir forwarder B already recorded, A would mirror B's transcript
    and PATCH B's id as A's ``external_session_id``. Excluding ids claimed by
    other live bridge dirs prevents that cross-bind.

    :param monkeypatch: pytest monkeypatch fixture.
    :param _isolate_bridge_and_brain: Per-test brain root (autouse isolation).
    :returns: None.
    """
    brain = _isolate_bridge_and_brain
    claimed_cid = "11111111-1111-4111-8111-111111111111"
    unclaimed_cid = "22222222-2222-4222-8222-222222222222"
    _make_brain_dir(brain, claimed_cid)
    _make_brain_dir(brain, unclaimed_cid)

    # Another concurrent launch's bridge dir already claims ``claimed_cid``.
    other_bridge = prepare_bridge_dir("bridge-other")
    write_bridge_state(
        other_bridge,
        AntigravityNativeBridgeState(session_id="conv_other", conversation_id=claimed_cid),
    )
    my_bridge = prepare_bridge_dir("bridge-mine")

    # Both candidates would "be owned by a live agy" — the claimed one must
    # still be excluded purely by the claim scan, never reaching this check for
    # a bind.
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)

    # No tmux pane → fallback path; after exclusion only one unclaimed candidate
    # remains, so the lone-candidate branch binds it.
    discovered = forwarder._discover_conversation_id(
        since=0.0, bridge_dir=my_bridge, pane=None, ambiguity_deadline=None
    )
    assert discovered == unclaimed_cid


def test_discover_refuses_when_multiple_unclaimed_candidates(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    Two unclaimed in-window candidates → refuse to bind (return ``None``).

    With no claim to disambiguate and no pid to identify "our" agy, binding the
    newer dir by mtime could still be the wrong conversation. Discovery must
    refuse and keep polling rather than guess — the caller retries until the
    ambiguity resolves (the other launch records its id) or the run times out.

    :param monkeypatch: pytest monkeypatch fixture.
    :param _isolate_bridge_and_brain: Per-test brain root (autouse isolation).
    :returns: None.
    """
    brain = _isolate_bridge_and_brain
    _make_brain_dir(brain, "33333333-3333-4333-8333-333333333333")
    _make_brain_dir(brain, "44444444-4444-4444-8444-444444444444")
    my_bridge = prepare_bridge_dir("bridge-mine")

    # Even if both would verify as live, ambiguity must win and refuse the bind.
    owned_calls: list[str] = []

    def _owned(cid: str) -> bool:
        owned_calls.append(cid)
        return True

    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", _owned)

    # Fallback path (no pane), ambiguity has not yet hit the deadline → refuse to
    # bind this poll (return None) rather than guess.
    discovered = forwarder._discover_conversation_id(
        since=0.0, bridge_dir=my_bridge, pane=None, ambiguity_deadline=None
    )
    assert discovered is None
    # Refusal happens before any ownership probe — ambiguity is not "resolved"
    # by picking whichever verifies.
    assert owned_calls == []


def test_discover_returns_single_unclaimed_owned_candidate(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    A single unclaimed candidate owned by a live agy is bound.

    The happy fresh-launch path: one new brain dir, no competing claim, and a
    reachable agy that reports metadata for it.

    :param monkeypatch: pytest monkeypatch fixture.
    :param _isolate_bridge_and_brain: Per-test brain root (autouse isolation).
    :returns: None.
    """
    brain = _isolate_bridge_and_brain
    cid = "55555555-5555-4555-8555-555555555555"
    _make_brain_dir(brain, cid)
    my_bridge = prepare_bridge_dir("bridge-mine")
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda c: c == cid)

    assert (
        forwarder._discover_conversation_id(
            since=0.0, bridge_dir=my_bridge, pane=None, ambiguity_deadline=None
        )
        == cid
    )


def test_discover_waits_when_single_candidate_not_owned(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    A lone candidate that no reachable agy owns is not bound (keep polling).

    Guards the positive-ownership gate: a stale/foreign brain dir with no live
    agy hosting it must not be bound (and PATCHed) — the forwarder returns
    ``None`` and retries until agy's connect-RPC confirms ownership or the run
    times out.

    :param monkeypatch: pytest monkeypatch fixture.
    :param _isolate_bridge_and_brain: Per-test brain root (autouse isolation).
    :returns: None.
    """
    brain = _isolate_bridge_and_brain
    _make_brain_dir(brain, "66666666-6666-4666-8666-666666666666")
    my_bridge = prepare_bridge_dir("bridge-mine")
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: False)

    assert (
        forwarder._discover_conversation_id(
            since=0.0, bridge_dir=my_bridge, pane=None, ambiguity_deadline=None
        )
        is None
    )


def test_parser_high_water_mark_dedup() -> None:
    """
    The high-water-mark dedup skips steps at or below the last seen index.

    agy step_index is monotonically increasing but non-contiguous (e.g. 0→2→4).
    A step whose index is already at or below the high-water mark is silently
    dropped; the next strictly-greater index is always accepted.
    """
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)
    import json as _json

    step2 = _json.dumps(_planner_text_step(text="first", step_index=2)) + "\n"
    step4 = _json.dumps(_planner_text_step(text="second", step_index=4)) + "\n"

    # First feed: step_index=2 accepted, high-water becomes 2.
    events = parser.feed(step2)
    assert any(e.event_type == "external_conversation_item" for e in events)

    # Replay step_index=2 (same value as high-water) → skipped.
    assert parser.feed(step2) == []

    # step_index=4 > 2 → accepted.
    events2 = parser.feed(step4)
    assert any(e.event_type == "external_conversation_item" for e in events2)

    # Replay step_index=4 → skipped (≤ high-water).
    assert parser.feed(step4) == []


# ── event-loop non-blocking regression (offload fix) ──────────────────────


async def test_discovery_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    Verifiable-discovery path does not starve the event loop during polling.

    ``_resolve_transcript`` calls ``_resolve_transcript_once`` in a thread so
    that the blocking ownership/port probe (pgrep + lsof + httpx TLS) never
    runs on the loop thread.  This test exercises the REAL discovery seam
    (``_conversation_is_owned_by_live_agy`` is patched to something that
    actually sleeps — simulating the subprocess + network latency — rather than
    mocked to a no-op).  A concurrent asyncio task increments a counter on a
    short interval; if discovery blocked the loop, the counter would not advance
    during the blocking sleep.

    :param monkeypatch: pytest monkeypatch fixture.
    :param _isolate_bridge_and_brain: Per-test brain root (autouse isolation).
    :returns: None.
    """
    brain = _isolate_bridge_and_brain
    cid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _make_brain_dir(brain, cid)
    bridge_dir = prepare_bridge_dir("bridge-offload")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_offload",
            conversation_id="agy_conv_minted",
        ),
    )

    # Patch the ownership probe to a function that sleeps 50 ms — imitating
    # the real pgrep + lsof + httpx cost.  This runs inside the thread spawned
    # by ``asyncio.to_thread``; if the offload is absent it would run on the
    # loop and block the counter task below.
    _PROBE_SLEEP_S = 0.05

    def _blocking_owned(conversation_id: str) -> bool:
        time.sleep(_PROBE_SLEEP_S)
        return True

    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", _blocking_owned)

    # Speed up the discovery poll so we don't wait 0.25 s per cycle.
    async def _fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    # A counter incremented by a separate asyncio task on every 1 ms tick.
    # If the event loop is blocked the counter will not advance.
    counter: list[int] = [0]

    async def _tick() -> None:
        while True:
            await asyncio.sleep(0.001)
            counter[0] += 1

    tick_task = asyncio.create_task(_tick())
    sink = _EventSink()

    # Write the transcript after a short delay so discovery has a chance to
    # run at least one blocking poll before it succeeds.
    async def _create_transcript() -> None:
        await asyncio.sleep(0.02)
        _write_transcript(
            brain,
            cid,
            [_planner_text_step(text="hi", step_index=2)],
        )

    create_task = asyncio.create_task(_create_transcript())

    start_counter = counter[0]

    fwd_task = asyncio.create_task(
        forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_offload",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=sink.transport(),
            transcript_discovery_timeout_s=5.0,
        )
    )

    # Wait up to 2 s for at least one event (meaning discovery succeeded).
    for _ in range(400):
        if sink.events:
            break
        await asyncio.sleep(0.005)

    fwd_task.cancel()
    tick_task.cancel()
    create_task.cancel()
    for t in (fwd_task, tick_task, create_task):
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t

    # The discovery probe slept for ~50 ms; if it ran on the loop the counter
    # (which ticks every 1 ms) would advance by 0 or 1 during that window.
    # With the offload the counter should have advanced by several ticks (the
    # tick task kept running while the probe thread slept).  We require at
    # least 3 ticks to rule out coincidental scheduling noise.
    ticks_during_probe = counter[0] - start_counter
    assert ticks_during_probe >= 3, (
        f"Event loop appears to have been blocked during discovery: "
        f"counter advanced by only {ticks_during_probe} ticks while the "
        f"ownership probe slept {_PROBE_SLEEP_S * 1000:.0f} ms"
    )
    # Discovery must have succeeded.
    assert sink.events, "Forwarder produced no events — discovery may have failed"


def test_supervisor_latch_prevents_double_patch(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    The supervisor-lifetime latch suppresses ``_patch_external_session_id``
    on forwarder restarts so the PATCH fires exactly once per supervisor run.

    On crash+restart the supervisor re-enters
    ``forward_antigravity_transcript_to_session`` with the same
    ``_external_session_id_patched`` list; the second run must skip the PATCH
    because the list already holds ``[True]``.
    """
    brain = _isolate_bridge_and_brain
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)
    bridge_dir = prepare_bridge_dir("bridge-latch")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_latch",
            conversation_id="agy_conv_minted",
        ),
    )
    _write_transcript(
        brain,
        _CID,
        [_user_input_step(text="hi", step_index=0), _planner_text_step(text="hi", step_index=2)],
    )
    sink = _EventSink()

    async def _fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    latch: list[bool] = [False]

    async def _run_once() -> None:
        await forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_latch",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=sink.transport(),
            transcript_discovery_timeout_s=5.0,
            _external_session_id_patched=latch,
        )

    # First run: latch is [False] → PATCH fires, latch becomes [True].
    asyncio.run(_wait_for_patch_then_cancel(_run_once, sink))
    patches_after_first_run = list(sink.external_id_patches)

    # Reset events so we can count cleanly.
    sink.external_id_patches.clear()

    # Second run: latch is already [True] → PATCH must NOT fire.
    asyncio.run(_run_and_cancel(_run_once))
    assert sink.external_id_patches == [], "second run must not re-PATCH when latch is set"
    assert _CID in patches_after_first_run, "first run must have PATCHed the id"


async def _wait_for_patch_then_cancel(coro_fn: object, sink: _EventSink) -> None:
    """
    Start a forwarder coroutine, wait until the PATCH fires, then cancel.

    :param coro_fn: Async callable returning the forwarder coroutine.
    :param sink: Event sink whose ``external_id_patches`` list we poll.
    :returns: None after cancellation.
    """
    import contextlib

    task = asyncio.create_task(coro_fn())  # type: ignore[operator]
    for _ in range(500):
        if sink.external_id_patches:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _run_and_cancel(coro_fn: object) -> None:
    """
    Start a forwarder coroutine, let it tick once, then cancel.

    :param coro_fn: Async callable returning the forwarder coroutine.
    :returns: None after cancellation.
    """
    import contextlib

    task = asyncio.create_task(coro_fn())  # type: ignore[operator]
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_discover_excludes_own_bridge_dir_claim(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    The forwarder's own bridge-dir claim does not block its own rediscovery.

    On a restart this forwarder's bridge dir may already record the id; the
    claim scan must skip *its own* dir so it can re-bind the same conversation
    (only *other* bridges' claims exclude a candidate).

    :param monkeypatch: pytest monkeypatch fixture.
    :param _isolate_bridge_and_brain: Per-test brain root (autouse isolation).
    :returns: None.
    """
    brain = _isolate_bridge_and_brain
    cid = "77777777-7777-4777-8777-777777777777"
    _make_brain_dir(brain, cid)
    my_bridge = prepare_bridge_dir("bridge-mine")
    # Our own bridge already claims the id (e.g. a restart after first discovery).
    write_bridge_state(
        my_bridge,
        AntigravityNativeBridgeState(session_id="conv_mine", conversation_id=cid),
    )
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda c: True)

    assert (
        forwarder._discover_conversation_id(
            since=0.0, bridge_dir=my_bridge, pane=None, ambiguity_deadline=None
        )
        == cid
    )


# ── Finding 1: durable restart/resume dedup cursor ─────────────────────────


def test_parser_seeds_high_water_from_initial(_isolate_bridge_and_brain: Path) -> None:
    """
    A parser seeded with an initial high-water suppresses steps at/below it.

    On a (re)start the parser is constructed with the persisted
    ``forwarded_step_index``; steps already mirrored (index ≤ seed) must produce
    no events, and only strictly-greater steps post.
    """
    parser = TranscriptParser(conversation_id=_CID, emit_status=False, initial_step_high_water=3)
    # step_index 2 and 3 were already mirrored before the restart → suppressed.
    assert _feed_jsonl(parser, [_planner_text_step(text="old2", step_index=2)]) == []
    assert _feed_jsonl(parser, [_planner_text_step(text="old3", step_index=3)]) == []
    # step_index 4 is new → posts, and the high-water advances to 4.
    events = _feed_jsonl(parser, [_planner_text_step(text="new4", step_index=4)])
    assert any(e.event_type == "external_conversation_item" for e in events)
    assert parser.step_high_water == 4


def test_parser_emits_out_of_order_lower_step(_isolate_bridge_and_brain: Path) -> None:
    """A ``step_index`` written AFTER a higher index (out of order) is NOT dropped.

    agy 1.0.10 can write the transcript with ``step_index`` out of order (verified
    live: step 14 before step 13). A strict high-water dedup would suppress the
    later-but-lower index as a false duplicate and silently drop that step from
    the mirror; the per-run seen-set must emit it instead.
    """
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)
    # Real out-of-order shape: 12, then 14, then 13 (lower, AFTER 14), then 15.
    out = _feed_jsonl(
        parser,
        [
            _planner_text_step(text="s12", step_index=12),
            _planner_text_step(text="s14", step_index=14),
            _planner_text_step(text="s13", step_index=13),
            _planner_text_step(text="s15", step_index=15),
        ],
    )
    posted = {e.step_index for e in out if e.event_type == "external_conversation_item"}
    assert posted == {12, 13, 14, 15}  # step 13 emitted, not dropped
    # A genuine re-read of an already-emitted step IS still suppressed (dedup).
    assert _feed_jsonl(parser, [_planner_text_step(text="s13-again", step_index=13)]) == []


async def test_restart_with_persisted_cursor_emits_only_new_steps(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    Regression (Finding 1): a restart re-reads from offset 0 but re-emits
    nothing for already-mirrored steps and posts only steps beyond the cursor.

    Simulates the real failure: the forwarder runs once (mirroring a transcript
    prefix and persisting its cursor), then a crash/--resume reconstructs the
    forwarder against the SAME on-disk transcript (offset resets to 0). With the
    persisted cursor it must NOT duplicate the already-mirrored prefix; when agy
    later appends a new step, only that step is mirrored.
    """
    brain = _isolate_bridge_and_brain
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)
    bridge_dir = prepare_bridge_dir("bridge-restart")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_restart",
            conversation_id="agy_conv_minted",
        ),
    )
    transcript = _write_transcript(
        brain,
        _CID,
        [
            _user_input_step(text="hi", step_index=0),
            _planner_text_step(text="first answer", step_index=2),
        ],
    )

    async def _fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    # ── First run: mirror the prefix, persist the cursor, then cancel. ──
    sink1 = _EventSink()

    async def _run_first() -> None:
        await forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_restart",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=sink1.transport(),
            transcript_discovery_timeout_s=5.0,
        )

    task1 = asyncio.create_task(_run_first())
    for _ in range(400):
        state = read_bridge_state(bridge_dir)
        if state is not None and state.forwarded_step_index == 2:
            break
        await asyncio.sleep(0.005)
    task1.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task1

    first_items = [d for t, d in sink1.events if t == "external_conversation_item"]
    assert first_items, "first run should have mirrored the prefix"
    # Cursor persisted at the highest mirrored step index.
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.conversation_id == _CID
    assert state.forwarded_step_index == 2

    # ── Append a new step, then RESTART against the same transcript (offset 0). ──
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_planner_text_step(text="second answer", step_index=4)) + "\n")

    sink2 = _EventSink()

    async def _run_second() -> None:
        await forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_restart",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=sink2.transport(),
            transcript_discovery_timeout_s=5.0,
        )

    task2 = asyncio.create_task(_run_second())
    for _ in range(400):
        if any(t == "external_conversation_item" for t, _ in sink2.events):
            break
        await asyncio.sleep(0.005)
    task2.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task2

    # The restart must NOT re-post the already-mirrored prefix (steps 0, 2);
    # only the newly appended step 4 is emitted.
    second_item_texts = [
        d["item_data"]["content"][0]["text"]
        for t, d in sink2.events
        if t == "external_conversation_item"
        and isinstance(d.get("item_data"), dict)
        and d["item_data"].get("content")
    ]
    assert "first answer" not in second_item_texts, "restart must not duplicate mirrored prefix"
    assert "hi" not in second_item_texts
    assert second_item_texts == ["second answer"]
    # Cursor advanced to the new step.
    state2 = read_bridge_state(bridge_dir)
    assert state2 is not None
    assert state2.forwarded_step_index == 4


async def test_restart_with_no_new_steps_emits_nothing(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    A restart with a fully-mirrored transcript (no new steps) posts no items.

    The pure "resume re-mirrors nothing" case: the whole transcript is already
    behind the persisted cursor, so a from-0 re-read yields zero conversation
    items (status edges are allowed but no duplicated content).
    """
    brain = _isolate_bridge_and_brain
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)
    bridge_dir = prepare_bridge_dir("bridge-resume-noop")
    # Bridge state already records the discovered id AND a cursor past the last
    # step — i.e. a resume of a fully-mirrored conversation.
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_resume",
            conversation_id=_CID,
            forwarded_step_index=2,
        ),
    )
    _write_transcript(
        brain,
        _CID,
        [
            _user_input_step(text="hi", step_index=0),
            _planner_text_step(text="answer", step_index=2),
        ],
    )
    sink = _EventSink()

    async def _fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    task = asyncio.create_task(
        forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_resume",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=sink.transport(),
            transcript_discovery_timeout_s=5.0,
        )
    )
    # Let it run several poll cycles so any (buggy) re-post would land.
    for _ in range(50):
        await asyncio.sleep(0.005)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    item_events = [t for t, _ in sink.events if t == "external_conversation_item"]
    assert item_events == [], "resume of a fully-mirrored transcript must emit no items"


# ── Finding 1: durable cursor reflects DELIVERY, not PARSE ─────────────────


def _event(event_type: str, step_index: int, *, text: str = "x") -> OutboundEvent:
    """
    Build a minimal OutboundEvent for the delivery-layer tests.

    :param event_type: The Omnigent event type.
    :param step_index: The step the event belongs to.
    :param text: Marker text embedded in the payload (for identifiability).
    :returns: An OutboundEvent stamped with ``step_index``.
    """
    return OutboundEvent(event_type=event_type, data={"text": text}, step_index=step_index)


class _FakeResponse:
    """
    Minimal httpx.Response stand-in exposing ``status_code`` and ``text``.

    :param status_code: The HTTP status to report.
    """

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = "body"


async def test_post_events_stops_cursor_at_first_failed_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Regression (Finding 1): ``_post_events`` advances the contiguous delivered
    high-water only up to the step BEFORE the first failed POST.

    A batch spans steps 2 (ok), 4 (POST fails), 6 (ok). The durable cursor must
    report 2 (the last contiguously-delivered step), never 6 — so a resume re-
    posts steps 4 and 6 rather than permanently skipping the failed step 4.
    """

    async def _fake_post(
        client: object, session_id: str, *, event_type: str, data: dict[str, Any]
    ) -> object:
        # Step 4 fails delivery (non-2xx); all other steps deliver.
        if data.get("step") == 4:
            return _FakeResponse(500)
        return _FakeResponse(200)

    monkeypatch.setattr(forwarder, "_post_session_event", _fake_post)

    events = [
        OutboundEvent("external_conversation_item", {"step": 2}, step_index=2),
        OutboundEvent("external_conversation_item", {"step": 4}, step_index=4),
        OutboundEvent("external_conversation_item", {"step": 6}, step_index=6),
    ]
    delivery = await forwarder._post_events(object(), "conv_x", events)  # type: ignore[arg-type]
    assert delivery.contiguous_high_water == 2
    assert delivery.fully_delivered is False


async def test_post_events_none_response_is_not_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``None`` from the retry helper (ambiguous transport failure) counts as NOT
    delivered, so the cursor does not advance past that step.
    """

    async def _fake_post(
        client: object, session_id: str, *, event_type: str, data: dict[str, Any]
    ) -> object | None:
        if data.get("step") == 4:
            return None  # ambiguous conversation-item failure → not retried
        return _FakeResponse(200)

    monkeypatch.setattr(forwarder, "_post_session_event", _fake_post)

    events = [
        OutboundEvent("external_conversation_item", {"step": 2}, step_index=2),
        OutboundEvent("external_conversation_item", {"step": 4}, step_index=4),
    ]
    delivery = await forwarder._post_events(object(), "conv_x", events)  # type: ignore[arg-type]
    assert delivery.contiguous_high_water == 2
    assert delivery.fully_delivered is False


async def test_post_events_partial_step_failure_excludes_that_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A step only commits when ALL its events deliver: if step 4 emits two events
    and the second fails, step 4 is excluded (cursor stays at the prior step 2).
    """
    calls: list[tuple[str, int]] = []

    async def _fake_post(
        client: object, session_id: str, *, event_type: str, data: dict[str, Any]
    ) -> object:
        calls.append((event_type, data["step"]))
        # The function_call_output of step 4 fails; its message succeeds.
        if data["step"] == 4 and event_type == "external_conversation_item" and data.get("second"):
            return _FakeResponse(503)
        return _FakeResponse(200)

    monkeypatch.setattr(forwarder, "_post_session_event", _fake_post)

    events = [
        OutboundEvent("external_conversation_item", {"step": 2}, step_index=2),
        OutboundEvent("external_conversation_item", {"step": 4, "second": False}, step_index=4),
        OutboundEvent("external_conversation_item", {"step": 4, "second": True}, step_index=4),
    ]
    delivery = await forwarder._post_events(object(), "conv_x", events)  # type: ignore[arg-type]
    assert delivery.contiguous_high_water == 2
    assert delivery.fully_delivered is False


async def test_post_events_all_delivered_advances_fully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every POST succeeds, the cursor advances to the last step."""

    async def _fake_post(
        client: object, session_id: str, *, event_type: str, data: dict[str, Any]
    ) -> object:
        return _FakeResponse(200)

    monkeypatch.setattr(forwarder, "_post_session_event", _fake_post)
    events = [_event("external_conversation_item", 2), _event("external_conversation_item", 5)]
    delivery = await forwarder._post_events(object(), "conv_x", events)  # type: ignore[arg-type]
    assert delivery.contiguous_high_water == 5
    assert delivery.fully_delivered is True


async def test_post_events_out_of_order_all_delivered_advances_to_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-order steps that ALL deliver advance the cursor to the highest
    index, not the last one processed.

    agy 1.0.10 writes step_index out of order (e.g. 14 before 13). A positional
    scan would commit step 13 (the last closed) and under-advance, re-posting
    14,15 on restart; the value-based gap-free watermark commits 15.
    """

    async def _fake_post(
        client: object, session_id: str, *, event_type: str, data: dict[str, Any]
    ) -> object:
        return _FakeResponse(200)

    monkeypatch.setattr(forwarder, "_post_session_event", _fake_post)
    events = [
        OutboundEvent("external_conversation_item", {"step": 12}, step_index=12),
        OutboundEvent("external_conversation_item", {"step": 14}, step_index=14),
        OutboundEvent("external_conversation_item", {"step": 13}, step_index=13),
        OutboundEvent("external_conversation_item", {"step": 15}, step_index=15),
    ]
    delivery = await forwarder._post_events(object(), "conv_x", events)  # type: ignore[arg-type]
    assert delivery.contiguous_high_water == 15
    assert delivery.fully_delivered is True


async def test_post_events_out_of_order_failed_lower_step_freezes_below_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An out-of-order LOWER step that fails after a higher step delivered freezes
    the cursor BELOW the failed step, not at the already-delivered higher one —
    otherwise the failed step is silently dropped on resume (the resume floor
    would suppress it).
    """

    async def _fake_post(
        client: object, session_id: str, *, event_type: str, data: dict[str, Any]
    ) -> object:
        # Step 13 (written AFTER 14) fails; 12, 14, 15 succeed.
        return _FakeResponse(503 if data["step"] == 13 else 200)

    monkeypatch.setattr(forwarder, "_post_session_event", _fake_post)
    events = [
        OutboundEvent("external_conversation_item", {"step": 12}, step_index=12),
        OutboundEvent("external_conversation_item", {"step": 14}, step_index=14),
        OutboundEvent("external_conversation_item", {"step": 13}, step_index=13),
        OutboundEvent("external_conversation_item", {"step": 15}, step_index=15),
    ]
    delivery = await forwarder._post_events(object(), "conv_x", events)  # type: ignore[arg-type]
    # Highest delivered step below the lowest failed step (13) -> 12, NOT 14.
    assert delivery.contiguous_high_water == 12
    assert delivery.fully_delivered is False


def test_highest_step_below_handles_out_of_order_events() -> None:
    """The audit freeze ceiling takes the max step_index below the boundary even
    when events arrive out of order (agy 1.0.10) — not a positional scan that
    stops at the first index >= boundary, which would lower the ceiling and
    re-warn an already-audited step on restart.
    """
    events = [
        _event("external_conversation_item", 12),
        _event("external_conversation_item", 14),  # >= boundary, mid-list
        _event("external_conversation_item", 13),  # < boundary, arrives AFTER 14
    ]
    # boundary=14: highest delivered step below 14 is 13, despite 14 preceding it.
    assert forwarder._highest_step_below(events, 14) == 13
    # No step strictly below the boundary -> None.
    assert forwarder._highest_step_below(events, 12) is None


async def test_tail_persists_delivered_cursor_below_failed_step(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    Regression (Finding 1) end-to-end: when step 4's POST FAILS during tailing,
    the persisted ``forwarded_step_index`` stays BELOW 4 (it advances only to the
    last contiguously-delivered step, 2), so a subsequent restart/resume re-posts
    step 4 instead of permanently skipping it.

    The transcript has user(0) → planner(2) → planner(4). The event sink fails
    every POST whose payload belongs to step 4 (``response_id``/``message_id``
    carry the index). The parser's PARSE high-water reaches 4, but the DURABLE
    cursor must not.
    """
    brain = _isolate_bridge_and_brain
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)
    bridge_dir = prepare_bridge_dir("bridge-faildeliver")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_fail",
            conversation_id="agy_conv_minted",
        ),
    )
    _write_transcript(
        brain,
        _CID,
        [
            _user_input_step(text="hi", step_index=0),
            _planner_text_step(text="first", step_index=2),
            _planner_text_step(text="second", step_index=4),
        ],
    )

    async def _fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    def _belongs_to_step4(data: dict[str, Any]) -> bool:
        # Conversation items carry response_id ``agy_<cid>_<step>``; the planner
        # delta carries message_id ``antigravity:<cid>:<step>:planner``.
        response_id = data.get("response_id")
        if isinstance(response_id, str) and response_id.endswith("_4"):
            return True
        message_id = data.get("message_id")
        return isinstance(message_id, str) and f":{4}:" in message_id

    async def _fake_post(
        client: object, session_id: str, *, event_type: str, data: dict[str, Any]
    ) -> httpx.Response | None:
        if _belongs_to_step4(data):
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(forwarder, "_post_session_event", _fake_post)
    sink = _EventSink()

    task = asyncio.create_task(
        forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_fail",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=sink.transport(),
            transcript_discovery_timeout_s=5.0,
        )
    )
    # Wait until the cursor has been persisted at the delivered prefix (step 2).
    for _ in range(400):
        state = read_bridge_state(bridge_dir)
        if state is not None and state.forwarded_step_index == 2:
            break
        await asyncio.sleep(0.005)
    # Give several extra poll cycles so any (buggy) advance past 4 would land.
    for _ in range(20):
        await asyncio.sleep(0.005)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    state = read_bridge_state(bridge_dir)
    assert state is not None
    # The durable cursor must NOT have advanced to or past the failed step 4.
    assert state.forwarded_step_index is not None
    assert state.forwarded_step_index < 4, (
        f"durable cursor advanced to {state.forwarded_step_index}, past the failed "
        f"step 4 — a resume would permanently skip step 4 (silent data loss)"
    )
    assert state.forwarded_step_index == 2


async def test_tail_cursor_frozen_after_failure_even_if_later_step_delivers(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    Regression (Finding 1): once step 4 fails, a LATER step 6 appended afterward
    must NOT advance the durable cursor past 4 within the same run — step 4 is
    never retried in-run, so advancing past it would skip it on resume.

    First the transcript has steps 0,2,4 with step 4's POST failing → cursor
    freezes at 2. Then step 6 is appended (its POST would succeed); the cursor
    must REMAIN at 2 for the rest of the run (the gap at step 4 is unfilled).
    """
    brain = _isolate_bridge_and_brain
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)
    bridge_dir = prepare_bridge_dir("bridge-frozen")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_frozen",
            conversation_id="agy_conv_minted",
        ),
    )
    transcript = _write_transcript(
        brain,
        _CID,
        [
            _user_input_step(text="hi", step_index=0),
            _planner_text_step(text="first", step_index=2),
            _planner_text_step(text="second", step_index=4),
        ],
    )

    async def _fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    async def _fake_post(
        client: object, session_id: str, *, event_type: str, data: dict[str, Any]
    ) -> httpx.Response:
        response_id = data.get("response_id")
        message_id = data.get("message_id")
        is_step4 = (isinstance(response_id, str) and response_id.endswith("_4")) or (
            isinstance(message_id, str) and ":4:" in message_id
        )
        return httpx.Response(500) if is_step4 else httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(forwarder, "_post_session_event", _fake_post)
    sink = _EventSink()

    task = asyncio.create_task(
        forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_frozen",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=sink.transport(),
            transcript_discovery_timeout_s=5.0,
        )
    )
    for _ in range(400):
        state = read_bridge_state(bridge_dir)
        if state is not None and state.forwarded_step_index == 2:
            break
        await asyncio.sleep(0.005)

    # Append a NEW step 6 whose POST would succeed.
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_planner_text_step(text="third", step_index=6)) + "\n")

    # Let many poll cycles run so step 6 is read and posted.
    for _ in range(40):
        await asyncio.sleep(0.005)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    state = read_bridge_state(bridge_dir)
    assert state is not None
    # Cursor stays frozen at 2 despite step 6 delivering — the step-4 gap blocks
    # any further advance for the rest of the run.
    assert state.forwarded_step_index == 2, (
        f"cursor advanced to {state.forwarded_step_index} after a later step "
        f"delivered, but the step-4 gap must freeze it at 2 for the run"
    )


# ── Finding 2: deterministic process-tie discovery ─────────────────────────


def _pane() -> PaneTarget:
    """
    Build a PaneTarget for tests (the socket/target values are never dialed —
    the tmux/process seams are stubbed).

    :returns: A PaneTarget with placeholder socket/target.
    """
    return PaneTarget(tmux_socket=Path("/tmp/agy-test.sock"), tmux_target="main")


def test_discover_deterministic_binds_pane_owned_among_ambiguous(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    Deterministic bind: with TWO unclaimed candidates, the one THIS session's
    pane-owned agy hosts is bound — no ambiguity, no livelock.

    This is the core Finding-2 fix: under two near-simultaneous same-host
    launches both brain dirs are in-window and unclaimed; the old code refused
    forever. Tying to this session's own agy pid resolves the right conversation
    deterministically.
    """
    brain = _isolate_bridge_and_brain
    ours = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    theirs = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    _make_brain_dir(brain, ours)
    _make_brain_dir(brain, theirs)
    my_bridge = prepare_bridge_dir("bridge-mine")

    # This session's pane resolves to agy pid 5000; that pid's port owns ``ours``.
    monkeypatch.setattr(forwarder, "_tmux_pane_pid", lambda pane: 4000)
    monkeypatch.setattr(forwarder, "_agy_pids_under_pane", lambda pane_pid: [5000])

    seen: list[tuple[int, list[str]]] = []

    def _owned_by_pid(pid: int, candidates: list[str]) -> str | None:
        seen.append((pid, list(candidates)))
        return ours if pid == 5000 else None

    monkeypatch.setattr(forwarder, "conversation_id_owned_by_pid", _owned_by_pid)
    # The fallback ownership probe must NOT be consulted on the deterministic path.
    monkeypatch.setattr(
        forwarder,
        "_conversation_is_owned_by_live_agy",
        lambda cid: pytest.fail("fallback ownership probe must not run with a pane"),
    )

    discovered = forwarder._discover_conversation_id(
        since=0.0, bridge_dir=my_bridge, pane=_pane(), ambiguity_deadline=None
    )
    assert discovered == ours
    # The by-pid resolver received this session's agy pid and both candidates.
    assert seen and seen[0][0] == 5000
    assert sorted(seen[0][1]) == sorted([ours, theirs])


def test_conversation_id_for_pane_skips_wrapper_picks_real_ls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Finding 2: a pane with a wrapper ``bin/agy`` (no live LS port) AND the real
    ``bin/agy`` (LS port serving the conversation) selects the REAL one.

    The pane→agy walk can surface several ``bin/agy`` processes; the wrapper
    appears first in tree order. The old code returned that first match and would
    have probed the wrong (portless) process and given up. The fix probes every
    descendant and binds the one whose connect-RPC port confirms a candidate —
    so the wrapper is skipped and the language-server process wins.
    """
    cid = "abcdabcd-abcd-4bcd-8bcd-abcdabcdabcd"
    wrapper_pid = 5000
    real_ls_pid = 5001
    monkeypatch.setattr(forwarder, "_tmux_pane_pid", lambda pane: 4000)
    # Both wrapper and real LS match ``bin/agy``; wrapper is first in tree order.
    monkeypatch.setattr(
        forwarder, "_agy_pids_under_pane", lambda pane_pid: [wrapper_pid, real_ls_pid]
    )

    probed: list[int] = []

    def _owned_by_pid(pid: int, candidates: list[str]) -> str | None:
        # The wrapper has no live LS port → owns nothing; the real LS process
        # owns the conversation. Mirrors ``conversation_id_owned_by_pid``'s real
        # behavior (it returns ``None`` when ``discover_language_server_port``
        # finds no port for the pid).
        probed.append(pid)
        return cid if pid == real_ls_pid else None

    monkeypatch.setattr(forwarder, "conversation_id_owned_by_pid", _owned_by_pid)

    resolved = forwarder._conversation_id_for_pane(_pane(), [cid])
    assert resolved == cid
    # The wrapper was probed first (and rejected), then the real LS process.
    assert probed == [wrapper_pid, real_ls_pid]


def test_conversation_id_for_pane_none_when_no_descendant_owns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With several agy descendants but none owning a candidate yet (cold start /
    ports not bound), ``_conversation_id_for_pane`` returns ``None`` so the
    caller keeps polling — every descendant is probed before giving up.
    """
    monkeypatch.setattr(forwarder, "_tmux_pane_pid", lambda pane: 4000)
    monkeypatch.setattr(forwarder, "_agy_pids_under_pane", lambda pane_pid: [5000, 5001])
    probed: list[int] = []

    def _owned_by_pid(pid: int, candidates: list[str]) -> str | None:
        probed.append(pid)
        return None

    monkeypatch.setattr(forwarder, "conversation_id_owned_by_pid", _owned_by_pid)
    cold_cid = "zzzzzzzz-zzzz-4zzz-8zzz-zzzzzzzzzzzz"
    assert forwarder._conversation_id_for_pane(_pane(), [cold_cid]) is None
    assert probed == [5000, 5001]


def test_discover_deterministic_waits_until_agy_child_appears(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    Deterministic path returns ``None`` (keep polling) until the pane's agy
    child exists.

    With ``tmux_start_on_attach`` agy does not exist until the client attaches,
    so the pane pid resolves but has no agy descendant yet. Discovery must wait
    rather than bind, and must never fall back to a guess while a pane is set.
    """
    brain = _isolate_bridge_and_brain
    cid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    _make_brain_dir(brain, cid)
    my_bridge = prepare_bridge_dir("bridge-cold")

    monkeypatch.setattr(forwarder, "_tmux_pane_pid", lambda pane: 4000)
    # agy has not started under the pane yet.
    monkeypatch.setattr(forwarder, "_agy_pids_under_pane", lambda pane_pid: [])
    monkeypatch.setattr(
        forwarder,
        "conversation_id_owned_by_pid",
        lambda pid, candidates: pytest.fail("must not probe by-pid without an agy pid"),
    )

    assert (
        forwarder._discover_conversation_id(
            since=0.0, bridge_dir=my_bridge, pane=_pane(), ambiguity_deadline=None
        )
        is None
    )


def test_discover_deterministic_returns_none_when_no_pane_pid(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    With a pane configured but ``tmux list-panes`` yielding no pid, discovery
    returns ``None`` (keep polling) — never the fallback guess.
    """
    brain = _isolate_bridge_and_brain
    _make_brain_dir(brain, "dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    my_bridge = prepare_bridge_dir("bridge-nopid")
    monkeypatch.setattr(forwarder, "_tmux_pane_pid", lambda pane: None)
    monkeypatch.setattr(
        forwarder,
        "_agy_pids_under_pane",
        lambda pane_pid: pytest.fail("must not walk the tree without a pane pid"),
    )
    assert (
        forwarder._discover_conversation_id(
            since=0.0, bridge_dir=my_bridge, pane=_pane(), ambiguity_deadline=None
        )
        is None
    )


def test_discover_fallback_raises_after_ambiguity_deadline(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    Loud fallback (no pane): sustained ambiguity past the deadline raises rather
    than livelocking forever.

    When there is no pane to deterministically bind AND more than one unclaimed
    candidate persists past ``ambiguity_deadline``, the old silent indefinite
    refusal becomes a surfaced :class:`AmbiguousDiscoveryError` (the supervisor
    retries). A deadline already in the past forces the raise immediately.
    """
    brain = _isolate_bridge_and_brain
    _make_brain_dir(brain, "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    _make_brain_dir(brain, "ffffffff-ffff-4fff-8fff-ffffffffffff")
    my_bridge = prepare_bridge_dir("bridge-loud")
    with pytest.raises(AmbiguousDiscoveryError) as excinfo:
        forwarder._discover_conversation_id(
            since=0.0,
            bridge_dir=my_bridge,
            pane=None,
            ambiguity_deadline=time.monotonic() - 1.0,  # already past
        )
    assert "could not deterministically bind" in str(excinfo.value)


def test_discover_fallback_refuses_before_deadline(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    Fallback ambiguity before the deadline still refuses (returns ``None``) so a
    transient concurrent-launch race can resolve itself first.
    """
    brain = _isolate_bridge_and_brain
    _make_brain_dir(brain, "1a1a1a1a-1a1a-4a1a-8a1a-1a1a1a1a1a1a")
    _make_brain_dir(brain, "2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b")
    my_bridge = prepare_bridge_dir("bridge-wait")
    assert (
        forwarder._discover_conversation_id(
            since=0.0,
            bridge_dir=my_bridge,
            pane=None,
            ambiguity_deadline=time.monotonic() + 60.0,  # not yet
        )
        is None
    )


def test_pane_target_from_tmux_requires_existing_socket(tmp_path: Path) -> None:
    """
    ``_pane_target_from_tmux`` builds a PaneTarget only when the socket exists.

    A remote runner's socket does not exist locally, so the helper returns
    ``None`` and the forwarder uses the bounded-ambiguity fallback instead of
    trying to ``tmux list-panes`` a socket it cannot reach.
    """
    # Missing socket → None.
    assert forwarder._pane_target_from_tmux(tmp_path / "absent.sock", "main") is None
    # Missing pieces → None.
    assert forwarder._pane_target_from_tmux(None, "main") is None
    assert forwarder._pane_target_from_tmux(tmp_path / "x.sock", None) is None
    # Existing socket + target → a PaneTarget.
    sock = tmp_path / "tmux.sock"
    sock.write_text("")
    pane = forwarder._pane_target_from_tmux(sock, "main")
    assert pane == PaneTarget(tmux_socket=sock, tmux_target="main")


class _FakeProc:
    """
    Minimal psutil.Process stand-in for the process-tree walk tests.

    :param pid: The process id.
    :param cmdline: The argv list ``cmdline()`` returns.
    :param children: Descendants ``children(recursive=True)`` returns.
    """

    def __init__(self, pid: int, cmdline: list[str], children: list[_FakeProc]) -> None:
        self.pid = pid
        self._cmdline = cmdline
        self._children = children

    def cmdline(self) -> list[str]:
        """Return the fake argv."""
        return self._cmdline

    def children(self, recursive: bool = False) -> list[_FakeProc]:
        """Return the fake descendants (recursive list is precomputed)."""
        return self._children


def test_agy_pids_under_pane_finds_descendant(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``_agy_pids_under_pane`` returns the agy descendant(s) of a pane process.

    The pane shell spawns agy as a child; the walk matches the agy binary marker
    against each descendant's argv and returns the pids in tree order.
    """
    agy = _FakeProc(5000, ["/home/u/.local/bin/agy", "--conversation", "x"], [])
    shell = _FakeProc(4000, ["-bash"], [agy])
    monkeypatch.setattr("omnigent.antigravity_native_forwarder.psutil.Process", lambda pid: shell)
    assert forwarder._agy_pids_under_pane(4000) == [5000]


def test_agy_pids_under_pane_empty_when_no_agy_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``_agy_pids_under_pane`` returns ``[]`` when no descendant looks like agy.

    Before the user attaches (``tmux_start_on_attach``), the pane runs only a
    ``tmux wait-for`` shell with no agy child — discovery must keep polling.
    """
    shell = _FakeProc(4000, ["bash", "-c", "tmux wait-for x"], [])
    monkeypatch.setattr("omnigent.antigravity_native_forwarder.psutil.Process", lambda pid: shell)
    assert forwarder._agy_pids_under_pane(4000) == []


def test_agy_pids_under_pane_returns_all_matches_in_tree_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Finding 2: a pane with a wrapper ``bin/agy`` plus the real LS ``bin/agy``
    returns BOTH pids (in tree order) so the caller can pick the right one.

    The old single-match walk returned only the first cmdline match, which could
    be the wrapper/launcher rather than the language-server process that owns the
    connect-RPC port.
    """
    wrapper = _FakeProc(5000, ["/home/u/.local/bin/agy", "launch"], [])
    real_ls = _FakeProc(5001, ["/home/u/.local/bin/agy", "--language-server"], [])
    # Tree order: pane shell, then the wrapper, then the real LS process.
    shell = _FakeProc(4000, ["-bash"], [wrapper, real_ls])
    monkeypatch.setattr("omnigent.antigravity_native_forwarder.psutil.Process", lambda pid: shell)
    assert forwarder._agy_pids_under_pane(4000) == [5000, 5001]


# ── Post-hoc policy audit (tasks 2-4) ──────────────────────────────────────


class _AuditSink:
    """
    Records ``/events`` POSTs and serves ``/policies/evaluate`` with a verdict.

    :param verdict: The ``EvaluationResponse`` body returned for every
        ``/policies/evaluate`` POST.
    """

    def __init__(self, verdict: dict[str, Any]) -> None:
        """Initialize the sink with a fixed evaluate verdict."""
        self.verdict = verdict
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.eval_requests: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        """
        Return a MockTransport recording events and serving evaluate verdicts.

        :returns: A MockTransport for the audit-enabled forwarder client.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            if request.url.path.endswith("/policies/evaluate"):
                self.eval_requests.append(body)
                return httpx.Response(200, json=self.verdict)
            if request.method == "PATCH":
                return httpx.Response(200, json={"ok": True})
            self.events.append((body["type"], body["data"]))
            return httpx.Response(200, json={"ok": True})

        return httpx.MockTransport(handler)


def test_audit_tool_calls_from_events_decodes_function_calls() -> None:
    """The audit reuses posted function_call items, decoding their arguments."""
    events = step_to_events(
        _planner_tool_step(step_index=2), conversation_id=_CID, allocator=_allocator()
    )
    records = _audit_tool_calls_from_events(events)
    assert len(records) == 1
    step_index, call_ordinal, name, tool_input = records[0]
    assert step_index == 2
    assert call_ordinal == 0
    assert name == "list_dir"
    # Display-only keys were stripped when the function_call item was built.
    assert tool_input == {"DirectoryPath": "/Users/bryanli/Projects/askcv.ai"}


def test_audit_tool_calls_ignores_non_function_call_events() -> None:
    """Message / status events are not audited as tool calls."""
    events = step_to_events(
        _planner_text_step(step_index=2), conversation_id=_CID, allocator=_allocator()
    )
    assert _audit_tool_calls_from_events(events) == []


async def test_audit_batch_posts_warning_on_deny() -> None:
    """A DENY verdict posts a [Policy violation] warning conversation item."""
    sink = _AuditSink({"result": "POLICY_ACTION_DENY", "reason": "no shell"})
    events = step_to_events(
        _planner_tool_step(step_index=2), conversation_id=_CID, allocator=_allocator()
    )
    async with httpx.AsyncClient(base_url="http://test", transport=sink.transport()) as client:
        await _audit_batch(
            client, "conv_x", conversation_id=_CID, events=events, model="gemini-2.5-pro"
        )

    # The evaluate request was sent on the tool_call phase with harness + model.
    assert len(sink.eval_requests) == 1
    event = sink.eval_requests[0]["event"]
    assert event["type"] == "PHASE_TOOL_CALL"
    assert event["context"]["harness"] == "antigravity-native"
    assert event["context"]["model"] == "gemini-2.5-pro"
    # A single warning conversation item was posted.
    warnings = [
        d
        for t, d in sink.events
        if t == "external_conversation_item" and d.get("item_type") == "message"
    ]
    assert len(warnings) == 1
    text = warnings[0]["item_data"]["content"][0]["text"]
    assert "no shell" in text
    assert "already executed" in text


async def test_audit_batch_treats_ask_as_violation() -> None:
    """An ASK verdict is surfaced DENY-style (the tool already ran)."""
    sink = _AuditSink({"result": "POLICY_ACTION_ASK", "reason": "confirm?"})
    events = step_to_events(
        _planner_tool_step(step_index=2), conversation_id=_CID, allocator=_allocator()
    )
    async with httpx.AsyncClient(base_url="http://test", transport=sink.transport()) as client:
        await _audit_batch(client, "conv_x", conversation_id=_CID, events=events, model=None)

    warnings = [
        d
        for t, d in sink.events
        if t == "external_conversation_item" and d.get("item_type") == "message"
    ]
    assert len(warnings) == 1


async def test_audit_batch_no_warning_on_allow() -> None:
    """An ALLOW verdict posts no warning item."""
    sink = _AuditSink({"result": "POLICY_ACTION_ALLOW"})
    events = step_to_events(
        _planner_tool_step(step_index=2), conversation_id=_CID, allocator=_allocator()
    )
    async with httpx.AsyncClient(base_url="http://test", transport=sink.transport()) as client:
        await _audit_batch(client, "conv_x", conversation_id=_CID, events=events, model=None)

    assert sink.events == []  # no warning posted on ALLOW


async def test_audit_batch_fail_open_on_eval_error() -> None:
    """A 500 from /policies/evaluate is fail-open: no warning, no raise."""
    events = step_to_events(
        _planner_tool_step(step_index=2), conversation_id=_CID, allocator=_allocator()
    )
    posted: list[str] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/policies/evaluate"):
            return httpx.Response(500, json={"error": "boom"})
        body = json.loads(request.content) if request.content else {}
        posted.append(body["type"])
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(recording_handler)
    ) as client:
        # Must not raise.
        await _audit_batch(client, "conv_x", conversation_id=_CID, events=events, model=None)

    assert posted == []  # fail-open → no warning


def test_audit_tool_calls_assigns_per_step_call_ordinal() -> None:
    """Two tool calls in one step get ordinals 0 and 1 (reset per step_index)."""
    events = step_to_events(
        _planner_two_tool_step(step_index=2), conversation_id=_CID, allocator=_allocator()
    )
    records = _audit_tool_calls_from_events(events)
    assert len(records) == 2
    assert [(step, ordinal) for step, ordinal, _name, _input in records] == [(2, 0), (2, 1)]


async def test_audit_batch_distinct_response_ids_for_two_violations_in_one_step() -> None:
    """
    Two DENY'd tool calls in ONE step post two warnings with DISTINCT response
    ids (keyed on the per-call ordinal), so neither clobbers the other.
    """
    sink = _AuditSink({"result": "POLICY_ACTION_DENY", "reason": "no rm -rf"})
    events = step_to_events(
        _planner_two_tool_step(step_index=2), conversation_id=_CID, allocator=_allocator()
    )
    async with httpx.AsyncClient(base_url="http://test", transport=sink.transport()) as client:
        await _audit_batch(client, "conv_x", conversation_id=_CID, events=events, model=None)

    warning_ids = [
        d["response_id"]
        for t, d in sink.events
        if t == "external_conversation_item" and d.get("item_type") == "message"
    ]
    assert warning_ids == [f"agy_{_CID}_2_0_policy", f"agy_{_CID}_2_1_policy"]
    assert len(set(warning_ids)) == 2  # distinct — no collision


def _policy_warning_texts(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """
    Extract the [Policy violation] warning texts from recorded session events.

    :param events: ``(type, data)`` tuples recorded by a sink.
    :returns: The text of each posted policy-violation warning, in order.
    """
    texts: list[str] = []
    for event_type, data in events:
        if event_type != "external_conversation_item":
            continue
        if data.get("item_type") != "message":
            continue
        content = data.get("item_data", {}).get("content", [{}])
        text = content[0].get("text", "") if content else ""
        if text.startswith("[Policy violation]"):
            texts.append(text)
    return texts


async def test_audit_is_cursor_durable_on_gap_then_restart(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    Regression (gov FIX 1): the audit advances in LOCKSTEP with the durable
    cursor, so a stalled mid-batch step is not audited until it delivers, and a
    restart does NOT re-audit the already-delivered (already-audited) prefix.

    Transcript: tool(2) → tool(4) → tool(6), every tool call DENY'd. Step 4's POST
    fails (a mid-batch delivery gap), so the cursor freezes at 2 and steps 4/6 are
    NOT audited this run (4 never delivered; 6 is past the gap). Only step 2's
    violation is surfaced. On RESTART against the same transcript (offset 0, step 4
    now delivering) the parser skips step 2 (<= persisted cursor) so it is NOT
    re-audited — no duplicate warning for step 2 — and steps 4 and 6 audit for the
    first time.
    """
    brain = _isolate_bridge_and_brain
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)
    bridge_dir = prepare_bridge_dir("bridge-audit-durable")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="conv_ad", conversation_id="agy_conv_minted"),
    )
    _write_transcript(
        brain,
        _CID,
        [
            _user_input_step(text="hi", step_index=0),
            _planner_tool_step(step_index=2),
            _planner_tool_step(step_index=4),
            _planner_tool_step(step_index=6),
        ],
    )

    async def _fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    def _step_of(data: dict[str, Any]) -> int | None:
        # Conversation items carry response_id ``agy_<cid>_<step>``; the function
        # call item's response_id is ``agy_<cid>_<step>`` (no per-call suffix on
        # the mirrored item — that suffix is only on the audit warning).
        response_id = data.get("response_id")
        if isinstance(response_id, str):
            parts = response_id.rsplit("_", 1)
            if len(parts) == 2 and parts[1].isdigit():
                return int(parts[1])
        return None

    fail_step_holder = {"step": 4}

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/policies/evaluate"):
            return httpx.Response(200, json={"result": "POLICY_ACTION_DENY", "reason": "no shell"})
        if request.method == "PATCH":
            return httpx.Response(200, json={"ok": True})
        data = body["data"]
        # Fail the mirror POST for the configured gap step (function_call item).
        if data.get("item_type") == "function_call" and _step_of(data) == fail_step_holder["step"]:
            return httpx.Response(500, json={"error": "boom"})
        sink_events.append((body["type"], data))
        return httpx.Response(200, json={"ok": True})

    sink_events: list[tuple[str, dict[str, Any]]] = []

    async def _run() -> None:
        await forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_ad",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=httpx.MockTransport(_handler),
            transcript_discovery_timeout_s=5.0,
            audit_policies=True,
        )

    # ── First run: step 4 POST fails → cursor freezes at 2; only step 2 audits. ──
    task1 = asyncio.create_task(_run())
    for _ in range(600):
        state = read_bridge_state(bridge_dir)
        if state is not None and state.forwarded_step_index == 2:
            break
        await asyncio.sleep(0.005)
    for _ in range(20):  # extra cycles so any buggy past-gap audit would land
        await asyncio.sleep(0.005)
    task1.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task1

    first_warnings = _policy_warning_texts(sink_events)
    # Only the contiguously-delivered prefix (step 2) was audited; the stalled
    # step 4 and the past-gap step 6 were NOT audited this run.
    assert len(first_warnings) == 1, (
        f"expected exactly one warning (step 2 only), got {len(first_warnings)}: "
        "the audit must not run on a stalled/past-gap step"
    )
    state = read_bridge_state(bridge_dir)
    assert state is not None and state.forwarded_step_index == 2

    # ── Restart against the same transcript with step 4 now delivering. ──
    fail_step_holder["step"] = -1  # nothing fails now
    sink_events.clear()
    task2 = asyncio.create_task(_run())
    for _ in range(600):
        state = read_bridge_state(bridge_dir)
        if state is not None and state.forwarded_step_index == 6:
            break
        await asyncio.sleep(0.005)
    for _ in range(20):
        await asyncio.sleep(0.005)
    task2.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task2

    restart_warning_ids = [
        d["response_id"]
        for t, d in sink_events
        if t == "external_conversation_item" and d.get("item_type") == "message"
    ]
    # Step 2 was already audited on the first run and is <= the persisted cursor,
    # so the parser skips it: NO duplicate step-2 warning. Steps 4 and 6 audit for
    # the first time.
    assert f"agy_{_CID}_2_0_policy" not in restart_warning_ids, (
        "step 2 was re-audited after restart — a stalled-then-restart must not "
        "produce a duplicate [Policy violation] for an already-audited step"
    )
    assert f"agy_{_CID}_4_0_policy" in restart_warning_ids
    assert f"agy_{_CID}_6_0_policy" in restart_warning_ids


async def test_forwarder_posts_degrade_notice_once_when_audit_enabled(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """With audit on, the one-time audit-only degrade notice is posted."""
    brain = _isolate_bridge_and_brain
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)
    bridge_dir = prepare_bridge_dir("bridge-audit-notice")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="conv_n", conversation_id="agy_conv_minted"),
    )
    _write_transcript(
        brain,
        _CID,
        [
            _user_input_step(text="hi", step_index=0),
            _planner_text_step(text="Hello!", step_index=2),
        ],
    )
    sink = _AuditSink({"result": "POLICY_ACTION_ALLOW"})

    async def _fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    task = asyncio.create_task(
        forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_n",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=sink.transport(),
            transcript_discovery_timeout_s=5.0,
            audit_policies=True,
        )
    )
    notices: list[dict[str, Any]] = []
    for _ in range(200):
        notices = [
            d
            for t, d in sink.events
            if t == "external_conversation_item"
            and d.get("item_data", {}).get("content", [{}])[0].get("text") == DEGRADE_NOTICE_TEXT
        ]
        if notices:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(notices) == 1  # posted exactly once


# ── gov FIX A: at-least-once policy audit (never silently drop a violation) ──


def test_highest_step_below_caps_at_non_contiguous_boundary() -> None:
    """The cursor ceiling is the highest delivered step strictly below the gap."""
    events = [_event("external_conversation_item", s) for s in (0, 2, 4, 6)]
    # Freeze point is step 4: commit 0 and 2, never 4 or 6.
    assert _highest_step_below(events, 4) == 2
    # Freeze point is the first step: nothing may commit.
    assert _highest_step_below(events, 0) is None
    # No gap below any step beyond the last: the whole prefix commits.
    assert _highest_step_below(events, 99) == 6


async def test_audit_batch_returns_freeze_point_on_warning_post_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    gov FIX A (unit): ``_audit_batch`` reports the FIRST step whose violation
    warning POST failed as the cursor freeze point, and ``None`` when all warned.
    """
    # Two violating tool-call steps (2 and 4), both DENY.
    events = [
        *step_to_events(
            _planner_tool_step(step_index=2), conversation_id=_CID, allocator=_allocator()
        ),
        *step_to_events(
            _planner_tool_step(step_index=4), conversation_id=_CID, allocator=_allocator()
        ),
    ]

    async def _eval(
        client: object, session_id: str, *, tool_name: str, tool_input: dict[str, Any], model: Any
    ) -> dict[str, Any]:
        return {"result": "POLICY_ACTION_DENY", "reason": "no shell"}

    monkeypatch.setattr(forwarder, "_evaluate_tool_call_audit", _eval)

    # Warning POST for step 2 fails (None), step 4 would succeed.
    async def _post(
        client: object, session_id: str, *, event_type: str, data: dict[str, Any]
    ) -> object | None:
        response_id = data.get("response_id", "")
        if isinstance(response_id, str) and response_id.startswith(f"agy_{_CID}_2_"):
            return None  # ambiguous failure on the step-2 warning
        return _FakeResponse(200)

    monkeypatch.setattr(forwarder, "_post_session_event", _post)

    outcome = await _audit_batch(
        object(),  # type: ignore[arg-type]
        "conv_x",
        conversation_id=_CID,
        events=events,
        model=None,
    )
    assert isinstance(outcome, _AuditOutcome)
    assert outcome.first_unaudited_step == 2  # earliest failed warning step


async def test_audit_batch_eval_error_does_not_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    gov FIX A (unit): an eval ERROR (verdict ``None``) is FAIL-OPEN — it does NOT
    set a freeze point, so a policy-engine error never wedges the mirror.
    """
    events = step_to_events(
        _planner_tool_step(step_index=2), conversation_id=_CID, allocator=_allocator()
    )

    async def _eval(
        client: object, session_id: str, *, tool_name: str, tool_input: dict[str, Any], model: Any
    ) -> dict[str, Any] | None:
        return None  # could-not-evaluate

    monkeypatch.setattr(forwarder, "_evaluate_tool_call_audit", _eval)
    outcome = await _audit_batch(
        object(),  # type: ignore[arg-type]
        "conv_x",
        conversation_id=_CID,
        events=events,
        model=None,
    )
    assert outcome.first_unaudited_step is None  # fail-open: no freeze


async def test_audit_warning_post_failure_freezes_cursor_and_logs_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _isolate_bridge_and_brain: Path,
) -> None:
    """
    gov FIX A(ii) + B end-to-end: when a DENY'd tool call's [Policy violation]
    warning POST FAILS, the durable cursor FREEZES below that step (so the warning
    is re-attempted on restart) and an ERROR is logged naming the tool + step.

    Transcript: user(0) → tool(2 DENY). The mirror items for step 2 deliver, but
    the warning POST (response_id ``..._2_0_policy``) returns 500. The cursor must
    NOT advance to 2.
    """
    brain = _isolate_bridge_and_brain
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)
    bridge_dir = prepare_bridge_dir("bridge-warn-fail")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="conv_wf", conversation_id="agy_conv_minted"),
    )
    _write_transcript(
        brain,
        _CID,
        [_user_input_step(text="hi", step_index=0), _planner_tool_step(step_index=2)],
    )

    async def _fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/policies/evaluate"):
            return httpx.Response(200, json={"result": "POLICY_ACTION_DENY", "reason": "no shell"})
        if request.method == "PATCH":
            return httpx.Response(200, json={"ok": True})
        data = body["data"]
        response_id = data.get("response_id", "")
        # The policy-violation WARNING for step 2 fails; everything else succeeds.
        if isinstance(response_id, str) and response_id == f"agy_{_CID}_2_0_policy":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"ok": True})

    caplog.set_level(logging.ERROR, logger="omnigent.antigravity_native_forwarder")

    task = asyncio.create_task(
        forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_wf",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=httpx.MockTransport(_handler),
            transcript_discovery_timeout_s=5.0,
            audit_policies=True,
        )
    )
    # Give the run many poll cycles; a (buggy) advance to step 2 would land here.
    for _ in range(80):
        await asyncio.sleep(0.005)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    state = read_bridge_state(bridge_dir)
    # The cursor must NOT have advanced to step 2 — the warning POST failed, so the
    # violation must be re-warned on restart (at-least-once), not silently lost.
    assert state is None or state.forwarded_step_index is None or state.forwarded_step_index < 2, (
        f"cursor advanced to {state.forwarded_step_index if state else None} despite the "
        f"step-2 warning POST failing — the violation would be silently lost on resume"
    )
    # gov FIX B: the failed warning delivery was logged at ERROR with tool + step.
    error_records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR
        and "failed to deliver post-hoc policy-violation warning" in r.getMessage()
    ]
    assert error_records, "expected an ERROR log for the failed policy-violation warning POST"
    assert "step_index=2" in error_records[0].getMessage()


async def test_audit_warning_redelivered_on_restart_after_warning_post_failure(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    gov FIX A(ii) end-to-end: after a warning-POST failure freezes the cursor, a
    RESTART against the same transcript re-evaluates and re-POSTs the warning —
    the violation is never lost (at-least-once), at the cost of a possible
    duplicate warning (here the first attempt failed, so it is the FIRST delivery).

    First run: tool(2) DENY, the ``..._2_0_policy`` warning POST returns 500 →
    cursor frozen below 2. Second run (warning now succeeds): the parser re-seeds
    from the frozen cursor, re-delivers step 2 and re-audits it → the warning is
    delivered, and the cursor advances to 2.
    """
    brain = _isolate_bridge_and_brain
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)
    bridge_dir = prepare_bridge_dir("bridge-warn-redeliver")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="conv_wr", conversation_id="agy_conv_minted"),
    )
    _write_transcript(
        brain,
        _CID,
        [_user_input_step(text="hi", step_index=0), _planner_tool_step(step_index=2)],
    )

    async def _fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    warn_fails = {"on": True}
    delivered_warnings: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/policies/evaluate"):
            return httpx.Response(200, json={"result": "POLICY_ACTION_DENY", "reason": "no shell"})
        if request.method == "PATCH":
            return httpx.Response(200, json={"ok": True})
        data = body["data"]
        response_id = data.get("response_id", "")
        if isinstance(response_id, str) and response_id == f"agy_{_CID}_2_0_policy":
            if warn_fails["on"]:
                return httpx.Response(500, json={"error": "boom"})
            delivered_warnings.append(response_id)
        return httpx.Response(200, json={"ok": True})

    async def _run() -> None:
        await forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_wr",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=httpx.MockTransport(_handler),
            transcript_discovery_timeout_s=5.0,
            audit_policies=True,
        )

    # ── First run: warning POST fails → cursor frozen below 2. ──
    task1 = asyncio.create_task(_run())
    for _ in range(60):
        await asyncio.sleep(0.005)
    task1.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task1
    state = read_bridge_state(bridge_dir)
    assert state is None or state.forwarded_step_index is None or state.forwarded_step_index < 2
    assert delivered_warnings == []  # no warning landed on the first run

    # ── Restart: warning now succeeds → re-delivered, cursor advances to 2. ──
    warn_fails["on"] = False
    task2 = asyncio.create_task(_run())
    for _ in range(600):
        state = read_bridge_state(bridge_dir)
        if state is not None and state.forwarded_step_index == 2:
            break
        await asyncio.sleep(0.005)
    task2.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task2

    state = read_bridge_state(bridge_dir)
    assert state is not None and state.forwarded_step_index == 2
    assert delivered_warnings == [f"agy_{_CID}_2_0_policy"], (
        "the violation warning must be (re-)delivered on restart — never silently lost"
    )


async def test_audit_crash_before_cursor_advance_redelivers_warning_on_restart(
    monkeypatch: pytest.MonkeyPatch, _isolate_bridge_and_brain: Path
) -> None:
    """
    gov FIX A(i) end-to-end: a crash in the window AFTER the mirror POST but
    BEFORE the audit completes must NOT advance the cursor, so a restart re-audits
    the step and the warning is (re-)posted — never lost.

    The audit now runs BEFORE the cursor advance and gates it, so the crash window
    is closed: the cursor is only written after a successful audit. This test
    forces a crash mid-audit on the first run (``_audit_batch`` raises) and asserts
    (a) the cursor did not advance and (b) a clean restart posts the warning.
    """
    brain = _isolate_bridge_and_brain
    monkeypatch.setattr(forwarder, "_conversation_is_owned_by_live_agy", lambda cid: True)
    bridge_dir = prepare_bridge_dir("bridge-audit-crash")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="conv_ac", conversation_id="agy_conv_minted"),
    )
    _write_transcript(
        brain,
        _CID,
        [_user_input_step(text="hi", step_index=0), _planner_tool_step(step_index=2)],
    )

    async def _fast_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(forwarder, "_sleep", _fast_sleep)

    posted_warnings: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/policies/evaluate"):
            return httpx.Response(200, json={"result": "POLICY_ACTION_DENY", "reason": "no shell"})
        if request.method == "PATCH":
            return httpx.Response(200, json={"ok": True})
        data = body["data"]
        response_id = data.get("response_id", "")
        if isinstance(response_id, str) and response_id == f"agy_{_CID}_2_0_policy":
            posted_warnings.append(response_id)
        return httpx.Response(200, json={"ok": True})

    # First run: the mirror POST for step 2 succeeds, then the audit "crashes"
    # (raises) before completing — modelling a process crash in that window.
    real_audit_batch = forwarder._audit_batch
    crash_armed = {"on": True}

    async def _crashing_audit_batch(*args: Any, **kwargs: Any) -> Any:
        if crash_armed["on"]:
            crash_armed["on"] = False
            raise RuntimeError("simulated crash mid-audit")
        return await real_audit_batch(*args, **kwargs)

    monkeypatch.setattr(forwarder, "_audit_batch", _crashing_audit_batch)

    # The crash propagates out of the run (the supervisor would restart it); catch it.
    with contextlib.suppress(RuntimeError):
        await forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_ac",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=httpx.MockTransport(_handler),
            transcript_discovery_timeout_s=5.0,
            audit_policies=True,
        )

    state = read_bridge_state(bridge_dir)
    # Cursor must NOT have advanced to step 2 — the audit did not complete.
    assert state is None or state.forwarded_step_index is None or state.forwarded_step_index < 2, (
        "cursor advanced past a step whose audit crashed — the violation would be lost on restart"
    )
    assert posted_warnings == []  # the crash pre-empted the warning on run 1

    # Restart cleanly: the parser re-seeds from the (un-advanced) cursor,
    # re-delivers step 2 and re-audits it → the warning is posted.
    task2 = asyncio.create_task(
        forward_antigravity_transcript_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_ac",
            bridge_dir=bridge_dir,
            poll_interval_s=0.001,
            discovery_floor=0.0,
            ap_transport=httpx.MockTransport(_handler),
            transcript_discovery_timeout_s=5.0,
            audit_policies=True,
        )
    )
    for _ in range(600):
        if posted_warnings:
            break
        await asyncio.sleep(0.005)
    task2.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task2

    assert posted_warnings == [f"agy_{_CID}_2_0_policy"], (
        "the violation warning must be posted on restart after a mid-audit crash — "
        "never silently lost (at-least-once)"
    )


# ── gov FIX C: bounded per-poll transcript read ────────────────────────────


def test_read_transcript_caps_read_and_catches_up_over_polls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    gov FIX C: an oversized transcript append is read in bounded chunks across
    multiple polls — no single read exceeds the cap, ``offset`` advances only to
    the last complete line, and the parser still mirrors every step correctly.
    """
    # Shrink the cap so a handful of normal steps already overflow one read.
    monkeypatch.setattr(forwarder, "_MAX_TRANSCRIPT_READ_BYTES", 200)
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)

    # Write several text-only planner steps. Each line is well under 200 bytes but
    # together they exceed it, forcing multiple capped reads.
    steps = [_planner_text_step(text=f"msg-{i}", step_index=i * 2) for i in range(8)]
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(s) + "\n" for s in steps), encoding="utf-8")
    total_size = path.stat().st_size
    assert total_size > 200, "fixture must exceed the cap to exercise multi-poll catch-up"

    # Drive the reader until it reaches EOF, recording each chunk's byte span.
    offset = 0
    collected: list[OutboundEvent] = []
    max_chunk = 0
    polls = 0
    while offset < total_size:
        polls += 1
        prev = offset
        events, offset = forwarder._read_transcript_from_offset(path, prev, parser)
        chunk_bytes = offset - prev
        max_chunk = max(max_chunk, chunk_bytes)
        collected.extend(events)
        assert chunk_bytes <= 200, f"a single read consumed {chunk_bytes} bytes, over the cap"
        # Offset must land on a line boundary (the byte after a newline) until EOF.
        if offset < total_size:
            with path.open("rb") as fh:
                fh.seek(offset - 1)
                assert fh.read(1) == b"\n", "capped read did not stop at a complete line"
        if polls > 100:  # safety against a non-advancing loop
            raise AssertionError("reader failed to make forward progress")

    assert polls >= 2, "the oversized append should require multiple capped reads"
    assert max_chunk <= 200
    # Every step was mirrored exactly once, in order, despite the chunking.
    deltas = [e for e in collected if e.event_type == "external_output_text_delta"]
    assert [e.data["delta"] for e in deltas] == [f"msg-{i}" for i in range(8)]


def test_read_transcript_single_line_longer_than_cap_makes_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    gov FIX C edge: a single line longer than the cap still makes forward progress
    (the partial line is buffered and parsed once its newline is read) rather than
    livelocking on a zero-byte read.
    """
    monkeypatch.setattr(forwarder, "_MAX_TRANSCRIPT_READ_BYTES", 64)
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)
    # One planner step whose JSON line far exceeds 64 bytes AND is full of
    # 3-byte UTF-8 characters, so a fixed-byte cap will frequently land
    # mid-character — exercising the incomplete-UTF-8 tail trim (no corruption).
    text = "界" * 300  # U+754C is 3 bytes in UTF-8
    big = _planner_text_step(text=text, step_index=2)
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps(big, ensure_ascii=False) + "\n", encoding="utf-8")
    total = path.stat().st_size

    offset = 0
    collected: list[OutboundEvent] = []
    polls = 0
    while offset < total:
        polls += 1
        prev = offset
        events, offset = forwarder._read_transcript_from_offset(path, prev, parser)
        assert offset > prev, "reader made no progress on an over-cap line"
        collected.extend(events)
        if polls > 200:
            raise AssertionError("reader livelocked on an over-cap line")

    deltas = [e for e in collected if e.event_type == "external_output_text_delta"]
    assert len(deltas) == 1
    # The text round-trips with NO replacement chars — the cap never split a
    # multi-byte character across two ``replace``-decoded reads.
    assert deltas[0].data["delta"] == text
    assert "�" not in str(deltas[0].data["delta"])


def test_read_transcript_holds_back_partial_utf8_at_eof(tmp_path: Path) -> None:
    """
    gov FIX C regression: when a live ``agy`` flush ends the file mid multi-byte
    UTF-8 char at EOF (the read is NOT cap-limited), the partial char must be held
    back — never ``replace``-decoded and skipped past — so it round-trips intact
    once the continuation bytes are flushed on a later poll. The earlier fix only
    trimmed the incomplete tail on the capped path, so this common non-capped EOF
    case silently corrupted the char into U+FFFD.
    """
    parser = TranscriptParser(conversation_id=_CID, emit_status=False)
    step = _planner_text_step(text="x€y", step_index=2)
    full = (json.dumps(step, ensure_ascii=False) + "\n").encode("utf-8")
    euro = "€".encode()  # b"\xe2\x82\xac" — 3 bytes
    idx = full.index(euro)
    # Stage 1: file ends after the first 2 bytes of the euro char (mid-char), no
    # newline, far under the 1 MiB cap → a NON-capped read straight to EOF.
    partial, rest = full[: idx + 2], full[idx + 2 :]
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(partial)

    events1, offset1 = forwarder._read_transcript_from_offset(path, 0, parser)
    # The incomplete char is held back: the offset stops BEFORE it and nothing is
    # decoded past it (the buggy path advanced to len(partial) and emitted U+FFFD).
    assert offset1 == idx
    assert events1 == []

    # Stage 2: agy flushes the rest of the char and finishes the line.
    with path.open("ab") as fh:
        fh.write(rest)
    events2, offset2 = forwarder._read_transcript_from_offset(path, offset1, parser)
    assert offset2 == len(full)

    deltas = [e for e in events2 if e.event_type == "external_output_text_delta"]
    assert [d.data["delta"] for d in deltas] == ["x€y"]
    assert "�" not in str(deltas[0].data["delta"])

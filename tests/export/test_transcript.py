"""Unit tests for the ``omnigent.transcript/1`` writer and reader."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.entities.conversation import parse_item_data
from omnigent.export import (
    TRANSCRIPT_SCHEMA,
    TranscriptSchemaError,
    entry_from_item,
    header_from_session,
    item_from_entry,
    iter_transcript_entries,
    iter_transcript_lines,
    read_transcript,
)

_SESSION = {
    "id": "conv_abc123",
    "title": "Quarterly churn analysis",
    "created_at": 1_700_000_000,
    "agent_id": "ag_1",
    "agent_name": "analyst",
    "harness": "codex",
    "llm_model": "gpt-5.2-codex",
    "workspace": "/work/churn",
    "parent_session_id": None,
    "root_conversation_id": "conv_abc123",
    "model_override": "gpt-5.2-codex",
    "reasoning_effort": None,
    "status": "idle",
}


def _item(item_type: str, response_id: str = "resp_1", **payload: Any) -> dict[str, Any]:
    """Build a flat API item like :meth:`ConversationItem.to_api_dict` renders."""
    return {
        "id": f"{item_type}_{len(payload)}",
        "type": item_type,
        "status": "completed",
        "response_id": response_id,
        "created_at": 1_700_000_001,
        **payload,
    }


def _user(text: str, response_id: str = "resp_1") -> dict[str, Any]:
    return _item(
        "message", response_id, role="user", content=[{"type": "input_text", "text": text}]
    )


def _assistant(text: str, response_id: str = "resp_1") -> dict[str, Any]:
    return _item(
        "message",
        response_id,
        role="assistant",
        content=[{"type": "output_text", "text": text}],
        model="analyst",
    )


# ── Header ──────────────────────────────────────────────────


def test_header_names_schema_and_session() -> None:
    """The header carries the schema, the session identity, and only set settings."""
    header = header_from_session(_SESSION)
    line = json.loads(header.to_line())

    assert line["schema"] == TRANSCRIPT_SCHEMA
    assert line["session"] == "conv_abc123"
    assert line["created"] == "2023-11-14T22:13:20Z"
    assert line["agent"] == "analyst"
    assert line["harness"] == "codex"
    assert line["model"] == "gpt-5.2-codex"
    assert line["settings"] == {"model_override": "gpt-5.2-codex"}
    # Unset optionals are omitted rather than written as null.
    assert "parent_session" not in line


# ── Entry mapping ───────────────────────────────────────────


def test_message_entry_keeps_text_and_raw_blocks() -> None:
    entry = entry_from_item(_assistant("hello"), turn=1, seq=2)
    line = json.loads(entry.to_line())

    assert line["kind"] == "message"
    assert line["role"] == "assistant"
    assert line["text"] == "hello"
    assert line["content"] == [{"type": "output_text", "text": "hello"}]
    assert line["agent"] == "analyst"
    assert line["time"] == "2023-11-14T22:13:21Z"
    assert "sealed" not in line


def test_tool_call_parses_json_arguments() -> None:
    item = _item(
        "function_call",
        model="analyst",
        name="shell",
        arguments='{"command": ["ls", "lib.py"]}',
        call_id="call_1",
    )
    entry = entry_from_item(item, turn=1, seq=1)

    assert entry.kind == "tool_call"
    assert entry.tool == "shell"
    assert entry.tool_input == {"command": ["ls", "lib.py"]}
    assert entry.tool_input_raw is None
    assert entry.call_id == "call_1"


def test_tool_call_keeps_unparseable_arguments_raw() -> None:
    item = _item("function_call", model="a", name="shell", arguments="not json", call_id="c")
    entry = entry_from_item(item, turn=1, seq=1)

    assert entry.tool_input is None
    assert entry.tool_input_raw == "not json"


def test_tool_result_correlates_by_call_id() -> None:
    entry = entry_from_item(
        _item("function_call_output", call_id="call_1", output="lib.py"), turn=1, seq=1
    )

    assert entry.kind == "tool_result"
    assert entry.role == "tool"
    assert entry.call_id == "call_1"
    assert entry.tool_output == "lib.py"


def test_encrypted_reasoning_is_sealed_with_reason() -> None:
    item = _item(
        "reasoning",
        model="analyst",
        summary=[{"type": "summary_text", "text": "Plan the query"}],
        encrypted_content="gAAAA...",
    )
    line = json.loads(entry_from_item(item, turn=1, seq=1).to_line())

    assert line["kind"] == "reasoning"
    assert line["text"] == "Plan the query"
    assert line["sealed"] is True
    assert "encrypted" in line["sealed_reason"]
    assert "encrypted_content" not in line


def test_readable_reasoning_is_not_sealed() -> None:
    item = _item(
        "reasoning",
        model="analyst",
        summary=[{"type": "summary_text", "text": "s"}],
        content=[{"type": "reasoning_text", "text": "full chain"}],
    )
    entry = entry_from_item(item, turn=1, seq=1)

    assert entry.sealed is False
    assert entry.content == [{"type": "reasoning_text", "text": "full chain"}]


def test_compaction_marks_the_boundary() -> None:
    item = _item(
        "compaction",
        summary="User asked for churn stats; agent loaded data.csv.",
        last_item_id="msg_9",
        model="openai/gpt-4o",
        token_count=42,
    )
    entry = entry_from_item(item, turn=3, seq=10)

    assert entry.kind == "compaction"
    assert entry.text.startswith("User asked")
    assert entry.covers_through == "msg_9"
    assert entry.token_count == 42


def test_error_entry() -> None:
    item = _item("error", source="harness", code="codex_not_signed_in", message="Sign in first")
    entry = entry_from_item(item, turn=1, seq=1)

    assert entry.kind == "error"
    assert entry.role == "system"
    assert entry.code == "codex_not_signed_in"
    assert entry.source == "harness"
    assert entry.text == "Sign in first"


def test_provider_hosted_tool_is_sealed() -> None:
    native = {"type": "web_search_call", "id": "ws_1", "status": "completed"}
    entry = entry_from_item(_item("native_tool", item=native), turn=1, seq=1)

    assert entry.kind == "tool_call"
    assert entry.tool == "web_search_call"
    assert entry.sealed is True
    assert entry.tool_input == native


def test_terminal_command_pairs_as_call_and_result() -> None:
    call = entry_from_item(_item("terminal_command", kind="input", input="pwd"), turn=1, seq=1)
    result = entry_from_item(
        _item("terminal_command", kind="output", stdout="/work\n", stderr=""), turn=1, seq=2
    )

    assert (call.kind, call.role, call.tool) == ("tool_call", "user", "terminal")
    assert call.tool_input == {"command": "pwd"}
    assert (result.kind, result.role, result.tool_output) == ("tool_result", "tool", "/work\n")


def test_lifecycle_items_become_notes_with_raw_payload() -> None:
    item = _item(
        "routing_decision",
        model="databricks-claude-opus-4-8",
        applied=True,
        rationale="Multi-file refactor needs deep reasoning.",
    )
    entry = entry_from_item(item, turn=1, seq=1)

    assert entry.kind == "note"
    assert entry.note_type == "routing_decision"
    assert entry.text == "Multi-file refactor needs deep reasoning."
    assert entry.data["model"] == "databricks-claude-opus-4-8"


def test_unknown_item_type_is_kept_as_note() -> None:
    entry = entry_from_item(_item("future_thing", payload={"x": 1}), turn=1, seq=1)

    assert entry.kind == "note"
    assert entry.note_type == "future_thing"
    assert entry.data == {"payload": {"x": 1}}


# ── Numbering ───────────────────────────────────────────────


def test_turns_advance_with_response_id() -> None:
    items = [
        _user("one", "resp_1"),
        _assistant("1", "resp_1"),
        _user("two", "resp_2"),
        _assistant("2", "resp_2"),
    ]
    entries = list(iter_transcript_entries(items))

    assert [e.turn for e in entries] == [1, 1, 2, 2]
    assert [e.seq for e in entries] == [1, 2, 3, 4]


# ── Writer ↔ reader ─────────────────────────────────────────


def test_lines_round_trip_through_reader() -> None:
    items = [_user("hi"), _assistant("hello")]
    lines = list(iter_transcript_lines(_SESSION, items))

    assert all(line.endswith("\n") for line in lines)
    transcript = read_transcript(lines)

    assert transcript.header.session == "conv_abc123"
    assert transcript.header.title == "Quarterly churn analysis"
    assert [e.kind for e in transcript.entries] == ["message", "message"]
    assert transcript.entries[1].text == "hello"


def test_reader_refuses_unknown_schema() -> None:
    with pytest.raises(TranscriptSchemaError, match="unsupported transcript schema"):
        read_transcript(['{"schema": "omnigent.transcript/99", "session": "x"}\n'])


def test_reader_refuses_file_without_header() -> None:
    with pytest.raises(TranscriptSchemaError, match="missing 'schema'"):
        read_transcript(['{"record_type": "session_meta", "id": "x"}\n'])


def test_reader_refuses_empty_file() -> None:
    with pytest.raises(TranscriptSchemaError, match="empty"):
        read_transcript(["\n", "   \n"])


def test_reader_ignores_fields_it_does_not_know() -> None:
    """A future minor addition inside the same schema must not break reading."""
    lines = [
        json.dumps({"schema": TRANSCRIPT_SCHEMA, "session": "x", "future_field": 1}) + "\n",
        json.dumps({"turn": 1, "seq": 1, "role": "user", "kind": "message", "extra": True}) + "\n",
    ]
    transcript = read_transcript(lines)

    assert transcript.entries[0].kind == "message"


# ── Entry → item ────────────────────────────────────────────


@pytest.mark.parametrize(
    "item",
    [
        _user("hi"),
        _assistant("hello"),
        _item("function_call", model="a", name="shell", arguments='{"c": 1}', call_id="c1"),
        _item("function_call", model="a", name="shell", arguments="raw", call_id="c1"),
        _item("function_call_output", call_id="c1", output="done"),
        _item("reasoning", model="a", summary=[{"type": "summary_text", "text": "s"}]),
        _item(
            "reasoning",
            model="a",
            summary=[],
            content=[{"type": "reasoning_text", "text": "full"}],
        ),
        _item("error", source="llm", code="rate_limited", message="slow down"),
        _item("compaction", summary="sum", last_item_id="msg_1", token_count=3),
        _item("native_tool", item={"type": "web_search_call", "id": "ws_1"}),
        _item("terminal_command", kind="input", input="pwd"),
        _item("terminal_command", kind="output", stdout="/work", stderr="warn"),
        _item("slash_command", model="claude-native-ui", name="compact", arguments=""),
        _item(
            "routing_decision",
            model="databricks-claude-opus-4-8",
            applied=True,
            rationale="deep",
        ),
    ],
    ids=lambda item: item["type"] + ("/" + str(item.get("kind") or "")).rstrip("/"),
)
def test_every_item_type_rebuilds_into_a_valid_item(item: dict[str, Any]) -> None:
    """Export → read → rebuild yields a payload the entity model accepts."""
    line = entry_from_item(item, turn=1, seq=1).to_line()
    transcript = read_transcript(
        [json.dumps({"schema": TRANSCRIPT_SCHEMA, "session": "x"}) + "\n", line + "\n"]
    )
    payload = item_from_entry(transcript.entries[0])

    assert payload["type"] == item["type"]
    parse_item_data(payload["type"], payload["data"])


def test_rebuilt_message_restores_agent_and_blocks() -> None:
    payload = item_from_entry(entry_from_item(_assistant("hello"), turn=1, seq=1))

    assert payload == {
        "type": "message",
        "data": {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hello"}],
            "agent": "analyst",
        },
    }


def test_rebuilt_tool_call_restores_argument_string() -> None:
    item = _item("function_call", model="a", name="shell", arguments='{"c": 1}', call_id="c1")
    payload = item_from_entry(entry_from_item(item, turn=1, seq=1))

    assert payload["data"]["name"] == "shell"
    assert json.loads(payload["data"]["arguments"]) == {"c": 1}
    assert payload["data"]["call_id"] == "c1"


def test_rebuilt_terminal_output_keeps_stderr() -> None:
    item = _item("terminal_command", kind="output", stdout="out", stderr="err")
    payload = item_from_entry(entry_from_item(item, turn=1, seq=1))

    assert payload["data"] == {"kind": "output", "stdout": "out", "stderr": "err"}

"""Unit tests for the ``omnigent.transcript/1`` writer and reader."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest

from omnigent.entities.conversation import parse_item_data
from omnigent.export import (
    CONTENT_FIELDS,
    TRANSCRIPT_SCHEMA,
    EntryNumbering,
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


def test_header_settings_track_the_stored_overrides_the_create_route_accepts() -> None:
    """Every stored override that is a setting is exported, and every exported
    setting is something ``POST /v1/sessions`` can re-apply on import."""
    from omnigent.export.transcript import _NON_SETTING_OVERRIDES, SETTING_KEYS
    from omnigent.server.schemas import SessionCreateRequest, SessionResponse
    from omnigent.stores.conversation_store.overrides import SESSION_OVERRIDE_KEYS

    exported = set(SETTING_KEYS)
    assert exported >= set(SESSION_OVERRIDE_KEYS) - _NON_SETTING_OVERRIDES
    assert exported <= set(SessionCreateRequest.model_fields)
    # The CLI builds its header from the session snapshot, so every setting the
    # route exports must be on the snapshot too, or the two files differ.
    assert exported <= set(SessionResponse.model_fields)


# ── Entry mapping ───────────────────────────────────────────


def test_message_entry_keeps_text_and_leaves_the_blocks_to_raw() -> None:
    entry = entry_from_item(_assistant("hello"), turn=1, seq=2)
    line = json.loads(entry.to_line())

    assert line["kind"] == "message"
    assert line["role"] == "assistant"
    assert line["text"] == "hello"
    assert line["agent"] == "analyst"
    assert line["time"] == "2023-11-14T22:13:21Z"
    assert "sealed" not in line
    # Nothing is stored twice: the blocks live in ``raw`` only.
    assert "content" not in line
    assert line["raw"]["content"] == [{"type": "output_text", "text": "hello"}]


def test_every_entry_carries_the_stored_payload_without_the_envelope() -> None:
    """``raw`` is the item minus ``id``/``type``/``status``/..., so import is lossless."""
    item = _item(
        "message",
        role="user",
        content=[{"type": "input_text", "text": "hi"}],
        user_authored=True,
        subagent_return_id="ret_1",
        stream_message_id="sm_1",
    )
    line = json.loads(entry_from_item(item, turn=1, seq=1).to_line())

    assert line["raw"] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
        "user_authored": True,
        "subagent_return_id": "ret_1",
        "stream_message_id": "sm_1",
    }
    assert not {"id", "type", "status", "response_id", "created_at"} & line["raw"].keys()


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
    # The readable fields stay summary-only; ``raw`` keeps the blob so an
    # imported Codex session can still resume with its reasoning.
    assert "content" not in line
    assert line["raw"]["encrypted_content"] == "gAAAA..."


def test_readable_reasoning_is_not_sealed() -> None:
    item = _item(
        "reasoning",
        model="analyst",
        summary=[{"type": "summary_text", "text": "s"}],
        content=[{"type": "reasoning_text", "text": "full chain"}],
    )
    entry = entry_from_item(item, turn=1, seq=1)

    assert entry.sealed is False
    assert entry.content is None
    assert entry.raw["content"] == [{"type": "reasoning_text", "text": "full chain"}]


def test_compaction_marks_the_boundary() -> None:
    compacted = [{"role": "user", "content": "churn stats"}]
    item = _item(
        "compaction",
        summary="User asked for churn stats; agent loaded data.csv.",
        last_item_id="msg_9",
        model="openai/gpt-4o",
        token_count=42,
        compacted_messages=compacted,
        window_id=2,
    )
    entry = entry_from_item(item, turn=3, seq=10)

    assert entry.kind == "compaction"
    assert entry.text.startswith("User asked")
    assert entry.covers_through == "msg_9"
    assert entry.token_count == 42
    # The model context the summary replaced rides along for import.
    assert entry.raw["compacted_messages"] == compacted
    assert entry.raw["window_id"] == 2


def test_error_entry() -> None:
    item = _item(
        "error",
        source="harness",
        code="codex_not_signed_in",
        message="Sign in first",
        title="Codex is signed out",
        cause="No ChatGPT login was found.",
        remediation="Run `codex login`.",
    )
    entry = entry_from_item(item, turn=1, seq=1)

    assert entry.kind == "error"
    assert entry.role == "system"
    assert entry.code == "codex_not_signed_in"
    assert entry.source == "harness"
    assert entry.text == "Sign in first"
    assert (entry.raw["title"], entry.raw["cause"], entry.raw["remediation"]) == (
        "Codex is signed out",
        "No ChatGPT login was found.",
        "Run `codex login`.",
    )


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


def test_routing_decision_becomes_a_note_with_its_rationale() -> None:
    item = _item(
        "routing_decision",
        model="databricks-claude-opus-4-8",
        applied=True,
        rationale="Multi-file refactor needs deep reasoning.",
    )
    entry = entry_from_item(item, turn=1, seq=1)

    assert (entry.kind, entry.role, entry.note_type) == ("note", "system", "routing_decision")
    assert entry.text == "Multi-file refactor needs deep reasoning."
    assert entry.raw["model"] == "databricks-claude-opus-4-8"


def test_resource_event_becomes_a_note_without_a_reading() -> None:
    item = _item(
        "resource_event",
        event_type="terminal.exited",
        resource_id="term_1",
        resource_type="terminal",
        resource={"name": "main"},
    )
    entry = entry_from_item(item, turn=1, seq=1)

    assert (entry.kind, entry.note_type, entry.text) == ("note", "resource_event", None)
    assert entry.raw["event_type"] == "terminal.exited"


def test_unknown_item_type_is_kept_as_note() -> None:
    entry = entry_from_item(_item("future_thing", payload={"x": 1}), turn=1, seq=1)

    assert entry.kind == "note"
    assert entry.note_type == "future_thing"
    assert entry.raw == {"payload": {"x": 1}}


# ── Numbering ───────────────────────────────────────────────


def test_turns_start_at_each_user_message() -> None:
    """A turn is one user request; lifecycle items with their own response ids
    (resource events, compactions) and meta context stay in the current turn."""
    items = [
        _user("one", "resp_1"),
        _item("resource_event", "resource_1", event_type="terminal.created", resource_id="t"),
        _assistant("1", "resp_1"),
        _item("compaction", "codex:compaction", summary="s", last_item_id="x", token_count=1),
        _item(
            "message",
            "resp_2",
            role="user",
            content=[{"type": "input_text", "text": "context"}],
            is_meta=True,
        ),
        _user("two", "resp_2"),
        _assistant("2", "resp_2"),
    ]
    entries = list(iter_transcript_entries(items))

    assert [e.turn for e in entries] == [1, 1, 1, 1, 1, 2, 2]
    assert [e.seq for e in entries] == [1, 2, 3, 4, 5, 6, 7]


def test_a_file_that_opens_without_a_user_message_still_starts_at_turn_one() -> None:
    """Startup context and lifecycle items before the first request share turn 1
    with it; the first request is never pushed into turn 2."""
    entries = list(iter_transcript_entries([_assistant("hello")]))
    assert [e.turn for e in entries] == [1]

    items = [
        _item(
            "message",
            role="user",
            content=[{"type": "input_text", "text": "context"}],
            is_meta=True,
        ),
        _item("resource_event", event_type="terminal.created", resource_id="t"),
        _user("one"),
        _assistant("1"),
        _user("two"),
    ]
    assert [e.turn for e in iter_transcript_entries(items)] == [1, 1, 1, 1, 2]


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


def test_reader_reports_a_malformed_header_as_a_schema_error() -> None:
    """A header that names the schema but fails validation is still our error type."""
    with pytest.raises(TranscriptSchemaError, match="line 1: invalid header"):
        read_transcript([json.dumps({"schema": TRANSCRIPT_SCHEMA, "session": 42}) + "\n"])


def test_reader_refuses_a_schema_that_is_not_a_string() -> None:
    with pytest.raises(TranscriptSchemaError, match="unsupported transcript schema"):
        read_transcript([json.dumps({"schema": ["omnigent.transcript/1"], "session": "x"}) + "\n"])


@pytest.mark.parametrize(
    ("kind", "stray"),
    [
        ("tool_call", {"covers_through": "msg_1"}),
        ("message", {"call_id": "c1"}),
        ("note", {"tool_output": "x"}),
        ("compaction", {"sealed": True}),
    ],
)
def test_reader_refuses_a_field_that_does_not_belong_to_the_kind(
    kind: str, stray: dict[str, Any]
) -> None:
    """The kind table is enforced on read, not only described in the docs."""
    line = {"turn": 1, "seq": 1, "role": "system", "kind": kind, **stray}
    with pytest.raises(TranscriptSchemaError, match=f"{kind} entry cannot carry"):
        read_transcript(
            [
                json.dumps({"schema": TRANSCRIPT_SCHEMA, "session": "x"}) + "\n",
                json.dumps(line) + "\n",
            ]
        )


def test_every_written_entry_satisfies_its_kind_table() -> None:
    """The writer never produces a line the reader would refuse."""
    for item in _ROUND_TRIP_ITEMS:
        read_transcript(
            [
                json.dumps({"schema": TRANSCRIPT_SCHEMA, "session": "x"}) + "\n",
                entry_from_item(item, turn=1, seq=1).to_line() + "\n",
            ]
        )


def test_reader_ignores_fields_it_does_not_know() -> None:
    """A future minor addition inside the same schema must not break reading."""
    lines = [
        json.dumps({"schema": TRANSCRIPT_SCHEMA, "session": "x", "future_field": 1}) + "\n",
        json.dumps({"turn": 1, "seq": 1, "role": "user", "kind": "message", "extra": True}) + "\n",
    ]
    transcript = read_transcript(lines)

    assert transcript.entries[0].kind == "message"


# ── Entry → item ────────────────────────────────────────────


_ROUND_TRIP_ITEMS: list[dict[str, Any]] = [
    _user("hi"),
    _assistant("hello"),
    _item(
        "message",
        role="user",
        content=[{"type": "input_text", "text": "hi"}],
        user_authored=True,
        subagent_return_id="ret_1",
        stream_message_id="sm_1",
    ),
    _item("function_call", model="a", name="shell", arguments='{"c": 1}', call_id="c1"),
    _item("function_call", model="a", name="shell", arguments="raw", call_id="c1"),
    _item("function_call_output", call_id="c1", output="done", subagent_return_id="ret_2"),
    _item("reasoning", model="a", summary=[{"type": "summary_text", "text": "s"}]),
    _item(
        "reasoning",
        model="a",
        summary=[{"type": "summary_text", "text": "s"}],
        encrypted_content="gAAAA",
    ),
    _item(
        "reasoning",
        model="a",
        summary=[],
        content=[{"type": "reasoning_text", "text": "full"}],
    ),
    _item(
        "error",
        source="llm",
        code="rate_limited",
        message="slow down",
        title="Rate limited",
        cause="Too many requests.",
        remediation="Wait a minute.",
    ),
    _item(
        "compaction",
        summary="sum",
        last_item_id="msg_1",
        model="openai/gpt-4o",
        token_count=3,
        compacted_messages=[{"role": "user", "content": "hi"}],
        window_id="w1",
    ),
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
    _item(
        "resource_event",
        event_type="terminal.exited",
        resource_id="term_1",
        resource_type="terminal",
        resource={"name": "main"},
    ),
]


def _expected_payload(item: dict[str, Any]) -> dict[str, Any]:
    """The ``{"type", "data"}`` create payload that reproduces *item* exactly."""
    from omnigent.export import ITEM_ENVELOPE_KEYS, agent_serializes_as_model

    data = {k: v for k, v in item.items() if k not in ITEM_ENVELOPE_KEYS}
    if agent_serializes_as_model(item["type"]) and "model" in data:
        data["agent"] = data.pop("model")
    return {"type": item["type"], "data": data}


@pytest.mark.parametrize(
    "item",
    _ROUND_TRIP_ITEMS,
    ids=lambda item: item["type"] + ("/" + str(item.get("kind") or "")).rstrip("/"),
)
def test_every_item_type_round_trips_field_for_field(item: dict[str, Any]) -> None:
    """Export → read → rebuild reproduces the stored item, and the entity accepts it."""
    line = entry_from_item(item, turn=1, seq=1).to_line()
    transcript = read_transcript(
        [json.dumps({"schema": TRANSCRIPT_SCHEMA, "session": "x"}) + "\n", line + "\n"]
    )
    payload = item_from_entry(transcript.entries[0])

    assert payload == _expected_payload(item)
    parse_item_data(payload["type"], payload["data"])


def test_round_trip_matrix_covers_every_known_item_type() -> None:
    """A new item type must be added to the matrix, or it ships untested."""
    from omnigent.entities.conversation import ITEM_TYPE_TO_DATA_CLS

    assert {item["type"] for item in _ROUND_TRIP_ITEMS} >= set(ITEM_TYPE_TO_DATA_CLS)


# ── Entry → item without ``raw`` (hand-written or trimmed files) ─────────


def _entry(**fields: Any):
    return read_transcript(
        [
            json.dumps({"schema": TRANSCRIPT_SCHEMA, "session": "x"}) + "\n",
            json.dumps({"turn": 1, "seq": 1, **fields}) + "\n",
        ]
    ).entries[0]


def test_text_only_message_rebuilds_a_text_block() -> None:
    payload = item_from_entry(_entry(role="assistant", kind="message", text="hi", agent="a"))

    assert payload == {
        "type": "message",
        "data": {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi"}],
            "agent": "a",
        },
    }


def test_tool_call_without_raw_reserializes_its_arguments() -> None:
    payload = item_from_entry(
        _entry(role="assistant", kind="tool_call", tool="shell", tool_input={"c": 1}, call_id="c1")
    )

    assert payload["type"] == "function_call"
    assert json.loads(payload["data"]["arguments"]) == {"c": 1}
    assert payload["data"]["call_id"] == "c1"


def test_terminal_output_without_raw_keeps_the_text_as_stdout() -> None:
    payload = item_from_entry(
        _entry(role="tool", kind="tool_result", tool="terminal", tool_output="out")
    )

    assert payload["data"] == {"kind": "output", "stdout": "out", "stderr": ""}


def test_a_redacted_entry_keeps_its_shape_and_drops_every_content_field() -> None:
    """What a session did survives; what it said does not."""
    entry = EntryNumbering().entry(
        {
            "id": "fc_1",
            "type": "function_call",
            "name": "sql",
            "call_id": "call_1",
            "arguments": json.dumps({"query": "select * from customers"}),
            "created_at": 1_700_000_000,
        }
    )
    assert entry.tool_input == {"query": "select * from customers"}

    redacted = entry.redacted()

    assert (redacted.turn, redacted.seq) == (entry.turn, entry.seq)
    assert (redacted.kind, redacted.role) == (entry.kind, entry.role)
    assert (redacted.tool, redacted.call_id, redacted.time) == ("sql", "call_1", entry.time)
    assert all(getattr(redacted, field) is None for field in CONTENT_FIELDS)
    assert "customers" not in redacted.to_line()
    # The original is untouched, so redacting is safe mid-stream.
    assert entry.tool_input == {"query": "select * from customers"}


def test_a_redacted_entry_still_reports_what_the_provider_withheld() -> None:
    """``sealed`` is a structural marker, not content, so it outlives redaction."""
    entry = EntryNumbering().entry(
        {
            "id": "rsn_1",
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "opaque",
            "created_at": 1_700_000_000,
        }
    )
    assert entry.sealed is True

    assert entry.redacted().sealed is True


def test_redacted_lines_flag_the_header_and_carry_no_content() -> None:
    """The flag is what tells a reader the file is structure only."""
    session = {"id": "conv_1", "title": "Churn", "created_at": 1_700_000_000}
    items = [
        {
            "id": "msg_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "a secret question"}],
            "created_at": 1_700_000_000,
        }
    ]

    lines = list(iter_transcript_lines(session, items, redact=True))
    header, entry = (json.loads(line) for line in lines)

    assert header["redacted"] is True
    assert entry["kind"] == "message" and entry["role"] == "user"
    assert not CONTENT_FIELDS & entry.keys()
    assert "secret" not in "".join(lines)


def test_an_ordinary_export_says_nothing_about_redaction() -> None:
    """The flag is additive: a normal file reads exactly as it did before."""
    session = {"id": "conv_1", "created_at": 1_700_000_000}
    items = [
        {
            "id": "msg_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
            "created_at": 1_700_000_000,
        }
    ]

    header, entry = (json.loads(line) for line in iter_transcript_lines(session, items))

    assert "redacted" not in header
    assert entry["text"] == "hello"
    assert read_transcript(iter_transcript_lines(session, items)).header.redacted is False


def _attachment_message(**extra: object) -> dict[str, object]:
    """A user message carrying an image and naming a file by path."""
    payload = base64.b64encode(b"\x89PNG not really").decode()
    return {
        "id": "msg_1",
        "type": "message",
        "role": "user",
        "created_at": 1_700_000_000,
        "content": [
            {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{payload}",
                "filename": "diagram.png",
            },
            {"type": "input_text", "text": "compare with [Attached: /tmp/q3-report.pdf]"},
        ],
        **extra,
    }


def test_an_attachment_is_recorded_by_name_type_size_and_digest() -> None:
    """The bytes are in ``raw``; the reference is what survives a reader that
    ignores ``raw``, and identifies the same file wherever else it appears."""
    entry = EntryNumbering().entry(_attachment_message())

    assert entry.artifacts is not None
    image = entry.artifacts[0]
    assert (image.kind, image.name, image.media_type) == ("attachment", "diagram.png", "image/png")
    assert image.bytes == len(b"\x89PNG not really")
    assert image.sha256 == hashlib.sha256(b"\x89PNG not really").hexdigest()


def test_a_file_named_in_the_text_is_recorded_as_a_path() -> None:
    """Its bytes never travelled, so the path is the only honest reference."""
    entry = EntryNumbering().entry(_attachment_message())

    assert entry.artifacts is not None
    named = entry.artifacts[1]
    assert (named.kind, named.path, named.name) == ("path", "/tmp/q3-report.pdf", "q3-report.pdf")
    assert named.sha256 is None


def test_a_message_without_files_records_no_artifacts() -> None:
    """The field is absent rather than empty, so ordinary lines do not grow."""
    entry = EntryNumbering().entry(
        {
            "id": "msg_1",
            "type": "message",
            "role": "user",
            "created_at": 1_700_000_000,
            "content": [{"type": "input_text", "text": "no files here"}],
        }
    )

    assert entry.artifacts is None
    assert "artifacts" not in json.loads(entry.to_line())


def test_redaction_keeps_which_files_were_involved_but_not_their_names() -> None:
    """A file name can say as much as the message that carried it."""
    entry = EntryNumbering().entry(_attachment_message()).redacted()

    assert entry.artifacts is not None
    image, named = entry.artifacts
    assert image.sha256 is not None and image.bytes is not None
    assert image.name is None and image.path is None
    assert named.kind == "path" and named.path is None
    assert "q3-report" not in entry.to_line()
    assert "diagram" not in entry.to_line()


def test_an_unreadable_attachment_is_still_named() -> None:
    """A block whose payload will not decode is reported rather than dropped."""
    entry = EntryNumbering().entry(
        {
            "id": "msg_1",
            "type": "message",
            "role": "user",
            "created_at": 1_700_000_000,
            "content": [
                {
                    "type": "input_file",
                    "file_data": "data:application/pdf;base64,!!not base64!!",
                    "filename": "broken.pdf",
                },
                {"type": "input_file", "file_id": "file_abc123"},
            ],
        }
    )

    assert entry.artifacts is not None
    broken, unresolved = entry.artifacts
    assert (broken.name, broken.media_type, broken.sha256) == (
        "broken.pdf",
        "application/pdf",
        None,
    )
    # An unresolved id still says a file was involved.
    assert unresolved.name == "file_abc123"

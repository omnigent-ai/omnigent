"""Unit tests for :mod:`omnigent.transcript_import`.

These cover the Claude Code and Codex transcript parsers, format detection,
title synthesis, and a contract test proving the produced item ``data`` shapes
validate against the server-side conversation models (the ``agent`` vs ``model``
wire invariant). All fixtures are inline JSONL strings — no live LLM, no server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.entities.conversation import NewConversationItem
from omnigent.transcript_import import (
    ImportedItem,
    TranscriptImportError,
    detect_source,
    parse_claude_lines,
    parse_codex_lines,
    parse_transcript,
    parse_transcript_file,
    to_initial_items,
)


def _jsonl(*records: object) -> list[str]:
    """Render records as JSONL lines (one JSON object per line)."""
    return [json.dumps(record) for record in records]


# ── Claude Code parsing ─────────────────────────────────────────────────────


def test_parse_claude_messages_and_tool_calls() -> None:
    """A full Claude turn maps to message + function_call + output items.

    Proves the core mapping: user text, assistant text, ``tool_use`` →
    ``function_call`` (with compact JSON ``arguments`` and ``id`` → ``call_id``),
    and ``tool_result`` → ``function_call_output``. The assistant ``agent`` is
    taken from ``message.model``.
    """
    lines = _jsonl(
        {"type": "user", "message": {"role": "user", "content": "inspect TODO.md"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [
                    {"type": "text", "text": "Reading it."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "TODO.md"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "TODO body"}
                ],
            },
        },
    )

    items = parse_claude_lines(lines)

    assert [item.item_type for item in items] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
    ]
    assert items[0].data == {
        "role": "user",
        "content": [{"type": "input_text", "text": "inspect TODO.md"}],
    }
    assert items[1].data == {
        "role": "assistant",
        "agent": "claude-sonnet-4-6",
        "content": [{"type": "output_text", "text": "Reading it."}],
    }
    assert items[2].data == {
        "agent": "claude-sonnet-4-6",
        "name": "Read",
        "arguments": '{"file_path":"TODO.md"}',
        "call_id": "toolu_1",
    }
    assert items[3].data == {"call_id": "toolu_1", "output": "TODO body"}


def test_claude_skips_thinking_metadata_sidechain_and_scaffolding() -> None:
    """Thinking blocks, metadata records, sidechains, and CLI scaffolding drop."""
    lines = _jsonl(
        {"type": "permission-mode", "mode": "default"},
        {"type": "branch-update", "gitBranch": "feature/x"},
        {"type": "summary", "summary": "..."},
        {"message": {"role": "assistant", "model": "m"}, "isSidechain": True},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<caveat>"}},
        {
            "type": "user",
            "message": {"role": "user", "content": "<command-name>/clear</command-name>"},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-x",
                "content": [
                    {"type": "thinking", "thinking": "secret"},
                    {"type": "text", "text": "visible"},
                    {"type": "image", "source": {"data": "..."}},
                ],
            },
        },
    )

    items = parse_claude_lines(lines)

    assert len(items) == 1
    assert items[0].item_type == "message"
    assert items[0].data["content"] == [{"type": "output_text", "text": "visible"}]


def test_claude_user_content_as_list_merges_text() -> None:
    """A user record with a content list yields one merged user message."""
    lines = _jsonl(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "line one"},
                    {"type": "text", "text": "line two"},
                ],
            },
        }
    )

    items = parse_claude_lines(lines)

    assert len(items) == 1
    assert items[0].data == {
        "role": "user",
        "content": [{"type": "input_text", "text": "line one\nline two"}],
    }


def test_claude_tool_result_list_content_extracts_text() -> None:
    """A tool_result whose content is a block list extracts the text blocks."""
    lines = _jsonl(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t9",
                        "content": [
                            {"type": "text", "text": "first"},
                            {"type": "image", "source": {}},
                            {"type": "text", "text": "second"},
                        ],
                    }
                ],
            },
        }
    )

    items = parse_claude_lines(lines)

    assert items == [
        ImportedItem("function_call_output", {"call_id": "t9", "output": "first\nsecond"})
    ]


def test_claude_assistant_string_content() -> None:
    """An assistant record with string content yields one output_text message."""
    lines = _jsonl(
        {
            "type": "assistant",
            "message": {"role": "assistant", "model": "m1", "content": "just text"},
        }
    )

    items = parse_claude_lines(lines)

    assert items == [
        ImportedItem(
            "message",
            {
                "role": "assistant",
                "agent": "m1",
                "content": [{"type": "output_text", "text": "just text"}],
            },
        )
    ]


def test_claude_assistant_without_model_falls_back_to_claude_label() -> None:
    """When no model id appears, the assistant agent label defaults to 'claude'."""
    lines = _jsonl(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        }
    )

    items = parse_claude_lines(lines)

    assert items[0].data["agent"] == "claude"


# ── Codex parsing ───────────────────────────────────────────────────────────


def test_parse_codex_messages_and_tool_calls() -> None:
    """A Codex rollout's response_items map to messages + tool calls."""
    lines = _jsonl(
        {"type": "session_meta", "payload": {"id": "019e", "cwd": "/r"}},
        {"type": "turn_context", "payload": {"turn_id": "turn_1", "cwd": "/r"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "open TODO.md"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": '{"command":"cat TODO.md"}',
                "call_id": "call_1",
                "id": "fc_1",
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call_1", "output": "body"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        },
    )

    items = parse_codex_lines(lines)

    assert [item.item_type for item in items] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert items[0].data == {
        "role": "user",
        "content": [{"type": "input_text", "text": "open TODO.md"}],
    }
    assert items[1].data == {
        "agent": "codex",
        "name": "shell",
        "arguments": '{"command":"cat TODO.md"}',
        "call_id": "call_1",
    }
    assert items[2].data == {"call_id": "call_1", "output": "body"}
    assert items[3].data == {
        "role": "assistant",
        "agent": "codex",
        "content": [{"type": "output_text", "text": "Done."}],
    }


def test_codex_skips_structural_reasoning_developer_and_event_msg() -> None:
    """session_meta/turn_context/reasoning/developer/event_msg carry no items."""
    lines = _jsonl(
        {"type": "session_meta", "payload": {"id": "x"}},
        {"type": "turn_context", "payload": {"turn_id": "t"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "<env>"}],
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": [], "encrypted_content": "zzz"},
        },
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "dup"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "real"}],
            },
        },
    )

    items = parse_codex_lines(lines)

    assert len(items) == 1
    assert items[0].data["content"] == [{"type": "output_text", "text": "real"}]


def test_codex_function_output_missing_call_id_pairs_positionally() -> None:
    """A function_call_output without call_id pairs to the most recent call."""
    lines = _jsonl(
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": "{}",
                "call_id": "call_42",
            },
        },
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "stdout"}},
    )

    items = parse_codex_lines(lines)

    assert items[1].data == {"call_id": "call_42", "output": "stdout"}


def test_codex_function_call_missing_arguments_defaults_to_empty_object() -> None:
    """A function_call without arguments still produces a valid item."""
    lines = _jsonl(
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "noop", "call_id": "c1"},
        }
    )

    items = parse_codex_lines(lines)

    assert items[0].data == {"agent": "codex", "name": "noop", "arguments": "{}", "call_id": "c1"}


# ── Detection, titling, and top-level entry points ──────────────────────────


def test_detect_source_distinguishes_claude_and_codex() -> None:
    """Detection keys off session_meta/payload (codex) vs message.role (claude)."""
    codex = _jsonl({"type": "session_meta", "payload": {"id": "x"}})
    claude_meta_first = _jsonl(
        {"type": "permission-mode", "mode": "default"},
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    )
    assert detect_source(codex) == "codex"
    assert detect_source(claude_meta_first) == "claude"
    assert detect_source(_jsonl({"unrelated": "object"})) is None
    assert detect_source([""]) is None


def test_parse_transcript_auto_and_explicit_source() -> None:
    """``auto`` detects; an explicit source skips detection."""
    codex = _jsonl(
        {"type": "session_meta", "payload": {"id": "x"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "q"}],
            },
        },
    )
    auto = parse_transcript(codex, source="auto")
    explicit = parse_transcript(codex, source="codex")
    assert auto.source == "codex"
    assert explicit.source == "codex"
    assert auto.items == explicit.items


def test_parse_transcript_errors() -> None:
    """Undetectable format, empty result, and unknown source all raise."""
    with pytest.raises(TranscriptImportError, match="could not detect"):
        parse_transcript(_jsonl({"unrelated": 1}), source="auto")
    with pytest.raises(TranscriptImportError, match="no importable"):
        parse_transcript(_jsonl({"type": "permission-mode"}), source="claude")
    with pytest.raises(TranscriptImportError, match="unknown source"):
        parse_transcript(
            _jsonl({"type": "user", "message": {"role": "user", "content": "x"}}), source="bogus"
        )


def test_title_synthesized_from_first_user_message_and_truncated() -> None:
    """The title comes from the first user message, collapsed and truncated."""
    short = parse_transcript(
        _jsonl({"type": "user", "message": {"role": "user", "content": "  hello   world  "}}),
        source="claude",
    )
    assert short.title == "hello world"

    long_text = "word " * 40
    long = parse_transcript(
        _jsonl({"type": "user", "message": {"role": "user", "content": long_text}}),
        source="claude",
    )
    assert long.title is not None
    assert len(long.title) == 60
    assert long.title.endswith("…")


def test_title_none_when_no_user_text() -> None:
    """An assistant-only transcript has no synthesized title."""
    parsed = parse_transcript(
        _jsonl(
            {"type": "assistant", "message": {"role": "assistant", "model": "m", "content": "hi"}}
        ),
        source="claude",
    )
    assert parsed.title is None


def test_to_initial_items_shape_and_counts() -> None:
    """``to_initial_items`` renders {type, data} dicts; counts are reported."""
    parsed = parse_transcript(
        _jsonl(
            {"type": "user", "message": {"role": "user", "content": "q"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "m",
                    "content": [{"type": "tool_use", "id": "t", "name": "Run", "input": {}}],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t", "content": "r"}],
                },
            },
        ),
        source="claude",
    )
    payload = to_initial_items(parsed.items)
    assert all(set(item) == {"type", "data"} for item in payload)
    assert payload[0] == {"type": "message", "data": parsed.items[0].data}
    assert parsed.message_count == 1
    assert parsed.tool_call_count == 1
    assert parsed.tool_output_count == 1


def test_parse_transcript_file_reads_and_parses(tmp_path: Path) -> None:
    """``parse_transcript_file`` reads a .jsonl file and auto-detects format."""
    path = tmp_path / "session.jsonl"
    path.write_text(
        "\n".join(_jsonl({"type": "user", "message": {"role": "user", "content": "from file"}}))
        + "\n",
        encoding="utf-8",
    )
    parsed = parse_transcript_file(path)
    assert parsed.source == "claude"
    assert parsed.title == "from file"


def test_parse_transcript_file_missing_raises(tmp_path: Path) -> None:
    """A missing file raises TranscriptImportError, not a bare OSError."""
    with pytest.raises(TranscriptImportError, match="could not read"):
        parse_transcript_file(tmp_path / "nope.jsonl")


def test_malformed_lines_are_skipped_not_fatal() -> None:
    """Blank and non-JSON lines are tolerated and skipped."""
    lines = [
        "",
        "not json at all",
        json.dumps({"type": "user", "message": {"role": "user", "content": "survived"}}),
    ]
    items = parse_claude_lines(lines)
    assert len(items) == 1
    assert items[0].data["content"] == [{"type": "input_text", "text": "survived"}]


# ── Contract test: produced data validates against the server models ─────────


def test_produced_items_validate_against_conversation_models() -> None:
    """Every produced item's data validates as the server route builds it.

    The route constructs ``NewConversationItem(type=..., data=item.data)`` on
    the history-only seed path, which coerces ``data`` into the matching
    ``ItemData`` model and enforces type↔data agreement. If this fails, the
    importer would produce payloads the server rejects with a 422 — e.g. using
    the output-only ``model`` alias instead of the required ``agent`` key.
    """
    claude = parse_transcript(
        _jsonl(
            {"type": "user", "message": {"role": "user", "content": "q"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-x",
                    "content": [
                        {"type": "text", "text": "a"},
                        {"type": "tool_use", "id": "c1", "name": "Read", "input": {"p": 1}},
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "o"}],
                },
            },
        ),
        source="claude",
    )
    for item in claude.items:
        # Must not raise — mirrors omnigent/server/routes/sessions.py seed path.
        NewConversationItem(type=item.item_type, response_id="seed", data=item.data)


# ── Regression tests for review findings ────────────────────────────────────


def test_codex_imports_custom_tool_call_apply_patch() -> None:
    """custom_tool_call (apply_patch) and its output map to function items.

    apply_patch is Codex's primary file-editing tool, encoded as a
    custom_tool_call whose payload carries a raw string ``input`` (not JSON
    ``arguments``). Dropping it would lose every edit a coding session made.
    """
    lines = _jsonl(
        {"type": "session_meta", "payload": {"id": "x"}},
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": "*** Begin Patch\n...",
                "call_id": "c_patch",
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "c_patch", "output": "ok"},
        },
    )

    items = parse_codex_lines(lines)

    assert [item.item_type for item in items] == ["function_call", "function_call_output"]
    assert items[0].data == {
        "agent": "codex",
        "name": "apply_patch",
        "arguments": "*** Begin Patch\n...",
        "call_id": "c_patch",
    }
    assert items[1].data == {"call_id": "c_patch", "output": "ok"}


def test_codex_list_shaped_output_keeps_pairing_with_placeholder() -> None:
    """A list (image) tool output becomes a placeholder, never dropped.

    Dropping it would orphan the preceding function_call (no matching output),
    breaking rendering and any later resume.
    """
    lines = _jsonl(
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "view_image",
                "arguments": "{}",
                "call_id": "c_img",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c_img",
                "output": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
            },
        },
    )

    items = parse_codex_lines(lines)

    assert [item.item_type for item in items] == ["function_call", "function_call_output"]
    assert items[1].data == {"call_id": "c_img", "output": "[non-text tool output omitted]"}


def test_codex_parallel_call_burst_outputs_without_call_id_pair_fifo() -> None:
    """Outputs lacking call_id pair to calls in FIFO order (parallel bursts)."""
    lines = _jsonl(
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "a", "arguments": "{}", "call_id": "A"},
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "b", "arguments": "{}", "call_id": "B"},
        },
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "out-a"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "out-b"}},
    )

    outputs = [
        item for item in parse_codex_lines(lines) if item.item_type == "function_call_output"
    ]

    assert outputs[0].data == {"call_id": "A", "output": "out-a"}
    assert outputs[1].data == {"call_id": "B", "output": "out-b"}


def test_codex_explicit_call_id_consumed_so_later_blank_output_pairs_next() -> None:
    """An explicit call_id is consumed, so a later blank output pairs the next call."""
    lines = _jsonl(
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "a", "arguments": "{}", "call_id": "A"},
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "b", "arguments": "{}", "call_id": "B"},
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "A", "output": "out-a"},
        },
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "out-b"}},
    )

    outputs = [
        item for item in parse_codex_lines(lines) if item.item_type == "function_call_output"
    ]

    assert outputs[0].data == {"call_id": "A", "output": "out-a"}
    assert outputs[1].data == {"call_id": "B", "output": "out-b"}


def test_claude_whitespace_only_list_text_block_is_dropped() -> None:
    """A whitespace-only text block in a content list yields no message."""
    lines = _jsonl(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "m",
                "content": [{"type": "text", "text": "   "}],
            },
        }
    )
    assert parse_claude_lines(lines) == []


def test_claude_image_only_tool_result_uses_placeholder_not_base64() -> None:
    """An image-only tool_result becomes a placeholder, never a base64 dump."""
    lines = _jsonl(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t",
                        "content": [{"type": "image", "source": {"data": "iVBORw0KGgo"}}],
                    }
                ],
            },
        }
    )

    items = parse_claude_lines(lines)

    assert items == [
        ImportedItem(
            "function_call_output", {"call_id": "t", "output": "[non-text tool output omitted]"}
        )
    ]


def test_detect_source_scans_past_non_decisive_first_record() -> None:
    """A non-decisive first record is skipped to reach a decisive later one."""
    codex = _jsonl({"foo": "bar"}, {"type": "session_meta", "payload": {"id": "x"}})
    claude = _jsonl({"foo": "bar"}, {"type": "user", "message": {"role": "user", "content": "hi"}})
    assert detect_source(codex) == "codex"
    assert detect_source(claude) == "claude"


def test_title_rstrip_drops_trailing_space_before_ellipsis() -> None:
    """When the truncation cut lands just after a space, no ' …' dangles."""
    text = ("x" * 58) + " tail words here"
    parsed = parse_transcript(
        _jsonl({"type": "user", "message": {"role": "user", "content": text}}),
        source="claude",
    )
    assert parsed.title is not None
    assert parsed.title.endswith("…")
    assert not parsed.title.endswith(" …")
    assert len(parsed.title) < 60

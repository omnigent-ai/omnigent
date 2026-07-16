"""Tests for importing local coding-harness sessions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.session_import.local import (
    load_claude_session,
    load_codex_session,
)
from omnigent.session_import.models import SessionImportNotFoundError


def test_load_claude_session_normalizes_parent_transcript(tmp_path: Path) -> None:
    """Claude parent messages and tools become ordinary Omnigent items."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    transcript = tmp_path / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "uuid": "user-1",
            "cwd": "/repo",
            "message": {"role": "user", "content": "inspect TODO.md"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_read_1",
                        "name": "Read",
                        "input": {"file_path": "TODO.md"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "result-1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_read_1",
                        "content": "contents",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "assistant-2",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
            },
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )
    # A same-id sub-agent transcript must never be selected as the parent.
    subagent = tmp_path / "projects" / "-repo" / "subagents" / f"{session_id}.jsonl"
    subagent.parent.mkdir()
    subagent.write_text("{}\n", encoding="utf-8")

    imported = load_claude_session(session_id, claude_home=tmp_path)

    assert imported.source == "claude"
    assert imported.external_session_id == session_id
    assert imported.workspace == "/repo"
    assert imported.title == "inspect TODO.md"
    assert [item.type for item in imported.items] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert imported.items[1].data.model_dump()["call_id"] == "toolu_read_1"
    assert imported.items[3].data.model_dump()["agent"] == "claude-native-ui"


def test_load_claude_session_rejects_empty_history(tmp_path: Path) -> None:
    """An empty Claude transcript cannot create a claimed import."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    transcript = tmp_path / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()

    with pytest.raises(SessionImportNotFoundError, match="no importable history"):
        load_claude_session(session_id, claude_home=tmp_path)


def test_load_codex_session_normalizes_response_items(tmp_path: Path) -> None:
    """Codex response items retain turn grouping and omit scaffolding."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    rollout = (
        tmp_path
        / "sessions"
        / "2026"
        / "07"
        / "15"
        / f"rollout-2026-07-15T12-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    records = [
        {
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/repo"},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn_1", "cwd": "/repo"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "internal"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<environment_context>\n<cwd>/repo</cwd>\n</environment_context>",
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "inspect TODO.md"},
                    {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": '{"command":"cat TODO.md"}',
                "call_id": "call_1",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [
                    {"type": "input_text", "text": "first line\n"},
                    {"type": "input_text", "text": "second line"},
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": "*** Begin Patch",
                "call_id": "call_2",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call_2",
                "output": [{"type": "output_text", "text": ""}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        },
    ]
    rollout.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )

    imported = load_codex_session(session_id, codex_home=tmp_path)

    assert imported.source == "codex"
    assert imported.workspace == "/repo"
    assert imported.title == "inspect TODO.md"
    assert [item.type for item in imported.items] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert {item.response_id for item in imported.items} == {"codex:turn_1"}
    assert imported.items[0].data.model_dump()["is_meta"] is True
    assert imported.items[1].data.model_dump()["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,abc",
    }
    assert imported.items[2].data.model_dump() == {
        "agent": "codex-native-ui",
        "name": "shell",
        "arguments": '{"command":"cat TODO.md"}',
        "call_id": "call_1",
    }
    assert imported.items[3].data.model_dump() == {
        "call_id": "call_1",
        "output": "first line\nsecond line",
    }
    assert imported.items[5].data.model_dump() == {
        "call_id": "call_2",
        "output": "",
    }


def test_load_codex_session_finds_archived_rollout(tmp_path: Path) -> None:
    """Archived Codex sessions remain importable by their original id."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    rollout = tmp_path / "archived_sessions" / f"rollout-2026-07-15-{session_id}.jsonl"
    rollout.parent.mkdir()
    rollout.write_text(
        "".join(
            [
                json.dumps(
                    {"type": "session_meta", "payload": {"id": session_id, "cwd": "/repo"}}
                ),
                "\n",
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "archived prompt"}],
                        },
                    }
                ),
                "\n",
            ]
        ),
        encoding="utf-8",
    )

    imported = load_codex_session(session_id, codex_home=tmp_path)

    assert imported.workspace == "/repo"
    assert imported.title == "archived prompt"


def test_load_codex_session_rejects_empty_history(tmp_path: Path) -> None:
    """A structurally present but unreadable history must not claim an import."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    rollout = tmp_path / "sessions" / "2026" / "07" / "15" / f"rollout-x-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(SessionImportNotFoundError, match="no importable history"):
        load_codex_session(session_id, codex_home=tmp_path)

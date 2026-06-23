"""Unit tests for the Claude Code transcript adapter."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.entities import (
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    ReasoningData,
)
from omnigent.importers.claude_code import ClaudeCodeAdapter


def _parse(fixtures_dir: Path):
    """:returns: The parsed Claude Code fixture transcript."""
    return ClaudeCodeAdapter().parse(fixtures_dir / "claude_code_session.jsonl")


def test_parse_item_types_and_order(fixtures_dir: Path) -> None:
    """The fixture yields exactly the expected items, in transcript order,
    with the sidechain record and sidecar lines skipped."""
    parsed = _parse(fixtures_dir)
    types = [(item.type, getattr(item.data, "role", None)) for item in parsed.items]
    assert types == [
        ("message", "user"),
        ("reasoning", None),
        ("message", "assistant"),
        ("function_call", None),
        ("function_call_output", None),
        ("message", "assistant"),
    ]


def test_sidechain_record_is_skipped(fixtures_dir: Path) -> None:
    """No item carries the sidechain sub-agent text."""
    parsed = _parse(fixtures_dir)
    blob = json.dumps([item.data.model_dump() for item in parsed.items])
    assert "SUBAGENT SIDECHAIN WORK" not in blob


def test_user_message_has_no_agent(fixtures_dir: Path) -> None:
    """User messages use ``input_text`` content and never set an agent."""
    parsed = _parse(fixtures_dir)
    user = parsed.items[0].data
    assert isinstance(user, MessageData)
    assert user.role == "user"
    assert user.agent is None
    assert user.content == [{"type": "input_text", "text": "Can you read README.md?"}]


def test_reasoning_from_thinking_block(fixtures_dir: Path) -> None:
    """A ``thinking`` block becomes a reasoning item carrying the agent and
    the thinking text in its summary."""
    parsed = _parse(fixtures_dir)
    reasoning = parsed.items[1].data
    assert isinstance(reasoning, ReasoningData)
    assert reasoning.agent == "claude-opus-4-8"
    assert reasoning.summary == [
        {"type": "summary_text", "text": "The user wants the README. I should read it."}
    ]


def test_assistant_message_uses_output_text_and_agent(fixtures_dir: Path) -> None:
    """Assistant messages require an agent and use ``output_text`` content."""
    parsed = _parse(fixtures_dir)
    assistant = parsed.items[2].data
    assert isinstance(assistant, MessageData)
    assert assistant.role == "assistant"
    assert assistant.agent == "claude-opus-4-8"
    assert assistant.content == [{"type": "output_text", "text": "Sure, let me read it."}]


def test_function_call_arguments_is_json_string(fixtures_dir: Path) -> None:
    """``tool_use.input`` (an object) is serialized to a JSON-encoded string
    on the function_call item, and the call id is preserved."""
    parsed = _parse(fixtures_dir)
    call = parsed.items[3].data
    assert isinstance(call, FunctionCallData)
    assert call.name == "Read"
    assert call.call_id == "toolu_1"
    assert call.agent == "claude-opus-4-8"
    assert isinstance(call.arguments, str)
    assert json.loads(call.arguments) == {"file_path": "README.md"}


def test_function_call_output_pairs_by_call_id(fixtures_dir: Path) -> None:
    """A ``tool_result`` becomes a function_call_output paired by call id."""
    parsed = _parse(fixtures_dir)
    output = parsed.items[4].data
    assert isinstance(output, FunctionCallOutputData)
    assert output.call_id == "toolu_1"
    assert output.output == "# My Project\nHello."


def test_response_id_grouping(fixtures_dir: Path) -> None:
    """The user prompt opens its own response; every assistant-side item and
    the tool output that follows share the one assistant-turn response id."""
    parsed = _parse(fixtures_dir)
    response_ids = [item.response_id for item in parsed.items]
    assert response_ids[0] != response_ids[1]
    assert len(set(response_ids[1:])) == 1


def test_metadata(fixtures_dir: Path) -> None:
    """Title (from the ``ai-title`` sidecar), session id, model, cwd, branch,
    and earliest timestamp are all harvested."""
    parsed = _parse(fixtures_dir)
    assert parsed.title == "Reading a file"
    assert parsed.external_session_id == "11111111-2222-3333-4444-555555555555"
    assert parsed.model == "claude-opus-4-8"
    assert parsed.cwd == "/home/me/repo"
    assert parsed.git_branch == "feature/login"
    assert parsed.created_at is not None


def test_discover_skips_subagents(tmp_path: Path) -> None:
    """Discovery finds top-level ``<sessionId>.jsonl`` files but skips any
    ``subagents/`` directory."""
    root = tmp_path / "projects" / "encoded-cwd"
    root.mkdir(parents=True)
    (root / "session-a.jsonl").write_text("{}\n", encoding="utf-8")
    subagents = root / "session-a" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-1.jsonl").write_text("{}\n", encoding="utf-8")

    refs = ClaudeCodeAdapter().discover(tmp_path)

    assert [ref.session_id for ref in refs] == ["session-a"]


def test_discover_missing_root_is_empty(tmp_path: Path) -> None:
    """Discovery returns an empty list when the root does not exist."""
    assert ClaudeCodeAdapter().discover(tmp_path / "nope") == []

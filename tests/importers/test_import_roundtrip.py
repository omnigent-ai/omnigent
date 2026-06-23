"""Round-trip tests: import a fixture transcript into a real conversation
store, then read the conversation and its items back."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from omnigent.entities import FunctionCallData
from omnigent.importers import IMPORTED_FROM_LABEL_KEY, import_all, import_transcript
from omnigent.importers.claude_code import ClaudeCodeAdapter
from omnigent.importers.codex import CodexAdapter
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def test_claude_roundtrip(
    conversation_store: SqlAlchemyConversationStore,
    fixtures_dir: Path,
) -> None:
    """Importing the Claude fixture persists a labeled conversation whose
    items round-trip in order with their structure intact."""
    adapter = ClaudeCodeAdapter()
    conversation_id = import_transcript(
        conversation_store, adapter, fixtures_dir / "claude_code_session.jsonl"
    )
    assert conversation_id is not None

    conversation = conversation_store.get_conversation(conversation_id)
    assert conversation is not None
    assert conversation.title == "Reading a file"
    assert conversation.external_session_id == "11111111-2222-3333-4444-555555555555"
    assert conversation.workspace == "/home/me/repo"
    assert conversation.git_branch == "feature/login"
    assert conversation.agent_id is None  # imports are agentless history
    assert conversation.labels.get(IMPORTED_FROM_LABEL_KEY) == "claude_code"

    items = conversation_store.list_items(conversation_id, limit=100, order="asc").data
    assert [item.type for item in items] == [
        "message",
        "reasoning",
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    # The function_call arguments survive as a JSON-encoded string.
    call = items[3].data
    assert isinstance(call, FunctionCallData)
    assert json.loads(call.arguments) == {"file_path": "README.md"}


def test_codex_roundtrip(
    conversation_store: SqlAlchemyConversationStore,
    fixtures_dir: Path,
) -> None:
    """Importing the Codex fixture persists a labeled conversation with the
    expected item structure and ordering."""
    adapter = CodexAdapter()
    conversation_id = import_transcript(
        conversation_store, adapter, fixtures_dir / "codex_session.jsonl"
    )
    assert conversation_id is not None

    conversation = conversation_store.get_conversation(conversation_id)
    assert conversation is not None
    assert conversation.external_session_id == "019e42f3-07d6-7c83-b04d-caee8078cf51"
    assert conversation.workspace == "/home/me/project"
    assert conversation.git_branch == "main"
    assert conversation.labels.get(IMPORTED_FROM_LABEL_KEY) == "codex"

    items = conversation_store.list_items(conversation_id, limit=100, order="asc").data
    assert [item.type for item in items] == [
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
        "message",
        "function_call",
        "function_call_output",
    ]


def test_import_all_discovers_and_imports(
    conversation_store: SqlAlchemyConversationStore,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """``import_all`` discovers every transcript under a root and imports each
    into its own conversation."""
    root = tmp_path / "projects" / "encoded"
    root.mkdir(parents=True)
    for name in ("aaaa.jsonl", "bbbb.jsonl"):
        shutil.copy(fixtures_dir / "claude_code_session.jsonl", root / name)

    ids = import_all(conversation_store, ClaudeCodeAdapter(), tmp_path)

    assert len(ids) == 2
    assert len(set(ids)) == 2
    for conversation_id in ids:
        conversation = conversation_store.get_conversation(conversation_id)
        assert conversation is not None
        assert conversation.labels.get(IMPORTED_FROM_LABEL_KEY) == "claude_code"


def test_import_transcript_skips_empty_file(
    conversation_store: SqlAlchemyConversationStore,
    tmp_path: Path,
) -> None:
    """An empty transcript parses to zero items and is skipped — no empty
    conversation is created."""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    result = import_transcript(conversation_store, ClaudeCodeAdapter(), empty)

    assert result is None
    assert conversation_store.list_conversations(limit=10).data == []


def test_import_all_skips_empty_and_garbage(
    conversation_store: SqlAlchemyConversationStore,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """``import_all`` imports only the transcripts that yield items; empty and
    unparseable files are skipped."""
    root = tmp_path / "projects" / "encoded"
    root.mkdir(parents=True)
    shutil.copy(fixtures_dir / "claude_code_session.jsonl", root / "good.jsonl")
    (root / "empty.jsonl").write_text("", encoding="utf-8")
    (root / "garbage.jsonl").write_text("not json\n{also not valid\n", encoding="utf-8")

    ids = import_all(conversation_store, ClaudeCodeAdapter(), tmp_path)

    assert len(ids) == 1

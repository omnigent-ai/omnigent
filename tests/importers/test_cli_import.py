"""CLI tests for ``omnigent import`` via click's :class:`CliRunner`.

Each test drives the command against the temp ``db_uri`` fixture and then reads
the conversations/items back through the ``conversation_store`` fixture (the
same database) to assert the import actually landed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.importers import IMPORTED_FROM_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def _seed_root(fixtures_dir: Path, tmp_path: Path, names: list[str]) -> Path:
    """Copy the Claude fixture under a fresh root using the given file names.

    :returns: The root directory to pass as ``--root``.
    """
    root = tmp_path / "projects" / "encoded"
    root.mkdir(parents=True)
    for name in names:
        shutil.copy(fixtures_dir / "claude_code_session.jsonl", root / name)
    return tmp_path


def test_import_file(
    conversation_store: SqlAlchemyConversationStore,
    fixtures_dir: Path,
    db_uri: str,
) -> None:
    """``--file`` imports one transcript; the conversation and its items land
    in the store with the harness label."""
    result = CliRunner().invoke(
        cli,
        [
            "import",
            "claude_code",
            "--file",
            str(fixtures_dir / "claude_code_session.jsonl"),
            "--database-uri",
            db_uri,
        ],
    )

    assert result.exit_code == 0, result.output
    conversations = conversation_store.list_conversations(limit=10).data
    assert len(conversations) == 1
    conversation = conversations[0]
    assert conversation.id in result.output
    assert conversation.labels.get(IMPORTED_FROM_LABEL_KEY) == "claude_code"
    items = conversation_store.list_items(conversation.id, limit=100, order="asc").data
    assert len(items) == 6


def test_import_all(
    conversation_store: SqlAlchemyConversationStore,
    fixtures_dir: Path,
    tmp_path: Path,
    db_uri: str,
) -> None:
    """``--all`` imports every discovered transcript under the root."""
    root = _seed_root(fixtures_dir, tmp_path, ["a.jsonl", "b.jsonl"])

    result = CliRunner().invoke(
        cli,
        ["import", "claude_code", "--all", "--root", str(root), "--database-uri", db_uri],
    )

    assert result.exit_code == 0, result.output
    assert len(conversation_store.list_conversations(limit=10).data) == 2


def test_import_session(
    conversation_store: SqlAlchemyConversationStore,
    fixtures_dir: Path,
    tmp_path: Path,
    db_uri: str,
) -> None:
    """``--session`` imports only the transcript whose session id (filename
    stem) matches."""
    root = _seed_root(fixtures_dir, tmp_path, ["target-session.jsonl", "other.jsonl"])

    result = CliRunner().invoke(
        cli,
        [
            "import",
            "claude_code",
            "--session",
            "target-session",
            "--root",
            str(root),
            "--database-uri",
            db_uri,
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(conversation_store.list_conversations(limit=10).data) == 1


def test_import_empty_file_is_skipped(
    conversation_store: SqlAlchemyConversationStore,
    tmp_path: Path,
    db_uri: str,
) -> None:
    """An empty transcript is skipped with a warning — no conversation lands."""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        ["import", "claude_code", "--file", str(empty), "--database-uri", db_uri],
    )

    assert result.exit_code == 0, result.output
    assert "Skipped" in result.output
    assert conversation_store.list_conversations(limit=10).data == []


def test_no_selector_errors(db_uri: str) -> None:
    """Omitting --file/--session/--all is a usage error."""
    result = CliRunner().invoke(cli, ["import", "claude_code", "--database-uri", db_uri])

    assert result.exit_code != 0
    assert "exactly one of --file, --session, or --all" in result.output


def test_unknown_harness_errors(db_uri: str) -> None:
    """An unregistered harness is rejected with the available names."""
    result = CliRunner().invoke(cli, ["import", "pi", "--all", "--database-uri", db_uri])

    assert result.exit_code != 0
    assert "unknown harness" in result.output
    assert "claude_code" in result.output


def test_session_no_match_errors(
    conversation_store: SqlAlchemyConversationStore,
    fixtures_dir: Path,
    tmp_path: Path,
    db_uri: str,
) -> None:
    """A ``--session`` id with no matching transcript is an error and imports
    nothing."""
    root = _seed_root(fixtures_dir, tmp_path, ["other.jsonl"])

    result = CliRunner().invoke(
        cli,
        [
            "import",
            "claude_code",
            "--session",
            "does-not-exist",
            "--root",
            str(root),
            "--database-uri",
            db_uri,
        ],
    )

    assert result.exit_code != 0
    assert "no claude_code transcript with session id" in result.output
    assert conversation_store.list_conversations(limit=10).data == []

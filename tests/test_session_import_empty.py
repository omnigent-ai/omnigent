"""Empty sessions raise SessionImportEmptyError; loaders and classifiers handle them.

A session with no history is skipped, not failed. Codex writes a rollout as soon
as it opens, so a session closed without a prompt leaves one with only
``session_meta`` (or only the injected AGENTS.md / environment context). The
loaders now raise ``SessionImportEmptyError`` for them; the host reports it
with the ``session_empty`` code, the server counts it under ``skipped`` (stream
``skipped`` events, ``done.skipped`` / ``skipped_sessions``) instead of
``failed``, and the CLI prints a note without failing the command. Hosts that
predate the code send only the loader's "has no importable history" text, which
the server classifies the same way; servers that predate it still count the
empty session as failed, as before.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.session_import import local as local_module
from omnigent.session_import.errors import (
    SKIPPED_IMPORT_CODES,
    ImportErrorCode,
    import_code_is_retryable,
    reports_empty_session,
)
from omnigent.session_import.local import (
    list_recent_local_session_ids,
    load_local_session,
)
from omnigent.session_import.models import (
    SessionImportEmptyError,
    SessionImportNotFoundError,
)


def _rec(kind: str, payload: dict) -> str:
    return json.dumps({"timestamp": "2026-10-03T00:00:00.000Z", "type": kind, "payload": payload})


def _msg(role: str, text: str) -> str:
    block = "input_text" if role in ("user", "developer") else "output_text"
    return _rec(
        "response_item",
        {
            "type": "message",
            "role": role,
            "content": [{"type": block, "text": text}],
        },
    )


def _meta(session_id: str, source: object = "cli") -> str:
    return _rec(
        "session_meta",
        {"id": session_id, "cwd": "/repo", "originator": "codex_cli_rs", "source": source},
    )


def write_rollout(home: Path, session_id: str, lines: list[str], *, mtime: float) -> Path:
    path = (
        home
        / ".codex"
        / "sessions"
        / "2026"
        / "10"
        / "03"
        / f"rollout-2026-10-03T00-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def normal_lines(session_id: str) -> list[str]:
    return [
        _meta(session_id),
        _rec("turn_context", {"turn_id": "t0", "cwd": "/repo"}),
        _msg("user", "summarize the repo"),
        _msg("assistant", "here it is"),
    ]


def meta_only_lines(session_id: str) -> list[str]:
    """What Codex leaves when it is opened and closed without a prompt."""
    return [_meta(session_id)]


def context_only_lines(session_id: str) -> list[str]:
    """Injected context, then the first turn aborted before any message."""
    return [
        _meta(session_id),
        _msg("developer", "<permissions instructions>sandboxed</permissions instructions>"),
        _msg(
            "user",
            "# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>be brief</INSTRUCTIONS>",
        ),
        _msg("user", "<environment_context>\n  <cwd>/repo</cwd>\n</environment_context>"),
        _rec("turn_context", {"turn_id": "t0", "cwd": "/repo"}),
        _rec("event_msg", {"type": "turn_aborted", "reason": "interrupted"}),
    ]


# Session IDs used for tests
_NORMAL = "0199a5c0-0000-7abc-8def-000000000001"
_EMPTY = "0199a5c0-0000-7abc-8def-000000000002"
_CONTEXT_ONLY = "0199a5c0-0000-7abc-8def-000000000003"
_SUBAGENT = "0199a5c0-0000-7abc-8def-000000000004"
_EXEC = "0199a5c0-0000-7abc-8def-000000000005"
_CLAUDE_EMPTY = "a1b2c3d4-1234-5678-9abc-def012345670"


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A HOME for real Claude/Codex transcripts."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    return tmp_path


def test_session_empty_is_a_non_retryable_skip() -> None:
    assert ImportErrorCode.SESSION_EMPTY == "session_empty"
    assert import_code_is_retryable(ImportErrorCode.SESSION_EMPTY) is False
    assert frozenset({"session_empty"}) == SKIPPED_IMPORT_CODES


def test_empty_error_is_still_a_not_found_error_for_older_callers() -> None:
    assert issubclass(SessionImportEmptyError, SessionImportNotFoundError)


def test_reports_empty_session_matches_the_loader_wording_only() -> None:
    assert reports_empty_session("Codex session '0199' has no importable history")
    assert reports_empty_session("Claude Code session 'x' has no importable history.")
    assert not reports_empty_session("Codex session '0199' was not found")
    assert not reports_empty_session("This session's transcript could not be read.")
    assert not reports_empty_session(None)


def test_codex_rollout_with_only_session_meta_is_empty(home: Path) -> None:
    write_rollout(home, _EMPTY, meta_only_lines(_EMPTY), mtime=1_000)
    with pytest.raises(SessionImportEmptyError) as exc_info:
        load_local_session("codex", _EMPTY)
    # Still a not-found error for callers that predate the subclass.
    assert isinstance(exc_info.value, SessionImportNotFoundError)
    assert str(exc_info.value) == f"Codex session {_EMPTY!r} has no importable history"


def test_codex_rollout_with_only_injected_context_is_empty(home: Path) -> None:
    # It used to import as a blank "Untitled session" of hidden items.
    write_rollout(home, _CONTEXT_ONLY, context_only_lines(_CONTEXT_ONLY), mtime=1_000)
    with pytest.raises(SessionImportEmptyError):
        load_local_session("codex", _CONTEXT_ONLY)


def test_codex_budgeted_read_of_only_context_is_empty(home: Path) -> None:
    path = write_rollout(home, _CONTEXT_ONLY, context_only_lines(_CONTEXT_ONLY), mtime=1_000)
    with patch.object(local_module, "IMPORT_READ_BUDGET_BYTES", path.stat().st_size - 1):
        with pytest.raises(SessionImportEmptyError):
            load_local_session("codex", _CONTEXT_ONLY)


def test_codex_compaction_baseline_counts_as_history(home: Path) -> None:
    # A hidden compaction summary is the session's history, not injected context.
    summary = {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "The following is the Codex agent history so far"}
        ],
    }
    lines = [
        *context_only_lines(_CONTEXT_ONLY),
        _rec("compacted", {"replacement_history": [summary]}),
    ]
    write_rollout(home, _CONTEXT_ONLY, lines, mtime=1_000)
    with patch.object(local_module, "_IMPORT_COMPACT_TRIM_BYTES", 0):
        session = load_local_session("codex", _CONTEXT_ONLY)
    assert [item.response_id for item in session.items] == ["codex:compaction"]


def test_codex_session_with_a_prompt_still_imports(home: Path) -> None:
    write_rollout(
        home, _NORMAL, [*context_only_lines(_NORMAL), _msg("user", "real prompt")], mtime=1
    )
    session = load_local_session("codex", _NORMAL)
    assert session.title == "real prompt"


def test_claude_transcript_without_messages_is_empty(home: Path) -> None:
    project = home / ".claude" / "projects" / "-repo"
    project.mkdir(parents=True)
    (project / f"{_CLAUDE_EMPTY}.jsonl").write_text(
        json.dumps({"type": "summary", "summary": "nothing", "leafUuid": "u1"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SessionImportEmptyError):
        load_local_session("claude", _CLAUDE_EMPTY)


def test_missing_session_is_not_empty(home: Path) -> None:
    with pytest.raises(SessionImportNotFoundError) as exc_info:
        load_local_session("codex", _EMPTY)
    assert not isinstance(exc_info.value, SessionImportEmptyError)


def test_listing_skips_subagent_and_exec_rollouts(home: Path) -> None:
    write_rollout(home, _EMPTY, meta_only_lines(_EMPTY), mtime=1_000)
    write_rollout(home, _NORMAL, normal_lines(_NORMAL), mtime=2_000)
    write_rollout(home, _CONTEXT_ONLY, context_only_lines(_CONTEXT_ONLY), mtime=3_000)
    subagent = {"subagent": {"thread_spawn": {"parent_thread_id": _NORMAL, "depth": 1}}}
    write_rollout(
        home, _SUBAGENT, [_meta(_SUBAGENT, subagent), _msg("user", "child task")], mtime=4_000
    )
    write_rollout(home, _EXEC, [_meta(_EXEC, "exec"), _msg("user", "automation")], mtime=5_000)
    assert list_recent_local_session_ids("codex", limit=10) == (_CONTEXT_ONLY, _NORMAL, _EMPTY)

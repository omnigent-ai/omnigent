"""omnigent import reports empty sessions in a user-friendly way: skipped, not failed.

Empty sessions appear in batch imports with a "Skipped (no history): N" summary
line; in single-session imports they report "Nothing to import: ...". Exit code
is zero for empty-only batches (success) and one for batches mixed with failures.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from omnigent.cli import cli


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


_BASE = "http://localhost:6767"
_NORMAL = "0199a5c0-0000-7abc-8def-000000000001"
_EMPTY = "0199a5c0-0000-7abc-8def-000000000002"
_CONTEXT_ONLY = "0199a5c0-0000-7abc-8def-000000000003"
_SUBAGENT = "0199a5c0-0000-7abc-8def-000000000004"
_EXEC = "0199a5c0-0000-7abc-8def-000000000005"


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A HOME for real Codex transcripts."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    return tmp_path


def write_codex_fixture(home: Path) -> None:
    """One real session, two empty ones, and two runs the picker never lists."""
    write_rollout(home, _EMPTY, meta_only_lines(_EMPTY), mtime=1_000)
    write_rollout(home, _NORMAL, normal_lines(_NORMAL), mtime=2_000)
    write_rollout(home, _CONTEXT_ONLY, context_only_lines(_CONTEXT_ONLY), mtime=3_000)
    subagent = {"subagent": {"thread_spawn": {"parent_thread_id": _NORMAL, "depth": 1}}}
    write_rollout(
        home, _SUBAGENT, [_meta(_SUBAGENT, subagent), _msg("user", "child task")], mtime=4_000
    )
    write_rollout(home, _EXEC, [_meta(_EXEC, "exec"), _msg("user", "automation")], mtime=5_000)


def _run(home: Path, *args: str, status: int = 201) -> tuple[Any, list[dict[str, Any]]]:
    posts: list[dict[str, Any]] = []

    def _post(url: str, *, json: dict[str, Any], **_kwargs: Any) -> httpx.Response:
        posts.append(json)
        if status >= 400:
            body = {
                "error": {"message": "storage timed out", "import_code": "session_save_timeout"}
            }
            return httpx.Response(status, json=body, request=httpx.Request("POST", url))
        body = {
            "session_id": f"conv_{len(posts)}",
            "status": "imported",
            "item_count": len(json["items"]),
        }
        return httpx.Response(201, json=body, request=httpx.Request("POST", url))

    with patch("omnigent.cli._resolve_attach_server", return_value=_BASE):
        with patch("httpx.post", side_effect=_post):
            result = CliRunner().invoke(
                cli,
                ["import", *args],
                env={"HOME": str(home), "OMNIGENT_CONFIG_HOME": str(home / ".omnigent")},
            )
    return result, posts


def test_batch_notes_empty_sessions_and_exits_zero(home: Path) -> None:
    write_codex_fixture(home)
    result, posts = _run(home, "--harness", "codex", "--last", "10")
    assert result.exit_code == 0, result.output
    assert [p["external_session_id"] for p in posts] == [_NORMAL]
    assert f"Skipped {_EMPTY}: no history to import." in result.output
    assert f"Skipped {_CONTEXT_ONLY}: no history to import." in result.output
    assert "Failed " not in result.output
    assert "has no importable history" not in result.output
    assert "Imported: 1\n" in result.output
    assert "Skipped (no history to import): 2\n" in result.output
    assert "Failed: 0\n" in result.output


def test_batch_without_empty_sessions_prints_no_skipped_line(home: Path) -> None:
    write_rollout(home, _NORMAL, normal_lines(_NORMAL), mtime=1)
    result, _posts = _run(home, "--harness", "codex", "--last", "10")
    assert result.exit_code == 0, result.output
    assert "Skipped" not in result.output


def test_real_failures_still_fail_the_batch_without_counting_skips(home: Path) -> None:
    write_codex_fixture(home)
    result, _posts = _run(home, "--harness", "codex", "--last", "10", status=503)
    assert result.exit_code == 1, result.output
    assert f"Failed {_NORMAL}: Import failed (503): storage timed out" in result.output
    assert "Skipped (no history to import): 2\n" in result.output
    assert "Failed: 1\n" in result.output
    assert "1 session(s) failed to import" in result.output


def test_single_empty_session_is_a_note_not_an_error(home: Path) -> None:
    write_rollout(home, _EMPTY, meta_only_lines(_EMPTY), mtime=1)
    result, posts = _run(home, "--harness", "codex", "--session", _EMPTY)
    assert result.exit_code == 0, result.output
    assert posts == []
    assert (
        f"Nothing to import: Codex session {_EMPTY!r} has no importable history" in result.output
    )


def test_single_missing_session_still_errors(home: Path) -> None:
    result, _posts = _run(home, "--harness", "codex", "--session", _EMPTY)
    assert result.exit_code == 1, result.output
    assert "was not found" in result.output

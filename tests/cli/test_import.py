"""Tests for the top-level ``omnigent import`` command."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import respx
from click.testing import CliRunner

from omnigent.cli import _CLICK_SUBCOMMANDS, cli

_BASE = "http://localhost:6767"


def _write_claude_transcript(
    home: Path,
    session_id: str,
    *,
    text: str,
    modified_at: int | None = None,
    uuid_value: str = "user-1",
) -> Path:
    """Create one minimal parent transcript under a fake Claude home."""
    transcript = home / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": uuid_value,
                "cwd": "/repo",
                "message": {"role": "user", "content": text},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if modified_at is not None:
        os.utime(transcript, (modified_at, modified_at))
    return transcript


@respx.mock
def test_import_command_loads_local_session_and_posts_normalized_items(tmp_path: Path) -> None:
    """The CLI reads local history and submits only Omnigent item shapes."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    _write_claude_transcript(tmp_path, session_id, text="inspect TODO.md")
    route = respx.post(f"{_BASE}/v1/imports").mock(
        return_value=httpx.Response(
            201,
            json={"session_id": "conv_imported", "status": "imported", "item_count": 1},
        )
    )

    with patch("omnigent.cli._resolve_attach_server", return_value=_BASE):
        result = CliRunner().invoke(
            cli,
            ["import", "--harness", "claude", "--session", session_id],
            env={"HOME": str(tmp_path)},
        )

    assert result.exit_code == 0, result.output
    assert "import" in _CLICK_SUBCOMMANDS
    assert "conv_imported" in result.output
    request = route.calls.last.request
    payload = json.loads(request.content)
    assert payload == {
        "source": "claude",
        "external_session_id": session_id,
        "workspace": "/repo",
        "items": [
            {
                "type": "message",
                "response_id": "resp_claude_c6c289e49e9c05b2145860387b73bcb1",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "inspect TODO.md"}],
                },
            }
        ],
    }


def test_import_command_rejects_cursor() -> None:
    """The v0 import command accepts only Claude Code and Codex."""
    result = CliRunner().invoke(
        cli,
        ["import", "--harness", "cursor", "--session", "cursor-session"],
    )

    assert result.exit_code == 2
    assert "Invalid value for '--harness'" in result.output


@respx.mock
def test_import_command_imports_last_sessions_oldest_first_and_skips_duplicates(
    tmp_path: Path,
) -> None:
    """A batch preserves source recency and treats duplicate imports as skips."""
    session_ids = (
        "a1b2c3d4-1234-5678-9abc-def012345671",
        "a1b2c3d4-1234-5678-9abc-def012345672",
        "a1b2c3d4-1234-5678-9abc-def012345673",
    )
    for modified_at, session_id in enumerate(session_ids, start=1):
        _write_claude_transcript(
            tmp_path,
            session_id,
            text=f"prompt {modified_at}",
            modified_at=modified_at,
        )
    route = respx.post(f"{_BASE}/v1/imports").mock(
        side_effect=[
            httpx.Response(
                201,
                json={"session_id": "imported-middle", "status": "imported", "item_count": 1},
            ),
            httpx.Response(
                409,
                json={"error": {"message": "already imported"}},
            ),
        ]
    )

    with patch("omnigent.cli._resolve_attach_server", return_value=_BASE):
        result = CliRunner().invoke(
            cli,
            ["import", "--harness", "claude", "--last", "2"],
            env={"HOME": str(tmp_path)},
        )

    assert result.exit_code == 0, result.output
    payloads = [json.loads(call.request.content) for call in route.calls]
    assert [payload["external_session_id"] for payload in payloads] == list(session_ids[1:])
    assert "Imported: 1" in result.output
    assert "Already imported: 1" in result.output
    assert "Failed: 0" in result.output


@respx.mock
def test_import_command_continues_batch_after_session_failure(tmp_path: Path) -> None:
    """One invalid server response does not prevent later sessions importing."""
    session_ids = (
        "a1b2c3d4-1234-5678-9abc-def012345674",
        "a1b2c3d4-1234-5678-9abc-def012345675",
    )
    for modified_at, session_id in enumerate(session_ids, start=1):
        _write_claude_transcript(
            tmp_path,
            session_id,
            text=f"prompt {modified_at}",
            modified_at=modified_at,
        )
    route = respx.post(f"{_BASE}/v1/imports").mock(
        side_effect=[
            httpx.Response(422, json={"error": {"message": "invalid transcript"}}),
            httpx.Response(
                201,
                json={"session_id": "imported-new", "status": "imported", "item_count": 1},
            ),
        ]
    )

    with patch("omnigent.cli._resolve_attach_server", return_value=_BASE):
        result = CliRunner().invoke(
            cli,
            ["import", "--harness", "claude", "--last", "2"],
            env={"HOME": str(tmp_path)},
        )

    assert result.exit_code == 1
    assert len(route.calls) == 2
    assert "Imported: 1" in result.output
    assert "Failed: 1" in result.output


def test_import_command_requires_exactly_one_session_selector() -> None:
    """Single and batch selectors cannot be omitted or combined."""
    runner = CliRunner()

    missing = runner.invoke(cli, ["import", "--harness", "claude"])
    combined = runner.invoke(
        cli,
        ["import", "--harness", "claude", "--session", "session-id", "--last", "2"],
    )

    assert missing.exit_code == 2
    assert combined.exit_code == 2
    assert "Provide exactly one of --session or --last" in missing.output
    assert "Provide exactly one of --session or --last" in combined.output


def test_import_command_limits_batch_size() -> None:
    """The CLI rejects batch sizes above the safety cap."""
    result = CliRunner().invoke(
        cli,
        ["import", "--harness", "codex", "--last", "51"],
    )

    assert result.exit_code == 2
    assert "51 is not in the range 1<=x<=50" in result.output

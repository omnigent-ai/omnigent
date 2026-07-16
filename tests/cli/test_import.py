"""Tests for the top-level ``omnigent import`` command."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import respx
from click.testing import CliRunner

from omnigent.cli import _CLICK_SUBCOMMANDS, cli

_BASE = "http://localhost:6767"


@respx.mock
def test_import_command_loads_local_session_and_posts_normalized_items(tmp_path: Path) -> None:
    """The CLI reads local history and submits only Omnigent item shapes."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    transcript = tmp_path / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "user-1",
                "cwd": "/repo",
                "message": {"role": "user", "content": "inspect TODO.md"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
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

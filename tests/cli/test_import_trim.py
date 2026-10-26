"""CLI import with trim and budget: cap items, notice, CLI output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
from click.testing import CliRunner

from omnigent import cli as cli_module
from omnigent.cli import cli
from omnigent.server.routes import imports as imports_module
from omnigent.session_import import local as local_module
from omnigent.session_import.local import IMPORT_TRIMMED_NOTICE_CODE

_CLAUDE_ID = "a1b2c3d4-1234-5678-9abc-def012345670"
_BASE = "http://localhost:6767"


def _claude_record(record: dict[str, Any], session_id: str, n: int) -> dict[str, Any]:
    return {
        "uuid": str(UUID(int=n + 1)),
        "parentUuid": str(UUID(int=n)) if n else None,
        "sessionId": session_id,
        "cwd": "/repo",
        "timestamp": "2026-10-01T00:00:00.000Z",
        "isSidechain": False,
        "userType": "external",
        "version": "2.1.0",
        **record,
    }


def write_claude_transcript(
    home: Path, session_id: str, *, turns: int, title: str | None = None
) -> Path:
    """A Claude Code transcript of ``turns`` 4-item turns (prompt "turn N")."""
    records: list[dict[str, Any]] = []
    for n in range(turns):
        records.append({"type": "user", "message": {"role": "user", "content": f"turn {n}"}})
        records.append(
            {
                "type": "assistant",
                "message": {
                    "id": f"msg_{n}",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [
                        {"type": "text", "text": f"ok {n}"},
                        {
                            "type": "tool_use",
                            "id": f"toolu_{n}",
                            "name": "Bash",
                            "input": {"command": "true"},
                        },
                    ],
                },
            }
        )
        records.append(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": f"out {n}"}
                    ],
                },
            }
        )
    lines = [json.dumps(_claude_record(r, session_id, i)) for i, r in enumerate(records)]
    if title is not None:
        lines.append(json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": session_id}))
    path = home / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME with small import cap for CLI tests."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(local_module, "IMPORT_MAX_ITEMS", 10)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    return tmp_path


class TestCliTrim:
    def _import(self, home: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Any]:
        posts: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []

        def _post(url: str, *, json: dict[str, Any], **kwargs: Any) -> httpx.Response:
            posts.append(json)
            calls.append(kwargs)
            body = {
                "session_id": "conv_new",
                "status": "imported",
                "item_count": len(json["items"]),
            }
            return httpx.Response(201, json=body, request=httpx.Request("POST", url))

        with (
            patch("omnigent.cli._resolve_attach_server", return_value=_BASE),
            patch("httpx.post", side_effect=_post),
        ):
            result = CliRunner().invoke(
                cli,
                ["import", "--harness", "claude", "--session", _CLAUDE_ID],
                env={"HOME": str(home), "OMNIGENT_CONFIG_HOME": str(home / ".omnigent")},
            )
        assert result.exit_code == 0, result.output
        return posts, calls, result

    def test_cli_imports_the_trimmed_history_and_says_so(self, cli_home: Path) -> None:
        write_claude_transcript(cli_home, _CLAUDE_ID, turns=10)
        (payload,), (call,), result = self._import(cli_home)
        assert len(payload["items"]) == 10
        assert payload["items"][-1]["type"] == "error"
        assert payload["items"][-1]["data"]["code"] == IMPORT_TRIMMED_NOTICE_CODE
        # The first prompt is kept, so the server derives the title as usual.
        assert payload["title"] is None
        # Every item survives the server's own validation.
        imports_module.ImportSessionRequest.model_validate(payload)
        assert (
            "Imported 10 item(s) (later 31 left out: too long to import in full) into"
            in result.output
        )
        # A full-cap session can take over two minutes to save.
        assert call["timeout"] == cli_module._IMPORT_REQUEST_TIMEOUT_S == 270.0

    def test_untrimmed_cli_payload_keeps_its_title_unset(self, cli_home: Path) -> None:
        write_claude_transcript(cli_home, _CLAUDE_ID, turns=2)
        (payload,), _calls, result = self._import(cli_home)
        assert payload["title"] is None
        assert "Imported 8 item(s) into" in result.output
        assert "left out" not in result.output

    def test_budgeted_read_says_later_history(
        self, cli_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = write_claude_transcript(cli_home, _CLAUDE_ID, turns=40)
        monkeypatch.setattr(local_module, "IMPORT_READ_BUDGET_BYTES", path.stat().st_size // 4)
        monkeypatch.setattr(local_module, "IMPORT_MAX_ITEMS", 1000)
        (payload,), _calls, result = self._import(cli_home)
        assert payload["items"][-1]["data"]["message"].endswith("later history was left out.")
        imports_module.ImportSessionRequest.model_validate(payload)
        count = len(payload["items"])
        assert (
            f"Imported {count} item(s) (later history left out: too long to import in full) into"
            in result.output
        )

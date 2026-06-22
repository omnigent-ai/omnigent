"""Unit tests for the TUI ``/logs`` slash command."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from omnigent_ui_sdk import RichBlockFormatter

from omnigent.repl._repl import COMMANDS, handle_slash_command
from omnigent.repl._session_log import collect_log_files, write_logs_zip
from tests.unit.repl.helpers import CapturingHost


class _Session:
    """Minimal slash-command session stub."""

    model = "test-agent"
    session_id = "sess_current"


class _Client:
    """Minimal client stub; _cmd_logs' transcript write is monkeypatched."""


# ---------------------------------------------------------------------------
# Log zip helpers
# ---------------------------------------------------------------------------


def test_collect_log_files_uses_only_explicit_paths(tmp_path: Path) -> None:
    """Collector does not walk whole log/debug dirs from one path."""
    logs = tmp_path / "logs"
    debug = tmp_path / "debug"
    logs.mkdir()
    debug.mkdir()
    current_cli = logs / "cli-current.log"
    old_cli = logs / "cli-old.log"
    current_event = debug / "events-current.jsonl"
    current_cli.write_text("cli\n", encoding="utf-8")
    old_cli.write_text("old\n", encoding="utf-8")
    current_event.write_text("{}\n", encoding="utf-8")
    (logs / "old.zip").write_text("skip me\n", encoding="utf-8")

    entries = collect_log_files([current_cli, current_event])

    assert [(path.name, arcname) for path, arcname in entries] == [
        ("events-current.jsonl", "debug/events-current.jsonl"),
        ("cli-current.log", "logs/cli-current.log"),
    ]


def test_write_logs_zip_contains_only_explicit_current_session_files(tmp_path: Path) -> None:
    """The helper writes a zip from explicit paths, not sibling logs."""
    logs = tmp_path / "logs"
    debug = tmp_path / "debug"
    logs.mkdir()
    debug.mkdir()
    current_cli = logs / "cli-current.log"
    old_cli = logs / "cli-old.log"
    current_event = debug / "events-current.jsonl"
    current_cli.write_text("cli log\n", encoding="utf-8")
    old_cli.write_text("old log\n", encoding="utf-8")
    current_event.write_text("event log\n", encoding="utf-8")

    zip_path, count = write_logs_zip(
        tmp_path / "bundle.zip",
        log_paths=[current_cli, current_event],
        session_id="sess_current",
    )

    assert count == 2
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        assert sorted(zf.namelist()) == ["debug/events-current.jsonl", "logs/cli-current.log"]
        assert zf.read("logs/cli-current.log") == b"cli log\n"
        assert zf.read("debug/events-current.jsonl") == b"event log\n"


# ---------------------------------------------------------------------------
# /logs command
# ---------------------------------------------------------------------------


def test_logs_command_registered() -> None:
    """/logs appears in the slash-command registry and /help."""
    assert "/logs" in COMMANDS
    assert "current session" in COMMANDS["/logs"][0].lower()
    assert "zip" in COMMANDS["/logs"][0].lower()


@pytest.mark.asyncio
async def test_logs_command_creates_current_session_zip_at_requested_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing a path to /logs bundles only current-session files."""
    import omnigent.cli_diagnostics as cli_diagnostics

    logs = tmp_path / "logs"
    debug = tmp_path / "debug"
    logs.mkdir()
    debug.mkdir()
    cli_current = logs / "cli-current.log"
    cli_old = logs / "cli-old.log"
    event_current = debug / "events-current.jsonl"
    transcript = logs / "20260520-sess_current.json"
    cli_current.write_text("cli log\n", encoding="utf-8")
    cli_old.write_text("old cli\n", encoding="utf-8")
    event_current.write_text("event log\n", encoding="utf-8")
    transcript.write_text('{"session": "current"}\n', encoding="utf-8")

    async def fake_write_session_log(*args: object, **kwargs: object) -> Path:
        return transcript

    monkeypatch.setattr(cli_diagnostics, "current_cli_log_path", lambda: cli_current)
    # _cmd_logs imports write_session_log inside the function, so patch the source module.
    import omnigent.repl._session_log as session_log

    monkeypatch.setattr(session_log, "write_session_log", fake_write_session_log)

    session = _Session()
    session._event_log_path = event_current
    target = tmp_path / "requested.zip"
    host = CapturingHost()

    await handle_slash_command(
        f"/logs {target}",
        session,  # type: ignore[arg-type]
        _Client(),  # type: ignore[arg-type]
        host,
        RichBlockFormatter(),
    )

    assert target.exists(), f"Expected /logs to create {target}"
    with zipfile.ZipFile(target) as zf:
        assert sorted(zf.namelist()) == [
            "debug/events-current.jsonl",
            "logs/20260520-sess_current.json",
            "logs/cli-current.log",
        ]
    assert "cli-old.log" not in zipfile.ZipFile(target).namelist()
    assert "Collected 3 current-session log files" in host.text
    assert "Conversation ID: sess_current" in host.text

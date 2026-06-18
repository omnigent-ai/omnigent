"""Tests for native-wrapper resume hint formatting."""

from __future__ import annotations

import pytest

from omnigent._native_resume_hint import (
    echo_native_cold_resume_hint,
    format_native_resume_command,
)


def test_format_native_resume_command_includes_remote_context() -> None:
    """
    Remote native-wrapper hints include enough context to copy/paste.

    The command must carry the wrapper name, Omnigent server, and
    Omnigent conversation id. If any of those fields are dropped, a
    user who launched against a non-default remote workspace cannot
    reliably resume the same conversation from the printed hint.
    There is no ``--profile`` part: the CLI flag was removed, so a
    hint containing it would tell the user to run a command that
    no longer parses.
    """
    command = format_native_resume_command(
        native_command="claude",
        server="https://example.databricks.com",
        session_id="conv_abc",
    )

    assert command == ("omnigent claude --server https://example.databricks.com --resume conv_abc")


def test_cold_resume_hint_is_honest_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """
    The cold-resume hint must tell the user, on stderr, that prior turns
    are gone.

    Cursor cannot reattach to a chat once its terminal exits, so resume
    cold-starts a fresh TUI. If the hint were silent (or printed an
    upbeat "resumed!" line), the user would reasonably assume their
    earlier conversation came back and be misled. The message therefore
    has to state both facts: the terminal was not running, and the prior
    chat was not restored. It must go to stderr so it never pollutes the
    TUI's stdout.
    """
    echo_native_cold_resume_hint(agent_label="Cursor")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Terminal not running" in captured.err
    assert "fresh Cursor session" in captured.err
    assert "prior chat not restored" in captured.err

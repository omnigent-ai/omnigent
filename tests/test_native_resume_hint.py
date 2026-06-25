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


def test_cold_resume_hint_not_restored_is_honest_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    The default (``restored=False``) hint must say prior turns are gone.

    Wrappers that record no resumable chat id (e.g. Hermes/qwen/kimi)
    cold-start a fresh TUI on resume. If the hint were silent (or printed
    an upbeat "resumed!" line), the user would assume their earlier
    conversation came back and be misled. The message states both facts:
    the terminal was not running, and the prior chat was not restored. It
    must go to stderr so it never pollutes the TUI's stdout.
    """
    echo_native_cold_resume_hint(agent_label="Hermes")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Terminal not running" in captured.err
    assert "fresh Hermes session" in captured.err
    assert "prior chat not restored" in captured.err


def test_cold_resume_hint_restored_reports_reload(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    With ``restored=True`` the hint says the prior conversation is reloaded.

    Cursor reuses its chat store across ``cursor-agent --resume``, so a cold
    resume relaunches the TUI *with* the prior turns. The message must not
    claim the chat was lost (that would now be the misleading case), and must
    stay on stderr.
    """
    echo_native_cold_resume_hint(agent_label="Cursor", restored=True)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Terminal not running" in captured.err
    assert "resuming the prior conversation" in captured.err
    assert "not restored" not in captured.err

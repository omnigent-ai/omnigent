"""Helpers for native-wrapper resume hints."""

from __future__ import annotations

import shlex

import click


def format_native_resume_command(
    *,
    native_command: str,
    session_id: str,
    server: str | None = None,
) -> str:
    """
    Build a copyable native-wrapper resume command.

    :param native_command: Native wrapper subcommand, e.g.
        ``"claude"``.
    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param server: Optional Omnigent server URL, e.g.
        ``"https://example.databricks.com"``.
    :returns: Shell-quoted command string, e.g.
        ``"omnigent claude --resume conv_abc123"``.
    """
    parts = ["omnigent", native_command]
    if server is not None:
        parts.extend(["--server", server])
    parts.extend(["--resume", session_id])
    return " ".join(shlex.quote(part) for part in parts)


def echo_native_resume_hint(
    *,
    native_command: str,
    session_id: str,
    server: str | None = None,
) -> None:
    """
    Print a copyable native-wrapper resume command to stderr.

    :param native_command: Native wrapper subcommand, e.g.
        ``"codex"``.
    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param server: Optional Omnigent server URL, e.g.
        ``"https://example.databricks.com"``.
    :returns: None.
    """
    command = format_native_resume_command(
        native_command=native_command,
        session_id=session_id,
        server=server,
    )
    click.echo(f"Resume with: {command}", err=True)


def echo_native_cold_resume_hint(
    *,
    agent_label: str = "Cursor",
) -> None:
    """
    Warn that a cold resume is starting a fresh session, not restoring one.

    Some native wrappers (notably Cursor) cannot reattach to a prior
    chat once the session terminal has exited: the TUI records no
    resumable chat id, so ``--resume`` relaunches a *fresh* agent with
    none of the prior turns. The reattach-to-a-live-terminal path is
    unaffected; this hint only fires when the terminal is gone and we
    are cold-starting a new TUI, so the user is not misled into
    thinking their earlier conversation came back.

    :param agent_label: Human-readable agent name for the message,
        e.g. ``"Cursor"``.
    :returns: None.
    """
    click.echo(
        f"Terminal not running - starting a fresh {agent_label} session "
        "(prior chat not restored).",
        err=True,
    )

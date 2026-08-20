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
    restored: bool = False,
) -> None:
    """
    Tell the user a cold resume is relaunching the agent's TUI.

    Fires only when the session terminal has exited and we are
    cold-starting a new TUI (the reattach-to-a-live-terminal path is
    unaffected). Two outcomes, selected by *restored*:

    - ``restored=False`` (default): the wrapper cannot reattach to the
      prior chat, so ``--resume`` relaunches a *fresh* agent with none of
      the prior turns. Used by wrappers that record no resumable chat id.
    - ``restored=True``: the wrapper captured a resumable chat id and is
      reloading the prior conversation into the new TUI (e.g. Cursor,
      which reuses its chat store across ``cursor-agent --resume``).

    :param agent_label: Human-readable agent name for the message,
        e.g. ``"Cursor"``.
    :param restored: Whether the prior conversation is being reloaded.
    :returns: None.
    """
    if restored:
        click.echo(
            f"Terminal not running - relaunching {agent_label} and "
            "resuming the prior conversation.",
            err=True,
        )
        return
    click.echo(
        f"Terminal not running - starting a fresh {agent_label} session "
        "(prior chat not restored).",
        err=True,
    )

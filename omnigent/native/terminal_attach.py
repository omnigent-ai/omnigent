"""Harness-independent selection of the native terminal attachment transport."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

import click
from websockets.exceptions import WebSocketException

from omnigent.native.native_terminal import terminal_attach_url

CONTROL_MODE_ATTACH_ENV = "OMNIGENT_EXPERIMENTAL_CONTROL_MODE_ATTACH"
AttachResult = TypeVar("AttachResult")


def control_mode_attach_enabled() -> bool:
    """Return whether the host terminal should receive raw control-mode output."""
    return os.environ.get(CONTROL_MODE_ATTACH_ENV) == "1"


async def attach_native_terminal(
    *,
    default_attach: Callable[[], Awaitable[AttachResult]],
    control_mode_attach: Callable[[], Awaitable[AttachResult]],
) -> AttachResult:
    """Select a transport without changing a launcher's return or cleanup contract."""
    if not control_mode_attach_enabled():
        return await default_attach()
    click.echo(
        "Experimental control-mode attach: using the WebSocket relay for "
        "terminal-native selection. Tmux status and popups are unavailable.",
        err=True,
    )
    return await control_mode_attach()


async def attach_terminal_websocket(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    terminal_id: str,
    session_name: str,
) -> None:
    """Reuse the native TTY relay for launchers without custom reconnect state."""
    from omnigent.harnesses.claude_native.main import (
        _attach_with_reconnect,
        attach_local_terminal,
    )

    try:
        await _attach_with_reconnect(
            attach=attach_local_terminal,
            attach_url=terminal_attach_url(base_url, session_id, terminal_id),
            headers=headers,
            recover=None,
            session_name=session_name,
            base_url=base_url,
            session_id=session_id,
            terminal_id=terminal_id,
            close_attach_on_terminal_gone=True,
        )
    except (WebSocketException, OSError) as exc:
        raise click.ClickException(
            f"Terminal WebSocket connection failed ({type(exc).__name__}: {exc}). "
            f"Rerun your resume command for session {session_id} to reconnect."
        ) from exc

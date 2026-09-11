"""Bounded terminal startup and browser palette negotiation for managed tmux."""

from __future__ import annotations

import re
import shlex

BROWSER_READY_CHANNEL = "omnigent-browser-ready"
BROWSER_READY_OPTION = "@omnigent-browser-ready"
BROWSER_READY_TIMEOUT_SECONDS = 2

BROWSER_READY_RELEASE = (
    f"if-shell -F '#{{==:#{{{BROWSER_READY_OPTION}}},pending}}' "
    f"'set-option -g {BROWSER_READY_OPTION} started ; "
    f"wait-for -S {BROWSER_READY_CHANNEL}'"
)


def browser_ready_commands(target: str, foreground: object, background: object) -> bytes | None:
    """Configure tmux's OSC 10/11 answers before releasing a waiting TUI.

    Only RGB hex colors are accepted; browser input never becomes tmux syntax.
    Window styles work on the supported tmux 3.3 floor, unlike refresh-client -r.
    """
    if not all(
        isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color)
        for color in (foreground, background)
    ):
        return None
    return (
        f"set-option -w -t {shlex.quote(target)} window-style "
        f"'fg={foreground},bg={background}'\n{BROWSER_READY_RELEASE}\n"
    ).encode()

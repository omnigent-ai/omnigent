"""Native tmux identities used to retire only an injection's observed terminal."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class NativeTmuxTerminal:
    """One server, session, pane and launched process, independent of logical names."""

    socket_path: str
    server_pid: int
    server_started_at: int
    session_id: str
    pane_id: str
    pane_pid: int


def capture_tmux_terminal(
    socket_path: str, target: str, *, timeout_s: float = 10.0
) -> NativeTmuxTerminal | None:
    """Read native identities before an injection begins waiting or writing."""
    try:
        result = subprocess.run(
            [
                "tmux",
                "-S",
                socket_path,
                "display-message",
                "-p",
                "-t",
                target,
                "#{pid} #{start_time} #{session_id} #{pane_id} #{pane_pid}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields = result.stdout.split()
    if (
        result.returncode != 0
        or len(fields) != 5
        or any(not fields[index].isdecimal() for index in (0, 1, 4))
        or re.fullmatch(r"\$[0-9]+", fields[2]) is None
        or re.fullmatch(r"%[0-9]+", fields[3]) is None
    ):
        return None
    if min(int(fields[index]) for index in (0, 1, 4)) <= 0:
        return None
    return NativeTmuxTerminal(
        socket_path, int(fields[0]), int(fields[1]), fields[2], fields[3], int(fields[4])
    )


def stop_tmux_terminal(terminal: NativeTmuxTerminal, *, timeout_s: float = 10.0) -> None:
    """
    Check and stop an owned generation on one native tmux connection.

    The default budget matches the bridge's other tmux calls; a tighter one
    would report a kill that merely ran late as a cleanup failure.
    """
    if (
        min(terminal.server_pid, terminal.server_started_at, terminal.pane_pid) <= 0
        or re.fullmatch(r"\$[0-9]+", terminal.session_id) is None
        or re.fullmatch(r"%[0-9]+", terminal.pane_id) is None
    ):
        raise RuntimeError("Invalid native terminal identity; cleanup refused")
    matches = [
        f"#{{==:#{{pid}},{terminal.server_pid}}}",
        f"#{{==:#{{start_time}},{terminal.server_started_at}}}",
        f"#{{==:#{{session_id}},{terminal.session_id}}}",
        f"#{{==:#{{pane_id}},{terminal.pane_id}}}",
        f"#{{==:#{{pane_pid}},{terminal.pane_pid}}}",
    ]
    condition = matches[-1]
    for match in reversed(matches[:-1]):
        condition = f"#{{&&:{match},{condition}}}"
    try:
        result = subprocess.run(
            [
                "tmux",
                "-S",
                terminal.socket_path,
                "if-shell",
                "-F",
                "-t",
                terminal.pane_id,
                condition,
                f"kill-pane -t {terminal.pane_id}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Native terminal cleanup could not complete") from exc
    # A gone pane, session, server or socket is not a failure. These are tmux's
    # own phrases; the strerror detail after "error connecting to" is localized.
    if result.returncode != 0 and not any(
        reason in result.stderr.lower()
        for reason in ("can't find", "no server running", "error connecting to")
    ):
        raise RuntimeError(f"Native terminal cleanup failed (rc={result.returncode})")

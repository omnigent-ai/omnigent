"""When a person last typed into a managed tmux pane."""

from __future__ import annotations

import contextlib
import subprocess

# The web terminal bridge stamps this pane option with the epoch time of the
# browser's latest keystroke. tmux times directly attached terminals itself.
HUMAN_INPUT_AT_PANE_OPTION = "@omnigent_human_input_at"

_TMUX_QUERY_TIMEOUT_S = 5.0


def human_input_stamp_command(target: str, typed_at: float) -> str:
    """
    Return the tmux command that records a browser keystroke on a pane.

    :param target: tmux pane target, e.g. ``"main"``.
    :param typed_at: Epoch seconds of the keystroke, e.g. ``1795227026.25``.
    :returns: A control-mode command line without its newline.
    """
    return f"set-option -p -t {target} {HUMAN_INPUT_AT_PANE_OPTION} {typed_at:.3f}"


def last_human_input_at(socket_path: str, target: str) -> float | None:
    """
    Return when a person last typed into a pane, or ``None`` when no one has.

    tmux records a directly attached terminal's activity in whole seconds, so
    its keystroke is taken to be up to a second later than recorded. tmux does
    not time keys a control-mode client sends, so the web terminal's stamp
    (:data:`HUMAN_INPUT_AT_PANE_OPTION`) covers those.

    :param socket_path: The pane's tmux socket, e.g. ``"/tmp/x/tmux.sock"``.
    :param target: tmux pane target, e.g. ``"main"``.
    :returns: Epoch seconds, e.g. ``1795227027.0``, or ``None``.
    """
    typed_at: list[float] = []
    clients = _tmux_output(
        socket_path, "list-clients", "-F", "#{client_control_mode} #{client_activity}"
    )
    for line in clients.splitlines():
        control_mode, _, activity = line.partition(" ")
        if control_mode == "0" and activity.isdigit():
            typed_at.append(int(activity) + 1.0)
    stamp = _tmux_output(
        socket_path, "display-message", "-p", "-t", target, f"#{{{HUMAN_INPUT_AT_PANE_OPTION}}}"
    ).strip()
    with contextlib.suppress(ValueError):
        typed_at.append(float(stamp))
    return max(typed_at, default=None)


def _tmux_output(socket_path: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["tmux", "-S", socket_path, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_QUERY_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout if completed.returncode == 0 else ""

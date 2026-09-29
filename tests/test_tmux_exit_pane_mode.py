"""``_exit_pane_mode`` against a real private tmux server."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import _exit_pane_mode

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


def _tmux(socket: str, *args: str) -> str:
    return subprocess.run(
        ["tmux", "-S", socket, *args], check=True, capture_output=True, text=True, timeout=5
    ).stdout.strip()


@pytest.fixture
def tmux_socket() -> Iterator[str]:
    # A short /tmp path keeps the socket under the Unix socket path limit.
    socket = str(Path(tempfile.mkdtemp(dir="/tmp")) / "s")
    _tmux(socket, "-f", "/dev/null", "new-session", "-d", "-s", "p", "cat")
    try:
        yield socket
    finally:
        subprocess.run(["tmux", "-S", socket, "kill-server"], check=False, capture_output=True)


def _in_mode(socket: str) -> str:
    return _tmux(socket, "display-message", "-p", "-t", "p", "#{pane_in_mode}")


def test_exit_pane_mode_leaves_copy_mode(tmux_socket: str) -> None:
    """A scrolled-back (copy-mode) pane returns to the live program."""
    _tmux(tmux_socket, "copy-mode", "-t", "p")
    assert _in_mode(tmux_socket) == "1"

    _exit_pane_mode(tmux_socket, "p")

    assert _in_mode(tmux_socket) == "0"


def test_exit_pane_mode_is_noop_outside_a_mode(tmux_socket: str) -> None:
    """Outside a mode it neither fails nor sends keys to the program."""
    _exit_pane_mode(tmux_socket, "p")

    assert _in_mode(tmux_socket) == "0"
    assert _tmux(tmux_socket, "capture-pane", "-p", "-t", "p") == ""

"""Exercise injection into a real tmux pane the reader left scrolled back in copy mode."""

from __future__ import annotations

import contextlib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge

pytestmark = [
    pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required"),
    pytest.mark.timeout(20),
]

# A stand-in for Claude Code's composer: it records every byte the pane
# receives and submits a bracketed paste on the next carriage return.
_TERMINAL = r"""
import json
import os
import sys
import tty
from pathlib import Path

received, delivered = map(Path, sys.argv[1:3])
panel_open = len(sys.argv) > 3 and sys.argv[3] == "btw"
tty.setraw(sys.stdin.fileno())
sys.stdout.write("\x1b[?2004h")


def render(draft=""):
    rule = "─" * min(80, os.get_terminal_size().columns)
    sys.stdout.write("\x1b[H\x1b[2J" + rule + "\r\n❯ " + draft + "\r\n" + rule + "\r\n")
    sys.stdout.flush()


if panel_open:
    sys.stdout.write("\x1b[H\x1b[2J" + "▔" * os.get_terminal_size().columns + "\r\n"
        "    /btw do the checks pass?\r\n      Yes, the checks pass.\r\n"
        "    ↑/↓ to scroll · c to copy · f to fork · Esc to close\r\n")
    sys.stdout.flush()
else:
    render()
buffer = b""
draft = None
messages = []
while True:
    chunk = os.read(sys.stdin.fileno(), 65536)
    with received.open("ab") as log:
        log.write(chunk)
    buffer += chunk
    if panel_open:
        if b"\x1b" in buffer:
            panel_open = False
            buffer = b""
            render()
        continue
    if b"\x1b[200~" in buffer and b"\x1b[201~" in buffer:
        pasted, buffer = buffer.split(b"\x1b[200~", 1)[1].split(b"\x1b[201~", 1)
        draft = pasted.decode().strip()
        render(draft)
    if draft is not None and b"\r" in buffer:
        messages.append(draft)
        delivered.write_text(json.dumps(messages))
        draft = None
        buffer = b""
        render()
"""


@dataclass(frozen=True)
class _Pane:
    """A live tmux pane running the stand-in composer."""

    tmux: list[str]
    bridge_dir: Path
    received: Path
    delivered: Path

    def in_mode(self) -> bool:
        """Return whether tmux reports the pane in copy mode (or another mode)."""
        result = subprocess.run(
            [*self.tmux, "display-message", "-p", "-t", "main", "#{pane_in_mode}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() == "1"

    def scroll_back(self) -> None:
        """Enter copy mode, as scrolling the embedded terminal up does."""
        subprocess.run(
            [*self.tmux, "copy-mode", "-t", "main"], check=True, capture_output=True, timeout=5
        )
        assert self.in_mode(), "Expected the pane to be in copy mode after scrolling back."


@pytest.fixture(params=["emacs", "vi"])
def pane(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Pane]:
    """Start the stand-in composer in tmux with the parametrized copy-mode key table."""
    mode_keys, panel = request.param if isinstance(request.param, tuple) else (request.param, "")
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    terminal = tmp_path / "terminal.py"
    terminal.write_text(_TERMINAL)
    received = tmp_path / "received.bin"
    delivered = tmp_path / "delivered.json"
    # A short /tmp socket path stays under the Unix socket path limit.
    with tempfile.TemporaryDirectory(prefix="claude-copy-", dir="/tmp") as socket_dir:
        socket_path = Path(socket_dir) / "tmux.sock"
        tmux = ["tmux", "-S", str(socket_path)]
        command = shlex.join([sys.executable, str(terminal), str(received), str(delivered), panel])
        try:
            subprocess.run(
                [
                    *tmux,
                    "-f",
                    "/dev/null",
                    "new-session",
                    "-d",
                    "-s",
                    "main",
                    "-x",
                    "48" if panel else "120",
                    "-y",
                    "24",
                    command,
                ],
                check=True,
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                [*tmux, "set-option", "-g", "mode-keys", mode_keys],
                check=True,
                capture_output=True,
                timeout=5,
            )
            bridge_dir = tmp_path / "bridge"
            bridge.write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="main")
            # Keys sent before the composer switches its tty to raw mode are
            # flushed by tcsetattr(TCSAFLUSH), so wait for its first render.
            deadline = time.monotonic() + 5.0
            marker = "▔" if panel else "❯"
            while marker not in bridge._capture_pane(str(socket_path), "main"):
                assert time.monotonic() < deadline, "The stand-in composer never rendered."
                time.sleep(0.05)
            yield _Pane(tmux=tmux, bridge_dir=bridge_dir, received=received, delivered=delivered)
        finally:
            with contextlib.suppress(subprocess.SubprocessError):
                subprocess.run([*tmux, "kill-server"], check=False, capture_output=True, timeout=5)


def _wait_for_bytes(path: Path, expected: bytes) -> bytes:
    """Poll *path* until it contains *expected*, returning what was received."""
    deadline = time.monotonic() + 5.0
    data = b""
    while time.monotonic() < deadline:
        data = path.read_bytes() if path.exists() else b""
        if expected in data:
            return data
        time.sleep(0.05)
    return data


def test_message_from_web_ui_reaches_scrolled_back_pane(pane: _Pane) -> None:
    """A message sent while the reader is scrolled back is still delivered and submitted."""
    pane.scroll_back()

    bridge.inject_user_message(pane.bridge_dir, content="SENT_WHILE_SCROLLED_BACK", timeout_s=5.0)

    assert json.loads(pane.delivered.read_text()) == ["SENT_WHILE_SCROLLED_BACK"]
    assert not pane.in_mode(), "Delivery should return the pane to the live view."


@pytest.mark.parametrize("pane", [("emacs", "btw")], indirect=True, ids=["narrow-quick-answer"])
def test_quick_answer_can_be_read_and_closed_in_a_narrow_pane(pane: _Pane) -> None:
    """A terminal-wrapped footer still relays the answer and lets the next message through."""
    signals = bridge.read_pane_signals(pane.bridge_dir)
    assert signals.btw_overlay is not None
    assert signals.btw_overlay.answer == "Yes, the checks pass."

    assert bridge.dismiss_btw_overlay(pane.bridge_dir) is True
    assert _wait_for_bytes(pane.received, b"\x1b") == b"\x1b"
    bridge.inject_user_message(pane.bridge_dir, content="SENT_AFTER_QUICK_ANSWER", timeout_s=5.0)
    assert json.loads(pane.delivered.read_text()) == ["SENT_AFTER_QUICK_ANSWER"]


def test_interrupt_reaches_scrolled_back_pane(pane: _Pane) -> None:
    """The stop button's Escape reaches Claude instead of only closing copy mode."""
    pane.scroll_back()

    bridge.inject_interrupt(pane.bridge_dir, timeout_s=5.0)

    assert _wait_for_bytes(pane.received, b"\x1b") == b"\x1b"
    assert not pane.in_mode(), "The interrupt should return the pane to the live view."


# No key is sent on this path, so the key table cannot change the outcome.
@pytest.mark.parametrize("pane", ["emacs"], indirect=True)
def test_watchers_leave_scrollback_alone_when_nothing_is_on_screen(pane: _Pane) -> None:
    """Polling helpers that find nothing to act on keep the reader's scrollback position."""
    pane.scroll_back()

    assert bridge.confirm_dialog_if_open(pane.bridge_dir, hint="Not on screen") is False
    assert bridge.dismiss_btw_overlay(pane.bridge_dir) is False

    assert pane.in_mode(), "A watcher with nothing to confirm must not leave copy mode."
    assert not pane.received.exists(), "A watcher with nothing to confirm must not send keys."

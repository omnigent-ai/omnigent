"""Exercise native input through real tmux scrollback and clipped composer redraws."""

from __future__ import annotations

import contextlib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
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
import signal
import sys
import tty
from pathlib import Path

received, delivered = map(Path, sys.argv[1:3])
tty.setraw(sys.stdin.fileno())
sys.stdout.write("\x1b[?2004h")


def render(draft=""):
    sys.stdout.write("\x1b[H\x1b[2J" + "─" * 80 + "\r\n❯ " + draft + "\r\n" + "─" * 80
                     + "\r\n  mode hints\r\n  status line\r\n  agent status\r\n")
    sys.stdout.flush()


buffer = b""
draft = None
messages = []
signal.signal(signal.SIGWINCH, lambda *_: render(draft or ""))
render()
while True:
    chunk = os.read(sys.stdin.fileno(), 65536)
    with received.open("ab") as log:
        log.write(chunk)
    buffer += chunk
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
        command = shlex.join([sys.executable, str(terminal), str(received), str(delivered)])
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
                    "120",
                    "-y",
                    "24",
                    command,
                ],
                check=True,
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                [*tmux, "set-option", "-g", "mode-keys", request.param],
                check=True,
                capture_output=True,
                timeout=5,
            )
            bridge_dir = tmp_path / "bridge"
            bridge.write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="main")
            # Keys sent before the composer switches its tty to raw mode are
            # flushed by tcsetattr(TCSAFLUSH), so wait for its first render.
            deadline = time.monotonic() + 5.0
            while "❯" not in bridge._capture_pane(str(socket_path), "main"):
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


@pytest.mark.parametrize("pane", ["emacs"], indirect=True)
@pytest.mark.parametrize("blocked_by", [None, "approval", "cancellation"])
def test_five_row_delivery_restores_geometry_and_preserves_input_guards(
    pane: _Pane, monkeypatch: pytest.MonkeyPatch, blocked_by: str | None
) -> None:
    """A clipped input row needs room before readiness can recognize the composer."""
    monkeypatch.setattr(bridge, "_TMUX_READY_SLOW_BOOT_TIMEOUT_S", 0.3)
    monkeypatch.setattr(bridge, "_OCCUPIED_INPUT_DISMISS_TIMEOUT_S", 0.1)
    subprocess.run(
        [*pane.tmux, "resize-window", "-t", "main", "-y", "5"],
        check=True,
        capture_output=True,
        timeout=5,
    )
    deadline = time.monotonic() + 5.0
    while bridge._claude_prompt_rendered(bridge._capture_pane(pane.tmux[2], "main")):
        assert time.monotonic() < deadline, "The five-row redraw never clipped the input row."
        time.sleep(0.05)
    geometry = [
        *pane.tmux,
        "display-message",
        "-p",
        "-t",
        "main",
        "#{window_width} #{window_height} #{window-size}",
    ]
    original = subprocess.run(
        geometry,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout
    assert original.startswith("120 5 ")

    cancelled = threading.Event()
    if blocked_by == "approval":
        monkeypatch.setattr(bridge, "has_pending_user_prompt", lambda _: True)
    elif blocked_by == "cancellation":
        capture = bridge._capture_pane

        def cancel_after_redraw(*args: str, **kwargs: bool) -> str:
            screen = capture(*args, **kwargs)
            if bridge._claude_prompt_rendered(screen):
                cancelled.set()
            return screen

        monkeypatch.setattr(bridge, "_capture_pane", cancel_after_redraw)
    with bridge.cancellable_injection(cancelled):
        if blocked_by is None:
            bridge.inject_user_message(
                pane.bridge_dir, content="SENT_FROM_FIVE_ROWS", timeout_s=0.3
            )
            assert json.loads(pane.delivered.read_text()) == ["SENT_FROM_FIVE_ROWS"]
        else:
            error = (
                bridge.ClaudeUserPromptPending
                if blocked_by == "approval"
                else bridge.ClaudeInjectionCancelled
            )
            with pytest.raises(error):
                bridge.inject_user_message(
                    pane.bridge_dir, content="SENT_FROM_FIVE_ROWS", timeout_s=0.3
                )
            assert not pane.received.exists(), "A blocked delivery must send no input bytes."
            assert not pane.delivered.exists()
    assert (
        subprocess.run(
            geometry,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        == original
    )

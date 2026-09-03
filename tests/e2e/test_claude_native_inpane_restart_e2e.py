"""End-to-end regression for the claude-native in-pane-restart first-message drop.

The bug: ``inject_user_message`` gates the
first web-UI message on ``_wait_for_claude_prompt_ready``, which only
proves *a* prompt glyph is rendered — it carries no process identity.
When the wrapped Claude Code process restarts inside the same tmux pane
between prompt-render and injection (observed: claude-code 2.1.170's
fullscreen-switch re-exec ~12s into boot; the class also covers
auto-update restarts and crash-restarts), the paste + Enter land in the
dying process and are discarded by the restarted process's boot-time
input flush. Nothing notices: the turn is marked injected, no error
surfaces in the web UI, and the message never reaches Claude.

Making the race deterministic
-----------------------------
The genuine web journey needs an interactive Claude login (see
``test_host_claude_native_e2e.py``, which is opt-in for that reason) and
a claude-code build whose boot restarts in-pane — the 2.1.170 trigger is
fixed upstream in >= 2.1.202. So, following that test's wrapper pattern,
this test drives the *real* delivery path (``inject_user_message`` — the
exact function the claude-native executor's web turn calls) against a
real tmux pane running a scripted Claude-Code-shaped TUI that:

1. renders a genuine composer (box rule + ``❯`` row) so the readiness
   gate passes, then
2. restarts in-pane after a delay without ever reading its input
   (modeling the dying pre-restart process), and
3. the restarted process flushes pending terminal input
   (``termios.tcflush``) — faithfully modeling Claude Code's boot-time
   input flush, which is what discards the early keystrokes — and then
   runs a working composer that records every submitted message.

With the model, an injection gated only on the prompt glyph reliably
loses the message: the paste lands in the pre-restart process's pty
buffer and the restart flushes it, while ``inject_user_message`` returns
success (its draft-visibility check fails open and submits blind).

The control test (no restart) proves the fake TUI faithfully accepts the
same delivery: the readiness gate, paste, and submit all work, and the
message is recorded. Only the in-pane restart makes it vanish.

The regression contract: after ``inject_user_message`` reports success,
the message must actually have been delivered to the (post-restart)
Claude process. A loud ``RuntimeError`` also passes — an error surfacing
in the web UI is not a *silent* drop. Today the restart test FAILS: the
inject succeeds and the message vanishes.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from omnigent.claude_native_bridge import (
    bridge_dir_for_bridge_id,
    inject_user_message,
    write_tmux_target,
)

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux")

_MARKER = "the first message must survive an in-pane restart"

# Seconds the pre-restart fake Claude keeps its prompt on screen before
# re-exec'ing in place. Must comfortably exceed the time the readiness
# gate needs to pass and paste (< 2s here), so the injection reliably
# lands in the dying process — modeling 2.1.170's ~12s fullscreen re-exec.
_RESTART_AFTER_S = 4.0

# A Claude-Code-shaped TUI: composer box framed by rules with a leading
# "❯" glyph (what _wait_for_claude_prompt_ready / _draft_in_input_box
# key on), bracketed paste enabled, drafts rendered in the box, and
# every submitted (Enter outside a paste) non-empty draft appended to
# $FAKE_CLAUDE_DELIVERED — the analog of a UserPromptSubmit hook event.
# Enter may arrive as "\r" or "\n" (cbreak leaves ICRNL on); either
# submits outside a paste and is a plain line break inside one.
_FAKE_CLAUDE_TUI = r"""
import os
import sys
import termios
import time
import tty

PHASE = os.environ.get("FAKE_CLAUDE_PHASE", "boot")
DELIVERED = os.environ["FAKE_CLAUDE_DELIVERED"]
RESTART_AFTER = float(os.environ.get("FAKE_CLAUDE_RESTART_AFTER", "4"))

RULE = "─" * 40
GLYPH = "❯"
FD = sys.stdin.fileno()


def draw(draft: str = "") -> None:
    sys.stdout.write("\x1b[?2004h")  # request bracketed paste
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.write("fake claude code TUI\r\n")
    sys.stdout.write(RULE + "\r\n")
    sys.stdout.write(GLYPH + " " + draft + "\r\n")
    sys.stdout.write(RULE + "\r\n")
    sys.stdout.flush()


tty.setcbreak(FD)

if PHASE == "boot":
    # The dying pre-restart process: renders a real prompt, never reads
    # its input, then restarts in-pane (like claude-code 2.1.170's
    # fullscreen-switch re-exec). Keystrokes typed at it are stranded in
    # the pty buffer and discarded by the restarted process's flush.
    draw()
    time.sleep(RESTART_AFTER)
    os.environ["FAKE_CLAUDE_PHASE"] = "ready"
    os.execv(sys.executable, [sys.executable] + sys.argv)

# The restarted process: boot-time input flush (exactly what Claude Code
# does when its TUI initializes), then a working composer.
termios.tcflush(FD, termios.TCIFLUSH)
draft = ""
pending = ""
in_paste = False
draw()
while True:
    try:
        chunk = os.read(FD, 4096).decode("utf-8", "replace")
    except OSError:
        break
    if not chunk:
        break
    pending += chunk
    while pending:
        if pending.startswith("\x1b[200~"):
            in_paste = True
            pending = pending[6:]
            continue
        if pending.startswith("\x1b[201~"):
            in_paste = False
            pending = pending[6:]
            continue
        if pending.startswith("\x1b"):
            if len(pending) < 6:
                break  # possibly a split escape sequence; read more
            pending = pending[1:]
            continue
        ch = pending[0]
        pending = pending[1:]
        if ch in ("\r", "\n") and not in_paste:
            if draft.strip():
                with open(DELIVERED, "a", encoding="utf-8") as fh:
                    fh.write(draft + "\n")
            draft = ""
        elif ch in ("\r", "\n", "\t"):
            draft += " "
        elif ord(ch) >= 0x20:
            draft += ch
    draw(draft)
"""


def _tmux(socket: Path, *args: str) -> None:
    subprocess.run(
        ["tmux", "-S", str(socket), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _launch_fake_claude(
    tmp_path: Path,
    *,
    phase: str,
) -> tuple[Path, Path, Path]:
    """Start the fake Claude TUI in a fresh tmux pane and advertise it.

    :param tmp_path: Test temp dir for the socket / script / record file.
    :param phase: ``"boot"`` (renders a prompt, then restarts in-pane) or
        ``"ready"`` (control: a working composer from the start).
    :returns: ``(bridge_dir, delivered_file, socket_path)``.
    """
    bridge_dir = bridge_dir_for_bridge_id(f"bridge_e2e_{uuid.uuid4().hex}")
    delivered = tmp_path / "delivered.txt"
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE_TUI, encoding="utf-8")
    socket = tmp_path / "tmux.sock"
    _tmux(
        socket,
        "new-session",
        "-d",
        "-s",
        "claude",
        "-x",
        "100",
        "-y",
        "30",
        "-e",
        f"FAKE_CLAUDE_DELIVERED={delivered}",
        "-e",
        f"FAKE_CLAUDE_RESTART_AFTER={_RESTART_AFTER_S}",
        "-e",
        f"FAKE_CLAUDE_PHASE={phase}",
        f"{sys.executable} {script}",
    )
    # What the runner does when it launches the terminal.
    write_tmux_target(bridge_dir, socket_path=socket, tmux_target="claude")
    return bridge_dir, delivered, socket


def _cleanup(socket: Path, bridge_dir: Path) -> None:
    subprocess.run(
        ["tmux", "-S", str(socket), "kill-server"],
        check=False,
        capture_output=True,
    )
    shutil.rmtree(bridge_dir, ignore_errors=True)


def _message_recorded(delivered: Path, *, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if delivered.exists() and _MARKER in delivered.read_text(encoding="utf-8"):
            return True
        time.sleep(0.25)
    return False


def test_control_message_delivered_without_restart(tmp_path: Path) -> None:
    """Control: with no in-pane restart the same injection is delivered."""
    bridge_dir, delivered, socket = _launch_fake_claude(tmp_path, phase="ready")
    try:
        inject_user_message(bridge_dir, content=_MARKER, timeout_s=20.0)
        assert _message_recorded(delivered), (
            "control failed: the fake Claude TUI never recorded the message even "
            "without a restart — the harness model is broken, not the product"
        )
    finally:
        _cleanup(socket, bridge_dir)


def test_first_web_message_survives_inpane_claude_restart(tmp_path: Path) -> None:
    """A first message injected across an in-pane restart must not vanish silently."""
    bridge_dir, delivered, socket = _launch_fake_claude(tmp_path, phase="boot")
    try:
        # What the web UI's first turn does (the claude-native executor's
        # run_turn delegates straight to this function).
        try:
            inject_user_message(bridge_dir, content=_MARKER, timeout_s=20.0)
        except RuntimeError:
            # A loud failure surfaces in the web UI as an error — that is
            # an acceptable non-silent outcome, not the reported bug.
            return

        # inject_user_message reported success, so the message must have
        # actually reached the (post-restart) Claude process.
        assert _message_recorded(delivered), (
            "inject_user_message returned success, but the first message never "
            "reached the Claude process that survived the in-pane restart — it "
            "was silently dropped (no delivery record, no error surfaced). "
            "The readiness gate passed against the dying pre-restart process's "
            "prompt and the restart's boot-time input flush discarded the paste."
        )
    finally:
        _cleanup(socket, bridge_dir)

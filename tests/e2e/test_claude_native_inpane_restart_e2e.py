"""End-to-end regression for the claude-native in-pane-restart first-message drop.

The bug
-------
``inject_user_message`` (the exact function the claude-native web turn
calls) gates delivery on ``_wait_for_claude_prompt_ready``, which polls
``capture-pane`` for the ``❯`` prompt glyph. That gate proves *a* prompt
is rendered but carries no process identity. When Claude Code restarts
inside the same tmux pane during boot — either before it consumes the
paste, or after the draft is observed but before the submit Enter lands —
the keystrokes are pasted into the dying process and flushed away by the
replacement process's boot-time stdin flush. Pane state alone cannot tell
that apart from a successful submit (both leave an empty composer), so a
build without delivery recovery marks the turn injected, surfaces no error
in the web UI, and the message never reaches Claude: the first web-UI
message is silently dropped.

One historical trigger (a fullscreen-mode re-exec ~12s into boot) is
fixed in current claude-code builds, but the race *class* remains
reachable via any in-pane relaunch during boot (auto-update restart,
crash-restart, a future TUI transition). An earlier guard that raised
whenever the pasted draft never became visible was reverted for false
positives (large pastes legitimately render as a collapsed placeholder),
which restored the blind-submit behavior this test locks out.

Making the race deterministic
-----------------------------
The genuine web journey needs an interactive Claude login (see
``test_host_claude_native_e2e.py``, opt-in for that reason) and a
claude-code build whose boot restarts in-pane — a combination not
reproducible in CI (the historical trigger is fixed upstream; the
installed claude-code does not restart in-pane). So, following that
test's wrapper pattern, this test drives the *real* delivery path —
``inject_user_message``, the exact function the claude-native executor's
web turn calls — against a real tmux pane running a scripted
Claude-Code-shaped TUI that:

1. renders a genuine composer (box rule + ``❯`` row),
2. restarts either before consuming the paste (``boot``) or after
   rendering the draft but before the submit lands (``restart_after_draft``),
3. flushes pending input while starting the replacement process
   (exactly what Claude Code does when its TUI initializes), and
4. records ``SessionStart`` / ``UserPromptSubmit`` hooks and appends every
   genuinely-submitted draft to a delivered-messages file.

The control proves the same fake TUI accepts and records an ordinary
message with no restart. The two restart cases must NOT report silent
success without delivery: a correct build either delivers the message
(e.g. by re-delivering it into the replacement process) or raises a
delivery error — never returns success while the message vanishes.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import (
    bridge_dir_for_bridge_id,
    inject_user_message,
    write_tmux_target,
)

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux")

_MARKER = "the first message must survive an in-pane restart"

# Seconds the pre-restart fake Claude keeps its prompt on screen before
# re-exec'ing in place. Must comfortably exceed the time the readiness
# gate needs to pass and paste (< 2s here), so the injection reliably
# lands in the dying process — modeling the ~12s fullscreen re-exec of
# affected claude-code builds.
_RESTART_AFTER_S = 4.0
# For the "restart after the draft was seen" window the restart fires the
# instant the paste-end marker lands, before the submit Enter is verified.
_DRAFT_RESTART_AFTER_S = 0.3

# A Claude-Code-shaped TUI: composer box framed by rules with a leading
# "❯" glyph (what _wait_for_claude_prompt_ready / _draft_in_input_box key
# on), bracketed paste enabled, drafts rendered in the box, and every
# submitted (Enter outside a paste) non-empty draft appended to
# $FAKE_CLAUDE_DELIVERED and recorded as a UserPromptSubmit hook event.
# Enter may arrive as "\r" or "\n" (cbreak leaves ICRNL on); either
# submits outside a paste and is a plain line break inside one.
_FAKE_CLAUDE_TUI = r"""
import json
import os
import sys
import termios
import time
import tty

PHASE = os.environ.get("FAKE_CLAUDE_PHASE", "boot")
DELIVERED = os.environ["FAKE_CLAUDE_DELIVERED"]
HOOKS = os.environ["FAKE_CLAUDE_HOOKS"]
SESSION_ID = os.environ["FAKE_CLAUDE_SESSION_ID"]
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


def record_hook(event: str, *, prompt: str | None = None) -> None:
    payload = {"hook_event_name": event, "session_id": SESSION_ID}
    if prompt is not None:
        payload["prompt"] = prompt
    with open(HOOKS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"recorded_at": time.time(), "payload": payload}) + "\n")


tty.setcbreak(FD)
record_hook("SessionStart")

if PHASE == "boot":
    # The dying pre-restart process: renders a real prompt, never reads
    # its input, then restarts in-pane (like the fullscreen-switch
    # re-exec of affected claude-code builds). Keystrokes typed at it are
    # stranded in the pty buffer and discarded by the restarted process's
    # flush.
    draw()
    time.sleep(RESTART_AFTER)
    os.environ["FAKE_CLAUDE_PHASE"] = "ready"
    os.environ["FAKE_CLAUDE_SESSION_ID"] = "ready-session"
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
            if PHASE == "restart_after_draft":
                draw(draft)
                time.sleep(RESTART_AFTER)
                os.environ["FAKE_CLAUDE_PHASE"] = "ready"
                os.environ["FAKE_CLAUDE_SESSION_ID"] = "ready-session"
                os.execv(sys.executable, [sys.executable] + sys.argv)
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
                submitted = draft.rstrip()
                with open(DELIVERED, "a", encoding="utf-8") as fh:
                    fh.write(submitted + "\n")
                record_hook("UserPromptSubmit", prompt=submitted)
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

    :param tmp_path: Test temp dir for the script / delivered-messages file.
    :param phase: ``"boot"`` (restart before paste consumption),
        ``"restart_after_draft"``, or ``"ready"`` (control).
    :returns: ``(bridge_dir, delivered_file, socket_path)``.
    """
    bridge_dir = bridge_dir_for_bridge_id(f"bridge_e2e_{uuid.uuid4().hex}")
    delivered = tmp_path / f"delivered_{uuid.uuid4().hex}.txt"
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE_TUI, encoding="utf-8")
    socket = Path("/tmp") / f"og-claude-{uuid.uuid4().hex[:12]}.sock"
    restart_after = _DRAFT_RESTART_AFTER_S if phase == "restart_after_draft" else _RESTART_AFTER_S
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
        f"FAKE_CLAUDE_HOOKS={bridge_dir / 'hooks.jsonl'}",
        "-e",
        f"FAKE_CLAUDE_RESTART_AFTER={restart_after}",
        "-e",
        f"FAKE_CLAUDE_PHASE={phase}",
        "-e",
        f"FAKE_CLAUDE_SESSION_ID={'ready-session' if phase == 'ready' else 'boot-session'}",
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
    socket.unlink(missing_ok=True)
    shutil.rmtree(bridge_dir, ignore_errors=True)


def _message_recorded(delivered: Path, *, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if delivered.exists() and _MARKER in delivered.read_text(encoding="utf-8"):
            return True
        time.sleep(0.25)
    return False


def _inject_outcome(bridge_dir: Path, delivered: Path) -> tuple[bool, bool]:
    """Run the real web-turn delivery and report ``(raised, delivered)``.

    Mirrors the claude-native executor's web turn, which delegates the
    first message straight to :func:`inject_user_message`. ``raised`` is
    whether the call surfaced a delivery error; ``delivered`` is whether
    the fake Claude actually recorded (submitted) the message.
    """
    raised = False
    try:
        inject_user_message(bridge_dir, content=_MARKER, timeout_s=20.0)
    except RuntimeError:
        raised = True
    return raised, _message_recorded(delivered, timeout_s=2.0)


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
    """A first message injected across an in-pane restart must not vanish silently.

    Restart fires before the pre-restart process ever consumes the paste
    (the ``boot`` window). A correct build either delivers the message or
    raises a delivery error; the bug is a silent success with no delivery.
    """
    bridge_dir, delivered, socket = _launch_fake_claude(tmp_path, phase="boot")
    try:
        raised, was_delivered = _inject_outcome(bridge_dir, delivered)
        assert raised or was_delivered, (
            "silent drop reproduced: inject_user_message reported success "
            "(no delivery error) but the message never reached Claude "
            "(no UserPromptSubmit, no transcript entry) after an in-pane "
            "restart during boot"
        )
    finally:
        _cleanup(socket, bridge_dir)


def test_restart_after_draft_visibility_does_not_report_silent_success(
    tmp_path: Path,
) -> None:
    """A restart after the draft was seen but before submit must not vanish silently.

    The draft is pasted and rendered (the readiness/draft gate is
    satisfied), then the process restarts and flushes stdin before the
    submit Enter takes — the second race window in the report. A correct
    build must not report success while the message is lost.
    """
    bridge_dir, delivered, socket = _launch_fake_claude(tmp_path, phase="restart_after_draft")
    try:
        raised, was_delivered = _inject_outcome(bridge_dir, delivered)
        assert raised or was_delivered, (
            "silent drop reproduced: inject_user_message reported success "
            "(no delivery error) but the message never reached Claude after "
            "an in-pane restart that fired once the draft was visible"
        )
    finally:
        _cleanup(socket, bridge_dir)

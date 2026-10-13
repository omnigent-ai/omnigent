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

The same race is reachable through any in-pane relaunch during boot: an
auto-update or crash restart, or a TUI mode transition.

Making the race deterministic
-----------------------------
The genuine web journey needs an interactive Claude login (see
``test_host_claude_native_e2e.py``, opt-in for that reason) and a
claude-code build whose boot restarts in-pane — a combination not
reproducible in CI (the installed claude-code does not restart in-pane
during boot). So, following that
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
message with no restart. The two restart cases are recoverable, so a
correct build must re-deliver the message exactly once with no error; a
silent success that returns while the message vanishes is the bug.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as _bridge
from omnigent.harnesses.claude_native.bridge import (
    bridge_dir_for_bridge_id,
    inject_user_message,
    write_tmux_target,
)

# The exactly-once settle must outlast the restart-receipt grace a stray
# re-delivery waits out. Derive it from the bridge constant, with a fallback
# so this module still imports against a build that predates that constant.
_DUPLICATE_SETTLE_S = getattr(_bridge, "_RESTART_RECEIPT_GRACE_S", 5.0) + 1.0


def _tmux_version() -> tuple[int, ...] | None:
    exe = shutil.which("tmux")
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, "-V"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.search(r"(\d+)\.(\d+)", out)
    return tuple(int(part) for part in match.groups()) if match else None


# ``new-session -e`` (per-session environment) requires tmux >= 3.2.
_TMUX_VERSION = _tmux_version()
pytestmark = pytest.mark.skipif(
    _TMUX_VERSION is None or _TMUX_VERSION < (3, 2),
    reason="requires tmux >= 3.2 (new-session -e)",
)

_MARKER = "the first message must survive an in-pane restart"

# For the "restart after the draft was seen" window the restart fires a beat
# after the paste-end marker lands, before the submit Enter is verified. The
# "boot" window instead restarts off the submit itself (not a wall clock) so
# a slow host cannot let the paste reach the already-restarted process.
_DRAFT_RESTART_AFTER_S = 0.3

# A Claude-Code-shaped TUI: a composer box framed by rules with a leading
# "❯" glyph and bracketed paste. Every submitted non-empty draft is appended
# to $FAKE_CLAUDE_DELIVERED and recorded as a UserPromptSubmit hook.
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
    # The dying process drains the injected keystrokes and restarts in-pane
    # the instant the submit Enter lands after the bracketed paste, losing the
    # message; gating on the submit, not a clock, keeps the race deterministic.
    draw()
    seen_paste_end = False
    while True:
        chunk = os.read(FD, 4096).decode("utf-8", "replace")
        if not chunk:
            break
        if "\x1b[201~" in chunk:
            seen_paste_end = True
            chunk = chunk.split("\x1b[201~", 1)[1]
        if seen_paste_end and ("\r" in chunk or "\n" in chunk):
            break
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
            # Drop a whole CSI/escape sequence (``ESC [ … final``), not just
            # the ESC, so stray key-report sequences never leak into the
            # draft as literal text.
            end = next(
                (i for i, c in enumerate(pending) if i >= 2 and "@" <= c <= "~"),
                None,
            )
            if end is None:
                break  # final byte not here yet; read more
            pending = pending[end + 1 :]
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
    restart_after = _DRAFT_RESTART_AFTER_S if phase == "restart_after_draft" else 0.0
    # Advertise (and create) the bridge dir before the fake TUI starts, so
    # its first hook write finds hooks.jsonl's parent already present. This
    # is also what the runner does when it launches the terminal.
    write_tmux_target(bridge_dir, socket_path=socket, tmux_target="claude")
    try:
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
            f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
        )
    except Exception:
        # Don't leak the bridge dir / tmux server if the pane never started.
        _cleanup(socket, bridge_dir)
        raise
    return bridge_dir, delivered, socket


def _cleanup(socket: Path, bridge_dir: Path) -> None:
    subprocess.run(
        ["tmux", "-S", str(socket), "kill-server"],
        check=False,
        capture_output=True,
    )
    socket.unlink(missing_ok=True)
    shutil.rmtree(bridge_dir, ignore_errors=True)


def _delivered_count(
    delivered: Path, *, timeout_s: float = 10.0, settle_s: float = _DUPLICATE_SETTLE_S
) -> int:
    """Copies of the marker the fake Claude recorded (waits for the first).

    After the first copy lands, settle ``settle_s`` before counting. A
    spurious re-delivery in the product only fires once the restart receipt
    grace lapses without a receipt, so the restart cases pass the full settle
    to catch a duplicate; a presence-only check passes ``0``.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if delivered.exists() and _MARKER in delivered.read_text(encoding="utf-8"):
            if settle_s:
                time.sleep(settle_s)
            return delivered.read_text(encoding="utf-8").count(_MARKER)
        time.sleep(0.25)
    return 0


def _message_recorded(delivered: Path, *, timeout_s: float = 10.0) -> bool:
    return _delivered_count(delivered, timeout_s=timeout_s, settle_s=0.0) > 0


def _inject_outcome(bridge_dir: Path, delivered: Path) -> tuple[bool, int]:
    """Run the real web-turn delivery and report ``(raised, delivered_count)``.

    Mirrors the claude-native executor's web turn, which delegates the
    first message straight to :func:`inject_user_message`. ``raised`` is
    whether the call surfaced a delivery error; ``delivered_count`` is how
    many times the fake Claude recorded (submitted) the message.
    """
    raised = False
    try:
        inject_user_message(bridge_dir, content=_MARKER, timeout_s=20.0)
    except RuntimeError:
        raised = True
    return raised, _delivered_count(delivered)


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
    """A first message injected across an in-pane restart must be re-delivered.

    Restart fires before the pre-restart process ever consumes the paste
    (the ``boot`` window). This scenario is recoverable, so a correct
    build must re-deliver the message exactly once with no error — not
    merely avoid a silent success.
    """
    bridge_dir, delivered, socket = _launch_fake_claude(tmp_path, phase="boot")
    try:
        raised, delivered_count = _inject_outcome(bridge_dir, delivered)
        assert not raised and delivered_count == 1, (
            "the first message must be re-delivered exactly once across an "
            "in-pane restart during boot, with no delivery error; got "
            f"raised={raised}, delivered_count={delivered_count}"
        )
    finally:
        _cleanup(socket, bridge_dir)


def test_restart_after_draft_visibility_does_not_report_silent_success(
    tmp_path: Path,
) -> None:
    """A restart after the draft was seen but before submit must be re-delivered.

    The draft is pasted and rendered (the readiness/draft gate is
    satisfied), then the process restarts and flushes stdin before the
    submit Enter takes — the second race window in the report. A correct
    build must re-deliver the message exactly once with no error.
    """
    bridge_dir, delivered, socket = _launch_fake_claude(tmp_path, phase="restart_after_draft")
    try:
        raised, delivered_count = _inject_outcome(bridge_dir, delivered)
        assert not raised and delivered_count == 1, (
            "the message must be re-delivered exactly once after an in-pane "
            "restart that fired once the draft was visible, with no delivery "
            f"error; got raised={raised}, delivered_count={delivered_count}"
        )
    finally:
        _cleanup(socket, bridge_dir)

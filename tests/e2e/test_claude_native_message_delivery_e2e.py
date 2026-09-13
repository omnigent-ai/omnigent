"""End-to-end reproduction for web-UI message delivery into a claude-native pane.

Reproduction + regression suite for the reported claude-native failure "pasted
web-UI message loses its Enter, stuck draft wedges all later deliveries": a
chat message typed in the
web UI is delivered into the Claude Code TUI running in a tmux pane; its submit
``Enter`` is lost, leaving the text unsubmitted at the ``❯`` prompt, and every
later delivery then fails the readiness gate with the card::

    inner executor error: Claude Code terminal did not become ready within
    30.0s (input prompt never rendered). The message was not delivered.

The report carries three distinct facets, each covered by one test here:

1. ``test_slow_composer_delivery_honors_operator_ready_budget`` — the readiness
   budget is a hardcoded ``_TMUX_READY_TIMEOUT_S = 30.0`` and there is no
   operator knob to raise it. A composer that legitimately takes longer to
   render (a ~250k-token session resume) permanently fails delivery with the
   report's error card. The test models a composer that renders at 45s and an
   operator who set ``OMNIGENT_CLAUDE_READY_TIMEOUT_S=90``; the message must be
   delivered once the composer renders. **This test fails on a build with the
   bug** — ``inject_user_message`` dies at the hardcoded 30s (the env override
   does not exist) before the composer is ready — and passes once the budget is
   configurable.

2. ``test_delivery_not_wedged_by_stuck_draft`` — a stale unsubmitted draft
   sitting at ``❯`` must not wedge later deliveries, and must not corrupt the
   next message by leaving the pasted text concatenated behind the stuck draft.

3. ``test_swallowed_submit_enter_is_retried`` — a submit ``Enter`` folded into
   the paste by a busy TUI must be retried until the draft actually leaves the
   input box (the verify-after-paste fix), instead of leaving the message
   unsubmitted forever.

How the journey is driven
-------------------------
This exercises the **real** claude-native delivery path the runner invokes for
every web-UI chat message: ``inject_user_message`` →
``_wait_for_claude_prompt_ready`` (the readiness gate that raises the reported
card) → ``_paste_and_submit`` (clear stale draft, bracketed-paste, verify the
draft committed, submit Enter, verify submission and re-send Enter). It runs
that code against a **real** tmux pane over a private socket — the same
``tmux send-keys`` / ``load-buffer`` + ``paste-buffer -p`` mechanics the bridge
uses in production — calling ``inject_user_message`` exactly as
``omnigent.inner.claude_native_executor`` does (no explicit ``timeout_s``).

Only the ``claude`` binary itself is a scripted stand-in
(:data:`_FAKE_CLAUDE_TUI`): a real logged-in Claude Code cannot run in CI, and
its TUI timing (a 45s resume, a swallowed Enter) cannot be scheduled
deterministically. The stand-in renders the exact frames the bridge keys on
(box rule + ``❯`` composer row) and models the reported input races; the tmux
transport and the entire bridge delivery/verification code are the real product
path.
"""

from __future__ import annotations

import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import (
    bridge_dir_for_bridge_id,
    inject_user_message,
    write_tmux_target,
)

# The delivery path is tmux send-keys/paste-buffer against a real pane.
pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="claude-native delivery e2e drives a real tmux pane; tmux is not installed",
)

# Seconds the slow-resume pane takes to render its composer: past the hardcoded
# 30s budget, comfortably inside the operator's 90s budget.
_SLOW_COMPOSER_DELAY_S = 45.0

# The readiness budget the operator configures. The configurable-budget fix
# must honor this env var for the slow-composer delivery to succeed; the
# buggy build ignores it and dies at the hardcoded 30s.
_READY_TIMEOUT_ENV_VAR = "OMNIGENT_CLAUDE_READY_TIMEOUT_S"
_OPERATOR_READY_BUDGET_S = "90"

# The unsubmitted message the report shows sitting at the ❯ prompt.
_STUCK_DRAFT_TEXT = "Ping me when the ADR file lands"


_FAKE_CLAUDE_TUI = r'''#!/usr/bin/env python3
"""Scripted stand-in for the Claude Code TUI (delivery e2e fixture).

Renders the frames the claude-native bridge keys on — an opening box
rule, a ``❯ `` composer row, a closing rule and a status footer — and
models the input behaviours the bug report describes:

* ``slow-resume``   — no composer for COMPOSER_DELAY_S seconds (a large
  250k-token session resume), then the composer mounts; pre-composer
  input is flushed, like Claude Code's own boot-time input flush.
* ``stuck-draft``   — the composer starts out holding an unsubmitted
  draft (a previously pasted message whose Enter was lost).
* ``swallow-enter`` — the first submit Enter after a paste is folded
  into the draft as a newline instead of submitting (the lost-Enter
  race on a busy TUI).

A successful submit clears the composer, echoes the message into the
transcript as ``> <text>`` / ``DELIVERED: <text>`` and appends the full
text to DELIVERY_LOG so the test can assert on delivery.
"""
import os
import select
import sys
import termios
import time
import tty

MODE = "__MODE__"
COMPOSER_DELAY_S = float("__COMPOSER_DELAY_S__")
DELIVERY_LOG = "__DELIVERY_LOG__"
STUCK_DRAFT = "__STUCK_DRAFT__"

RULE = "─" * 60
PROMPT = "❯ "
PASTE_OPEN = "\x1b[200~"
PASTE_CLOSE = "\x1b[201~"

transcript = []
draft = STUCK_DRAFT if MODE == "stuck-draft" else ""
in_paste = False
swallowed = 0


def render(composer_up):
    sys.stdout.write("\x1b[2J\x1b[H")
    if not composer_up:
        sys.stdout.write("✳ Resuming session… (250k tokens, large history)\r\n")
        sys.stdout.write("  Loading transcript, please wait\r\n")
        sys.stdout.flush()
        return
    for line in transcript[-8:]:
        sys.stdout.write(line + "\r\n")
    sys.stdout.write(RULE + "\r\n")
    sys.stdout.write(PROMPT + draft.split("\n")[0] + "\r\n")
    sys.stdout.write(RULE + "\r\n")
    sys.stdout.write("  ⏵⏵ accept edits on (shift+tab to cycle)\r\n")
    sys.stdout.flush()


def submit():
    global draft
    text = draft.strip("\n")
    draft = ""
    if not text:
        return
    first = text.split("\n")[0]
    transcript.append("> " + first)
    transcript.append("DELIVERED: " + first)
    with open(DELIVERY_LOG, "a") as fh:
        fh.write(text + "\n---SUBMIT---\n")


def feed(buf):
    """Consume input; return any trailing partial escape sequence."""
    global draft, in_paste, swallowed
    i = 0
    while i < len(buf):
        ch = buf[i]
        if ch == "\x1b":
            seq = buf[i : i + 6]
            if PASTE_OPEN.startswith(seq) or PASTE_CLOSE.startswith(seq):
                if len(seq) < 6:
                    return buf[i:]  # split across reads; wait for the rest
                in_paste = seq == PASTE_OPEN
                i += 6
                continue
            i += 1  # lone Escape (or unknown sequence intro): ignore
            continue
        if in_paste:
            # Bracketed paste: CR is data (an interior newline), never submit.
            draft += "\n" if ch == "\r" else ch
            i += 1
            continue
        if ch == "\r":
            if MODE == "swallow-enter" and swallowed == 0:
                # Busy TUI folds the first submit Enter into the draft.
                swallowed += 1
                draft += "\n"
            else:
                submit()
            i += 1
            continue
        if ch == "\x01":  # Ctrl-A (Home)
            i += 1
            continue
        if ch == "\x0b":  # Ctrl-K (kill to end; cursor at Home clears the draft)
            draft = ""
            i += 1
            continue
        if ch >= " ":
            draft += ch
        i += 1
    return ""


def main():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    start = time.time()
    pending = ""
    try:
        while True:
            composer_up = time.time() - start >= COMPOSER_DELAY_S
            render(composer_up)
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            data = os.read(fd, 65536)
            if not data:
                time.sleep(0.2)
                continue
            if not composer_up:
                # Claude Code flushes pending terminal input when its TUI
                # mounts; drop pre-composer keystrokes the same way.
                pending = ""
                continue
            pending = feed(pending + data.decode("utf-8", "replace"))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
'''


def _write_fake_claude(
    bin_path: Path,
    *,
    mode: str,
    composer_delay_s: float,
    delivery_log: Path,
    stuck_draft: str = "",
) -> None:
    """
    Materialize the scripted Claude Code stand-in as an executable.

    :param bin_path: Where to write the executable, e.g.
        ``tmp_path / "fake-claude"``. Launched directly in the tmux pane
        by :func:`_launch_fake_pane`.
    :param mode: One of ``"slow-resume"``, ``"stuck-draft"``,
        ``"swallow-enter"`` (see :data:`_FAKE_CLAUDE_TUI`).
    :param composer_delay_s: Seconds before the composer renders.
    :param delivery_log: File the stand-in appends submitted messages to.
    :param stuck_draft: Initial unsubmitted draft (``stuck-draft`` mode).
    :returns: None.
    """
    script = (
        _FAKE_CLAUDE_TUI.replace("__MODE__", mode)
        .replace("__COMPOSER_DELAY_S__", str(composer_delay_s))
        .replace("__DELIVERY_LOG__", str(delivery_log))
        .replace("__STUCK_DRAFT__", stuck_draft)
    )
    bin_path.write_text(script)
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _launch_fake_pane(
    *,
    tmp_path: Path,
    mode: str,
    composer_delay_s: float,
    delivery_log: Path,
    stuck_draft: str = "",
) -> tuple[Path, Path, Path]:
    """
    Launch the scripted Claude stand-in in a real tmux pane and advertise it.

    Writes the stand-in, starts it under a private tmux socket (the same
    transport the runner's terminal launch uses), and calls
    :func:`write_tmux_target` so :func:`inject_user_message` resolves the pane
    exactly as it would in production.

    :param tmp_path: Per-test temp dir for the stand-in + delivery log.
    :param mode: One of ``"slow-resume"``, ``"stuck-draft"``, ``"swallow-enter"``.
    :param composer_delay_s: Seconds before the composer renders.
    :param delivery_log: File the stand-in appends submitted messages to.
    :param stuck_draft: Initial unsubmitted draft (``stuck-draft`` mode).
    :returns: ``(bridge_dir, socket_dir, socket_path)`` for teardown.
    """
    fake = tmp_path / "fake-claude"
    _write_fake_claude(
        fake,
        mode=mode,
        composer_delay_s=composer_delay_s,
        delivery_log=delivery_log,
        stuck_draft=stuck_draft,
    )
    # The bridge dir must live under the bridge root so _ensure_secure_dir
    # accepts it; write_tmux_target creates the (owner-only) chain.
    bridge_dir = bridge_dir_for_bridge_id(f"delivery-e2e-{uuid.uuid4().hex}")
    # A short socket dir: a tmux socket path over ~104 chars overflows the
    # AF_UNIX sockaddr and fails with "socket path too long".
    socket_dir = Path(tempfile.mkdtemp(prefix="odlv"))
    socket_path = socket_dir / "t.sock"
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(fake))}"
    # -d: detached; the pane's PTY is what the stand-in reads/renders on.
    subprocess.run(
        [
            "tmux",
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "main",
            "-x",
            "160",
            "-y",
            "48",
            command,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="main")
    return bridge_dir, socket_dir, socket_path


def _teardown_pane(bridge_dir: Path, socket_dir: Path, socket_path: Path) -> None:
    """Kill the tmux server and remove the pane's scratch dirs (best-effort)."""
    subprocess.run(
        ["tmux", "-S", str(socket_path), "kill-server"],
        check=False,
        capture_output=True,
    )
    shutil.rmtree(socket_dir, ignore_errors=True)
    shutil.rmtree(bridge_dir, ignore_errors=True)


def _submissions(delivery_log: Path) -> list[str]:
    """Return the texts the stand-in has submitted so far, in order."""
    if not delivery_log.exists():
        return []
    raw = delivery_log.read_text(encoding="utf-8", errors="replace")
    return [chunk for chunk in raw.split("\n---SUBMIT---\n") if chunk.strip()]


def _wait_delivered(delivery_log: Path, needle: str, *, timeout: float = 8.0) -> bool:
    """Poll the delivery log until a submission contains *needle*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(needle in sub for sub in _submissions(delivery_log)):
            return True
        time.sleep(0.1)
    return False


def test_slow_composer_delivery_honors_operator_ready_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow-but-eventual composer must deliver under the operator's budget.

    Facet 1 (configurable readiness budget). The composer renders at 45s — past
    the hardcoded 30s budget, inside the operator's 90s budget. The message is
    injected exactly as ``claude_native_executor`` injects it (no explicit
    ``timeout_s``), so the only lever on the budget is
    ``OMNIGENT_CLAUDE_READY_TIMEOUT_S``.

    On the buggy build that env var does not exist: ``inject_user_message``
    waits only the hardcoded 30s, the composer is not up yet, and it raises
    ``ClaudePromptTimeout`` ("...did not become ready within 30.0s ... The
    message was not delivered.") — the report's card — so delivery never
    happens and this test fails. Once the budget is operator-configurable the
    45s composer renders inside the 90s budget and the message lands.
    """
    monkeypatch.setenv(_READY_TIMEOUT_ENV_VAR, _OPERATOR_READY_BUDGET_S)
    delivery_log = tmp_path / "deliveries.log"
    bridge_dir, socket_dir, socket_path = _launch_fake_pane(
        tmp_path=tmp_path,
        mode="slow-resume",
        composer_delay_s=_SLOW_COMPOSER_DELAY_S,
        delivery_log=delivery_log,
    )
    try:
        message = f"slow resume delivery {uuid.uuid4().hex[:6]}"
        inject_user_message(bridge_dir, content=message)
        assert _wait_delivered(delivery_log, message), (
            "the slow-resume composer message was never delivered within the "
            "operator's configured readiness budget"
        )
    finally:
        _teardown_pane(bridge_dir, socket_dir, socket_path)


def test_delivery_not_wedged_by_stuck_draft(tmp_path: Path) -> None:
    """A stale unsubmitted draft must not wedge or corrupt the next delivery.

    Facet 2 (stuck-draft wedge). The composer starts holding a previously
    pasted message whose Enter was lost. Delivering a fresh message must clear
    that stale draft and submit only the new text — never the stale draft, and
    never the two concatenated.
    """
    delivery_log = tmp_path / "deliveries.log"
    bridge_dir, socket_dir, socket_path = _launch_fake_pane(
        tmp_path=tmp_path,
        mode="stuck-draft",
        composer_delay_s=0.0,
        delivery_log=delivery_log,
        stuck_draft=_STUCK_DRAFT_TEXT,
    )
    try:
        message = f"fresh delivery {uuid.uuid4().hex[:6]}"
        inject_user_message(bridge_dir, content=message)
        assert _wait_delivered(delivery_log, message), (
            "the fresh message was wedged by the stuck draft and never delivered"
        )
        submissions = _submissions(delivery_log)
        # The stale draft must have been cleared (Ctrl-A/Ctrl-K) before the
        # paste — never submitted, and never prefixing the new message.
        assert all(_STUCK_DRAFT_TEXT not in sub for sub in submissions), (
            f"the stuck draft leaked into a submission (corruption): {submissions!r}"
        )
    finally:
        _teardown_pane(bridge_dir, socket_dir, socket_path)


def test_swallowed_submit_enter_is_retried(tmp_path: Path) -> None:
    """A submit Enter folded into the paste must be retried until it submits.

    Facet 3 (swallowed submit Enter). A busy TUI coalesces the first submit
    Enter into the paste burst as a newline, leaving the message drafted. The
    verify-after-paste loop must re-send Enter while the draft is still in the
    box, so the message is delivered instead of sitting unsent forever.
    """
    delivery_log = tmp_path / "deliveries.log"
    bridge_dir, socket_dir, socket_path = _launch_fake_pane(
        tmp_path=tmp_path,
        mode="swallow-enter",
        composer_delay_s=0.0,
        delivery_log=delivery_log,
    )
    try:
        message = f"retried enter {uuid.uuid4().hex[:6]}"
        inject_user_message(bridge_dir, content=message)
        assert _wait_delivered(delivery_log, message), (
            "the swallowed submit Enter was never retried; the message stayed "
            "unsubmitted at the prompt"
        )
    finally:
        _teardown_pane(bridge_dir, socket_dir, socket_path)

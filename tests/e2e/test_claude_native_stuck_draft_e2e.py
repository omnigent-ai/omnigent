"""E2E regression tests: a swallowed submit Enter must not wedge the session.

Covers the reported "claude-native: pasted web-UI message loses its Enter,
stuck draft wedges all later deliveries" journey:

1. A web-UI chat message is delivered into the claude-native tmux pane while
   the TUI is busy (long multi-sub-agent turn). Claude Code coalesces the
   rapid stdin burst into a paste, and the trailing submit Enter is folded
   into the burst as a newline instead of submitting.
2. The message text then sits **unsubmitted at the ``❯`` prompt** (a "stuck
   draft") and is silently never delivered.
3. Every subsequent web-UI message fails with ``Claude Code terminal did not
   become ready within 30.0s (input prompt never rendered)`` until a human
   presses Enter in the pane — the session is wedged.

The reporter's suggested fix — "verify submission after paste, and retry the
Enter" — is what ``inject_user_message`` implements on the current build: it
polls until the pasted draft is visible in the input box, sends Enter, then
polls that the draft actually left the box, **re-sending Enter** while it has
not (``_SUBMIT_VERIFY_TIMEOUT_S`` / ``_SUBMIT_RETRY_INTERVAL_S``), and raises
loud instead of silently dropping the message when it never clears. A stuck
draft also cannot wedge later deliveries: the readiness gate accepts a
draft-holding composer, and the pre-paste ``C-a``/``C-k`` clear removes the
leftover text so the next message is delivered clean, not pasted behind it.

The report's related ask — the hardcoded 30s readiness budget
(``_TMUX_READY_TIMEOUT_S``): a legitimately slow prompt render (e.g. resuming
a 250k-token session) still trips the same error card with no way to raise
the budget. ``test_ready_budget_configurable_for_slow_prompt_render`` guards
the supported escape hatch: an ``OMNIGENT_CLAUDE_READY_TIMEOUT_S`` env
override that the delivery path's default budget honors.

How the race is made deterministic
----------------------------------
The swallowed Enter is a timing race inside Claude Code's TUI, so it cannot
be reproduced on demand against the real binary (and this environment has no
interactive Claude login). Instead the tests drive the *exact* host-side
delivery path the runner invokes for a web-UI message —
``omnigent.claude_native_bridge.inject_user_message`` against a **real tmux
pane** — where the pane hosts a minimal fake TUI that faithfully misbehaves
the way the report describes:

* it renders Claude Code's input-box shape (a ``───`` rule directly above a
  ``❯`` row) so the readiness gate and draft detection see the real frames;
* it enables bracketed paste mode and accepts ``paste-buffer -p`` pastes into
  its input box, like Claude Code;
* it **swallows the first Enter after each paste** — the draft stays in the
  box — modelling the paste-burst coalescing race;
* it honors ``C-a``/``C-k`` (clear input) and submits on a later Enter,
  echoing ``SUBMITTED: <text>`` into its transcript;
* optionally it renders its composer only after a boot delay, modelling a
  large-session resume that legitimately outlasts the 30s readiness budget.

With the verify-retry fix present, the first two tests pass. On the
originally reported behavior (single blind Enter, no verification; readiness gate that
rejects a draft-holding prompt) ``test_swallowed_enter_is_retried`` returns
"success" with the message undelivered (its assertions fail) and
``test_stuck_draft_does_not_wedge_next_delivery`` times out with the exact
error card from the report — so this file is the durable fail→pass guard.

Run::

    .venv/bin/pytest tests/e2e/test_claude_native_stuck_draft_e2e.py -v

Requires only ``tmux`` on PATH (no Claude login, no server).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

import omnigent.claude_native_bridge as _bridge_mod
from omnigent.claude_native_bridge import (
    inject_user_message,
    write_tmux_target,
)

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="requires tmux on PATH (inject_user_message shells out to tmux)",
)

# The message from the bug report's representative capture.
_STUCK_MESSAGE = "Ping me when the ADR file lands"
_SECOND_MESSAGE = "Second message after the wedge"
_SLOW_BOOT_MESSAGE = "Message into a slowly resuming session"

# How long the fake TUI delays its composer in the slow-resume test. Must be
# comfortably above the hardcoded 30s budget and below the raised budget the
# test configures.
_SLOW_BOOT_DELAY_S = 40.0

# Fake Claude Code TUI that runs inside the tmux pane. It renders the real
# input-box shape (rule + "❯" row + rule), accepts bracketed pastes into the
# draft, optionally swallows the FIRST Enter after each paste (the reported
# race), and submits on a later Enter. State is mirrored to FAKE_TUI_STATE as
# JSON: {"draft": str, "submitted": str, "enters": int, "swallowed": int}.
_FAKE_TUI_SOURCE = textwrap.dedent(
    """\
    import json, os, sys, time, tty

    STATE_PATH = os.environ["FAKE_TUI_STATE"]
    SWALLOW = os.environ.get("FAKE_TUI_SWALLOW") == "1"
    BOOT_DELAY = float(os.environ.get("FAKE_TUI_BOOT_DELAY_S", "0") or 0)

    state = {
        "draft": os.environ.get("FAKE_TUI_INITIAL_DRAFT", ""),
        "submitted": "",
        "enters": 0,
        "swallowed": 0,
    }
    pending_swallow = False
    transcript = []

    def save():
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)

    def redraw():
        rule = "\\u2500" * 40
        first = state["draft"].split("\\r")[0].split("\\n")[0]
        lines = transcript[-8:] + [rule, "\\u276f " + first, rule]
        sys.stdout.write("\\x1b[2J\\x1b[H" + "\\r\\n".join(lines))
        sys.stdout.flush()

    tty.setraw(0)
    if BOOT_DELAY:
        # Model a large-session resume: the TUI is alive but its input box
        # does not render until the transcript finishes loading.
        sys.stdout.write("* Resuming session... (large transcript, 250k tokens)\\r\\n")
        sys.stdout.flush()
        time.sleep(BOOT_DELAY)
    # Request bracketed paste mode so ``tmux paste-buffer -p`` wraps the
    # payload in ESC[200~ / ESC[201~ markers, exactly as Claude Code does.
    sys.stdout.write("\\x1b[?2004h")
    redraw()
    save()

    PASTE_OPEN = b"\\x1b[200~"
    PASTE_CLOSE = b"\\x1b[201~"
    buf = b""
    while True:
        chunk = os.read(0, 4096)
        if not chunk:
            break
        buf += chunk
        while buf:
            if buf.startswith(PASTE_OPEN):
                end = buf.find(PASTE_CLOSE)
                if end < 0:
                    break  # paste split across reads; wait for the rest
                paste = buf[len(PASTE_OPEN):end].decode("utf-8", "replace")
                buf = buf[end + len(PASTE_CLOSE):]
                state["draft"] += paste.rstrip("\\r")
                if SWALLOW:
                    pending_swallow = True
                redraw()
                save()
            elif buf[:1] in (b"\\r", b"\\n"):
                buf = buf[1:]
                state["enters"] += 1
                if state["draft"]:
                    if pending_swallow:
                        # The reported race: the Enter is folded into the
                        # paste burst and the draft stays in the box.
                        pending_swallow = False
                        state["swallowed"] += 1
                    else:
                        state["submitted"] = state["draft"].split("\\r")[0]
                        transcript.append("SUBMITTED: " + state["submitted"])
                        state["draft"] = ""
                redraw()
                save()
            elif buf[:1] == b"\\x01":  # C-a (home)
                buf = buf[1:]
            elif buf[:1] == b"\\x0b":  # C-k after C-a: clear the input box
                buf = buf[1:]
                state["draft"] = ""
                pending_swallow = False
                redraw()
                save()
            elif buf[:1] == b"\\x1b":
                if PASTE_OPEN.startswith(buf):
                    break  # possibly a partial paste-open marker
                buf = buf[1:]  # bare Escape or other sequence: ignore
            else:
                ch = buf[:1]
                buf = buf[1:]
                if 32 <= ch[0] < 127:
                    state["draft"] += ch.decode()
                    redraw()
                    save()
    """
)


def _capture(socket_path: Path) -> str:
    """Return the pane's visible text (for user-observable assertions)."""
    proc = subprocess.run(
        ["tmux", "-S", str(socket_path), "capture-pane", "-t", "main", "-p"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout


def _read_state(state_path: Path) -> dict:
    return json.loads(state_path.read_text())


def _start_fake_claude_pane(
    tmp_path: Path,
    *,
    initial_draft: str = "",
    swallow: bool = True,
    boot_delay_s: float = 0.0,
) -> tuple[Path, Path, Path]:
    """Launch the fake TUI in a real tmux pane and advertise it to the bridge.

    :returns: ``(bridge_dir, socket_path, state_path)`` ready for
        :func:`inject_user_message`.
    """
    tui_path = tmp_path / "fake_tui.py"
    tui_path.write_text(_FAKE_TUI_SOURCE)
    state_path = tmp_path / "state.json"
    socket_path = tmp_path / "tmux.sock"
    env_prefix = (
        f"FAKE_TUI_STATE={state_path} FAKE_TUI_SWALLOW={'1' if swallow else '0'} "
        f"FAKE_TUI_BOOT_DELAY_S={boot_delay_s} "
        f"FAKE_TUI_INITIAL_DRAFT={json.dumps(initial_draft)} LANG=C.UTF-8"
    )
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
            "100",
            "-y",
            "30",
            f"env {env_prefix} {sys.executable} {tui_path}",
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    if boot_delay_s == 0:
        # Wait for the TUI to come up (state mirrored + composer rendered).
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if state_path.exists() and "❯" in _capture(socket_path):
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"fake TUI never rendered its composer: {_capture(socket_path)!r}")
    # The bridge dir must live under the real claude-native bridge root
    # ($TMPDIR/omnigent-<uid>/claude-native/...): the bridge validates the
    # path before trusting tmux.json, exactly as in production.
    bridge_dir = _bridge_mod._BRIDGE_ROOT / f"e2e-stuck-draft-{uuid.uuid4().hex[:12]}"
    write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="main")
    return bridge_dir, socket_path, state_path


@pytest.fixture()
def kill_tmux(tmp_path: Path):
    yield
    subprocess.run(
        ["tmux", "-S", str(tmp_path / "tmux.sock"), "kill-server"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    for leftover in _bridge_mod._BRIDGE_ROOT.glob("e2e-stuck-draft-*"):
        shutil.rmtree(leftover, ignore_errors=True)


def test_swallowed_enter_is_retried(tmp_path: Path, kill_tmux: None) -> None:
    """Facet 1: the message whose Enter is swallowed still gets delivered.

    The TUI folds the first Enter into the paste burst (the draft stays in
    the box). The delivery path must notice the draft did not clear and
    retry the Enter, so the message submits instead of silently sitting
    unsent at ``❯`` forever.
    """
    bridge_dir, socket_path, state_path = _start_fake_claude_pane(tmp_path)

    # The web-UI delivery path the runner invokes. On the buggy build this
    # returned "success" after a single blind Enter with the message unsent.
    inject_user_message(bridge_dir, content=_STUCK_MESSAGE, timeout_s=15.0)

    # The submit-verify loop returns once the box cleared; the state file is
    # written before the box redraws, but poll briefly to be robust.
    deadline = time.monotonic() + 5
    state = _read_state(state_path)
    while time.monotonic() < deadline and state["submitted"] != _STUCK_MESSAGE:
        time.sleep(0.1)
        state = _read_state(state_path)

    assert state["swallowed"] == 1, f"the race was not exercised: {state}"
    assert state["submitted"] == _STUCK_MESSAGE, f"message never submitted (stuck draft): {state}"
    assert state["draft"] == "", f"draft still in the input box: {state}"
    assert state["enters"] >= 2, f"the swallowed Enter was never retried: {state}"
    # User-observable outcome in the pane: the message reached the transcript.
    assert f"SUBMITTED: {_STUCK_MESSAGE}" in _capture(socket_path)


def test_stuck_draft_does_not_wedge_next_delivery(tmp_path: Path, kill_tmux: None) -> None:
    """Facet 2: a stuck draft at ``❯`` must not fail every later delivery.

    The pane starts with the report's exact wedge state: a previously sent
    message sitting unsubmitted at the prompt. On the buggy build every
    subsequent delivery raised ``Claude Code terminal did not become ready
    within 30.0s`` until a human pressed Enter. The delivery path must
    instead accept the draft-holding composer, clear the leftover text, and
    deliver the new message clean (not pasted behind the stuck draft).
    """
    bridge_dir, socket_path, state_path = _start_fake_claude_pane(
        tmp_path, initial_draft=_STUCK_MESSAGE
    )
    assert _STUCK_MESSAGE in _capture(socket_path)  # the wedge state is live

    # Must not raise the 30s readiness RuntimeError from the report.
    inject_user_message(bridge_dir, content=_SECOND_MESSAGE, timeout_s=15.0)

    deadline = time.monotonic() + 5
    state = _read_state(state_path)
    while time.monotonic() < deadline and state["submitted"] != _SECOND_MESSAGE:
        time.sleep(0.1)
        state = _read_state(state_path)

    assert state["submitted"] == _SECOND_MESSAGE, (
        f"delivery into a stuck-draft pane failed: {state}"
    )
    # No corruption: the stuck draft was cleared, not prepended to the new one.
    assert _STUCK_MESSAGE not in state["submitted"]
    assert state["draft"] == "", f"input box not clean after delivery: {state}"
    assert f"SUBMITTED: {_SECOND_MESSAGE}" in _capture(socket_path)


def test_ready_budget_configurable_for_slow_prompt_render(
    tmp_path: Path,
    kill_tmux: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facet 3 (report's related ask): the 30s readiness budget is hardcoded.

    A healthy TUI that legitimately takes longer than 30s to render its
    prompt — the report's example is resuming a 250k-token session — trips
    the exact same error card, and there is no supported way to raise the
    budget (``_TMUX_READY_TIMEOUT_S`` is a module constant; the executor
    calls ``inject_user_message`` with the default).

    This test encodes the supported behavior: an operator-set
    ``OMNIGENT_CLAUDE_READY_TIMEOUT_S`` environment override must be honored
    by the delivery path's default budget, so the message is delivered once
    the slow-but-healthy prompt renders. Without the override plumbing it
    fails with ``ClaudePromptTimeout`` at the hardcoded 30s budget.
    """
    monkeypatch.setenv("OMNIGENT_CLAUDE_READY_TIMEOUT_S", "90")
    bridge_dir, socket_path, state_path = _start_fake_claude_pane(
        tmp_path, swallow=False, boot_delay_s=_SLOW_BOOT_DELAY_S
    )

    # Deliberately no ``timeout_s=`` argument: the executor calls it with the
    # default budget, which is exactly where the override must apply.
    inject_user_message(bridge_dir, content=_SLOW_BOOT_MESSAGE)

    deadline = time.monotonic() + 5
    state = _read_state(state_path)
    while time.monotonic() < deadline and state["submitted"] != _SLOW_BOOT_MESSAGE:
        time.sleep(0.1)
        state = _read_state(state_path)

    assert state["submitted"] == _SLOW_BOOT_MESSAGE, (
        f"message never delivered to the slowly resuming session: {state}"
    )
    assert f"SUBMITTED: {_SLOW_BOOT_MESSAGE}" in _capture(socket_path)

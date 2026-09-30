"""E2E regression: claude-native leaves a first message unsent in the input
box while reporting the turn as delivered.

The failure this guards: a user sends the first message
from the Omnigent web chat to a fresh ``claude-native`` session whose Claude
Code TUI is still booting (several MCP servers is enough). Claude Code renders
the pasted draft on the row *below* an empty prompt glyph -- an empty ``❯`` row
with the ``[Pasted text #1 …]`` placeholder underneath it:

    ────────────────────────────
    ❯
      [Pasted text #1 +4 lines]
    ────────────────────────────

The bridge's submit verification (``_paste_and_submit`` → ``_verify_submit_accepted``
→ ``_draft_in_input_box``) looks only at the LAST line carrying the ``❯`` glyph.
That line is empty, so ``_draft_in_input_box`` answers "draft gone", the bridge
concludes the submit was accepted and never retries Enter -- even though the
message is still sitting in the composer, unsent, needing a manual Enter. The
executor yields ``TurnComplete`` and nothing is logged, so the web UI shows the
session idle/working forever (in a Polly run the worker never starts).

This test drives the REAL transport end to end: a real ``tmux`` server on a
private socket advertised through the production ``write_tmux_target``, and a
fake Claude TUI that reproduces the case-1 render. The fake parses the same
bracketed-paste stream the bridge delivers (``ESC [ 2 0 0 ~`` … ``ESC [ 2 0 1 ~``
with interior newlines as CR), so a carriage return inside the paste stays draft
data; only a standalone ``Enter`` keystroke is a submit. It shows the committed
paste on the glyph row (so the bridge's draft-visibility poll passes), then on
the first submit Enter moves the draft to the row below an empty glyph (the
stranded "review" state) instead of submitting, and only a RETRIED Enter submits
it into the transcript and clears the box.

Correct behavior (either acceptable fix): ``inject_user_message`` must not
report success while the draft is stranded -- it must get the message submitted
(retry the Enter) or raise that delivery failed. The bug is that it returns
successfully with the message still in the box. Confirmed live against the real
Claude Code 2.1.281 TUI booting slow MCP servers: ``inject_user_message``
returned in 0.6s while the pane showed the message stranded below an empty
glyph.

Runs with no LLM, no ``claude`` binary and no server -- only ``tmux``::

    pytest tests/e2e/test_claude_native_unsent_draft_below_glyph_e2e.py -v
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import (
    _BRIDGE_ROOT,
    _capture_pane,
    inject_user_message,
    write_tmux_target,
)

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH")

# The first web-chat message. Its first line is the needle the bridge looks for
# in the input box to confirm the paste committed.
_MESSAGE = "kick off the first worker turn"

# The fake Claude TUI. It parses the bracketed-paste stream the bridge delivers
# and reproduces the below-glyph stranded render precisely:
#
#   * empty composer, then when the paste closes (ESC[201~) it renders the
#     [Pasted text …] placeholder ON the glyph row -- so the bridge's draft
#     -visibility poll sees the draft land, exactly like the real TUI at commit
#     time (before any submit Enter);
#   * on the FIRST standalone Enter after the paste committed it does NOT
#     submit. It moves the placeholder to the row BELOW an empty glyph row --
#     the render where the live glyph row is empty and the draft sits
#     underneath -- and stays there (Claude Code's "review and press Enter to
#     send" gate);
#   * only a RETRIED Enter, sent while that stranded draft is still on screen,
#     submits the message into the transcript and clears the input box.
#
# Carriage returns arriving inside the bracketed paste are draft data (not
# submits), matching a real paste. So a bridge that reads "draft gone" off the
# empty glyph row sends exactly one Enter and leaves the message stranded; a
# bridge that still sees the draft under the glyph retries the Enter and the
# message is delivered.
_FAKE_CLAUDE_TUI = """\
import os, sys, termios, tty

PROMPT_GLYPH = "\\u276f"
RULE = "\\u2500" * 30
NEEDLE = sys.argv[1]
PASTE_START = b"\\x1b[200~"
PASTE_END = b"\\x1b[201~"


def render(rows):
    sys.stdout.write("\\x1b[2J\\x1b[H")
    for line in rows:
        sys.stdout.write(line + "\\r\\n")
    sys.stdout.flush()


def empty_composer(transcript):
    return transcript + [RULE, PROMPT_GLYPH + " ", RULE]


def committed(transcript):
    # Paste committed: placeholder on the glyph row, as the real TUI shows it
    # at commit time (before the submit Enter).
    return transcript + [RULE, PROMPT_GLYPH + " [Pasted text #1 +4 lines]", RULE]


def stranded(transcript):
    # Stranded render: empty glyph row, the draft placeholder on the row below.
    return transcript + [RULE, PROMPT_GLYPH + " ", "  [Pasted text #1 +4 lines]", RULE]


def main():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    buf = b""
    draft = ""  # accumulated paste content
    transcript = []
    state = "empty"  # empty -> committed -> stranded -> (submitted -> empty)
    in_paste = False
    # Request bracketed paste, as the real Claude TUI does: only then does tmux
    # wrap paste-buffer -p content in the ESC[200~/ESC[201~ markers the bridge
    # relies on to keep interior newlines as draft data.
    sys.stdout.write("\\x1b[?2004h")
    sys.stdout.flush()
    render(empty_composer(transcript))
    try:
        while True:
            data = os.read(fd, 1)
            if not data:
                break
            buf += data
            # Resolve complete bracketed-paste markers first.
            if PASTE_START.startswith(buf) or PASTE_END.startswith(buf):
                if buf == PASTE_START:
                    in_paste = True
                    buf = b""
                elif buf == PASTE_END:
                    in_paste = False
                    buf = b""
                    if NEEDLE in draft and state == "empty":
                        state = "committed"
                        render(committed(transcript))
                # Otherwise still matching a marker prefix; keep buffering.
                continue
            byte = buf[0]
            buf = b""
            if in_paste:
                # Paste data: CR (mapped newline) and printable text are the
                # draft, never a submit.
                if byte == 13 or byte >= 0x20:
                    draft += chr(byte)
                continue
            if byte == 3:  # Ctrl-C tears the pane down cleanly on teardown
                break
            if byte == 13:  # a standalone Enter keystroke
                if state == "committed":
                    # First Enter does not submit -- strand the draft below an
                    # empty glyph row and wait for a review Enter.
                    state = "stranded"
                    render(stranded(transcript))
                elif state == "stranded":
                    # A retried Enter finally submits.
                    transcript = transcript + ["sent: " + draft.replace("\\r", " ").strip()]
                    draft = ""
                    state = "empty"
                    render(empty_composer(transcript))
                continue
            # Other control bytes (Escape, Ctrl-A/Ctrl-K) are swallowed.
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


main()
"""


@pytest.fixture
def booting_claude_pane() -> Iterator[tuple[Path, str]]:
    """A claude-native bridge dir advertising a real tmux pane whose fake TUI
    strands the draft (empty glyph row, placeholder below) on the first
    submit Enter and only submits on a retried Enter.

    Yields the bridge dir and the tmux socket path. The tmux server is always
    killed on teardown, even when the test body raises.
    """
    work = Path(tempfile.mkdtemp(prefix="unsentdraft-"))
    # Keep the socket path short: a long path overflows the AF_UNIX limit.
    socket_path = work / "t.sock"
    tui_path = work / "fake_claude_tui.py"
    tui_path.write_text(_FAKE_CLAUDE_TUI, encoding="utf-8")

    subprocess.run(
        [
            "tmux",
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "claude",
            "-x",
            "80",
            "-y",
            "24",
            sys.executable,
            str(tui_path),
            _MESSAGE,
        ],
        check=True,
        timeout=30.0,
    )

    # The bridge validates its dir sits under the trusted claude-native root,
    # so the fixture cannot use the temp dir for it.
    bridge_dir = _BRIDGE_ROOT / f"unsentdraft-{uuid.uuid4().hex}"
    write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="claude")

    # Give the fake TUI a beat to paint the composer before delivery starts.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "❯" in _capture_pane(str(socket_path), "claude"):
            break
        time.sleep(0.1)

    try:
        yield bridge_dir, str(socket_path)
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            check=False,
            timeout=30.0,
        )
        shutil.rmtree(bridge_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            for child in work.iterdir():
                child.unlink()
            work.rmdir()


def test_inject_does_not_strand_first_message_below_the_glyph(
    booting_claude_pane: tuple[Path, str],
) -> None:
    """The bridge must not report delivery while the first message sits unsent
    on the row below an empty prompt glyph.

    Fails on the buggy bridge (``_draft_in_input_box`` reads the empty glyph
    row, concludes the submit was accepted after a single Enter, and returns
    successfully with the message stranded). Passes once the bridge either
    retries the Enter so the message submits, or raises that delivery failed.
    """
    bridge_dir, socket_path = booting_claude_pane

    raised: Exception | None = None
    try:
        inject_user_message(bridge_dir, content=_MESSAGE)
    except RuntimeError as exc:
        raised = exc

    pane = _capture_pane(socket_path, "claude")
    submitted = "sent: " in pane and _MESSAGE[:20] in pane

    assert submitted or raised is not None, (
        "inject_user_message reported delivery, but the message is still "
        f"stranded unsent in the composer (never submitted):\n{pane}"
    )

"""E2E regression: a message injected while a previous turn is still
streaming is left unsent because the previous turn's transcript echo carries
the prompt glyph.

While an earlier turn streams, its echo (``❯ what happened?``) sits above the
composer frame and carries the same ``❯`` glyph as the live input box. When the
new message's paste lands as a placeholder on the row below an empty composer
glyph, the pane holds two glyph lines:

    ❯ what happened?                 <- previous turn's echo (transcript)
    ● ...streaming response...
    ────────────────────────────
    ❯                                 <- live composer, empty glyph row
      [Pasted text #1 +4 lines]        <- the new draft, unsent
    ────────────────────────────

``_draft_in_input_box`` looks only at the LAST glyph line -- the empty composer
row -- so it answers "draft gone" and the bridge reports the turn delivered
without retrying Enter, even though the message is stranded. (In the wild the new
turn's Enter first submits the *previous* stranded message; both turns then
report success while only one ``UserPromptSubmit`` is recorded.) A fix that
anchors to the composer's rule frame must find the placeholder there and not be
fooled by the ``❯ what happened?`` echo above it.

This is the sibling of ``test_claude_native_unsent_draft_below_glyph_e2e.py``:
same defect, different render shape, so a fix that repairs only one shape is
still caught. It drives the REAL transport end to end -- a real ``tmux``
server advertised through the production ``write_tmux_target`` and the real
``inject_user_message`` -- against a fake Claude TUI that keeps a streaming
transcript above the composer and reproduces that render. It parses the
bridge's bracketed-paste stream so interior newlines stay draft data and only a
standalone Enter is a submit.

Correct behavior (either acceptable fix): ``inject_user_message`` must not report
success while the draft is stranded -- it must submit the message (retry the
Enter) or raise that delivery failed.

Runs with no LLM, no ``claude`` binary and no server -- only ``tmux``::

    pytest tests/e2e/test_claude_native_unsent_draft_transcript_echo_e2e.py -v
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

# The new message injected mid-stream. Its first line is the needle the bridge
# looks for in the input box to confirm the paste committed.
_MESSAGE = "and now summarize the findings"

# The previous turn's user prompt, still echoed in the transcript (with the
# prompt glyph) while its response streams.
_PRIOR_ECHO = "what happened?"

# The fake Claude TUI. It keeps the previous turn's echo and a streaming response
# above the composer frame, and reproduces the observed render:
#
#   * paste commit renders the placeholder on the composer glyph row (so the
#     bridge's draft-visibility poll passes);
#   * the FIRST standalone Enter does not submit -- it moves the placeholder to
#     the row BELOW an empty composer glyph while the "❯ what happened?" echo
#     stays above, so the LAST glyph line the bridge scans is the empty composer;
#   * only a RETRIED Enter submits the new message into the transcript.
#
# Carriage returns arriving inside the bracketed paste are draft data, not
# submits. A bridge that reads "draft gone" off the empty composer row sends one
# Enter and strands the message; a bridge that anchors to the composer frame
# still sees the draft and retries the Enter, delivering it.
_FAKE_CLAUDE_TUI = """\
import os, sys, termios, tty

PROMPT_GLYPH = "\\u276f"
RULE = "\\u2500" * 30
NEEDLE = sys.argv[1]
PRIOR_ECHO = sys.argv[2]
PASTE_START = b"\\x1b[200~"
PASTE_END = b"\\x1b[201~"

# The previous turn, still streaming, sits above the composer with its glyph.
STREAMING_TRANSCRIPT = [
    PROMPT_GLYPH + " " + PRIOR_ECHO,
    "\\u25cf working on it\\u2026",
]


def render(rows):
    sys.stdout.write("\\x1b[2J\\x1b[H")
    for line in rows:
        sys.stdout.write(line + "\\r\\n")
    sys.stdout.flush()


def empty_composer(transcript):
    return transcript + [RULE, PROMPT_GLYPH + " ", RULE]


def committed(transcript):
    return transcript + [RULE, PROMPT_GLYPH + " [Pasted text #1 +4 lines]", RULE]


def stranded(transcript):
    # Stranded render: empty composer glyph, the draft placeholder on the row
    # below, and the previous turn's "❯ ..." echo still above the frame.
    return transcript + [RULE, PROMPT_GLYPH + " ", "  [Pasted text #1 +4 lines]", RULE]


def main():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    buf = b""
    draft = ""
    submitted = list(STREAMING_TRANSCRIPT)
    state = "empty"  # empty -> committed -> stranded -> (submitted -> empty)
    in_paste = False
    # Request bracketed paste, as the real Claude TUI does: only then does tmux
    # wrap paste-buffer -p content in the ESC[200~/ESC[201~ markers the bridge
    # relies on to keep interior newlines as draft data.
    sys.stdout.write("\\x1b[?2004h")
    sys.stdout.flush()
    render(empty_composer(submitted))
    try:
        while True:
            data = os.read(fd, 1)
            if not data:
                break
            buf += data
            if PASTE_START.startswith(buf) or PASTE_END.startswith(buf):
                if buf == PASTE_START:
                    in_paste = True
                    buf = b""
                elif buf == PASTE_END:
                    in_paste = False
                    buf = b""
                    if NEEDLE in draft and state == "empty":
                        state = "committed"
                        render(committed(submitted))
                continue
            byte = buf[0]
            buf = b""
            if in_paste:
                if byte == 13 or byte >= 0x20:
                    draft += chr(byte)
                continue
            if byte == 3:  # Ctrl-C tears the pane down cleanly on teardown
                break
            if byte == 13:  # a standalone Enter keystroke
                if state == "committed":
                    state = "stranded"
                    render(stranded(submitted))
                elif state == "stranded":
                    echo = PROMPT_GLYPH + " " + draft.replace("\\r", " ").strip()
                    submitted = submitted + [echo]
                    draft = ""
                    state = "empty"
                    render(empty_composer(submitted))
                continue
            # Other control bytes (Escape, Ctrl-A/Ctrl-K) are swallowed.
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


main()
"""


@pytest.fixture
def streaming_claude_pane() -> Iterator[tuple[Path, str]]:
    """A claude-native bridge dir advertising a real tmux pane whose fake TUI
    keeps a streaming transcript (with the previous turn's ``❯`` echo) above the
    composer and strands a mid-stream paste below an empty composer glyph.

    Yields the bridge dir and the tmux socket path. The tmux server is always
    killed on teardown, even when the test body raises.
    """
    work = Path(tempfile.mkdtemp(prefix="echodraft-"))
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
            _PRIOR_ECHO,
        ],
        check=True,
        timeout=30.0,
    )

    # The bridge validates its dir sits under the trusted claude-native root,
    # so the fixture cannot use the temp dir for it.
    bridge_dir = _BRIDGE_ROOT / f"echodraft-{uuid.uuid4().hex}"
    write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="claude")

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _PRIOR_ECHO in _capture_pane(str(socket_path), "claude"):
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


def test_inject_does_not_strand_message_behind_a_transcript_echo(
    streaming_claude_pane: tuple[Path, str],
) -> None:
    """The bridge must not report delivery while the message sits unsent below an
    empty composer glyph with a previous turn's ``❯`` echo above it.

    Fails on the buggy bridge (``_draft_in_input_box`` reads the empty composer
    row -- the last glyph line -- concludes the submit was accepted after a
    single Enter, and returns successfully with the message stranded). Passes
    once the bridge anchors to the composer frame and retries the Enter so the
    message submits, or raises that delivery failed.
    """
    bridge_dir, socket_path = streaming_claude_pane

    raised: Exception | None = None
    try:
        inject_user_message(bridge_dir, content=_MESSAGE)
    except RuntimeError as exc:
        raised = exc

    pane = _capture_pane(socket_path, "claude")
    submitted = f"{_MESSAGE[:20]}" in pane.rsplit("[Pasted text", 1)[0].split(_PRIOR_ECHO, 1)[-1]

    assert submitted or raised is not None, (
        "inject_user_message reported delivery, but the message is still "
        f"stranded unsent below the transcript echo (never submitted):\n{pane}"
    )

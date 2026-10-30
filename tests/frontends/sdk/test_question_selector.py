"""
PTY test: the inline question selector answers with real keystrokes.

The driver (``_question_selector_driver.py``) asks seven questions through
:meth:`TerminalHost.ask_questions` and prints each result. This test drives
them the way a person would: arrows and Enter for a single choice, Space to
check multi-select options, typing for a custom answer, editing a prefilled
answer, and Esc to dismiss. Answers must never be submitted as chat or saved
to the input history, whatever key accepts them, and undo in the chat line
never brings one back.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pexpect
import pytest

from tests._helpers.terminal_screen import TerminalScreen

_DRIVER = Path(__file__).parent / "_question_selector_driver.py"
_TERM = "xterm-256color"
_ROWS = 40
_COLS = 120
_BOOT_TIMEOUT = 10.0
_RENDER_TIMEOUT = 5.0
_DOWN = "\x1b[B"
_ESC = "\x1b"
_ALT_HASH = "\x1b#"
_UNDO = "\x1f"  # Ctrl+_


@pytest.fixture
def selector_child(tmp_path: Path) -> Iterator[tuple[pexpect.spawn, Path]]:
    """Spawn the selector driver under a PTY with a throwaway history file."""
    history = tmp_path / "history"
    child = pexpect.spawn(
        sys.executable,
        [str(_DRIVER), str(history)],
        env={"TERM": _TERM, "LINES": str(_ROWS), "COLUMNS": str(_COLS)},
        encoding="utf-8",
        timeout=_BOOT_TIMEOUT,
        dimensions=(_ROWS, _COLS),
    )
    yield child, history
    child.close(force=True)


@pytest.fixture
def selector_screen(selector_child: tuple[pexpect.spawn, Path]) -> TerminalScreen:
    """Render the driver's output, attached right after spawning."""
    return TerminalScreen(selector_child[0])


def test_selector_answers_with_real_keystrokes(
    selector_child: tuple[pexpect.spawn, Path], selector_screen: TerminalScreen
) -> None:
    child, history = selector_child
    screen = selector_screen

    def wait(text: str) -> str:
        return screen.wait_for(
            lambda shown: text in shown, timeout=_RENDER_TIMEOUT, what=repr(text)
        )

    wait("question 1 of 1 · Environment")
    wait("▸ 1. dev — Development")
    child.send(_DOWN)
    wait("▸ 2. staging")
    child.send("\r")
    wait('ANSWER 1 {"Env?": "staging"}')

    wait("Which regions?")
    child.send(" ")  # check us-east
    child.send(_DOWN)
    child.send(_DOWN)
    child.send(" ")  # check ap-south
    wait("[x] 3. ap-south")
    child.send("\r")
    wait('ANSWER 2 {"regions": ["us-east", "ap-south"]}')

    wait("Which name?")
    child.send("qa")  # typing highlights "Type something"
    wait("▸ 3. Type something")
    child.send("\r")
    wait('ANSWER 3 {"Name?": "qa"}')

    wait("Release notes?")
    wait("draft notes")  # the prefill is in the answer line, editable
    child.send(" v2\r")
    wait('ANSWER 4 {"0": "draft notes v2"}')

    wait("Dismiss me")
    child.send(_ESC)
    wait("ANSWER 5 null")

    # Alt+# accepts the line (as a comment): it answers, never sends chat.
    wait("Comment me")
    child.send("synthetic-answer")
    wait("▸ 2. Type something")
    child.send(_ALT_HASH)
    wait('ANSWER 6 {"Comment?": "#synthetic-answer"}')

    # Undo in the chat line can't bring back a dismissed answer.
    wait("Discard me")
    child.send("synthetic-discarded-answer")
    wait("▸ 2. Type something")
    child.send(_ESC)
    wait("ANSWER 7 null")
    child.send(_UNDO)
    child.send("\r")
    child.send("probe\r")
    rendered = wait('CHAT "probe"')

    chat = [line.strip() for line in rendered.splitlines() if line.strip().startswith("CHAT")]
    assert chat == ['CHAT "probe"'], f"an answer reached the chat:\n{rendered}"
    saved = history.read_text() if history.exists() else ""
    for answer in ("qa", "draft notes", "synthetic"):
        assert answer not in saved, saved

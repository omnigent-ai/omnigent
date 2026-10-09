"""Kitty keyboard protocol (CSI-u) keys in the SDK TUI prompt: Ctrl+J and Alt+letter
must act as keys, not leak ``[106;5u``-style text. Drives the real ``omnigent run``
prompt under a PTY and reads the result off a pyte screen."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")
pyte = pytest.importorskip("pyte")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASK_DEMO_DIR = _REPO_ROOT / "tests" / "resources" / "agents" / "ask-demo"

# Launch budget: daemon spawn + local server boot + runner bring-up (see
# test_repl_approval_e2e._LAUNCH_TIMEOUT for the breakdown).
_LAUNCH_TIMEOUT = 120
_ROWS, _COLS = 30, 120
_TEXT = "hello world"
_PROMPT_MARKER = "❯"

# What a Kitty-protocol terminal sends for these keys once the prompt has pushed
# ``CSI > 1 u`` (flag 1, "disambiguate escape codes").
_CSI_U_CTRL_A = b"\x1b[97;5u"
_CSI_U_CTRL_J = b"\x1b[106;5u"
_CSI_U_ALT_B = b"\x1b[98;3u"
_CSI_U_ALT_F = b"\x1b[102;3u"
_CSI_U_SHIFT_ENTER = b"\x1b[13;2u"


def _omnigent_cli() -> str:
    venv_cli = Path(sys.executable).parent / "omnigent"
    if venv_cli.exists():
        return str(venv_cli)
    path = shutil.which("omnigent")
    if path is None:
        pytest.skip("omnigent CLI not found")
    return path


def _prompt_env(home: Path, mock_llm_server_url: str) -> dict[str, str]:
    """Child env: fresh HOME with a persisted theme (skips the first-run picker),
    the mock LLM as the OpenAI endpoint, and no inherited runner/host state."""
    config_home = home / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\ntui:\n  theme: dark\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("OMNIGENT_")}
    env.pop("RUNNER_SERVER_URL", None)
    env.update(
        {
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_SKIP_ONBOARD": "1",
            "OPENAI_API_KEY": "mock-key",
            "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
            "PYTHONPATH": str(_REPO_ROOT),
            "TERM": "xterm-256color",
            "PROMPT_TOOLKIT_NO_CPR": "1",
        }
    )
    return env


class _PromptScreen:
    """The live ``omnigent run`` prompt, mirrored into a pyte screen so tests can
    read the prompt row and cursor exactly as a terminal would show them."""

    def __init__(self, child: pexpect.spawn) -> None:
        self._child = child
        self._screen = pyte.Screen(_COLS, _ROWS)
        self._stream = pyte.ByteStream(self._screen)
        child.logfile_read = self

    def write(self, data: bytes) -> None:
        self._stream.feed(data)

    def flush(self) -> None:
        pass

    def settle(self, quiet: float = 0.6, limit: float = 5.0) -> None:
        """Drain PTY output until it has been silent for ``quiet`` seconds."""
        deadline = time.monotonic() + limit
        last = time.monotonic()
        while time.monotonic() < deadline and time.monotonic() - last < quiet:
            try:
                self._child.read_nonblocking(4096, timeout=0.1)
                last = time.monotonic()
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                break

    def press(self, raw: bytes) -> None:
        self._child.send(raw)
        self.settle()

    @property
    def text(self) -> str:
        return "\n".join(line.rstrip() for line in self._screen.display)

    @property
    def prompt_row(self) -> str:
        rows = [line.rstrip() for line in self._screen.display if _PROMPT_MARKER in line]
        assert rows, f"no {_PROMPT_MARKER} prompt row on screen:\n{self.text}"
        return rows[-1]

    @property
    def cursor(self) -> tuple[int, int]:
        return (self._screen.cursor.y, self._screen.cursor.x)


@pytest.fixture
def prompt(mock_llm_server_url: str, tmp_path: Path) -> Iterator[_PromptScreen]:
    child = pexpect.spawn(
        _omnigent_cli(),
        ["run", str(_ASK_DEMO_DIR)],
        env=_prompt_env(tmp_path / "home", mock_llm_server_url),
        encoding=None,
        dimensions=(_ROWS, _COLS),
        timeout=_LAUNCH_TIMEOUT,
    )
    screen = _PromptScreen(child)
    try:
        child.expect(rb"ask.demo", timeout=_LAUNCH_TIMEOUT)
        # The banner precedes the input loop; ``· ready`` in the toolbar means
        # prompt_toolkit is live and the next send lands in the prompt.
        child.expect(rb"\xc2\xb7\s*ready", timeout=_LAUNCH_TIMEOUT)
        screen.settle(limit=3.0)
        screen.press(_TEXT.encode())
        assert screen.prompt_row.endswith(_TEXT), (
            f"control failed: typed text did not reach the prompt:\n{screen.text}"
        )
        yield screen
    finally:
        with contextlib.suppress(Exception):
            child.send(b"\x03")
            time.sleep(0.3)
            child.send(b"\x03")
            time.sleep(0.5)
        with contextlib.suppress(Exception):
            child.close(force=True)


def _leak_message(key: str, leaked: str, expected: str, screen: _PromptScreen) -> str:
    return (
        f"{key} under the Kitty keyboard protocol was inserted as literal text "
        f"{leaked!r} instead of {expected}; prompt row now: {screen.prompt_row!r}"
    )


def test_shift_enter_csi_u_inserts_newline_control(prompt: _PromptScreen) -> None:
    """Control: the registered Shift+Enter CSI-u sequence still inserts a newline."""
    y0, _ = prompt.cursor
    prompt.press(_CSI_U_SHIFT_ENTER)
    assert "[13;2u" not in prompt.text, prompt.prompt_row
    assert prompt.cursor[0] == y0 + 1, f"Shift+Enter did not insert a newline:\n{prompt.text}"


def test_ctrl_j_csi_u_inserts_newline(prompt: _PromptScreen) -> None:
    y0, _ = prompt.cursor
    prompt.press(_CSI_U_CTRL_J)
    assert "[106;5u" not in prompt.text, _leak_message("Ctrl+J", "[106;5u", "a newline", prompt)
    assert prompt.cursor[0] == y0 + 1, f"Ctrl+J did not insert a newline:\n{prompt.text}"


def test_alt_b_csi_u_moves_back_a_word(prompt: _PromptScreen) -> None:
    y0, x0 = prompt.cursor
    prompt.press(_CSI_U_ALT_B)
    assert "[98;3u" not in prompt.text, _leak_message("Alt+B", "[98;3u", "backward-word", prompt)
    assert prompt.cursor == (y0, x0 - len("world")), (
        f"Alt+B did not move the cursor to the start of 'world':\n{prompt.text}"
    )


def test_alt_f_csi_u_moves_forward_a_word(prompt: _PromptScreen) -> None:
    y0, x0 = prompt.cursor
    home_x = x0 - len(_TEXT)
    prompt.press(_CSI_U_CTRL_A)
    assert prompt.cursor == (y0, home_x), (
        f"control failed: registered Ctrl+A CSI-u did not move to line start:\n{prompt.text}"
    )
    prompt.press(_CSI_U_ALT_F)
    assert "[102;3u" not in prompt.text, _leak_message("Alt+F", "[102;3u", "forward-word", prompt)
    assert prompt.cursor == (y0, home_x + len("hello")), (
        f"Alt+F did not move the cursor to the end of 'hello':\n{prompt.text}"
    )

"""Unit tests for the raw-terminal paths of ``omnigent.repl._theme_picker``.

``termios``/``tty``/``select`` and the ``os`` byte I/O are replaced with an
in-memory fake terminal, so the OSC 11 probe and the interactive picker loop run
without a real TTY (and on any platform).
"""

from __future__ import annotations

import io
import sys
from collections import deque
from types import ModuleType, SimpleNamespace

import pytest
from omnigent_ui_sdk.terminal._theme import DARK_THEME, LIGHT_THEME

from omnigent.repl import _theme_picker
from omnigent.repl._theme_picker import (
    _clear_picker,
    _detect_terminal_background,
    _term_width,
    startup_theme_picker,
)

_FD = 7


class _TermiosError(Exception):
    pass


class _FakeTerminal:
    """Scripted terminal: serves *keys* to ``os.read`` and records side effects."""

    def __init__(
        self,
        keys: bytes = b"",
        *,
        tcgetattr_fails: bool = False,
        write_fails: bool = False,
    ) -> None:
        self.pending = deque(keys)
        self.tcgetattr_fails = tcgetattr_fails
        self.write_fails = write_fails
        self.written: list[bytes] = []
        self.restored: list[object] = []
        self.modes: list[str] = []
        self.saved_theme: list[str] = []

    # os
    def read(self, fd: int, n: int) -> bytes:
        assert fd == _FD
        return bytes(self.pending.popleft() for _ in range(min(n, len(self.pending))))

    def write(self, fd: int, data: bytes) -> int:
        if self.write_fails:
            raise OSError("write failed")
        self.written.append(data)
        return len(data)

    def get_terminal_size(self) -> SimpleNamespace:
        raise OSError("not a tty")

    # termios / tty / select
    def tcgetattr(self, fd: int) -> list[str]:
        if self.tcgetattr_fails:
            raise _TermiosError("not a terminal")
        return ["saved-attrs"]

    def tcsetattr(self, fd: int, when: int, attrs: object) -> None:
        self.restored.append(attrs)

    def select(
        self, rlist: list[int], wlist: list[int], xlist: list[int], timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        return (rlist if self.pending else [], [], [])


@pytest.fixture()
def term(monkeypatch: pytest.MonkeyPatch) -> _FakeTerminal:
    """Install a fake TTY with no scripted input; tests set ``term.pending``."""
    fake = _FakeTerminal()

    termios = ModuleType("termios")
    termios.error = _TermiosError  # type: ignore[attr-defined]
    termios.TCSADRAIN = 1  # type: ignore[attr-defined]
    termios.tcgetattr = fake.tcgetattr  # type: ignore[attr-defined]
    termios.tcsetattr = fake.tcsetattr  # type: ignore[attr-defined]
    tty = ModuleType("tty")
    tty.setraw = lambda fd: fake.modes.append("raw")  # type: ignore[attr-defined]
    tty.setcbreak = lambda fd: fake.modes.append("cbreak")  # type: ignore[attr-defined]
    select = ModuleType("select")
    select.select = fake.select  # type: ignore[attr-defined]
    for name, module in (("termios", termios), ("tty", tty), ("select", select)):
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setattr(
        _theme_picker,
        "os",
        SimpleNamespace(
            read=fake.read, write=fake.write, get_terminal_size=fake.get_terminal_size
        ),
    )
    # Patch the module's ``sys`` rather than the global one: pytest's capture
    # reinstalls ``sys.stdout`` when the test body starts.
    monkeypatch.setattr(
        _theme_picker,
        "sys",
        SimpleNamespace(
            stdin=SimpleNamespace(isatty=lambda: True, fileno=lambda: _FD),
            stdout=SimpleNamespace(isatty=lambda: True, fileno=lambda: 8),
        ),
    )
    monkeypatch.setattr(
        _theme_picker,
        "update_user_config",
        lambda *, theme: fake.saved_theme.append(theme),
    )
    return fake


# ── OSC 11 probe ─────────────────────────────────────────────


def test_detect_background_returns_none_when_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _theme_picker,
        "sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: False)),
    )

    assert _detect_terminal_background() is None


def test_detect_background_returns_none_without_termios(
    term: _FakeTerminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "termios", None)

    assert _detect_terminal_background() is None
    assert term.written == []


def test_detect_background_returns_none_when_attrs_unreadable(term: _FakeTerminal) -> None:
    term.tcgetattr_fails = True

    assert _detect_terminal_background() is None
    assert term.written == []


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (b"\x1b]11;rgb:0000/0000/0000\x1b\\", "dark"),
        (b"\x1b]11;rgb:ffff/ffff/ffff\x07", "light"),
    ],
)
def test_detect_background_classifies_terminal_reply(
    term: _FakeTerminal, reply: bytes, expected: str
) -> None:
    term.pending.extend(reply)

    assert _detect_terminal_background() == expected
    assert term.written == [b"\x1b]11;?\x1b\\"]
    assert term.modes == ["raw"]
    # Terminal attributes are always handed back, whatever the outcome.
    assert term.restored == [["saved-attrs"]]


def test_detect_background_returns_none_when_terminal_stays_silent(
    term: _FakeTerminal,
) -> None:
    assert _detect_terminal_background() is None
    assert term.restored == [["saved-attrs"]]


def test_detect_background_stops_reading_on_eof(term: _FakeTerminal) -> None:
    term.pending.extend(b"\x1b]11;rgb:0000/0000")  # never terminated, then EOF

    # Truncated reply has no complete rgb triple.
    assert _detect_terminal_background() is None
    assert term.restored == [["saved-attrs"]]


def test_detect_background_survives_write_failure(term: _FakeTerminal) -> None:
    term.write_fails = True

    assert _detect_terminal_background() is None
    assert term.restored == [["saved-attrs"]]


# ── interactive picker ───────────────────────────────────────


@pytest.mark.parametrize(
    ("detected", "keys", "expected"),
    [
        # Enter / Ctrl-C / bare Esc / EOF all accept the pre-selected theme.
        ("dark", b"\r", DARK_THEME),
        ("dark", b"\n", DARK_THEME),
        ("light", b"\x03", LIGHT_THEME),
        ("dark", b"\x1b", DARK_THEME),
        ("light", b"", LIGHT_THEME),
        # Arrow keys (normal and application cursor mode) and vi keys move.
        ("dark", b"\x1b[B\r", LIGHT_THEME),
        ("dark", b"\x1bOB\r", LIGHT_THEME),
        ("light", b"\x1b[A\r", DARK_THEME),
        ("light", b"\x1bOA\r", DARK_THEME),
        ("dark", b"j\r", LIGHT_THEME),
        ("dark", b"J\r", LIGHT_THEME),
        ("light", b"k\r", DARK_THEME),
        ("light", b"K\r", DARK_THEME),
        # Selection wraps around at both ends.
        ("dark", b"k\r", LIGHT_THEME),
        ("light", b"j\r", DARK_THEME),
        # Unknown escape sequences and stray keys are ignored.
        ("dark", b"\x1b[C\rx", DARK_THEME),
        ("dark", b"x\r", DARK_THEME),
    ],
)
def test_startup_picker_key_handling(
    term: _FakeTerminal,
    monkeypatch: pytest.MonkeyPatch,
    detected: str,
    keys: bytes,
    expected: object,
) -> None:
    monkeypatch.setattr(_theme_picker, "_detect_terminal_background", lambda: detected)
    term.pending.extend(keys)

    result = startup_theme_picker(out=io.StringIO())

    assert result is expected
    assert term.saved_theme == [expected.name]  # type: ignore[attr-defined]
    assert term.modes == ["cbreak"]
    assert term.restored == [["saved-attrs"]]


def test_startup_picker_redraws_in_place_and_clears_on_exit(
    term: _FakeTerminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_theme_picker, "_detect_terminal_background", lambda: "dark")
    term.pending.extend(b"j\r")
    out = io.StringIO()

    startup_theme_picker(out=out)

    drawn = out.getvalue()
    # First frame draws over a clean line; the second moves up over the first.
    assert drawn.count("\x1b[J") == 3  # initial, redraw after 'j', final clear
    assert "\x1b[" in drawn and "A\x1b[J" in drawn


def test_startup_picker_falls_back_when_raw_mode_unavailable(
    term: _FakeTerminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_theme_picker, "_detect_terminal_background", lambda: "dark")
    term.tcgetattr_fails = True

    result = startup_theme_picker(out=io.StringIO())

    assert result is DARK_THEME
    assert term.saved_theme == ["dark"]
    assert term.modes == []


# ── small helpers ────────────────────────────────────────────


def test_clear_picker_is_a_noop_for_zero_lines() -> None:
    out = io.StringIO()

    _clear_picker(out, 0)
    _clear_picker(out, -3)

    assert out.getvalue() == ""


def test_clear_picker_moves_up_and_erases() -> None:
    out = io.StringIO()

    _clear_picker(out, 5)

    assert out.getvalue() == "\x1b[5A\x1b[J"


def test_term_width_clamps_to_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _theme_picker,
        "os",
        SimpleNamespace(get_terminal_size=lambda: SimpleNamespace(columns=10)),
    )

    assert _term_width() == 40


def test_term_width_falls_back_when_size_unavailable(term: _FakeTerminal) -> None:
    assert _term_width() == 80

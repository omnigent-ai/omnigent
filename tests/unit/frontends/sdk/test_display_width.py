"""Unit tests for ``_display_width``, ``_term_width``, and ``_term_height``.

Exercises CJK/emoji width handling via ``wcswidth``, the ``len()``
fallback for control characters, and the ``OSError``/``ValueError``
fallback paths for ``os.get_terminal_size``.
"""

from __future__ import annotations

import os

import pytest
from omnigent_ui_sdk.terminal._host import (
    _display_width,
    _term_height,
    _term_width,
)

# ── _display_width ──────────────────────────────────────────────


def test_display_width_ascii() -> None:
    """Pure ASCII → width equals character count."""
    assert _display_width("hello") == 5, (
        "ASCII characters are each 1 column wide; 'hello' should be 5."
    )


def test_display_width_cjk() -> None:
    """CJK ideographs are double-width → 2 columns each."""
    # "你好" = 2 characters, each occupying 2 columns = 4 total.
    assert _display_width("你好") == 4, (
        "CJK characters are double-width; '你好' (2 chars) should be 4 columns."
    )


def test_display_width_mixed() -> None:
    """Mixed ASCII + CJK → ASCII at 1 col + CJK at 2 col each."""
    # "Hi" = 2 columns, "你好" = 4 columns → 6 total.
    assert _display_width("Hi你好") == 6, (
        "'Hi' (2 cols) + '你好' (4 cols) should be 6 columns total."
    )


def test_display_width_emoji() -> None:
    """Emoji are typically double-width."""
    # "👋" is a single emoji, rendered as 2 columns by wcwidth.
    result = _display_width("👋")
    assert result == 2, (
        f"Emoji '👋' should be 2 columns wide, got {result}. "
        f"If 1, wcwidth may not recognize this codepoint as wide."
    )


def test_display_width_empty() -> None:
    """Empty string → 0 columns."""
    assert _display_width("") == 0, "Empty string should have 0 display width."


def test_display_width_control_chars_fallback() -> None:
    """Control characters cause ``wcswidth`` to return -1 → fallback to ``len()``."""
    # wcswidth returns -1 for strings containing control chars
    # (C0 controls like \x01, \x02). The fallback is len().
    text = "\x01\x02"
    result = _display_width(text)
    # Fallback: len("\x01\x02") == 2.
    assert result == 2, (
        f"Expected fallback to len() == 2 for control characters, got {result}. "
        f"If -1, the wcswidth return was not caught by the fallback."
    )


# ── _term_width fallbacks ──────────────────────────────────────


def test_term_width_fallback_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.get_terminal_size`` raises ``OSError`` → fallback to 80."""
    monkeypatch.setattr(os, "get_terminal_size", _raise_os_error)
    assert _term_width() == 80, (
        "Expected 80 as fallback width when os.get_terminal_size raises OSError."
    )


def test_term_height_fallback_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.get_terminal_size`` raises ``OSError`` → fallback to 24."""
    monkeypatch.setattr(os, "get_terminal_size", _raise_os_error)
    assert _term_height() == 24, (
        "Expected 24 as fallback height when os.get_terminal_size raises OSError."
    )


def test_term_width_fallback_on_valueerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.get_terminal_size`` raises ``ValueError`` → fallback to 80."""
    monkeypatch.setattr(os, "get_terminal_size", _raise_value_error)
    assert _term_width() == 80, (
        "Expected 80 as fallback width when os.get_terminal_size raises ValueError."
    )


def test_term_height_fallback_on_valueerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.get_terminal_size`` raises ``ValueError`` → fallback to 24."""
    monkeypatch.setattr(os, "get_terminal_size", _raise_value_error)
    assert _term_height() == 24, (
        "Expected 24 as fallback height when os.get_terminal_size raises ValueError."
    )


def test_term_width_reads_real_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``os.get_terminal_size`` works, ``_term_width`` returns its columns."""
    monkeypatch.setattr(os, "get_terminal_size", lambda *_a, **_kw: os.terminal_size((120, 40)))
    assert _term_width() == 120, (
        "Expected _term_width to return 120 from a faked terminal_size(120, 40)."
    )


def test_term_height_reads_real_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``os.get_terminal_size`` works, ``_term_height`` returns its lines."""
    monkeypatch.setattr(os, "get_terminal_size", lambda *_a, **_kw: os.terminal_size((120, 40)))
    assert _term_height() == 40, (
        "Expected _term_height to return 40 from a faked terminal_size(120, 40)."
    )


# ── helpers ─────────────────────────────────────────────────────


def _raise_os_error(*_args: object, **_kwargs: object) -> os.terminal_size:
    raise OSError("not a terminal")


def _raise_value_error(*_args: object, **_kwargs: object) -> os.terminal_size:
    raise ValueError("bad fd")

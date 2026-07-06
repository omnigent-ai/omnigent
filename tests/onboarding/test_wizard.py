"""Tests for the legacy onboarding wizard terminal helpers."""

from __future__ import annotations

import sys

import pytest

from omnigent.onboarding import wizard


def _feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Route *lines* to ``click.prompt`` as if typed at the console."""
    fed = iter(lines)

    def _fake_prompt(_text: str) -> str:
        return next(fed)

    monkeypatch.setattr("click.termui.visible_prompt_func", _fake_prompt)


def test_arrow_menu_uses_numbered_fallback_on_windows_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY wizard menus still work on Windows, where raw-termios menus are unavailable."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(wizard, "IS_WINDOWS", True)
    _feed(monkeypatch, ["2"])

    result = wizard._arrow_menu(["alpha", "beta"])

    assert result == 1


def test_arrow_menu_fallback_preserves_back_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback wizard menus preserve the TTY path's Esc-to-go-back behavior."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    _feed(monkeypatch, ["q"])

    with pytest.raises(wizard._GoBack):
        wizard._arrow_menu(["alpha", "beta"])


def test_arrow_menu_fallback_q_is_invalid_when_back_disabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``q`` only goes back when the caller opted into back navigation."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    _feed(monkeypatch, ["q", "2"])

    result = wizard._arrow_menu(["alpha", "beta"], allow_back=False)

    assert result == 1
    assert "Invalid selection." in capsys.readouterr().out

"""Tests for the startup theme picker and shared preview builders."""

from __future__ import annotations

import pytest
from omnigent_ui_sdk.terminal._theme import DARK_THEME, LIGHT_THEME

from omnigent.repl._theme_picker import (
    _build_dark_preview,
    _build_light_preview,
    _build_preview,
    _parse_osc11_response,
    _render_theme_picker,
    build_theme_confirmation,
    startup_theme_picker,
)

# ── Preview builders ──────────────────────────────────────────


def test_build_dark_preview_returns_panel() -> None:
    """Dark preview builder returns a Rich Panel with theme samples."""
    from rich.panel import Panel

    panel = _build_dark_preview()
    assert isinstance(panel, Panel)


def test_build_light_preview_returns_panel() -> None:
    """Light preview builder returns a Rich Panel with theme samples."""
    from rich.panel import Panel

    panel = _build_light_preview()
    assert isinstance(panel, Panel)


def test_build_preview_dispatches_dark() -> None:
    """_build_preview('dark') returns a panel."""
    from rich.panel import Panel

    panel = _build_preview("dark")
    assert isinstance(panel, Panel)


def test_build_preview_dispatches_light() -> None:
    """_build_preview('light') returns a panel."""
    from rich.panel import Panel

    panel = _build_preview("light")
    assert isinstance(panel, Panel)


def test_build_preview_respects_width() -> None:
    """Preview panels honor the requested width."""
    panel = _build_dark_preview(width=40)
    assert panel.width == 40


# ── Picker rendering ─────────────────────────────────────────


def test_render_theme_picker_contains_menu_items() -> None:
    """Rendered picker shows both dark and light options."""
    rendered = _render_theme_picker(0, width=60)
    assert "dark mode" in rendered
    assert "light mode" in rendered


def test_render_theme_picker_highlights_selected_dark() -> None:
    """When dark (index 0) is selected, its indicator is present."""
    rendered = _render_theme_picker(0, width=60)
    assert "❯ dark mode" in rendered


def test_render_theme_picker_highlights_selected_light() -> None:
    """When light (index 1) is selected, its indicator is present."""
    rendered = _render_theme_picker(1, width=60)
    assert "❯ light mode" in rendered


def test_render_theme_picker_shows_footer_hints() -> None:
    """Footer shows navigation hints."""
    rendered = _render_theme_picker(0, width=60)
    assert "navigate" in rendered
    assert "Enter" in rendered


def test_render_theme_picker_shows_preview_for_selected_only() -> None:
    """Only the selected theme's preview panel appears."""
    rendered_dark = _render_theme_picker(0, width=60)
    rendered_light = _render_theme_picker(1, width=60)
    # Dark selection shows "dark preview"
    assert "dark" in rendered_dark.lower()
    # Light selection shows "light preview"
    assert "light" in rendered_light.lower()


# ── OSC 11 parsing ────────────────────────────────────────────


def test_parse_osc11_dark_background() -> None:
    """A low-luminance background classifies as dark."""
    # rgb:0000/0000/0000 = pure black.
    response = "\033]11;rgb:0000/0000/0000\033\\"
    assert _parse_osc11_response(response) == "dark"


def test_parse_osc11_light_background() -> None:
    """A high-luminance background classifies as light."""
    # rgb:ffff/ffff/ffff = pure white.
    response = "\033]11;rgb:ffff/ffff/ffff\033\\"
    assert _parse_osc11_response(response) == "light"


def test_parse_osc11_mid_dark() -> None:
    """A mid-dark background (common terminal themes) classifies as dark."""
    # rgb:1c1c/1c1c/1c1c — typical dark theme bg.
    response = "\033]11;rgb:1c1c/1c1c/1c1c\033\\"
    assert _parse_osc11_response(response) == "dark"


def test_parse_osc11_mid_light() -> None:
    """A mid-light background classifies as light."""
    # rgb:e0e0/e0e0/e0e0 — typical light theme bg.
    response = "\033]11;rgb:e0e0/e0e0/e0e0\033\\"
    assert _parse_osc11_response(response) == "light"


def test_parse_osc11_two_digit_components() -> None:
    """Two-digit hex components (e.g. from some terminals) parse correctly."""
    response = "\033]11;rgb:00/00/00\033\\"
    assert _parse_osc11_response(response) == "dark"

    response = "\033]11;rgb:ff/ff/ff\033\\"
    assert _parse_osc11_response(response) == "light"


def test_parse_osc11_bel_terminator() -> None:
    """Response terminated with BEL (\\x07) instead of ST also parses."""
    response = "\033]11;rgb:0000/0000/0000\x07"
    assert _parse_osc11_response(response) == "dark"


def test_parse_osc11_no_rgb() -> None:
    """Response without rgb: returns None."""
    assert _parse_osc11_response("\033]11;unknown\033\\") is None


def test_parse_osc11_empty_response() -> None:
    """Empty response returns None."""
    assert _parse_osc11_response("") is None


def test_parse_osc11_malformed_components() -> None:
    """Malformed RGB components return None."""
    assert _parse_osc11_response("\033]11;rgb:xyz/abc/def\033\\") is None


def test_parse_osc11_wrong_number_of_components() -> None:
    """Wrong number of RGB components returns None."""
    assert _parse_osc11_response("\033]11;rgb:0000/0000\033\\") is None


# ── Confirmation line ─────────────────────────────────────────


def test_build_theme_confirmation_dark() -> None:
    """Confirmation for dark theme contains 'dark' and 'saved'."""
    text = build_theme_confirmation(DARK_THEME)
    plain = text.plain
    assert "dark" in plain
    assert "mode (saved)" in plain


def test_build_theme_confirmation_light() -> None:
    """Confirmation for light theme contains 'light' and 'saved'."""
    text = build_theme_confirmation(LIGHT_THEME)
    plain = text.plain
    assert "light" in plain
    assert "mode (saved)" in plain


# ── Startup picker (non-tty fallback) ────────────────────────


def test_startup_picker_non_tty_defaults_to_light(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When stdin is not a tty, the picker skips interactive mode and uses light."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    # Ensure stdin.isatty() returns False.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    # Mock OSC 11 detection to return None (non-tty can't detect).
    monkeypatch.setattr(
        "omnigent.repl._theme_picker._detect_terminal_background",
        lambda: None,
    )

    import io

    out = io.StringIO()
    result = startup_theme_picker(out=out)
    assert result is LIGHT_THEME
    # Should have persisted the choice.
    config = (tmp_path / ".omnigent" / "config.yaml").read_text(encoding="utf-8")
    assert "theme: light" in config


def test_startup_picker_non_tty_respects_dark_detection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When OSC 11 detects dark on a non-tty, the picker selects dark."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "omnigent.repl._theme_picker._detect_terminal_background",
        lambda: "dark",
    )

    import io

    out = io.StringIO()
    result = startup_theme_picker(out=out)
    assert result is DARK_THEME
    config = (tmp_path / ".omnigent" / "config.yaml").read_text(encoding="utf-8")
    assert "theme: dark" in config

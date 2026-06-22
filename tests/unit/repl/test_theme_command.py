"""Tests for the TUI ``/theme`` slash command and theme picker."""

from __future__ import annotations

import pytest
from omnigent_ui_sdk.terminal import RichBlockFormatter, TerminalHost
from omnigent_ui_sdk.terminal._theme import DARK_THEME, LIGHT_THEME

from omnigent.repl._repl import COMMANDS, _load_startup_theme, handle_slash_command


class DummyHost(TerminalHost):
    def __init__(self) -> None:
        super().__init__(model_name="test")
        self.outputs: list[object] = []

    def output(self, renderable, *, soft_wrap: bool = False) -> None:  # type: ignore[override]
        self.outputs.append(renderable)


class DummySession:
    pass


def _text(host: DummyHost) -> str:
    return "\n".join(str(item) for item in host.outputs)


def test_theme_command_registered() -> None:
    assert "/theme" in COMMANDS
    assert "theme" in COMMANDS["/theme"][0].lower()


def test_startup_theme_falls_back_to_light_for_corrupt_config(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    config_path = tmp_path / ".omnigent" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("tui:\n  theme: sepia\n", encoding="utf-8")

    assert _load_startup_theme() is LIGHT_THEME


def test_startup_theme_returns_persisted_dark(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When config already has a theme, return it without showing picker."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    config_path = tmp_path / ".omnigent" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("tui:\n  theme: dark\n", encoding="utf-8")

    assert _load_startup_theme() is DARK_THEME


def test_startup_theme_shows_picker_on_first_launch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no theme is persisted, _load_startup_theme calls the picker."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    # Mock the picker to return DARK_THEME without requiring a tty.
    monkeypatch.setattr(
        "omnigent.repl._theme_picker.startup_theme_picker",
        lambda **kwargs: DARK_THEME,
    )

    result = _load_startup_theme()
    assert result is DARK_THEME


@pytest.mark.asyncio
async def test_theme_no_args_shows_current_theme() -> None:
    """``/theme`` with no args shows the current theme and usage hint."""
    host = DummyHost()
    fmt = RichBlockFormatter()

    await handle_slash_command("/theme", DummySession(), None, host, fmt)  # type: ignore[arg-type]

    rendered = _text(host)
    assert "theme: light" in rendered
    assert "usage: /theme light" in rendered


@pytest.mark.asyncio
async def test_theme_light_updates_host_and_formatter(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    host = DummyHost()
    fmt = RichBlockFormatter()

    await handle_slash_command("/theme light", DummySession(), None, host, fmt)  # type: ignore[arg-type]

    assert host.theme is LIGHT_THEME
    assert fmt.theme is LIGHT_THEME
    assert fmt.code_theme == LIGHT_THEME.code_theme
    assert fmt.muted == LIGHT_THEME.muted
    assert host._style.get_attrs_for_style_str("class:bottom-toolbar").bgcolor == ""
    rendered = _text(host)
    assert "light" in rendered
    assert "mode (saved)" in rendered
    assert (tmp_path / ".omnigent" / "config.yaml").read_text(encoding="utf-8") == (
        "# Omnigent user configuration\ntui:\n  theme: light\n"
    )


@pytest.mark.asyncio
async def test_theme_dark_and_default_reset_to_default_theme(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    host = DummyHost()
    fmt = RichBlockFormatter(theme=LIGHT_THEME)
    host.set_theme(LIGHT_THEME)

    await handle_slash_command("/theme dark", DummySession(), None, host, fmt)  # type: ignore[arg-type]

    await handle_slash_command("/theme default", DummySession(), None, host, fmt)  # type: ignore[arg-type]

    assert host.theme is LIGHT_THEME
    assert fmt.theme is LIGHT_THEME
    assert fmt.code_theme == LIGHT_THEME.code_theme
    rendered = _text(host)
    assert "light" in rendered
    assert "mode (saved)" in rendered
    assert (tmp_path / ".omnigent" / "config.yaml").read_text(encoding="utf-8") == (
        "# Omnigent user configuration\n"
    )


@pytest.mark.asyncio
async def test_theme_rejects_unknown_value() -> None:
    host = DummyHost()
    fmt = RichBlockFormatter()

    await handle_slash_command("/theme sepia", DummySession(), None, host, fmt)  # type: ignore[arg-type]

    assert host.theme is LIGHT_THEME
    assert fmt.theme is LIGHT_THEME
    assert "Invalid theme" in _text(host)

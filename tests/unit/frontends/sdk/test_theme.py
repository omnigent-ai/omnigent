"""Terminal light/dark theme plumbing tests."""

from __future__ import annotations

from omnigent_ui_sdk.terminal import RichBlockFormatter, TerminalHost
from omnigent_ui_sdk.terminal._theme import LIGHT_THEME, get_theme


def test_formatter_defaults_to_light_markdown_theme() -> None:
    fmt = RichBlockFormatter()

    assert fmt.theme is LIGHT_THEME
    assert fmt.code_theme == "default"
    assert fmt.muted == LIGHT_THEME.muted


def test_formatter_light_theme_changes_markdown_and_palette() -> None:
    fmt = RichBlockFormatter(theme="light")

    assert fmt.theme is LIGHT_THEME
    assert fmt.code_theme == "default"
    assert fmt.muted == LIGHT_THEME.muted
    assert fmt.error == LIGHT_THEME.error


def test_formatter_set_theme_updates_future_markdown_theme() -> None:
    fmt = RichBlockFormatter(theme="dark")

    fmt.set_theme("light")

    assert fmt.theme is LIGHT_THEME
    assert fmt.code_theme == LIGHT_THEME.code_theme
    assert fmt.success == LIGHT_THEME.success


def test_host_status_bar_style_uses_selected_theme_background() -> None:
    dark = TerminalHost(model_name="test", theme="dark")
    light = TerminalHost(model_name="test", theme="light")

    assert dark._style.get_attrs_for_style_str("class:bottom-toolbar").bgcolor == "2a2a2a"
    assert light._style.get_attrs_for_style_str("class:bottom-toolbar").bgcolor == ""
    assert light._style.get_attrs_for_style_str("class:model-name").color == "4b5563"


def test_host_set_theme_updates_prompt_style() -> None:
    host = TerminalHost(model_name="test", theme="dark")

    host.set_theme(LIGHT_THEME)

    assert host.theme is LIGHT_THEME
    assert host._prompt.style is host._style
    assert host._style.get_attrs_for_style_str("class:bottom-toolbar").bgcolor == ""


def test_unknown_theme_is_rejected() -> None:
    try:
        get_theme("sepia")
    except ValueError as exc:
        assert "expected dark or light" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("get_theme accepted unknown theme")


def test_light_theme_overrides_rich_markdown_inline_code_background() -> None:
    style = LIGHT_THEME.rich_theme.styles["markdown.code"]

    assert style.bgcolor is not None
    assert style.bgcolor.name != "black"


def test_host_uses_selected_rich_theme_for_markdown_rendering() -> None:
    dark = TerminalHost(model_name="test", theme="dark")
    light = TerminalHost(model_name="test", theme="light")

    assert dark._console.get_style("markdown.code").bgcolor.name == "black"
    assert light._console.get_style("markdown.code").bgcolor.name == "bright_white"

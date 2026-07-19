"""E2E: the Settings → Appearance color-palette picker skins the app and persists.

Alongside the light/dark **mode** tiles, ``AppearanceSection``
(``pages/SettingsPage.tsx``) renders a "Color theme" dropdown (a shadcn
``Select``) — one option per palette (Omnigent, Dracula, GitHub, Catppuccin,
Gruvbox, Nord). Choosing one calls ``applyThemePalette`` (``lib/themePalette.ts``),
which sets ``data-theme`` on ``<html>`` and persists the id to
``localStorage["omnigent:ui-theme-palette"]``. The default "Omnigent" palette
carries no override, so choosing it removes the attribute and clears the key.

The palette axis is orthogonal to the light/dark class next-themes toggles, so
``data-theme`` and the ``dark`` class coexist on ``<html>``. On reload the saved
palette is re-applied before first paint (``main.tsx``), so the skin survives a
refresh with no flash.

No LLM turn is involved.
"""

from __future__ import annotations

import json

from playwright.sync_api import Locator, Page, expect


def _data_theme(page: Page) -> str | None:
    """The palette applied to ``<html>`` via ``data-theme``, or None when unset."""
    return page.evaluate("() => document.documentElement.getAttribute('data-theme')")


def _stored_palette(page: Page) -> str | None:
    """The persisted palette preference (raw JSON), or None when unset (default)."""
    return page.evaluate("() => window.localStorage.getItem('omnigent:ui-theme-palette')")


def _stored_custom_theme(page: Page) -> dict[str, object] | None:
    """The persisted custom-theme configuration, decoded from localStorage."""
    raw = page.evaluate("() => window.localStorage.getItem('omnigent:custom-theme')")
    return json.loads(raw) if raw else None


def _html_has_dark(page: Page) -> bool:
    """True when the ``dark`` mode class is applied to ``<html>`` (next-themes)."""
    return page.evaluate("() => document.documentElement.classList.contains('dark')")


def _theme_radiogroup(page: Page) -> Locator:
    """The appearance-mode radiogroup ("Mode"). Matched exactly so it can't also
    resolve the "Color theme" / "Terminal theme" radiogroups, whose cards reuse
    the Light/Dark labels."""
    return page.get_by_role("radiogroup", name="Mode", exact=True)


def _color_theme_select(page: Page) -> Locator:
    """The color-theme dropdown trigger."""
    return page.get_by_test_id("color-theme-select")


def _pick_palette(page: Page, name: str) -> None:
    """Open the color-theme dropdown and choose the option with the given name."""
    _color_theme_select(page).click()
    page.get_by_role("option", name=name).click()


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to the Settings Appearance section, wait for the color-theme dropdown."""
    page.goto(f"{base_url}/settings/appearance")
    expect(_color_theme_select(page)).to_be_visible(timeout=30_000)


def test_color_palette_applies_persists_and_resets(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Selecting a palette skins ``<html>`` + persists; the default clears it.

    Fresh load is the default "Omnigent" (its name shown, nothing stored, no
    ``data-theme``). Picking GitHub sets ``data-theme="github"`` and persists it —
    and survives a reload (re-applied at boot). Returning to Omnigent removes the
    attribute and clears the stored key.
    """
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    # Fresh context → default "Omnigent": the trigger shows it, no override, and
    # nothing persisted.
    expect(_color_theme_select(page)).to_contain_text("Omnigent")
    assert _data_theme(page) is None, "expected no data-theme override on a fresh load"
    assert _stored_palette(page) is None, "expected no persisted palette on a fresh load"

    # → GitHub: the data-theme attribute lands on <html> and the choice persists.
    _pick_palette(page, "GitHub")
    expect(_color_theme_select(page)).to_contain_text("GitHub")
    assert _data_theme(page) == "github", "data-theme=github not set after selecting GitHub"
    assert _stored_palette(page) == '"github"'

    # Reload: the saved palette is re-applied before first paint (main.tsx), so
    # <html> still carries data-theme=github and the trigger stays on GitHub.
    page.reload()
    expect(_color_theme_select(page)).to_be_visible(timeout=30_000)
    assert _data_theme(page) == "github", "saved palette not re-applied after reload"
    expect(_color_theme_select(page)).to_contain_text("GitHub")

    # → back to Omnigent (the default): the override is removed and the stored
    # key cleared, since the default reverts to the base brand tokens.
    _pick_palette(page, "Omnigent")
    expect(_color_theme_select(page)).to_contain_text("Omnigent")
    assert _data_theme(page) is None, "<html> kept data-theme after returning to Omnigent"
    assert _stored_palette(page) is None, "the palette key was not cleared for the default"


def test_color_palette_composes_with_dark_mode(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The palette (``data-theme``) and light/dark mode (``dark`` class) coexist.

    They are independent axes, so a palette + Dark mode leaves <html> carrying
    both the ``data-theme`` attribute and the ``dark`` class at once.
    """
    # Pin a light OS so Dark is an explicit, observable change.
    page.emulate_media(color_scheme="light")

    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    # Pick a palette (dropdown), then Dark mode (radiogroup) — independent axes.
    _pick_palette(page, "Catppuccin")

    dark = _theme_radiogroup(page).get_by_role("radio", name="Dark")
    dark.click()
    expect(dark).to_have_attribute("aria-checked", "true")

    # Both axes are live on <html> simultaneously.
    assert _data_theme(page) == "catppuccin", "palette override lost when switching to Dark"
    assert _html_has_dark(page), "dark class missing — the palette should compose with dark mode"


def test_guided_custom_theme_applies_to_both_modes_and_persists(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Editing a preset creates one custom configuration with light/dark variants."""
    page.emulate_media(color_scheme="light")
    base_url, session_id = seeded_session
    _open_appearance(page, base_url)

    _pick_palette(page, "GitHub")
    page.get_by_test_id("custom-theme-accent-trigger").click()
    accent = page.get_by_test_id("custom-theme-accent-input")
    expect(accent).to_have_value("#0969DA")
    accent.fill("#2563eb")

    expect(_color_theme_select(page)).to_contain_text("Custom")
    assert _data_theme(page) == "custom"
    assert _stored_palette(page) == '"custom"'
    stored = _stored_custom_theme(page)
    assert stored is not None
    assert stored["basePalette"] == "github"
    assert stored["accent"] == "#2563eb"

    translucent_sidebar = page.get_by_test_id("custom-theme-translucent-sidebar")
    translucent_sidebar.click()
    expect(translucent_sidebar).to_have_attribute("aria-checked", "true")
    sidebar_background = page.locator(".conversations-sidebar").evaluate(
        "element => getComputedStyle(element).backgroundColor"
    )
    assert sidebar_background.startswith("rgba("), "visible sidebar did not become translucent"

    light_background = page.evaluate(
        "() => getComputedStyle(document.documentElement)"
        ".getPropertyValue('--custom-light-background').trim()"
    )
    dark_background = page.evaluate(
        "() => getComputedStyle(document.documentElement)"
        ".getPropertyValue('--custom-dark-background').trim()"
    )
    assert light_background and dark_background and light_background != dark_background

    dark = _theme_radiogroup(page).get_by_role("radio", name="Dark")
    dark.click()
    expect(dark).to_have_attribute("aria-checked", "true")
    assert _data_theme(page) == "custom", "custom palette was lost when switching modes"

    page.reload()
    expect(_color_theme_select(page)).to_contain_text("Custom")
    expect(page.get_by_test_id("custom-theme-accent-trigger")).to_contain_text("#2563EB")
    assert _data_theme(page) == "custom"

    page.goto(f"{base_url}/c/{session_id}")
    workspace = page.get_by_role("complementary", name="Workspace")
    expect(workspace).to_be_visible(timeout=30_000)
    for rail in [page.locator(".conversations-sidebar"), workspace]:
        background = rail.evaluate("element => getComputedStyle(element).backgroundColor")
        assert background.startswith("rgba("), "both sidebars should share translucency"

    workspace_surface = workspace.locator("[data-workspace-panel-content] > *")
    expect(workspace_surface).to_be_visible()
    surface_background = workspace_surface.evaluate(
        "element => getComputedStyle(element).backgroundColor"
    )
    assert surface_background == "rgba(0, 0, 0, 0)", (
        "workspace content should not cover the translucent rail"
    )


def test_custom_theme_colors_can_be_randomized(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Randomizing accent and tint updates the picker and persisted theme."""
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)
    page.evaluate("Math.random = () => 0.5")

    for test_id in ["custom-theme-accent", "custom-theme-tint"]:
        page.get_by_test_id(f"{test_id}-trigger").click()
        page.get_by_test_id(f"{test_id}-randomize").click()
        expect(page.get_by_test_id(f"{test_id}-input")).to_have_value("#3AD2D2")
        page.keyboard.press("Escape")

    expect(_color_theme_select(page)).to_contain_text("Custom")
    assert _data_theme(page) == "custom"
    assert _stored_palette(page) == '"custom"'
    stored = _stored_custom_theme(page)
    assert stored is not None
    assert stored["accent"] == "#3ad2d2"
    assert stored["tint"] == "#3ad2d2"

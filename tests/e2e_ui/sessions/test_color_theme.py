"""E2E: the Settings → Appearance color-palette picker skins the app and persists.

Alongside the light/dark **mode** cards, ``AppearanceSection``
(``pages/SettingsPage.tsx``) renders a second ``role="radiogroup"`` labelled
"Color palette" — one card per palette (Omnigent, Nord, Monokai, Solarized,
Dracula). Selecting one calls ``applyThemePalette`` (``lib/themePalette.ts``),
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

from playwright.sync_api import Locator, Page, expect


def _data_theme(page: Page) -> str | None:
    """The palette applied to ``<html>`` via ``data-theme``, or None when unset."""
    return page.evaluate("() => document.documentElement.getAttribute('data-theme')")


def _stored_palette(page: Page) -> str | None:
    """The persisted palette preference (raw JSON), or None when unset (default)."""
    return page.evaluate("() => window.localStorage.getItem('omnigent:ui-theme-palette')")


def _html_has_dark(page: Page) -> bool:
    """True when the ``dark`` mode class is applied to ``<html>`` (next-themes)."""
    return page.evaluate("() => document.documentElement.classList.contains('dark')")


def _theme_radiogroup(page: Page) -> Locator:
    """The app-theme (mode) radiogroup, matched exactly so it can't also resolve
    the "Terminal theme" radiogroup (its name contains "Theme" and its cards
    reuse the Light/Dark labels)."""
    return page.get_by_role("radiogroup", name="Theme", exact=True)


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to the Settings Appearance section and wait for the palette group."""
    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_role("radiogroup", name="Color palette")).to_be_visible(timeout=30_000)


def test_color_palette_applies_persists_and_resets(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Selecting a palette skins ``<html>`` + persists; the default clears it.

    Fresh load is the default "Omnigent" (its card checked, nothing stored, no
    ``data-theme``). Picking GitHub sets ``data-theme="github"`` and persists it —
    and survives a reload (re-applied at boot). Returning to Omnigent removes the
    attribute and clears the stored key.
    """
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    # Fresh context → default "Omnigent" palette: card checked, no override, no
    # persisted preference.
    expect(page.get_by_role("radio", name="Omnigent")).to_have_attribute("aria-checked", "true")
    assert _data_theme(page) is None, "expected no data-theme override on a fresh load"
    assert _stored_palette(page) is None, "expected no persisted palette on a fresh load"

    # → GitHub: the data-theme attribute lands on <html> and the choice persists.
    github = page.get_by_role("radio", name="GitHub")
    github.click()
    expect(github).to_have_attribute("aria-checked", "true")
    assert _data_theme(page) == "github", "data-theme=github not set after selecting GitHub"
    assert _stored_palette(page) == '"github"'

    # Reload: the saved palette is re-applied before first paint (main.tsx), so
    # <html> still carries data-theme=github and the card stays selected.
    page.reload()
    expect(page.get_by_role("radiogroup", name="Color palette")).to_be_visible(timeout=30_000)
    assert _data_theme(page) == "github", "saved palette not re-applied after reload"
    expect(page.get_by_role("radio", name="GitHub")).to_have_attribute("aria-checked", "true")

    # → back to Omnigent (the default): the override is removed and the stored
    # key cleared, since the default reverts to the base brand tokens.
    omnigent = page.get_by_role("radio", name="Omnigent")
    omnigent.click()
    expect(omnigent).to_have_attribute("aria-checked", "true")
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

    # Pick a palette, then Dark mode; the two controls are independent.
    catppuccin = page.get_by_role("radio", name="Catppuccin")
    catppuccin.click()
    expect(catppuccin).to_have_attribute("aria-checked", "true")

    dark = _theme_radiogroup(page).get_by_role("radio", name="Dark")
    dark.click()
    expect(dark).to_have_attribute("aria-checked", "true")

    # Both axes are live on <html> simultaneously.
    assert _data_theme(page) == "catppuccin", "palette override lost when switching to Dark"
    assert _html_has_dark(page), "dark class missing — the palette should compose with dark mode"

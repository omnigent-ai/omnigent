"""E2E: the Settings → Appearance theme picker sets the app theme and persists it.

The theme control lives on the Settings page (``pages/SettingsPage.tsx``,
``AppearanceSection``): three radio cards — System / Light / Dark — under a
``role="radiogroup"`` labelled "Theme". Each card sets the chosen mode directly
(``onClick={() => setTheme(mode)}``); ``aria-checked`` reflects the current
selection. Unlike the previous sidebar cycle-button, every mode is selectable
directly regardless of the OS preference (no skipped "redundant" step).

The provider (``components/theme/ThemeProvider.tsx``) is next-themes configured
with ``attribute="class"`` + ``storageKey="web-theme"`` +
``defaultTheme="system"``, so a selection toggles the ``dark`` class on
``<html>`` and writes the choice to ``localStorage["web-theme"]``.
``system`` resolves to the emulated ``prefers-color-scheme``; we pin it with
``emulate_media`` so the resolved appearance is deterministic on any runner.

No LLM turn is involved.
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect


def _html_has_dark(page: Page) -> bool:
    """True when the ``dark`` class is applied to ``<html>`` (next-themes)."""
    return page.evaluate("() => document.documentElement.classList.contains('dark')")


def _stored_theme(page: Page) -> str | None:
    """The persisted theme preference, or None when unset (default ``system``)."""
    return page.evaluate("() => window.localStorage.getItem('web-theme')")


def _theme_radiogroup(page: Page) -> Locator:
    """The app-theme radiogroup, matched exactly so it can't also resolve the
    Appearance page's "Terminal theme" radiogroup, whose name contains "Theme"
    and whose cards reuse the Light/Dark labels."""
    return page.get_by_role("radiogroup", name="Theme", exact=True)


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to the Settings Appearance section and wait for the cards."""
    page.goto(f"{base_url}/settings/appearance")
    expect(_theme_radiogroup(page)).to_be_visible(timeout=30_000)


def test_theme_toggle_cycles_and_persists(page: Page, seeded_session: tuple[str, str]) -> None:
    """On a light OS, selecting Dark then System flips the class and persists.

    Fresh load is the default ``system`` (System card checked, nothing stored).
    Picking Dark adds the ``dark`` class and persists ``"dark"``; picking System
    clears it again (system resolves to light) and persists ``"system"``.
    """
    # Pin the OS preference so ``system`` resolves deterministically regardless
    # of the CI runner's default scheme. next-themes reads this for systemTheme.
    page.emulate_media(color_scheme="light")

    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    # Fresh context → no stored preference → default "system" is selected, and
    # on a light OS that renders without the dark class.
    expect(_theme_radiogroup(page).get_by_role("radio", name="System")).to_have_attribute(
        "aria-checked", "true"
    )
    assert _stored_theme(page) is None, "expected no persisted theme on a fresh load"
    assert not _html_has_dark(page), "system on a light OS should not apply the dark class"

    # → Dark: the dark class lands and the choice persists.
    dark = _theme_radiogroup(page).get_by_role("radio", name="Dark")
    dark.click()
    expect(dark).to_have_attribute("aria-checked", "true")
    assert _html_has_dark(page), "<html> did not gain the dark class after selecting Dark"
    assert _stored_theme(page) == "dark"

    # → System: the dark class clears (system resolves to light) and "system"
    # persists.
    system = _theme_radiogroup(page).get_by_role("radio", name="System")
    system.click()
    expect(system).to_have_attribute("aria-checked", "true")
    assert not _html_has_dark(page), "<html> kept the dark class after returning to System"
    assert _stored_theme(page) == "system"


def test_theme_toggle_reaches_explicit_light_on_dark_os(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """On a dark OS, explicit Light clears the class and persists ``"light"``.

    Pins the explicit-light DOM state + persistence (the light-OS path can't
    distinguish light from system), and confirms System re-resolves to dark.
    """
    page.emulate_media(color_scheme="dark")

    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    # Fresh "system" on a dark OS renders dark; System is the checked card.
    expect(_theme_radiogroup(page).get_by_role("radio", name="System")).to_have_attribute(
        "aria-checked", "true"
    )
    assert _html_has_dark(page), "system on a dark OS should apply the dark class"
    assert _stored_theme(page) is None, "expected no persisted theme on a fresh load"

    # → Light: the dark class clears and "light" persists.
    light = _theme_radiogroup(page).get_by_role("radio", name="Light")
    light.click()
    expect(light).to_have_attribute("aria-checked", "true")
    assert not _html_has_dark(page), "<html> kept the dark class after selecting Light"
    assert _stored_theme(page) == "light"

    # → System: the dark class returns (system resolves to dark) and "system"
    # persists.
    system = _theme_radiogroup(page).get_by_role("radio", name="System")
    system.click()
    expect(system).to_have_attribute("aria-checked", "true")
    assert _html_has_dark(page), "<html> did not regain the dark class after returning to System"
    assert _stored_theme(page) == "system"

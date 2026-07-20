"""E2E: the Settings → Appearance terminal-theme picker themes a live terminal
independently of the app theme, and persists.

The terminal-theme control lives on the Settings page (``pages/SettingsPage.tsx``,
``TerminalThemeControl``): three radio cards — Match app / Light / Dark — under a
``role="radiogroup"`` labelled "Terminal theme". Picking a mode writes the choice
to ``localStorage["omnigent:terminal-theme"]`` (absent = "auto" = follow the app).

Unlike the chrome, the terminal is an xterm.js widget whose colors are a JS
``ITheme`` object, so the choice can't ride the ``.dark`` class the app theme
uses. ``TerminalView`` resolves the mode against the app's resolved appearance
(``resolveTerminalIsDark``) and reflects the result on the mounted terminal as
``data-terminal-theme`` ("light"/"dark") — the portable DOM signal here, since
xterm paints to a WebGL canvas (see ``shells/test_new_shell.py`` for why terminal
pixels aren't asserted). ``auto`` follows the app; ``light``/``dark`` pin the
terminal regardless of the app theme.

This is exactly the pair the feature exists for: a light terminal under a dark
app, and a dark terminal under a light app. The shell is user-launched (no LLM
turn) via the rail's "+ New shell" affordance, mirroring
``shells/test_new_shell.py``.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

TERMINAL_THEME_KEY = "omnigent:terminal-theme"
APP_THEME_KEY = "web-theme"


def _html_has_dark(page: Page) -> bool:
    """True when the ``dark`` class is applied to ``<html>`` (next-themes)."""
    return page.evaluate("() => document.documentElement.classList.contains('dark')")


def _stored_terminal_theme(page: Page) -> str | None:
    """The persisted terminal-theme mode, or None when unset (default "auto")."""
    return page.evaluate(f"() => window.localStorage.getItem('{TERMINAL_THEME_KEY}')")


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to the Settings Appearance section and wait for the control."""
    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_role("radiogroup", name="Terminal theme")).to_be_visible(timeout=30_000)


def _pick_app_theme(page: Page, mode: str) -> None:
    """Pin the app theme to an explicit mode via its Appearance radio card."""
    card = page.get_by_test_id(f"theme-{mode}")
    card.click()
    expect(card).to_have_attribute("aria-checked", "true")


def _pick_terminal_theme(page: Page, mode: str) -> None:
    """Pick a terminal-theme mode via its Appearance radio card."""
    card = page.get_by_test_id(f"terminal-theme-{mode}")
    card.click()
    expect(card).to_have_attribute("aria-checked", "true")


def _open_new_shell(page: Page) -> None:
    """Open the Shells tab and click "+ New shell" (mirrors test_new_shell)."""
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("Shells")).click()
    rail.get_by_role("button", name="New shell").click()


def _connected_main_terminal(page: Page):
    """Wait for a freshly opened shell's xterm to mount + connect in the main view."""
    main_terminal = page.get_by_test_id("main-terminal-view")
    expect(main_terminal).to_be_visible(timeout=60_000)
    terminal_view = main_terminal.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=20_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
    return terminal_view


def test_light_terminal_under_dark_app(page: Page, terminal_session: tuple[str, str]) -> None:
    """A "Light" terminal stays light while the app runs Dark.

    Pins the app to Dark and the terminal to Light in Settings, then launches a
    shell: the mounted terminal resolves to light (``data-terminal-theme=light``)
    even though ``<html>`` carries the ``dark`` class. This is the "light terminal
    with a dark theme" case.
    """
    base_url, session_id = terminal_session

    _open_appearance(page, base_url)
    _pick_app_theme(page, "dark")
    _pick_terminal_theme(page, "light")
    assert _html_has_dark(page), "app should be dark after picking the Dark theme card"
    assert _stored_terminal_theme(page) == "light", "terminal theme choice was not persisted"

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    terminal_view = _connected_main_terminal(page)

    # The terminal is light despite the dark app chrome — the two themes are
    # independent, which is the whole point of the feature.
    expect(terminal_view).to_have_attribute("data-terminal-theme", "light")
    assert _html_has_dark(page), "app theme must stay dark while the terminal is light"


def test_dark_terminal_under_light_app(page: Page, terminal_session: tuple[str, str]) -> None:
    """A "Dark" terminal stays dark while the app runs Light.

    The mirror of the light-on-dark case: pin the app to Light and the terminal to
    Dark, launch a shell, and confirm the terminal resolves to dark
    (``data-terminal-theme=dark``) with no ``dark`` class on ``<html>``.
    """
    base_url, session_id = terminal_session

    _open_appearance(page, base_url)
    _pick_app_theme(page, "light")
    _pick_terminal_theme(page, "dark")
    assert not _html_has_dark(page), "app should be light after picking the Light theme card"
    assert _stored_terminal_theme(page) == "dark", "terminal theme choice was not persisted"

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    terminal_view = _connected_main_terminal(page)

    expect(terminal_view).to_have_attribute("data-terminal-theme", "dark")
    assert not _html_has_dark(page), "app theme must stay light while the terminal is dark"


def test_terminal_theme_control_defaults_and_persists(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Match-app is the default; a pick persists and survives a reload.

    A fast, shell-free check of the control itself: fresh context selects "Match
    app" with nothing stored; picking Light persists "light" and re-selects after
    a full reload (so the choice a terminal reads at mount is durable).
    """
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    # Fresh context → "Match app" (auto) is the selected default, nothing stored.
    expect(page.get_by_test_id("terminal-theme-auto")).to_have_attribute("aria-checked", "true")
    assert _stored_terminal_theme(page) is None, "a fresh load should store no terminal theme"

    _pick_terminal_theme(page, "light")
    assert _stored_terminal_theme(page) == "light"

    page.reload()
    expect(page.get_by_role("radiogroup", name="Terminal theme")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("terminal-theme-light")).to_have_attribute("aria-checked", "true")
    assert _stored_terminal_theme(page) == "light", "the terminal theme did not survive a reload"

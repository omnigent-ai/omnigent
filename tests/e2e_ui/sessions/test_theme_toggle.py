"""E2E: the sidebar theme toggle cycles the app theme and persists it.

The sidebar header carries a single icon button (``components/theme/
ThemeModeMenu.tsx``) that cycles ``system → dark → light`` on each click; the
icon and ``aria-label`` preview the *next* mode ("Switch to Dark", etc.). The
provider (``components/theme/ThemeProvider.tsx``) is next-themes configured with
``attribute="class"`` + ``storageKey="ap-web-theme"`` + ``defaultTheme="system"``,
so a selection toggles the ``dark`` class on ``<html>`` and writes the choice to
``localStorage["ap-web-theme"]``.

This is the one item in the medium-priority gap list with no coverage anywhere:
the menu component is mocked to ``null`` in every Sidebar vitest test, and only
the pure helpers (``themeMode.test.ts``) are exercised — neither the real DOM
class flip nor the persistence is. (The sibling ``AccountMenu`` is gated behind
an accounts-enabled, authenticated deploy, so it does not render on this
single-user local server and stays out of reach in this harness.)

A fresh Playwright context starts with no stored preference (mode ``system``),
so the button reliably reads "Switch to Dark" on load; the test then drives the
deterministic cycle and pins each step to both the ``<html>`` class and the
persisted ``localStorage`` value. No LLM turn is involved.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"


def _html_has_dark(page: Page) -> bool:
    """True when the ``dark`` class is applied to ``<html>`` (next-themes)."""
    return page.evaluate("() => document.documentElement.classList.contains('dark')")


def _stored_theme(page: Page) -> str | None:
    """The persisted theme preference, or None when unset (default ``system``)."""
    return page.evaluate("() => window.localStorage.getItem('ap-web-theme')")


def test_theme_toggle_cycles_and_persists(page: Page, seeded_session: tuple[str, str]) -> None:
    """Clicking the sidebar theme button cycles system → dark → light, flipping the theme state."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    # Fresh context → no stored preference → mode is the default "system", so
    # the button advertises the first cycle step.
    to_dark = page.get_by_role("button", name="Switch to Dark")
    expect(to_dark).to_be_visible(timeout=15_000)
    assert _stored_theme(page) is None, "expected no persisted theme on a fresh load"

    # system → dark: the dark class lands and the choice persists; the button
    # now advertises the next step.
    to_dark.click()
    to_light = page.get_by_role("button", name="Switch to Light")
    expect(to_light).to_be_visible(timeout=15_000)
    assert _html_has_dark(page), "<html> did not gain the dark class after switching to dark"
    assert _stored_theme(page) == "dark"

    # dark → light: the dark class clears and "light" persists.
    to_light.click()
    to_system = page.get_by_role("button", name="Switch to System")
    expect(to_system).to_be_visible(timeout=15_000)
    assert not _html_has_dark(page), "<html> kept the dark class after switching to light"
    assert _stored_theme(page) == "light"

    # light → system: the cycle closes and "system" persists.
    to_system.click()
    expect(page.get_by_role("button", name="Switch to Dark")).to_be_visible(timeout=15_000)
    assert _stored_theme(page) == "system"

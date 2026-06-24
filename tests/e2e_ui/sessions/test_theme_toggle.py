"""E2E: the sidebar theme toggle cycles the app theme and persists it.

The sidebar header carries a single icon button (``components/theme/
ThemeModeMenu.tsx``) that cycles ``system → dark → light`` on each click. The
icon shows the *current* mode while the ``aria-label`` previews the *next* mode
("Switch to Dark", etc.). The provider (``components/theme/ThemeProvider.tsx``)
is next-themes configured with ``attribute="class"`` +
``storageKey="ap-web-theme"`` + ``defaultTheme="system"``, so a selection
toggles the ``dark`` class on ``<html>`` and writes the choice to
``localStorage["ap-web-theme"]``.

The cycle skips a step that would render identically to the current
appearance: the concrete mode matching the OS preference looks the same as
``system``, so it is dropped (see ``nextThemeMode``). With an emulated **light**
OS the reachable cycle is therefore ``system → dark → system`` — the redundant
explicit ``light`` step is skipped. We pin ``prefers-color-scheme`` with
``emulate_media`` so the cycle is deterministic regardless of the CI runner's
default scheme.

This is the one item in the medium-priority gap list with no coverage anywhere:
the menu component is mocked to ``null`` in every Sidebar vitest test, and only
the pure helpers (``themeMode.test.ts``) are exercised — neither the real DOM
class flip nor the persistence is. (The sibling ``AccountMenu`` is gated behind
an accounts-enabled, authenticated deploy, so it does not render on this
single-user local server and stays out of reach in this harness.)

No LLM turn is involved.
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
    """Clicking the sidebar theme button cycles system → dark → system on a light OS.

    On a light system the explicit ``light`` step renders identically to
    ``system`` and is skipped, so the button advances dark → system directly.
    """
    # Pin the OS preference so the cycle is deterministic regardless of the CI
    # runner's default scheme. next-themes reads this for its ``systemTheme``.
    page.emulate_media(color_scheme="light")

    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    # Fresh context → no stored preference → mode is the default "system", so
    # the button advertises the first cycle step.
    to_dark = page.get_by_role("button", name="Switch to Dark")
    expect(to_dark).to_be_visible(timeout=15_000)
    assert _stored_theme(page) is None, "expected no persisted theme on a fresh load"

    # system → dark: the dark class lands and the choice persists. The next
    # step skips the redundant explicit "light" (identical to system on a light
    # OS) and advertises "Switch to System".
    to_dark.click()
    to_system = page.get_by_role("button", name="Switch to System")
    expect(to_system).to_be_visible(timeout=15_000)
    assert _html_has_dark(page), "<html> did not gain the dark class after switching to dark"
    assert _stored_theme(page) == "dark"

    # dark → system: the dark class clears (system resolves to light) and
    # "system" persists; the cycle closes back to "Switch to Dark".
    to_system.click()
    expect(page.get_by_role("button", name="Switch to Dark")).to_be_visible(timeout=15_000)
    assert not _html_has_dark(page), "<html> kept the dark class after returning to system"
    assert _stored_theme(page) == "system"


def test_theme_toggle_reaches_explicit_light_on_dark_os(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """On a dark OS the cycle skips explicit dark and reaches explicit light.

    Mirror of the light-OS case: explicit ``dark`` renders identically to
    ``system`` on a dark OS and is skipped, so the reachable cycle is
    ``system → light → system``. This pins the explicit-light DOM state and
    persistence that the light-OS cycle can never reach.
    """
    page.emulate_media(color_scheme="dark")

    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    # Fresh "system" on a dark OS renders dark; the redundant explicit "dark"
    # step is skipped, so the button advertises "Switch to Light".
    to_light = page.get_by_role("button", name="Switch to Light")
    expect(to_light).to_be_visible(timeout=15_000)
    assert _html_has_dark(page), "<html> should be dark under system mode on a dark OS"
    assert _stored_theme(page) is None, "expected no persisted theme on a fresh load"

    # system → light: the dark class clears and "light" persists.
    to_light.click()
    to_system = page.get_by_role("button", name="Switch to System")
    expect(to_system).to_be_visible(timeout=15_000)
    assert not _html_has_dark(page), "<html> kept the dark class after switching to light"
    assert _stored_theme(page) == "light"

    # light → system: the dark class returns (system resolves to dark) and
    # "system" persists.
    to_system.click()
    expect(page.get_by_role("button", name="Switch to Light")).to_be_visible(timeout=15_000)
    assert _html_has_dark(page), "<html> did not regain the dark class after returning to system"
    assert _stored_theme(page) == "system"

"""E2E: the command palette is a full-screen sheet at a phone viewport.

``CommandPalette.tsx`` renders two layouts off ``useIsMobileViewport()``:
the desktop palette is a centered card anchored at ``top-1/4`` and capped at
``sm:max-w-2xl``; the mobile palette is a top-anchored full-screen sheet sized
to the keyboard-aware visible viewport, so the soft keyboard can't cover the
search field and results. ``CommandPalette.test.tsx`` asserts the class swap in
jsdom, but jsdom does not apply the ``max-md`` media query the hook mirrors, so
it can't prove the real responsive geometry — that is what these tests pin.

Both open the palette with the window-level ⌘/Ctrl+K hotkey (viewport
independent), so neither depends on the mobile drawer's search entry point.
No LLM turn is needed — this is client-side layout + routing.
"""

from __future__ import annotations

from playwright.sync_api import Page, ViewportSize, expect

# iPhone-12-class portrait viewport — below the Tailwind ``md`` breakpoint
# (768px) so ``useIsMobileViewport`` reports mobile and the sheet layout applies.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

_COMPOSER = "Ask the agent anything…"


def _open_palette(page: Page, base_url: str, session_id: str) -> None:
    """Load the session at a phone width and open the palette via ⌘/Ctrl+K."""
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    # Wait for the shell to be interactive before firing the window-level hotkey.
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
    page.keyboard.press("Control+k")
    expect(page.get_by_test_id("command-palette-input")).to_be_focused(timeout=10_000)


def test_mobile_command_palette_is_a_fullscreen_sheet(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """At a phone width the palette fills the screen from the top.

    Asserts the geometry that separates the mobile sheet from the desktop
    centered card: the dialog is anchored at the very top (not ``top-1/4``),
    spans the full width (not ``sm:max-w-2xl``), and fills essentially the whole
    viewport height. A regression back to the centered card — the layout the
    keyboard used to overlap — would fail here.
    """
    base_url, session_id = seeded_session
    _open_palette(page, base_url, session_id)

    box = page.get_by_role("dialog").bounding_box()
    assert box is not None, "the palette dialog should have a layout box"
    # Top-anchored: safe-area insets are 0 in headless chromium, so the sheet
    # starts at the very top rather than a quarter of the way down.
    assert box["y"] <= 8, f"sheet is not top-anchored (y={box['y']})"
    # Full-bleed width and near-full height — the centered card would be far
    # narrower and shorter.
    assert box["width"] >= _MOBILE_VIEWPORT["width"] - 8, (
        f"sheet is not full-width (w={box['width']})"
    )
    assert box["height"] >= _MOBILE_VIEWPORT["height"] * 0.85, (
        f"sheet does not fill the viewport height (h={box['height']})"
    )

    # The mobile-only explicit close affordance is present (the sheet has no
    # visible ⌘K/Esc hint for touch users).
    expect(page.get_by_role("button", name="Close search")).to_be_visible()


def test_mobile_command_palette_close_button_dismisses(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Tapping the mobile close button dismisses the full-screen sheet."""
    base_url, session_id = seeded_session
    _open_palette(page, base_url, session_id)

    page.get_by_role("button", name="Close search").click()

    expect(page.get_by_test_id("command-palette-input")).to_have_count(0)

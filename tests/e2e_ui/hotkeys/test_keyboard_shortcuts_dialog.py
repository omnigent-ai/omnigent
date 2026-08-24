"""UI e2e: the Ctrl/Cmd+/ keyboard-shortcuts reference.

Two things only a real browser settles:

- The panel is sized against the viewport rather than pinned to a narrow
  column. It used to cap at ``sm:max-w-md`` (448px), which on any normal
  window rendered as a tall ribbon of rows in a mostly-empty modal.
- The archive shortcuts are listed. The dialog's contract is that it mirrors
  live behavior, so a shortcut that ships without a row here is invisible.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

# The dialog's own box, measured after its open animation settles.
_DIALOG_BOX_JS = """
() => {
  const el = document.querySelector('[role="dialog"]');
  return el ? el.getBoundingClientRect().width : -1;
}
"""

# The pre-change cap (Tailwind's max-w-md). The panel must clear it.
_OLD_MAX_WIDTH_PX = 448


def test_shortcuts_dialog_scales_with_the_viewport(page: Page, live_server: str) -> None:
    """The panel widens with the window and stops at a readable measure.

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Base URL of the spawned server.
    """
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(live_server)
    expect(page.get_by_test_id("sidebar-search-button")).to_be_visible(timeout=30_000)

    page.keyboard.press("Control+Slash")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(page.get_by_role("heading", name="Keyboard shortcuts")).to_be_visible()

    # Settle the open animation (zoom-in-95) before measuring.
    page.wait_for_timeout(400)
    width = page.evaluate(_DIALOG_BOX_JS)
    assert width > _OLD_MAX_WIDTH_PX, (
        f"shortcuts dialog is still pinned to the old narrow column ({width}px); "
        f"expected wider than {_OLD_MAX_WIDTH_PX}px on a 1440px viewport"
    )
    # Capped, not full-bleed — a modal spanning the window is not readable.
    assert width <= 900, f"shortcuts dialog overshot its readable cap ({width}px)"


def test_shortcuts_dialog_narrows_on_a_small_viewport(page: Page, live_server: str) -> None:
    """On a phone-sized window the panel shrinks to fit instead of overflowing.

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Base URL of the spawned server.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(live_server)
    page.wait_for_timeout(1_000)

    page.keyboard.press("Control+Slash")
    expect(page.get_by_role("dialog")).to_be_visible(timeout=30_000)
    page.wait_for_timeout(400)

    width = page.evaluate(_DIALOG_BOX_JS)
    assert 0 < width <= 390, f"shortcuts dialog overflows a 390px viewport ({width}px)"


def test_shortcuts_dialog_lists_both_archive_routes(page: Page, live_server: str) -> None:
    """Both archive shortcuts are documented: the chord and the menu key.

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Base URL of the spawned server.
    """
    page.goto(live_server)
    expect(page.get_by_test_id("sidebar-search-button")).to_be_visible(timeout=30_000)

    page.keyboard.press("Control+Slash")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()

    # One row under "In chats" (the chord) and one under "Session menu".
    expect(dialog.get_by_text("Archive session", exact=True)).to_have_count(2)
    expect(dialog.get_by_text("Session menu", exact=False)).to_be_visible()

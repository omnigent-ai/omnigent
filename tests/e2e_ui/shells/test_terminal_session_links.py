"""E2E: terminal session links stay in the current web app tab.

The embedded xterm makes URLs in TUI / shell output clickable via
``WebLinksAddon``. Same-origin Omnigent session URLs are app navigation, not
external content: clicking one should update the current SPA route instead of
opening a duplicate browser tab/window for the same chat.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e_ui.conftest import open_right_rail


def _open_new_shell(page: Page) -> None:
    """Open a user shell in the main terminal surface."""
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("Shells")).click()
    rail.get_by_role("button", name="New shell").click()


def _print_url_at_terminal_origin(page: Page, url: str) -> None:
    """Print *url* at row 1/column 1 of the active xterm."""
    terminal_view = page.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)

    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    page.keyboard.type(f"printf '\\033[2J\\033[H%s\\n' '{url}'")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)


def _click_first_terminal_row(page: Page) -> None:
    """Click near the start of row 1 in the active xterm screen."""
    terminal_view = page.get_by_test_id("terminal-view").last
    screen = terminal_view.locator(".xterm-screen").first
    expect(screen).to_be_visible(timeout=20_000)
    box = screen.bounding_box()
    assert box is not None, "xterm screen should have a clickable bounding box"
    page.mouse.click(box["x"] + 16, box["y"] + 10)


def test_same_origin_terminal_session_link_navigates_in_app(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """Clicking a terminal-printed session URL does not open a duplicate tab.

    The query string makes the destination visibly different from the current
    URL while still targeting the same session. That proves the click hit the
    xterm link: a missed click leaves the URL unchanged, while the old behavior
    opens a popup/new tab and also leaves the current URL unchanged.
    """
    base_url, session_id = terminal_session
    target_path = f"/c/{session_id}?terminal-link-e2e=1"
    target_url = f"{base_url}{target_path}"

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    _print_url_at_terminal_origin(page, target_url)

    try:
        with page.expect_popup(timeout=1_000):
            _click_first_terminal_row(page)
    except PlaywrightTimeoutError:
        pass
    else:
        raise AssertionError("same-origin terminal session link opened a popup")

    expect(page).to_have_url(f"{base_url}{target_path}")

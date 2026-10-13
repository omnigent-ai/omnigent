"""Find focus and toolbar behavior with the real Monaco editors."""

import json

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _open_monaco,
    _seed_citation_file,
)
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture


@pytest.mark.parametrize("layout", [None, "unified", "split"], ids=["code", "unified", "split"])
@pytest.mark.parametrize("maximized", [False, True], ids=["panel", "full-screen"])
@pytest.mark.parametrize("platform", ["mac", "windows"])
def test_find_refocuses_without_changing_toolbar_toggle(
    page: Page,
    seeded_session: BrowserSession,
    layout: str | None,
    maximized: bool,
    platform: str,
) -> None:
    """Repeated Find shortcuts focus the input even when its widget stays open."""
    mac = platform == "mac"
    navigator = {
        "platform": {"value": "MacIntel" if mac else "Win32"},
        "userAgentData": {"value": {"platform": "macOS" if mac else "Windows"}},
        "userAgent": {
            "value": "Mozilla/5.0 (Macintosh)" if mac else "Mozilla/5.0 (Windows NT 10.0)"
        },
    }
    page.add_init_script(f"Object.defineProperties(navigator, {json.dumps(navigator)});")
    shortcut = "Meta+f" if mac else "Control+f"
    _seed_citation_file(page, seeded_session)
    viewer = _open_monaco(page, seeded_session, layout)
    if maximized:
        page.get_by_role("button", name="Full screen", exact=True).click()

    editor = viewer.locator(".modified" if layout else ".monaco-editor")
    lines = editor.locator('.view-lines[role="presentation"]')
    find = editor.get_by_role("textbox", name="Find", exact=True)
    lines.click()
    page.keyboard.press(shortcut)
    expect(find).to_be_focused()

    for _ in range(2):
        lines.click()
        expect(find).not_to_be_focused()
        page.keyboard.press(shortcut)
        expect(find).to_be_focused()

    page.keyboard.press("Meta+a" if mac else "Control+a")
    page.keyboard.type("original")
    expect(find).to_have_value("original")
    page.keyboard.press(shortcut)
    expect(find).to_be_focused()
    expect(find).to_have_value("original")

    page.keyboard.press("Escape")
    expect(find).not_to_be_visible()
    page.keyboard.press(shortcut)
    expect(find).to_be_focused()

    # The toolbar still toggles visibility after repeated focus requests.
    for visible in (False, True):
        viewer.get_by_role("button", name="View settings", exact=True).click()
        page.get_by_role("menuitem", name="Find in file", exact=True).click()
        if visible:
            expect(find).to_be_visible()
        else:
            expect(find).not_to_be_visible()

    if maximized:
        page.get_by_role("button", name="Exit full screen", exact=True).click()
    composer = page.get_by_role("textbox", name="Message the agent")
    composer.click()
    page.keyboard.press(shortcut)
    expect(composer).to_be_focused()

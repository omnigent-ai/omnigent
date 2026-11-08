"""Repeated Cmd+F refocuses Find in the real code and diff editors."""

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
def test_cmd_f_refocuses_open_find(
    page: Page,
    seeded_session: BrowserSession,
    layout: str | None,
) -> None:
    """Typing after the second Cmd+F updates Find rather than the code."""
    page.add_init_script("""
        Object.defineProperties(navigator, {
            platform: {value: "MacIntel"},
            userAgentData: {value: {platform: "macOS"}},
            userAgent: {value: "Mozilla/5.0 (Macintosh)"},
        });
    """)
    _seed_citation_file(page, seeded_session)
    viewer = _open_monaco(page, seeded_session, layout)

    editor = viewer.locator(".modified" if layout else ".monaco-editor")
    lines = editor.locator('.view-lines[role="presentation"]')
    find = editor.get_by_role("textbox", name="Find", exact=True)
    lines.click()
    page.keyboard.press("Meta+f")
    expect(find).to_be_focused()
    find.fill("original")

    lines.click()
    expect(find).to_be_visible()
    expect(find).not_to_be_focused()
    page.keyboard.press("Meta+f")
    expect(find).to_be_focused()
    page.keyboard.type("refocus_sentinel")
    expect(find).to_have_value("refocus_sentinel")
    expect(lines).not_to_contain_text("refocus_sentinel")

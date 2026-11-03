"""The macOS desktop shell renders the SPA's text with grayscale antialiasing.

Chromium honours the property only on macOS, so these checks read computed style.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.browser_ui.chat._geometry_helpers import open_live
from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item

_MAC_NAVIGATOR = """
    Object.defineProperty(navigator, "platform", { value: "MacIntel" });
    Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (Macintosh)" });
"""
_WINDOWS_NAVIGATOR = """
    Object.defineProperty(navigator, "platform", { value: "Win32" });
    Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (Windows NT 10.0)" });
"""
_DESKTOP_BRIDGE = """
    window.omnigentDesktop = {
        kind: "electron",
        setBadgeCount() {},
        notify() { return Promise.resolve(false); },
        onNotificationActivated() { return () => {}; },
    };
"""


def _font_smoothing(locator: Locator) -> str:
    return locator.evaluate("element => getComputedStyle(element).webkitFontSmoothing")


@pytest.mark.parametrize(
    ("init_script", "expected"),
    [
        pytest.param(_MAC_NAVIGATOR + _DESKTOP_BRIDGE, "antialiased", id="macos-desktop"),
        pytest.param(_MAC_NAVIGATOR, "auto", id="macos-browser"),
        pytest.param(_WINDOWS_NAVIGATOR + _DESKTOP_BRIDGE, "auto", id="windows-desktop"),
    ],
)
def test_text_smoothing_follows_the_macos_desktop_shell(
    page: Page, chat_session_contract: ChatSessionContract, init_script: str, expected: str
) -> None:
    chat = chat_session_contract
    chat.set_items(
        [message_item("msg-user", "user", "Font smoothing sample", response_id="resp-1")]
    )
    page.add_init_script(init_script)
    open_live(page, chat)

    html = page.locator("html")
    if expected == "antialiased":
        expect(html).to_have_attribute("data-electron-mac", "true")
    else:
        expect(html).not_to_have_attribute("data-electron-mac", "true")
    message = page.get_by_text("Font smoothing sample", exact=True)
    expect(message).to_be_visible()
    assert _font_smoothing(message) == expected

    # Radix menus portal to <body>, outside the app shell, so they rely on inheritance.
    page.get_by_test_id("composer-attach").click()
    menu = page.get_by_role("menu", name="Add", exact=True)
    expect(menu).to_be_visible()
    assert not page.locator(".app-shell").get_by_role("menu", name="Add").count()
    assert _font_smoothing(menu.get_by_role("menuitem").first) == expected

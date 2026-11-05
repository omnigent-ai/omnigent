"""The macOS desktop shell renders the SPA's text with grayscale antialiasing.

Chromium honours the property only on macOS, so this check reads computed style.
Platform exclusion is covered by the CSS selector and embed scoping unit tests.
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect

from tests.browser_ui.chat._geometry_helpers import open_live
from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item

_MAC_DESKTOP_SHELL = """
    Object.defineProperty(navigator, "platform", { value: "MacIntel" });
    Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (Macintosh)" });
    window.omnigentDesktop = {
        kind: "electron",
        setBadgeCount() {},
        notify() { return Promise.resolve(false); },
        onNotificationActivated() { return () => {}; },
    };
"""


def _font_smoothing(locator: Locator) -> str:
    return locator.evaluate("element => getComputedStyle(element).webkitFontSmoothing")


def test_macos_desktop_shell_antialiases_content_and_portaled_menus(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    chat = chat_session_contract
    chat.set_items(
        [message_item("msg-user", "user", "Font smoothing sample", response_id="resp-1")]
    )
    page.add_init_script(_MAC_DESKTOP_SHELL)
    open_live(page, chat)

    expect(page.locator("html")).to_have_attribute("data-electron-mac", "true")
    message = page.get_by_text("Font smoothing sample", exact=True)
    expect(message).to_be_visible()
    assert _font_smoothing(message) == "antialiased"

    # Radix menus portal to <body>, outside the app shell, so they rely on inheritance.
    page.get_by_test_id("composer-attach").click()
    menu = page.get_by_role("menu", name="Add", exact=True)
    expect(menu).to_be_visible()
    assert not page.locator(".app-shell").get_by_role("menu", name="Add").count()
    assert _font_smoothing(menu.get_by_role("menuitem").first) == "antialiased"

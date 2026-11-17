"""Open in browser from the desktop shell's title strip.

Emulates the macOS shell in Chromium, as ``test_mac_shell_narrow_window_header.py``
does. The shell sends a page's ``window.open`` of a web URL to the system browser,
so the test records the call instead.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

_MAC_SHELL_RECORDING_OPEN = """
    Object.defineProperty(navigator, "platform", { value: "MacIntel" });
    Object.defineProperty(navigator, "userAgentData", { value: { platform: "macOS" } });
    Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (Macintosh)" });
    window.omnigentDesktop = {
        kind: "electron",
        setBadgeCount() {},
        notify() { return Promise.resolve(false); },
        onNotificationActivated() { return () => {}; },
        getServerPicker() { return Promise.resolve(null); },
        switchServer() { return Promise.resolve(); },
        openServerSetup() {},
    };
    window.__opened = [];
    window.open = (...args) => { window.__opened.push(args); return null; };
"""


def test_title_strip_opens_the_current_page_in_the_browser(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    page.add_init_script(_MAC_SHELL_RECORDING_OPEN)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator("html")).to_have_attribute("data-electron-mac", "true", timeout=30_000)

    button = page.locator(".electron-sidebar-header-actions").get_by_role(
        "button", name="Open in browser"
    )
    expect(button).to_be_visible()
    button.click()

    assert page.evaluate("window.__opened") == [
        [f"{base_url}/c/{session_id}", "_blank", "noopener,noreferrer"]
    ]

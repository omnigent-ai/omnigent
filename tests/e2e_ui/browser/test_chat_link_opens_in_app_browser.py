"""E2E: the "Open links in the in-app browser" setting on the desktop shell.

Plain Chromium stands in for Electron via a minimal ``window.omnigentDesktop``
stub (as in ``test_browser_tab.py``) that records ``browserOpenOrNavigate``.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn

# Loopback, so a click that leaks to a real navigation never leaves the host.
LINK_URL = "http://127.0.0.1:9/product-docs"

_NOOP_SUBSCRIPTIONS = ", ".join(
    f"{name}: () => () => {{}}"
    for name in (
        "onNotificationActivated",
        "onBrowserViewCreated",
        "onBrowserHostActiveChanged",
        "onBrowserViewClosed",
        "onBrowserUrlChanged",
        "onBrowserNavState",
    )
)
_SHELL_STUB = f"""
window.__calls = [];
window.omnigentDesktop = {{
  kind: "electron",
  setBadgeCount() {{}},
  notify: () => Promise.resolve(false),
  getServerPicker: () => Promise.resolve(null),
  switchServer: () => Promise.resolve(),
  openServerSetup() {{}},
  browserHasView: () => Promise.resolve({{ exists: false }}),
  browserOpenOrNavigate(conversationId, url) {{
    window.__calls.push({{ conversationId, url }});
    return Promise.resolve({{ ok: true, created: true }});
  }},
  {_NOOP_SUBSCRIPTIONS},
}};
"""


def test_setting_routes_plain_clicks_in_app(page: Page, seeded_session: tuple[str, str]) -> None:
    """Off by default; once enabled, a plain click opens in the Browser tab."""
    base_url, session_id = seeded_session
    seed_committed_turn(session_id, prompt="link?", reply=f"Docs: [{LINK_URL}]({LINK_URL})")
    page.add_init_script(_SHELL_STUB)
    page.goto(f"{base_url}/c/{session_id}")
    link = page.get_by_role("link", name=re.compile("product-docs"))

    # Default: the click keeps its external _blank path.
    with page.expect_popup():
        link.click()
    assert page.evaluate("window.__calls") == []

    page.get_by_test_id("settings-button").first.click()
    toggle = page.get_by_test_id("open-links-in-app-toggle")
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    page.get_by_role("link", name="Back", exact=True).click()

    # With a file open in the rail, the link creates and selects a Browser soft tab.
    page.goto(f"{base_url}/c/{session_id}?file=README.md")
    link.click()
    page.wait_for_function("window.__calls.length === 1")
    assert page.evaluate("window.__calls") == [{"conversationId": session_id, "url": LINK_URL}]
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_role("tab", name="Browser 1", exact=True)).to_be_visible()
    expect(rail.get_by_role("button", name="Close Browser 1", exact=True)).to_be_visible()
    expect(page).not_to_have_url(re.compile("file="))

    # A modified click stays external.
    link.click(modifiers=["ControlOrMeta"])
    page.wait_for_timeout(500)
    assert page.evaluate("window.__calls.length") == 1

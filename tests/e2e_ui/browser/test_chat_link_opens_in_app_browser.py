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
SECOND_LINK_URL = "http://127.0.0.1:9/release-notes"
THIRD_LINK_URL = "http://127.0.0.1:9/api-guide"

_NOOP_SUBSCRIPTIONS = ", ".join(
    f"{name}: () => () => {{}}"
    for name in (
        "onNotificationActivated",
        "onBrowserViewCreated",
        "onBrowserHostActiveChanged",
        "onBrowserViewClosed",
        "onBrowserNavState",
    )
)
_SHELL_STUB = f"""
window.__calls = [];
window.__views = {{}};
window.__titles = {{}};
window.__urlListeners = new Set();
window.__emitBrowserMetadata = (conversationId, title) => {{
  window.__titles[conversationId] = title;
  for (const callback of window.__urlListeners) {{
    callback({{ conversationId, url: window.__views[conversationId], title }});
  }}
}};
window.omnigentDesktop = {{
  kind: "electron",
  setBadgeCount() {{}},
  notify: () => Promise.resolve(false),
  getServerPicker: () => Promise.resolve(null),
  switchServer: () => Promise.resolve(),
  openServerSetup() {{}},
  browserHasView: (conversationId) => Promise.resolve({{
    exists: conversationId in window.__views,
    url: window.__views[conversationId],
    title: window.__titles[conversationId],
  }}),
  browserOpenOrNavigate(conversationId, url) {{
    window.__calls.push({{ conversationId, url }});
    window.__views[conversationId] = url;
    window.__titles[conversationId] = {{
      "{LINK_URL}": "Product docs",
      "{SECOND_LINK_URL}": "Release notes",
    }}[url] ?? "";
    return Promise.resolve({{ ok: true, created: true }});
  }},
  onBrowserUrlChanged(callback) {{
    window.__urlListeners.add(callback);
    return () => window.__urlListeners.delete(callback);
  }},
  {_NOOP_SUBSCRIPTIONS},
}};
"""


def test_setting_routes_plain_clicks_in_app(page: Page, seeded_session: tuple[str, str]) -> None:
    """Opted-in links reuse titled tabs and update labels for background pages."""
    base_url, session_id = seeded_session
    seed_committed_turn(
        session_id,
        prompt="links?",
        reply=(
            f"[Product docs]({LINK_URL}), [Release notes]({SECOND_LINK_URL}), "
            f"and [API guide]({THIRD_LINK_URL})"
        ),
    )
    page.add_init_script(_SHELL_STUB)
    page.goto(f"{base_url}/c/{session_id}")
    link = page.get_by_role("link", name="Product docs", exact=True)

    # Default: the click keeps its external _blank path.
    with page.expect_popup() as popup:
        link.click()
    popup.value.close()
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
    first_view = page.evaluate("window.__calls[0].conversationId")
    rail = page.get_by_role("complementary", name="Workspace")
    first_tab = rail.get_by_role("tab", name="Product docs", exact=True)
    expect(first_tab).to_have_attribute("aria-selected", "true")
    expect(rail.get_by_role("button", name="Close Product docs", exact=True)).to_be_visible()
    expect(rail.get_by_placeholder("Enter a URL")).to_have_value(LINK_URL)
    expect(page).not_to_have_url(re.compile("file="))

    # A second plain click selects a fresh tab without navigating the first one.
    page.get_by_role("link", name="Release notes", exact=True).click()
    page.wait_for_function("window.__calls.length === 2")
    second_view = page.evaluate("window.__calls[1].conversationId")
    second_tab = rail.get_by_role("tab", name="Release notes", exact=True)
    expect(second_tab).to_have_attribute("aria-selected", "true")
    expect(first_tab).to_have_attribute("aria-selected", "false")
    expect(rail.get_by_placeholder("Enter a URL")).to_have_value(SECOND_LINK_URL)

    # A background page can rename its tab without selecting it.
    page.evaluate("id => window.__emitBrowserMetadata(id, 'Product docs updated')", first_view)
    first_tab = rail.get_by_role("tab", name="Product docs updated", exact=True)
    expect(first_tab).to_have_attribute("aria-selected", "false")
    expect(second_tab).to_have_attribute("aria-selected", "true")
    expect(
        rail.get_by_role("button", name="Close Product docs updated", exact=True)
    ).to_be_visible()

    # Reopening the first URL selects its existing tab without reloading the page.
    link.click(modifiers=["ControlOrMeta"])
    expect(first_tab).to_have_attribute("aria-selected", "true")
    expect(second_tab).to_have_attribute("aria-selected", "false")
    expect(
        rail.get_by_role("tab", name=re.compile(r"^(Product docs|Release notes)"))
    ).to_have_count(2)
    expect(rail.get_by_placeholder("Enter a URL")).to_have_value(LINK_URL)
    assert page.evaluate("window.__calls.length") == 2

    second_tab.click()
    link.click()
    expect(first_tab).to_have_attribute("aria-selected", "true")
    expect(
        rail.get_by_role("tab", name=re.compile(r"^(Product docs|Release notes)"))
    ).to_have_count(2)
    expect(rail.get_by_placeholder("Enter a URL")).to_have_value(LINK_URL)
    assert page.evaluate("window.__calls.length") == 2

    # Cmd/Ctrl-click on a distinct URL opens another independent view.
    page.get_by_role("link", name="API guide", exact=True).click(modifiers=["ControlOrMeta"])
    page.wait_for_function("window.__calls.length === 3")
    third_view = page.evaluate("window.__calls[2].conversationId")
    # Until the page supplies a title, its hostname identifies the tab.
    expect(rail.get_by_role("tab", name="127.0.0.1", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    page.evaluate("id => window.__emitBrowserMetadata(id, 'API guide')", third_view)
    expect(rail.get_by_role("tab", name="API guide", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    assert len({first_view, second_view, third_view}) == 3
    assert page.evaluate("window.__views") == {
        first_view: LINK_URL,
        second_view: SECOND_LINK_URL,
        third_view: THIRD_LINK_URL,
    }

    with page.context.expect_page() as popup:
        link.click(modifiers=["Shift"])
    popup.value.close()
    assert page.evaluate("window.__calls.length") == 3

"""E2E coverage for the Electron sidebar server picker URL actions."""

from __future__ import annotations

from playwright.sync_api import Page, expect

_CURRENT_ORIGIN = "https://connected.example.com:8443"
_SERVER_PICKER_INIT_SCRIPT = f"""
window.omnigentDesktop = {{
  kind: "electron",
  setBadgeCount: function () {{}},
  notify: function () {{ return Promise.resolve(false); }},
  onNotificationActivated: function () {{ return function () {{}}; }},
  getServerPicker: function () {{
    return Promise.resolve({{
      currentOrigin: "{_CURRENT_ORIGIN}",
      recentServers: [],
    }});
  }},
  switchServer: function () {{ return Promise.resolve(); }},
  openServerSetup: function () {{}},
}};
"""


def test_server_picker_opens_and_copies_current_url(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The live menu opens and copies the exact bridge-provided server URL."""
    base_url, session_id = seeded_session
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
    page.context.route(
        f"{_CURRENT_ORIGIN}/**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<title>Connected Omnigent host</title>",
        ),
    )
    page.add_init_script(_SERVER_PICKER_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")

    trigger = page.get_by_test_id("sidebar-server-picker")
    expect(trigger).to_be_visible(timeout=30_000)
    trigger.click()

    open_action = page.get_by_role("menuitem", name="Open server in new tab")
    copy_action = page.get_by_role("menuitem", name="Copy server URL")
    expect(open_action).to_have_attribute("href", _CURRENT_ORIGIN)
    expect(open_action).to_have_attribute("target", "_blank")
    expect(open_action).to_have_attribute("rel", "noopener noreferrer")
    assert (copy_action.bounding_box() or {}).get("height", 0) >= 43.5

    page.keyboard.press("Escape")
    trigger.focus()
    trigger.press("Enter")
    open_action = page.get_by_role("menuitem", name="Open server in new tab")
    copy_action = page.get_by_role("menuitem", name="Copy server URL")
    expect(open_action).to_be_visible()
    copy_action.focus()
    expect(copy_action).to_be_focused()
    copy_action.press("Enter")

    expect(page.get_by_role("status")).to_contain_text("Server URL copied.")
    assert page.evaluate("navigator.clipboard.readText()") == _CURRENT_ORIGIN

    expect(open_action).to_be_hidden()
    trigger.click()
    open_action = page.get_by_role("menuitem", name="Open server in new tab")
    expect(open_action).to_be_visible()
    with page.expect_popup() as popup_info:
        open_action.click()
    popup = popup_info.value
    popup.wait_for_load_state()
    assert popup.url == f"{_CURRENT_ORIGIN}/"
    popup.close()

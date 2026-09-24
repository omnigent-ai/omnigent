"""E2E: where a plain click on a chat link opens on the desktop shell.

By default a normal click on an external link in an assistant reply keeps the
current behavior: the shell's window-open policy hands it to the default OS
browser, and nothing routes into the embedded browser pane. Settings (General
-> Links) offers a desktop-only "Open links in the in-app browser" toggle;
once enabled, a plain click routes the link into the conversation's embedded
browser view (``browserOpenOrNavigate``) and auto-surfaces the Browser
workspace tab, while a modified click stays on the external path.

The e2e_ui harness runs the SPA in plain Chromium, not Electron, so the
desktop path is exercised through a minimal ``window.omnigentDesktop`` stub
injected before any app script runs (the same feature-detection stubbing
``test_browser_tab.py`` uses). The stub records ``browserOpenOrNavigate``
calls so the assertions can see exactly what would reach the native
WebContentsView; the native attach itself is desktop-only and covered by the
``web/electron/e2e`` lane.

No LLM turn is involved; transcripts are seeded straight into the store.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn

# Loopback link: a click that leaks to a real navigation dies on a closed
# port instead of reaching the public internet.
LINK_URL = "http://127.0.0.1:9/product-docs"

# Minimal Electron-preload stand-in (see test_browser_tab.py), with a
# recording ``browserOpenOrNavigate`` so tests can assert what the SPA routed
# into the embedded browser view.
_ELECTRON_SHELL_INIT_SCRIPT = """
window.__openOrNavigateCalls = [];
window.omnigentDesktop = {
  kind: "electron",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  getServerPicker: function () { return Promise.resolve(null); },
  switchServer: function () { return Promise.resolve(); },
  openServerSetup: function () {},
  browserOpenOrNavigate: function (conversationId, url) {
    window.__openOrNavigateCalls.push({ conversationId: conversationId, url: url });
    return Promise.resolve({ ok: true, created: true });
  },
  browserHasView: function () { return Promise.resolve({ exists: false }); },
  onBrowserViewCreated: function () { return function () {}; },
  onBrowserHostActiveChanged: function () { return function () {}; },
  onBrowserViewClosed: function () { return function () {}; },
  onBrowserUrlChanged: function () { return function () {}; },
  onBrowserNavState: function () { return function () {}; },
};
"""


def _open_seeded_conversation(page: Page, base_url: str, session_id: str) -> None:
    """Seed a linked assistant reply and open its conversation."""
    seed_committed_turn(
        session_id,
        prompt="share the docs link",
        reply=f"Here are the docs: [{LINK_URL}]({LINK_URL})",
    )
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()


def _chat_link(page: Page):
    return page.get_by_role("link", name=re.compile("product-docs"))


def test_plain_click_keeps_external_default(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Without opting in, a plain click never routes into the embedded view.

    The default must keep the current behavior: the click stays on the
    anchor's own ``target="_blank"`` path (under real Electron the shell's
    window-open policy externals it), and ``browserOpenOrNavigate`` is never
    called.
    """
    base_url, session_id = seeded_session
    page.add_init_script(_ELECTRON_SHELL_INIT_SCRIPT)
    _open_seeded_conversation(page, base_url, session_id)

    link = _chat_link(page)
    expect(link).to_be_visible()
    with page.expect_popup():
        link.click()

    assert page.evaluate("window.__openOrNavigateCalls") == []


def test_settings_toggle_routes_plain_clicks_in_app(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Enable the setting in Settings; a plain click then opens in-app.

    Drives the full user journey: conversation -> Settings (the General
    section's Links toggle) -> back to the conversation -> plain click. The
    click must route the URL into the conversation's embedded browser view
    and auto-surface the Browser workspace tab. A ctrl/cmd-click afterwards
    must stay external (no further in-app routing).
    """
    base_url, session_id = seeded_session
    page.add_init_script(_ELECTRON_SHELL_INIT_SCRIPT)
    _open_seeded_conversation(page, base_url, session_id)

    # Opt in through the real Settings control (desktop-only: it renders
    # because the stub marks the shell browser-capable).
    page.get_by_test_id("settings-button").click()
    expect(page).to_have_url(f"{base_url}/settings/general", timeout=30_000)
    toggle = page.get_by_test_id("open-links-in-app-toggle")
    expect(toggle).to_be_visible()
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")

    # Back to the conversation the sidebar remembered.
    page.get_by_role("link", name="Back", exact=True).click()
    expect(page).to_have_url(f"{base_url}/c/{session_id}", timeout=30_000)

    link = _chat_link(page)
    expect(link).to_be_visible()
    link.click()

    # The link routed into the conversation's embedded browser view…
    page.wait_for_function("window.__openOrNavigateCalls.length === 1")
    assert page.evaluate("window.__openOrNavigateCalls") == [
        {"conversationId": session_id, "url": LINK_URL}
    ]
    # …and the Browser workspace tab auto-surfaced to host it.
    rail = page.get_by_role("complementary", name="Workspace")
    browser_tab = rail.get_by_role("tab", name=re.compile("Browser"))
    expect(browser_tab).to_have_attribute("aria-selected", "true", timeout=30_000)
    # The click was cancelled, so no _blank popup opened alongside.
    assert len(page.context.pages) == 1

    # A modified click keeps the external-browser path.
    link.click(modifiers=["ControlOrMeta"])
    page.wait_for_timeout(500)
    assert page.evaluate("window.__openOrNavigateCalls.length") == 1


def test_toggle_hidden_in_plain_browser(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Without a browser-capable shell the Links toggle must not render.

    The preference is meaningless where no embedded browser exists (plain
    web app, older desktop builds), so the Settings section hides it.
    """
    base_url, _session_id = seeded_session
    page.goto(f"{base_url}/settings/general")
    expect(page.get_by_test_id("always-steer-toggle")).to_be_visible()
    expect(page.get_by_test_id("open-links-in-app-toggle")).to_have_count(0)

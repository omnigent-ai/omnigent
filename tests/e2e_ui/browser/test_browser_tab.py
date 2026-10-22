"""E2E: the embedded-browser "Browser" tab in the right Workspace rail.

The browser pane is desktop-only: ``AppShell`` marks the Browser rail tab
available when ``supportsBrowser()`` is true, i.e. when the Electron preload
exposes ``window.omnigentDesktop.kind === "electron"`` *and* the embedded-
browser bridge method ``browserOpenOrNavigate`` (an older desktop build that
predates the feature lacks it) — see ``web/src/lib/nativeBridge.ts``. Browser
is offered only from ``+`` and opens as a closable soft tab; there is no fixed
Browser navigation icon.

The e2e_ui harness runs the SPA in a plain Chromium browser, not Electron, so
by default the tab is absent. To exercise the desktop path end-to-end we inject
a minimal ``window.omnigentDesktop`` stub via ``add_init_script`` *before any
app script runs* — the same feature-detection stubbing
``sessions/test_pinned_session_hotkeys.py`` uses. That covers the chain the
component/unit tests can't reach end to end: the injected bridge ->
``supportsBrowser()`` -> ``WorkspacePanel`` offering Browser from ``+`` ->
creating a soft tab -> selecting it mounting the pane.

No LLM turn is involved; the assertions are DOM-based.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

# Electron preload stub, run before app scripts so ``supportsBrowser()`` passes.
# Methods are guarded no-ops (``browserHasView`` reports no view) except
# ``onBrowserUrlChanged``, which records listeners for ``_emit_browser_url``.
_ELECTRON_SHELL_INIT_SCRIPT = """
window.omnigentDesktop = {
  kind: "electron",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  getServerPicker: function () { return Promise.resolve(null); },
  switchServer: function () { return Promise.resolve(); },
  openServerSetup: function () {},
  browserOpenOrNavigate: function () { return Promise.resolve({ ok: true }); },
  browserHasView: function () { return Promise.resolve({ exists: false }); },
  onBrowserViewCreated: function () { return function () {}; },
  onBrowserHostActiveChanged: function () { return function () {}; },
  onBrowserViewClosed: function () { return function () {}; },
  // Keeps listeners so tests can replay URL events via _emit_browser_url.
  onBrowserUrlChanged: function (callback) {
    window.__browserUrlListeners = window.__browserUrlListeners || [];
    window.__browserUrlListeners.push(callback);
    return function () {
      window.__browserUrlListeners = window.__browserUrlListeners.filter(function (listener) {
        return listener !== callback;
      });
    };
  },
  onBrowserNavState: function () { return function () {}; },
};
"""


def _emit_browser_url(page: Page, view_id: str, url: str) -> None:
    """Deliver a native ``browser-url-changed`` event to the SPA's listeners."""
    page.evaluate(
        """([viewId, url]) => {
          for (const listener of window.__browserUrlListeners || []) {
            listener({ conversationId: viewId, url });
          }
        }""",
        [view_id, url],
    )


def _browser_view_id(rail: Locator) -> str:
    """The native view id of the browser pane currently mounted in the rail."""
    view_id = rail.locator("[data-browser-pane-conversation]").get_attribute(
        "data-browser-pane-conversation"
    )
    assert view_id
    return view_id


def test_browser_is_soft_tab_only_and_opens_pane(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Under the Electron stub, Browser opens only as a soft tab.

    Asserts the desktop-only chain end to end:

    1. No permanent Browser navigation tab is present.
    2. ``+`` offers Browser and creates it as the last, closable soft tab.
    3. Selecting it mounts the browser pane region.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.add_init_script(_ELECTRON_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # (1) Browser has no permanent icon-only navigation tab.
    expect(rail.get_by_role("tab", name="Browser", exact=True)).to_have_count(0)

    # (2) The new-tab menu creates the Browser soft tab.
    rail.get_by_role("button", name="Open new", exact=True).click()
    page.get_by_role("menuitem", name="Browser", exact=True).click()
    browser_tab = rail.get_by_role("tab", name="New tab", exact=True)
    expect(browser_tab).to_be_visible()

    # It is the LAST tab. Read every rail tab's accessible name in DOM order.
    tab_names = rail.get_by_role("tab").all_inner_texts()
    assert tab_names, "expected at least one Workspace rail tab"
    assert re.search("New tab", tab_names[-1]), (
        f"Browser tab must be last; rail tab order was {tab_names!r}"
    )

    # (3) The soft tab is selected and mounts the browser content region.
    expect(browser_tab).to_have_attribute("aria-selected", "true")


def test_no_browser_tab_in_plain_browser(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A plain browser tab (no Electron bridge) never shows the Browser tab.

    Without the ``window.omnigentDesktop`` stub, ``supportsBrowser()`` is
    false, so ``AppShell`` marks the Browser rail tab unavailable and it must
    not render — the gate that keeps the embedded browser off the plain web
    app (there is no WebContentsView to host). This is the half of the
    contract only an end-to-end browser run can prove.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # The Agents tab proves the rail rendered; the new-tab menu must not offer
    # Browser in a non-Electron shell.
    expect(rail.get_by_role("tab", name=re.compile("Agents"))).to_be_visible()
    rail.get_by_role("button", name="Open new", exact=True).click()
    expect(page.get_by_role("menuitem").first).to_be_visible()
    expect(page.get_by_role("menuitem", name="Browser", exact=True)).to_have_count(0)


def test_multiple_browser_soft_tabs(page: Page, seeded_session: tuple[str, str]) -> None:
    """User-created browser tabs select independent views and close individually."""
    base_url, session_id = seeded_session
    page.add_init_script(_ELECTRON_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    browser_tabs = rail.get_by_role("tab", name="New tab", exact=True)

    for count in (1, 2):
        rail.get_by_role("button", name="Open new", exact=True).click()
        page.get_by_role("menuitem", name="Browser", exact=True).click()
        expect(browser_tabs).to_have_count(count)
        expect(browser_tabs.nth(count - 1)).to_have_attribute("aria-selected", "true")

    second_view = _browser_view_id(rail)
    browser_tabs.first.click()
    first_view = _browser_view_id(rail)
    assert first_view != second_view
    assert first_view != session_id
    browser_tabs.nth(1).click()
    expect(rail.locator("[data-browser-pane-conversation]")).to_have_attribute(
        "data-browser-pane-conversation", second_view
    )

    page.reload()
    expect(browser_tabs.nth(1)).to_have_attribute("aria-selected", "true")
    rail.get_by_role("button", name="Close New tab", exact=True).nth(1).click()
    expect(browser_tabs).to_have_count(1)
    expect(browser_tabs.first).to_have_attribute("aria-selected", "true")
    rail.get_by_role("button", name="Close New tab", exact=True).click()
    expect(browser_tabs).to_have_count(0)


def test_browser_tab_names_follow_url_until_renamed(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Tabs read their URL; a manual name sticks through navigation and reload."""
    base_url, session_id = seeded_session
    page.add_init_script(_ELECTRON_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    rail.get_by_role("button", name="Open new", exact=True).click()
    page.get_by_role("menuitem", name="Browser", exact=True).click()
    view_id = _browser_view_id(rail)
    _emit_browser_url(page, view_id, "https://docs.example.com/guide/install?ref=nav")
    tab = rail.get_by_role("tab", name="docs.example.com/guide/install", exact=True)
    expect(tab).to_be_visible()
    expect(
        rail.get_by_role("button", name="Close docs.example.com/guide/install", exact=True)
    ).to_be_visible()

    # Rename from the context menu; the closing menu must not steal the field's focus.
    tab.click(button="right")
    page.get_by_role("menuitem", name="Rename", exact=True).click()
    name_field = rail.get_by_role("textbox", name="Browser tab name")
    expect(name_field).to_be_focused()
    name_field.fill("Install guide")
    name_field.press("Enter")
    renamed = rail.get_by_role("tab", name="Install guide", exact=True)
    expect(renamed).to_be_visible()

    # The tooltip shows the new URL once the event has rendered; the name stays.
    _emit_browser_url(page, view_id, "https://docs.example.com/guide/upgrade")
    expect(renamed.locator("xpath=..")).to_have_attribute(
        "title", "https://docs.example.com/guide/upgrade"
    )
    expect(renamed).to_be_visible()
    page.reload()
    expect(renamed).to_be_visible()

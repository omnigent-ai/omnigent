"""E2E: the embedded-browser "Browser" tab in the right Workspace rail.

The browser pane is desktop-only: ``AppShell`` marks the Browser rail tab
available when ``isElectronShell()`` is true, i.e. when the Electron preload
exposes ``window.omnigentDesktop.kind === "electron"``
(``web/src/lib/nativeBridge.ts``). The tab is deliberately the LAST tab in the
rail (Files · Agents · Shells · Tasks · Browser).

The e2e_ui harness runs the SPA in a plain Chromium browser, not Electron, so
by default the tab is absent. To exercise the desktop path end-to-end we inject
a minimal ``window.omnigentDesktop`` stub via ``add_init_script`` *before any
app script runs* — the same feature-detection stubbing
``sessions/test_pinned_session_hotkeys.py`` uses. That covers the chain the
component/unit tests can't reach end to end: the injected bridge ->
``isElectronShell()`` -> ``AppShell`` marking the tab available -> the
``WorkspacePanel`` rendering it last -> selecting it mounting the pane.

No LLM turn is involved; the assertions are DOM-based.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# Minimal stand-in for the Electron preload bridge. Runs before any app script
# on every navigation (add_init_script), so the SPA's feature detection
# (``electronApi()`` in nativeBridge.ts, which checks
# ``window.omnigentDesktop?.kind === "electron"``) sees a native shell. Every
# method the web layer may call is a guarded no-op: ``kind`` is what gates the
# Browser tab, and the rest keep unrelated native calls (badge, notify, the
# title-bar server picker, and the browser-pane bridge probes) from throwing
# under the stub. ``browserHasView`` resolves "no view yet" so the pane shows
# its empty state instead of trying to attach a native WebContentsView.
_ELECTRON_SHELL_INIT_SCRIPT = """
window.omnigentDesktop = {
  kind: "electron",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  getServerPicker: function () { return Promise.resolve(null); },
  switchServer: function () { return Promise.resolve(); },
  openServerSetup: function () {},
  browserHasView: function () { return Promise.resolve({ exists: false }); },
  onBrowserViewCreated: function () { return function () {}; },
  onBrowserHostActiveChanged: function () { return function () {}; },
  onBrowserViewClosed: function () { return function () {}; },
  onBrowserUrlChanged: function () { return function () {}; },
  onBrowserNavState: function () { return function () {}; },
};
"""


def test_browser_tab_is_last_and_opens_pane(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Under the Electron stub, the Browser tab shows LAST and opens the pane.

    Asserts the desktop-only chain end to end:

    1. With the Electron bridge stubbed, the "Browser" tab appears in the
       Workspace rail (``isElectronShell()`` -> tab available).
    2. It is the LAST tab in the rail (the deliberate ordering — after
       Files / Agents / Shells / Tasks).
    3. Selecting it mounts the browser pane region.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.add_init_script(_ELECTRON_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Ask the agent anything…")).to_be_visible()

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # (1) The Browser tab is present under the Electron stub.
    browser_tab = rail.get_by_role("tab", name=re.compile("Browser"))
    expect(browser_tab).to_be_visible()

    # (2) It is the LAST tab. Read every rail tab's accessible name in DOM
    # order and confirm "Browser" is the final entry.
    tab_names = rail.get_by_role("tab").all_inner_texts()
    assert tab_names, "expected at least one Workspace rail tab"
    assert re.search("Browser", tab_names[-1]), (
        f"Browser tab must be last; rail tab order was {tab_names!r}"
    )

    # (3) Selecting it mounts the pane. The tab becomes the selected one
    # (aria-selected), which is what drives WorkspacePanel to render the
    # browser content region.
    browser_tab.click()
    expect(browser_tab).to_have_attribute("aria-selected", "true")


def test_no_browser_tab_in_plain_browser(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A plain browser tab (no Electron bridge) never shows the Browser tab.

    Without the ``window.omnigentDesktop`` stub, ``isElectronShell()`` is
    false, so ``AppShell`` marks the Browser rail tab unavailable and it must
    not render — the gate that keeps the embedded browser off the plain web
    app (there is no WebContentsView to host). This is the half of the
    contract only an end-to-end browser run can prove.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Ask the agent anything…")).to_be_visible()

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # The Files/Agents tabs prove the rail rendered; the Browser tab must be
    # absent (not merely hidden) in a non-Electron shell.
    expect(rail.get_by_role("tab", name=re.compile("Agents"))).to_be_visible()
    expect(rail.get_by_role("tab", name=re.compile("Browser"))).to_have_count(0)

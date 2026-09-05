"""The desktop window must stay movable (draggable) on the sign-in screens.

On macOS the Electron shell hides the native title bar (``titleBarStyle:
"hiddenInset"`` in ``web/electron/src/main.js``), so the web page is the
window's ONLY drag surface: whatever screen is showing must carry a
``-webkit-app-region: drag`` region or the user cannot move the window at all.
The signed-in ``AppShell`` provides one (``.electron-drag-strip``, gated on
``isMacElectronShell()``), and the bundled setup page carries its own
``.drag-strip`` — but ``/login`` and ``/register`` sit OUTSIDE the AppShell
route tree (``web/src/App.tsx``) and own minimal layouts, so they must render
their own drag region (``ElectronWindowDragStrip``).

Journey (the user-study report): install the desktop app on macOS → connect it
to an accounts-gated (shared) server → the 401 redirect lands on the Sign in
screen → try to drag the window → the window does not move (frozen).

These tests pin that invariant in the browser lane: a real accounts-mode
server (so ``/login`` / ``/register`` are actually routed) driven with the two
signals ``isMacElectronShell()`` sniffs — a Macintosh user agent and the
``window.omnigentDesktop`` preload bridge. The assertion is that each auth
screen exposes at least one *visible* draggable window region; zero regions is
exactly the frozen window. A control test pins the signed-in shell's drag
strip so the probe machinery itself stays honest.

The OS-level symptom (the window not following the mouse) only exists on a
macOS frameless window, which this harness cannot host — but the invariant
"every screen a frameless window can show carries a drag region" is fully
observable here, and is what a fix must restore.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Browser, Page

from tests.e2e_ui.auth._accounts_server import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    AccountsServer,
    spawn_accounts_server,
)

# The desktop shell's renderer UA: Chromium-on-macOS plus the Electron token.
# ``isMacElectronShell()`` (web/src/lib/nativeBridge.ts) requires "Macintosh"
# in the UA; the rest mirrors what a packaged mac build reports.
_MAC_ELECTRON_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) omnigent-desktop/1.0.0 Chrome/126.0.0.0 "
    "Electron/31.0.0 Safari/537.36"
)

# The Electron preload bridge surface ``isElectronShell()`` detects
# (``window.omnigentDesktop`` with ``kind: "electron"``), with no-op stubs for
# the calls the SPA chrome makes during boot. Same shape the existing desktop
# -bridge e2e tests stub (see sessions/test_settings_back_navigation.py).
_ELECTRON_BRIDGE_STUB = """
window.omnigentDesktop = {
  kind: "electron",
  setBadgeCount() {},
  notify() { return Promise.resolve(true); },
  onOpenPath() { return () => {}; },
};
"""

# Every visible element whose computed style makes it a window-drag handle.
# ``app-region`` is the standardized name; ``webkitAppRegion`` covers Chromium
# versions that only expose the prefixed form. Zero-sized elements are
# excluded — a collapsed drag region cannot be grabbed.
_VISIBLE_DRAG_REGIONS_JS = """
() => Array.from(document.querySelectorAll("*"))
  .filter((el) => {
    const style = getComputedStyle(el);
    const region =
      (style.getPropertyValue("app-region") || style.webkitAppRegion || "").trim();
    if (region !== "drag") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  })
  .map((el) => `${el.tagName.toLowerCase()}.${el.className}`)
"""


@pytest.fixture(scope="module")
def accounts_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[AccountsServer]:
    """An accounts-mode server, so ``/login`` / ``/register`` are routed.

    The suite's shared ``live_server`` runs single-user with auth disabled, so
    its route table omits the auth pages entirely (``accounts_enabled`` false).
    """
    server_tmp = tmp_path_factory.mktemp("e2e_ui_window_drag")
    yield from spawn_accounts_server(mock_llm_server_url, server_tmp)


@pytest.fixture
def mac_desktop_page(
    browser: Browser,
    browser_context_args: dict[str, Any],
) -> Iterator[Page]:
    """A page presenting as the macOS Electron desktop shell.

    Both signals ``isMacElectronShell()`` checks are supplied: the Macintosh
    user agent (context option) and the ``omnigentDesktop`` preload bridge
    (init script). The plugin's context args are spread so --video/--tracing
    keep working even though this builds its own context for the UA.
    """
    context = browser.new_context(
        **browser_context_args,
        user_agent=_MAC_ELECTRON_USER_AGENT,
    )
    page = context.new_page()
    page.add_init_script(_ELECTRON_BRIDGE_STUB)
    yield page
    context.close()


def _visible_drag_regions(page: Page) -> list[str]:
    """Descriptors of every visible window-drag region on the current page."""
    return page.evaluate(_VISIBLE_DRAG_REGIONS_JS)


@pytest.mark.parametrize(
    ("path", "ready_selector"),
    [
        # The 401 redirect target: what a desktop user connecting to a shared
        # accounts-gated server lands on first.
        pytest.param("/login", "#login-username", id="login"),
        # The invite-redemption page a brand-new member opens from their
        # invite link (rendered here in its no-invite state; the layout —
        # and its missing drag region — is the same either way).
        pytest.param("/register", "[role=alert]", id="register"),
    ],
)
def test_auth_screens_offer_window_drag_surface(
    accounts_server: AccountsServer,
    mac_desktop_page: Page,
    path: str,
    ready_selector: str,
) -> None:
    """Each auth screen must expose a draggable window region on mac Electron.

    With the native title bar hidden, a screen with zero ``app-region: drag``
    elements leaves the desktop window impossible to move — the "frozen
    window". Guards the ElectronWindowDragStrip each auth layout renders.
    """
    page = mac_desktop_page
    page.goto(f"{accounts_server.public_url}{path}")
    page.wait_for_selector(ready_selector, timeout=30_000)

    regions = _visible_drag_regions(page)
    assert regions, (
        f"{path} renders no visible `-webkit-app-region: drag` element on the "
        "macOS Electron shell. The shell hides the native title bar "
        "(titleBarStyle 'hiddenInset'), so without an in-page drag region the "
        "desktop window cannot be moved at all."
    )


def test_signed_in_shell_offers_window_drag_surface(
    accounts_server: AccountsServer,
    mac_desktop_page: Page,
) -> None:
    """Control: the signed-in AppShell exposes its title-bar drag strip.

    Passes today. Pins the working half of the journey — sign in through the
    real form and land in the shell — so the drag-region probe above is known
    to detect a strip when one exists, and so a regression that drops the
    shell's own strip is caught too.
    """
    page = mac_desktop_page
    page.goto(f"{accounts_server.public_url}/login")
    page.wait_for_selector("#login-username", timeout=30_000)
    page.fill("#login-username", ADMIN_USERNAME)
    page.fill("#login-password", ADMIN_PASSWORD)
    page.get_by_role("button", name="Sign in").click()

    # A successful login hard-navigates into the AppShell, which renders the
    # macOS title-bar drag strip (gated on isMacElectronShell()).
    page.wait_for_selector(".electron-drag-strip", state="attached", timeout=30_000)

    regions = _visible_drag_regions(page)
    assert any("electron-drag-strip" in region for region in regions), (
        f"signed-in shell lost its window-drag strip; visible drag regions: {regions}"
    )

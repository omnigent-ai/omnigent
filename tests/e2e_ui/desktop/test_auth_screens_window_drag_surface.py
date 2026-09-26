"""The desktop window must stay movable (draggable) on the sign-in screens.

On macOS the Electron shell hides the native title bar (``titleBarStyle:
"hiddenInset"`` in ``web/electron/src/main.js``), so the web page is the
window's ONLY drag surface: whatever screen is showing must carry a visible
``-webkit-app-region: drag`` element or the user cannot move the window at
all. The signed-in ``AppShell`` renders one (``.electron-drag-strip``, gated
on ``isMacElectronShell()``); ``/login`` and ``/register`` mount OUTSIDE the
AppShell route tree (``web/src/App.tsx``), so each must render its own
(``ElectronWindowDragStrip``) — without one, connecting the desktop app to an
accounts-gated (shared) server lands on a Sign in screen where the window is
frozen.

The OS-level symptom (the window not following the mouse) only exists on a
macOS frameless window, which this harness cannot host. The tests instead pin
the fully observable invariant behind it — every screen the frameless window
can show carries at least one visible drag region — against a real
accounts-mode server (so ``/login`` / ``/register`` are actually routed),
driven with the two signals ``isMacElectronShell()`` sniffs: a Macintosh user
agent and the ``window.omnigentDesktop`` preload bridge.
"""

from __future__ import annotations

import os
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

# What a packaged mac desktop build's renderer reports; isMacElectronShell()
# requires the "Macintosh" token.
_MAC_ELECTRON_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) omnigent-desktop/1.0.0 Chrome/126.0.0.0 "
    "Electron/31.0.0 Safari/537.36"
)

# The preload bridge surface isElectronShell() detects, with no-op stubs for
# the calls the SPA chrome makes during boot.
_ELECTRON_BRIDGE_STUB = """
window.omnigentDesktop = {
  kind: "electron",
  setBadgeCount() {},
  notify() { return Promise.resolve(true); },
  onOpenPath() { return () => {}; },
};
"""

# Every visible element whose computed style makes it a window-drag handle.
# `app-region` is the standardized name; `webkitAppRegion` covers Chromium
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
    its route table omits the auth pages entirely.
    """
    server_tmp = tmp_path_factory.mktemp("e2e_ui_window_drag")
    yield from spawn_accounts_server(mock_llm_server_url, server_tmp)


@pytest.fixture
def mac_desktop_page(
    browser: Browser,
    browser_context_args: dict[str, Any],
) -> Iterator[Page]:
    """A page presenting as the macOS Electron desktop shell.

    The plugin's context args are spread first so the UA override composes
    with them; OMNIGENT_E2E_RECORD_DIR is honored directly because the
    conftest's recording hook only patches the async Browser API.
    """
    context_args: dict[str, Any] = {
        **browser_context_args,
        "user_agent": _MAC_ELECTRON_USER_AGENT,
    }
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        context_args["record_video_dir"] = record_dir
    context = browser.new_context(**context_args)
    page = context.new_page()
    page.add_init_script(_ELECTRON_BRIDGE_STUB)
    yield page
    context.close()


def _visible_drag_regions(page: Page) -> list[str]:
    return page.evaluate(_VISIBLE_DRAG_REGIONS_JS)


def _attempt_window_drag(page: Page) -> None:
    """The reported user action: grab the window's top edge and pull.

    Paced like a real gesture so a journey recording shows the attempt.
    """
    page.mouse.move(400, 10)
    page.mouse.down()
    for x in range(420, 700, 40):
        page.mouse.move(x, 14)
        page.wait_for_timeout(80)
    page.mouse.up()
    page.wait_for_timeout(1_200)


@pytest.mark.parametrize(
    ("path", "ready_selector"),
    [
        # The 401 redirect target: what a desktop user connecting to a shared
        # accounts-gated server lands on first.
        pytest.param("/login", "#login-username", id="login"),
        # The invite-redemption page (rendered here in its no-invite state;
        # the layout — and its missing drag region — is the same either way).
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

    With the native title bar hidden, a screen with zero visible
    ``app-region: drag`` elements leaves the desktop window impossible to
    move — the "frozen window" from the user study.
    """
    page = mac_desktop_page
    page.goto(f"{accounts_server.public_url}{path}")
    page.wait_for_selector(ready_selector, timeout=30_000)

    _attempt_window_drag(page)

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

    Passes today. Proves the drag-region probe detects a strip when one
    exists, and guards the shell's own strip against regressing too.
    """
    page = mac_desktop_page
    page.goto(f"{accounts_server.public_url}/login")
    page.wait_for_selector("#login-username", timeout=30_000)
    page.fill("#login-username", ADMIN_USERNAME)
    page.fill("#login-password", ADMIN_PASSWORD)
    page.get_by_role("button", name="Sign in").click()

    # A successful login lands in the AppShell, which renders the macOS
    # title-bar drag strip (gated on isMacElectronShell()).
    page.wait_for_selector(".electron-drag-strip", state="attached", timeout=30_000)

    regions = _visible_drag_regions(page)
    assert any("electron-drag-strip" in region for region in regions), (
        f"signed-in shell lost its window-drag strip; visible drag regions: {regions}"
    )

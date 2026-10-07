"""The desktop window must stay movable on the screens shown outside the AppShell.

On macOS the Electron shell hides the native title bar (``titleBarStyle:
"hiddenInset"``), so the page is the window's only drag surface: a screen with
no visible ``-webkit-app-region: drag`` element under the top band leaves the
window impossible to move. The signed-in AppShell renders ``.electron-drag-strip``;
``/login``, ``/register`` and ``/approve`` mount outside it (``web/src/App.tsx``).

The frameless window itself needs macOS, so these tests pin the observable
invariants behind the symptom against a real accounts-mode server, presenting
as the mac shell through the two signals ``isMacElectronShell()`` sniffs: a
visible drag region covers the grab point in the top band, and every
interactive control keeps the ``no-drag`` counter-region so it stays clickable.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e_ui.auth._accounts_server import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    AccountsServer,
    spawn_accounts_server,
)

_MAC_ELECTRON_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) omnigent-desktop/1.0.0 Chrome/126.0.0.0 "
    "Electron/31.0.0 Safari/537.36"
)

_ELECTRON_BRIDGE_STUB = """
window.omnigentDesktop = {
  kind: "electron",
  setBadgeCount() {},
  notify() { return Promise.resolve(true); },
  onOpenPath() { return () => {}; },
};
"""

_APP_REGION_JS = """
(el) => {
  const style = getComputedStyle(el);
  return (style.getPropertyValue("app-region") || style.webkitAppRegion || "none").trim();
}
"""

# Where a user grabs the window: inside the 2.25rem title-bar band, clear of the
# traffic lights. The strip has pointer-events: none, so elementFromPoint would
# skip it; drag regions are matched by geometry instead.
_GRAB_POINT = (400, 10)

# The first visible drag region whose box covers the point, or null.
_DRAG_REGION_AT_JS = f"""
([x, y]) => {{
  const regionOf = {_APP_REGION_JS};
  for (const el of document.querySelectorAll("*")) {{
    if (regionOf(el) !== "drag") continue;
    const r = el.getBoundingClientRect();
    const covers = r.left <= x && x <= r.right && r.top <= y && y <= r.bottom;
    if (r.width > 0 && r.height > 0 && covers) {{
      const size = `${{Math.round(r.width)}}x${{Math.round(r.height)}}`;
      return `${{el.tagName.toLowerCase()}}.${{el.getAttribute("class") || ""}} ${{size}}`;
    }}
  }}
  return null;
}}
"""

# Interactive controls missing the no-drag counter-region (they would move the
# window instead of taking the click wherever they overlap a drag region).
_DRAGGABLE_CONTROLS_JS = f"""
() => Array.from(document.querySelectorAll('a, button, input, textarea, [role="button"]'))
  .filter((el) => ({_APP_REGION_JS})(el) !== "no-drag")
  .map((el) => el.tagName.toLowerCase() + (el.id ? `#${{el.id}}` : ""))
"""


@pytest.fixture(scope="module")
def accounts_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[AccountsServer]:
    """An accounts-mode server: the shared single-user ``live_server`` has no ``/login`` route."""
    yield from spawn_accounts_server(
        mock_llm_server_url, tmp_path_factory.mktemp("e2e_ui_window_drag")
    )


@pytest.fixture
def mac_desktop_page(browser: Browser, browser_context_args: dict[str, Any]) -> Iterator[Page]:
    context_args: dict[str, Any] = {
        **browser_context_args,
        "user_agent": _MAC_ELECTRON_USER_AGENT,
        "viewport": {"width": 1280, "height": 860},
    }
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        context_args["record_video_dir"] = record_dir
    context = browser.new_context(**context_args)
    page = context.new_page()
    page.add_init_script(_ELECTRON_BRIDGE_STUB)
    yield page
    context.close()


def _drag_region_at(page: Page, point: tuple[int, int]) -> str | None:
    return page.evaluate(_DRAG_REGION_AT_JS, list(point))


def _draggable_controls(page: Page) -> list[str]:
    return page.evaluate(_DRAGGABLE_CONTROLS_JS)


def _snapshot(page: Page, name: str) -> None:
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        page.screenshot(path=str(Path(record_dir) / f"{name}.png"))


def _sign_in(page: Page, server: AccountsServer) -> None:
    page.goto(f"{server.public_url}/login")
    page.wait_for_selector("#login-username", timeout=30_000)
    page.fill("#login-username", ADMIN_USERNAME)
    page.fill("#login-password", ADMIN_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page).not_to_have_url(re.compile(r"/login"), timeout=30_000)


def _assert_drag_surface(page: Page, screen: str) -> str:
    """Assert the screen is draggable by its top band without trapping its controls.

    :returns: A description of the drag region covering the grab point.
    """
    region = _drag_region_at(page, _GRAB_POINT)
    _snapshot(page, screen)
    assert region, (
        f"{screen} renders no visible `-webkit-app-region: drag` element covering the "
        f"top band at {_GRAB_POINT}; with the native title bar hidden on the macOS shell "
        "the window cannot be moved from this screen."
    )
    draggable_controls = _draggable_controls(page)
    assert not draggable_controls, (
        f"{screen}: these interactive controls lack the no-drag counter-region, so they "
        f"would move the window instead of taking clicks: {draggable_controls}"
    )
    return region


def test_sign_in_screen_offers_window_drag_surface(
    accounts_server: AccountsServer, mac_desktop_page: Page
) -> None:
    page = mac_desktop_page
    page.goto(f"{accounts_server.public_url}/login")
    page.wait_for_selector("#login-username", timeout=30_000)
    _assert_drag_surface(page, "sign-in")


def test_register_screen_offers_window_drag_surface(
    accounts_server: AccountsServer, mac_desktop_page: Page
) -> None:
    """Any invite token shows the form; the server only validates it on submit."""
    page = mac_desktop_page
    page.goto(f"{accounts_server.public_url}/register?invite=e2e-window-drag")
    page.wait_for_selector("#register-username", timeout=30_000)
    _assert_drag_surface(page, "register")


def test_approve_screen_offers_window_drag_surface(
    accounts_server: AccountsServer, mac_desktop_page: Page
) -> None:
    """Signing in first also exercises the form's controls under the sign-in strip."""
    page = mac_desktop_page
    _sign_in(page, accounts_server)
    page.goto(f"{accounts_server.public_url}/approve/no-such-session/no-such-elicitation")
    page.wait_for_selector("[role=alert]", timeout=30_000)
    _assert_drag_surface(page, "approve")


def test_signed_in_shell_offers_window_drag_surface(
    accounts_server: AccountsServer, mac_desktop_page: Page
) -> None:
    """Control: the AppShell's own strip satisfies the same probe."""
    page = mac_desktop_page
    _sign_in(page, accounts_server)
    page.wait_for_selector(".electron-drag-strip", state="attached", timeout=30_000)
    region = _assert_drag_surface(page, "signed-in-shell")
    assert "electron-drag-strip" in region, region

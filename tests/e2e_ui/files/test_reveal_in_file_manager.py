"""E2E: right-click a local file or folder to show it in the OS file manager.

The action is desktop-only and local-only: it appears when the SPA runs in the
desktop shell (an injected ``omnigentDesktop`` bridge) and the session's host is
this machine. A plain browser tab keeps the browser's own context menu.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry, open_right_rail

_FILE = "reveal-me.txt"
_FOLDER = "reveal-dir"
_HOST_ID = "host_this_machine"

_DESKTOP_BRIDGE = f"""
window.__revealCalls = [];
window.omnigentDesktop = {{
  kind: "electron",
  setBadgeCount: () => {{}},
  notify: () => Promise.resolve(false),
  onNotificationActivated: () => () => {{}},
  getServerPicker: () => Promise.resolve(null),
  switchServer: () => Promise.resolve(),
  openServerSetup: () => {{}},
  onHostStatusChanged: () => () => {{}},
  getDesktopFeatures: () => Promise.resolve(null),
  getHostIdentity: () => Promise.resolve({{ cliInstalled: true, hostId: {_HOST_ID!r} }}),
  revealFile: (hostId, path) => {{
    window.__revealCalls.push([hostId, path]);
    return Promise.resolve(true);
  }},
}};
"""


@pytest.fixture(autouse=True)
def _drop_routes(page: Page) -> Iterator[None]:
    yield
    page.unroute_all(behavior="ignoreErrors")


@pytest.fixture
def workspace(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str, str]]:
    """Seed a file and a folder; yield ``(base_url, session_id, workspace_root)``."""
    base_url, session_id = seeded_session
    environment = f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    for path in (_FILE, f"{_FOLDER}/inside.txt"):
        httpx.put(
            f"{environment}/filesystem/{path}",
            json={"content": "hi\n", "encoding": "utf-8"},
            timeout=30.0,
        ).raise_for_status()
    root = httpx.get(environment, timeout=30.0).json()["metadata"]["root"].rstrip("/")
    yield base_url, session_id, root
    httpx.delete(f"{environment}/filesystem/{_FILE}", timeout=10.0)
    httpx.delete(f"{environment}/filesystem/{_FOLDER}", params={"recursive": "true"}, timeout=10.0)


def _as_local_desktop(page: Page, session_id: str) -> None:
    """Inject the desktop bridge and bind the session to its host id."""
    page.add_init_script(_DESKTOP_BRIDGE)

    def bind(route: Route) -> None:
        if urlparse(route.request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
        route.fulfill(response=response, json={**response.json(), "host_id": _HOST_ID})

    page.route(f"**/v1/sessions/{session_id}*", bind)


def _files_rail(page: Page, base_url: str, session_id: str) -> Locator:
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    return rail


def test_reveal_local_file_and_folder(page: Page, workspace: tuple[str, str, str]) -> None:
    base_url, session_id, root = workspace
    _as_local_desktop(page, session_id)
    rail = _files_rail(page, base_url, session_id)

    rail.get_by_text(_FILE, exact=True).click(button="right")
    page.get_by_role("menuitem", name=re.compile(r"^Show in ")).click()
    expect(rail.get_by_test_id("file-viewer")).to_have_count(0)

    folder = rail.get_by_role("button", name=f"{_FOLDER}/", exact=True)
    expanded = folder.get_attribute("aria-expanded")
    folder.click(button="right")
    page.get_by_role("menuitem", name=re.compile(r"^Open in ")).click()
    assert folder.get_attribute("aria-expanded") == expanded

    page.wait_for_function("() => window.__revealCalls.length === 2")
    assert page.evaluate("() => window.__revealCalls") == [
        [_HOST_ID, f"{root}/{_FILE}"],
        [_HOST_ID, f"{root}/{_FOLDER}"],
    ]


def test_browser_tab_keeps_native_menu(page: Page, workspace: tuple[str, str, str]) -> None:
    base_url, session_id, _root = workspace
    rail = _files_rail(page, base_url, session_id)

    row = rail.get_by_text(_FILE, exact=True)
    expect(row).to_be_visible(timeout=30_000)
    row.click(button="right")
    expect(page.get_by_role("menuitem")).to_have_count(0)

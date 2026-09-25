"""E2E: local files and folders can be revealed in the OS file manager.

A desktop user wants to jump from a file the agent produced to Finder (or the
platform's file manager) -- reveal-and-select for a file, open for a folder --
instead of copying a path and hunting it down by hand. The Files rail's rows
used to offer only Download / Copy path on hover, right-clicking a row opened
nothing, and the file viewer's menu listed only Find in file / Download file.

The reveal is desktop-gated: it must appear only when the SPA runs in the
desktop shell and the session's files live on the viewing machine (the
session's host id equals the shell's host identity), because a browser tab
cannot open an OS file manager and a remote host's path must never resolve
against the viewing machine. These tests drive the real SPA with the injected
desktop bridge the suite uses for Electron-only journeys (see
``test_reconnect_local_host_from_app.py``), bind the session to that bridge's
host identity, and look for the action where a user would: the row's
right-click menu and the open file's toolbar / "View settings" menu. They then
invoke it and check the shell is asked to reveal the item's absolute path.
The plain-browser case (copy actions, no reveal) is covered by
``test_file_link_sharing.py``.

The file and folder are written through the session's filesystem API, so the
runner that lists the workspace is the one that creates them; no agent turn is
involved.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e_ui.conftest import fetch_with_retry, open_right_rail

_FILE_NAME = "reveal-me.txt"
_FOLDER_NAME = "reveal-dir"

# The host id the injected bridge reports for this machine; the session
# snapshot is bound to the same id so the session's files count as local.
_THIS_MACHINE_HOST_ID = "host_this_machine"

_REVEAL_ACTION = re.compile(
    r"(open|show|reveal) in (finder|file manager|file explorer|explorer|folder)", re.I
)

# Electron preload stub: makes the SPA detect the desktop shell, reports this
# machine's host identity, and records revealFile() calls so the test can
# assert what the shell was asked to reveal. Runs before any app script.
_DESKTOP_BRIDGE_INIT_SCRIPT = f"""
window.__revealCalls = [];
window.omnigentDesktop = {{
  kind: "electron",
  setBadgeCount: function () {{}},
  notify: function () {{ return Promise.resolve(false); }},
  onNotificationActivated: function () {{ return function () {{}}; }},
  getServerPicker: function () {{ return Promise.resolve(null); }},
  switchServer: function () {{ return Promise.resolve(); }},
  openServerSetup: function () {{}},
  getHostIdentity: function () {{
    return Promise.resolve({{ cliInstalled: true, hostId: {_THIS_MACHINE_HOST_ID!r} }});
  }},
  revealFile: function (hostId, path) {{
    window.__revealCalls.push([hostId, path]);
    return Promise.resolve(true);
  }},
  onHostStatusChanged: function () {{ return function () {{}}; }},
  getDesktopFeatures: function () {{ return Promise.resolve(null); }},
}};
"""


@pytest.fixture(autouse=True)
def _drop_routes(page: Page) -> Iterator[None]:
    """Unroute this module's handlers before the page closes.

    A snapshot refetch still in flight while the page tears down would
    otherwise fail the replay with ``TargetClosedError``.
    """
    yield
    page.unroute_all(behavior="ignoreErrors")


@pytest.fixture
def local_desktop_workspace(
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> tuple[str, str, str]:
    """Seed a file and a folder and make the SPA see them as this machine's.

    Injects the desktop bridge stub and rewrites the session snapshot's
    ``host_id`` to the bridge's host identity -- the condition under which
    the reveal action must be offered.

    :returns: ``(base_url, session_id, workspace_root)`` where the root is the
        workspace's absolute path on the machine hosting its files.
    """
    base_url, session_id = seeded_session
    filesystem = f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem"
    seeds = (
        (_FILE_NAME, "Find me in the file manager.\n"),
        (f"{_FOLDER_NAME}/inside.txt", "nested\n"),
    )
    # PUT through the runner (parents auto-created), so seeding works whether
    # or not the runner shares this process's filesystem.
    for path, content in seeds:
        resp = httpx.put(
            f"{filesystem}/{path}", json={"content": content, "encoding": "utf-8"}, timeout=30.0
        )
        resp.raise_for_status()

    def _cleanup() -> None:
        httpx.delete(f"{filesystem}/{_FILE_NAME}", timeout=10.0)
        httpx.delete(f"{filesystem}/{_FOLDER_NAME}", params={"recursive": "true"}, timeout=10.0)

    request.addfinalizer(_cleanup)

    env = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default", timeout=30.0
    )
    env.raise_for_status()
    root = str(env.json()["metadata"]["root"]).rstrip("/")

    page.add_init_script(_DESKTOP_BRIDGE_INIT_SCRIPT)

    def _bind_snapshot_to_this_machine(route: Route) -> None:
        request_ = route.request
        if request_.method != "GET" or urlparse(request_.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["host_id"] = _THIS_MACHINE_HOST_ID
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route(f"**/v1/sessions/{session_id}*", _bind_snapshot_to_this_machine)
    return base_url, session_id, root


def _open_files_tab(page: Page, base_url: str, session_id: str) -> Locator:
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    return rail


def _control_names(scope: Locator) -> list[str]:
    """Accessible names of every button in ``scope``, hover-revealed ones included."""
    return scope.get_by_role("button").evaluate_all(
        "els => els.map(el => (el.getAttribute('aria-label') ?? el.textContent).trim())"
    )


def _context_menu_items(page: Page, row: Locator) -> list[str]:
    """Right-click ``row`` and return the app context menu's items, if one opens."""
    row.click(button="right")
    menu = page.get_by_role("menu").first
    try:
        menu.wait_for(state="visible", timeout=3_000)
    except PlaywrightTimeoutError:
        return []
    items = menu.get_by_role("menuitem").all_inner_texts()
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    return [" ".join(item.split()) for item in items]


def _assert_reveal_calls(page: Page, expected: list[list[str]]) -> None:
    """Wait for the bridge to have been asked exactly for ``expected`` reveals."""
    with contextlib.suppress(PlaywrightTimeoutError):
        page.wait_for_function(
            "expected => JSON.stringify(window.__revealCalls) === expected",
            arg=json.dumps(expected),
            timeout=5_000,
        )
    calls = page.evaluate("() => window.__revealCalls")
    assert calls == expected, f"expected the shell to be asked to reveal {expected}, got {calls}"


def _offers_reveal(names: list[str]) -> bool:
    return any(_REVEAL_ACTION.search(name) for name in names)


def test_file_row_and_viewer_offer_reveal_in_file_manager(
    page: Page,
    local_desktop_workspace: tuple[str, str, str],
) -> None:
    """A local file's right-click menu and its open viewer both offer the OS reveal."""
    base_url, session_id, root = local_desktop_workspace
    rail = _open_files_tab(page, base_url, session_id)

    row = rail.get_by_role("button", name=re.compile(re.escape(_FILE_NAME))).filter(
        has_text=_FILE_NAME
    )
    expect(row).to_be_visible(timeout=30_000)
    row.hover()
    hover_actions = _control_names(row.locator("xpath=.."))
    context_items = _context_menu_items(page, row)
    # Opening the menu must not open the file.
    expect(rail.get_by_test_id("file-viewer")).to_have_count(0)

    row.click()
    viewer = rail.get_by_test_id("file-viewer")
    expect(viewer).to_be_visible()
    expect(rail.get_by_role("button", name=f"Close {_FILE_NAME}", exact=True)).to_be_visible()
    toolbar = _control_names(viewer)
    rail.get_by_role("button", name="View settings").or_(
        rail.get_by_role("button", name="More actions")
    ).first.click()
    menu = page.get_by_role("menu").first
    expect(menu).to_be_visible()
    viewer_items = [" ".join(t.split()) for t in menu.get_by_role("menuitem").all_inner_texts()]

    observed = {
        "row hover actions": hover_actions,
        "row context menu": context_items,
        "viewer toolbar": toolbar,
        "viewer settings menu": viewer_items,
    }
    assert _offers_reveal(context_items), (
        f"the file row's right-click menu offers no reveal-in-file-manager action; {observed}"
    )
    assert _offers_reveal(toolbar + viewer_items), (
        f"the file viewer offers no reveal-in-file-manager action; observed {observed}"
    )

    # Invoke it from the viewer menu (still open): the shell must be asked to
    # reveal the file's absolute path on this machine.
    menu.get_by_role("menuitem", name=_REVEAL_ACTION).first.click()
    expect(menu).to_be_hidden()
    _assert_reveal_calls(page, [[_THIS_MACHINE_HOST_ID, f"{root}/{_FILE_NAME}"]])


def test_folder_row_offers_reveal_in_file_manager(
    page: Page,
    local_desktop_workspace: tuple[str, str, str],
) -> None:
    """A local folder's right-click menu opens it in the OS file manager without toggling it."""
    base_url, session_id, root = local_desktop_workspace
    rail = _open_files_tab(page, base_url, session_id)

    row = rail.get_by_role("button", name=f"{_FOLDER_NAME}/", exact=True)
    expect(row).to_be_visible(timeout=30_000)
    expanded_before = row.get_attribute("aria-expanded")
    row.hover()
    hover_actions = _control_names(row.locator("xpath=.."))
    context_items = _context_menu_items(page, row)

    assert _offers_reveal(context_items), (
        "the folder row's right-click menu offers no reveal-in-file-manager action; "
        f"observed hover actions {hover_actions}, context menu {context_items}"
    )
    assert row.get_attribute("aria-expanded") == expanded_before, (
        "opening the folder's context menu must not toggle the folder"
    )

    row.click(button="right")
    item = page.get_by_role("menuitem", name=_REVEAL_ACTION).first
    expect(item).to_be_visible()
    item.click()
    _assert_reveal_calls(page, [[_THIS_MACHINE_HOST_ID, f"{root}/{_FOLDER_NAME}"]])

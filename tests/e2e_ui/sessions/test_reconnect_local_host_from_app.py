"""E2E: reconnect a disconnected LOCAL host from the desktop app.

When a session's host goes offline and that host is the user's own machine
(the "This Mac" connection the desktop shell can start itself), the Reconnect
action must PERFORM the reconnect in-app via the desktop bridge
(``window.omnigentDesktop.controlHost("start")``) -- the same bridge
``NewChatDialog``'s "Run on this machine" already drives -- rather than only
handing the user a copy-paste ``omnigent host`` command.

This drives the real user journey against a desktop-shell view: the SPA is the
same bytes the Electron shell loads, and the reconnect dialog keys the one-click
path off ``window.omnigentDesktop`` (kind ``electron`` + ``getHostIdentity`` +
``controlHost``), so injecting that bridge -- the established pattern in
``test_pinned_session_hotkeys.py`` / ``test_session_search.py`` -- reproduces the
exact dialog a real desktop shell renders for a local offline host.

The browser view is patched into a ``host_offline`` shape bound to THIS machine's
host id (same route-interception approach as ``test_host_badge.py``):

- ``GET /v1/sessions/{id}`` -> ``host_id`` set to this machine, ``host_resumable``
  false (a real laptop, not a dormant sandbox), old ``created_at`` (past the
  startup grace).
- ``GET /v1/hosts`` -> returns the bound host so the badge resolves its name.
- ``GET /health`` -> reports ``host_online`` false so the badge reads offline and
  becomes reconnectable.
- ``GET /v1/sessions`` (sidebar) -> drops the row so the open session resolves
  off the patched snapshot.
- ``WS /v1/sessions/updates`` -> blocked so a push can't revert liveness.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

# This machine's host id, as the injected desktop bridge reports it and as the
# patched session snapshot binds to -- so the offline host IS "This Mac".
_THIS_MACHINE_HOST_ID = "host_this_machine"
# Unix seconds well before now so the offline host is outside STARTING_GRACE_S
# and reads its real (offline) liveness rather than `starting`.
_OLD_CREATED_AT = 1_700_000_000

# Electron preload stub: makes isElectronShell() true, reports THIS machine as a
# CLI-installed host, and records controlHost() calls so the test can assert the
# in-app reconnect actually drove the desktop bridge. Runs before any app script.
_DESKTOP_BRIDGE_INIT_SCRIPT = f"""
window.__controlHostCalls = [];
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
  onHostStatusChanged: function () {{ return function () {{}}; }},
  controlHost: function (action) {{
    window.__controlHostCalls.push(action);
    return Promise.resolve({{ ok: true }});
  }},
  getDesktopFeatures: function () {{ return Promise.resolve(null); }},
}};
"""


@pytest.fixture(autouse=True)
def _drop_routes(page: Page) -> Iterator[None]:
    """Drop this module's route handlers before the page closes.

    A ``/health`` poll and snapshot refetches stay in flight; a handler
    replaying upstream as the page tears down raises ``TargetClosedError``.

    :param page: Playwright page fixture.
    :returns: Iterator yielding once, then unrouting.
    """
    yield
    page.unroute_all(behavior="ignoreErrors")


def _patch_local_offline_host(page: Page, session_id: str) -> None:
    """Patch the browser view into a ``host_offline`` session bound to this machine.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch.
    """
    host = {
        "host_id": _THIS_MACHINE_HOST_ID,
        "name": "This Mac",
        "owner": "e2e",
        "status": "offline",
        "sandbox_provider": None,
    }

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["host_id"] = _THIS_MACHINE_HOST_ID
        payload["host_resumable"] = False
        payload["created_at"] = _OLD_CREATED_AT
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_hosts(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/v1/hosts":
            route.continue_()
            return
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"hosts": [host]}),
        )

    def _patch_list(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/v1/sessions":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            payload["data"] = [
                r for r in rows if not (isinstance(r, dict) and r.get("id") == session_id)
            ]
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_health(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/health":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        live = {"runner_online": False, "host_online": False}
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = live
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **live}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route(re.compile(r"/v1/hosts(\?|$)"), _patch_hosts)
    page.route(re.compile(r"/v1/sessions(\?|$)"), _patch_list)
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def test_desktop_reconnect_performs_local_host_reconnect(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Reconnect on the desktop app must PERFORM the reconnect for a local host.

    Guards against the dialog offering only a copy-paste ``omnigent host``
    command for a ``host_offline`` session whose host is this machine: an
    in-app control must exist and must call the desktop bridge
    ``controlHost("start")``.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to a local ``host_offline`` shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    page.add_init_script(_DESKTOP_BRIDGE_INIT_SCRIPT)
    _patch_local_offline_host(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("composer-host-select")
    expect(badge).to_be_visible(timeout=15_000)
    badge.click()
    page.get_by_role("menuitem", name="Reconnect host", exact=True).click()

    dialog = page.get_by_test_id("reconnect-session-dialog")
    expect(dialog).to_be_visible(timeout=15_000)
    expect(dialog).to_contain_text("Host is offline")

    # The offline host is this machine, so Reconnect must offer an in-app
    # control that PERFORMS the reconnect via the desktop bridge -- not just a
    # command to copy. The locator excludes the "Reconnect" tab (role=tab).
    perform = dialog.get_by_role(
        "button", name=re.compile(r"reconnect|run on this", re.IGNORECASE)
    )
    expect(perform).to_be_visible(timeout=15_000)

    perform.click()

    # Activating it must drive the desktop shell's host daemon, exactly as
    # NewChatDialog's "Run on this machine" does.
    page.wait_for_function(
        "() => Array.isArray(window.__controlHostCalls) "
        "&& window.__controlHostCalls.includes('start')",
        timeout=15_000,
    )

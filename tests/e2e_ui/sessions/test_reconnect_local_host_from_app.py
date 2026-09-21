"""The initial reconnect action starts this desktop's host without a modal.

The real SPA/server journey uses an injected desktop bridge and an offline
host snapshot. Bridge completion is controlled to verify progress and failure.
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
    return new Promise(resolve => {{ window.__finishReconnect = resolve; }});
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


def _open_offline_session(page: Page, seeded_session: tuple[str, str]) -> None:
    """Open a session whose host is the desktop's offline local machine."""
    base_url, session_id = seeded_session
    page.add_init_script(_DESKTOP_BRIDGE_INIT_SCRIPT)
    _patch_local_offline_host(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("composer-host-select")).to_be_visible(timeout=15_000)
    page.evaluate(
        """() => {
          window.__reconnectDialogShown = false;
          new MutationObserver(() => {
            if (document.querySelector('[data-testid="reconnect-session-dialog"]')) {
              window.__reconnectDialogShown = true;
            }
          }).observe(document.body, { childList: true, subtree: true });
        }"""
    )
    page.get_by_test_id("composer-host-select").click()
    page.get_by_role("menuitem", name="Reconnect host", exact=True).click()


def test_desktop_reconnect_performs_local_host_reconnect(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """One reconnect action shows progress, calls the bridge, and never opens a dialog."""
    _open_offline_session(page, seeded_session)
    page.wait_for_function("() => window.__controlHostCalls.length === 1")
    assert page.evaluate("window.__controlHostCalls") == ["start"]
    expect(page.get_by_text("Reconnecting this machine…", exact=True)).to_be_visible()
    expect(page.get_by_test_id("reconnect-session-dialog")).not_to_be_visible()
    expect(page.get_by_text("Host reconnected.", exact=True)).not_to_be_visible()

    page.evaluate("window.__finishReconnect({ ok: true })")
    expect(page.get_by_text("Host reconnected.", exact=True)).to_be_visible()
    assert page.evaluate("window.__reconnectDialogShown") is False


def test_desktop_reconnect_failure_offers_retry(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A failed direct reconnect opens recovery, and retry can complete it."""
    _open_offline_session(page, seeded_session)
    page.wait_for_function("() => window.__controlHostCalls.length === 1")
    page.evaluate("window.__finishReconnect({ ok: false, authError: true })")
    dialog = page.get_by_test_id("reconnect-session-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("alert")).to_contain_text("finish signing in")
    expect(page.get_by_text("Host reconnected.", exact=True)).not_to_be_visible()
    expect(dialog.get_by_test_id("reconnect-session-command")).to_contain_text("omnigent host")

    dialog.get_by_role("button", name="Retry reconnect", exact=True).click()
    page.wait_for_function("() => window.__controlHostCalls.length === 2")
    expect(dialog.get_by_role("button", name="Reconnecting this machine…")).to_be_disabled()
    page.evaluate("window.__finishReconnect({ ok: true })")
    expect(dialog).not_to_be_visible()
    expect(page.get_by_text("Host reconnected.", exact=True)).to_be_visible()

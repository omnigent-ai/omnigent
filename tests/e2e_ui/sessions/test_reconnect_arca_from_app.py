"""Reconnect host starts an offline Arca host directly from the current chat."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_ARCA_HOST_ID = "host_arca"
_OLD_CREATED_AT = 1_700_000_000

_DESKTOP_BRIDGE_INIT_SCRIPT = """
window.__connectArcaCalls = 0;
window.__hostStatusListeners = [];
window.omnigentDesktop = {
  kind: "electron",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  getServerPicker: function () { return Promise.resolve(null); },
  switchServer: function () { return Promise.resolve(); },
  openServerSetup: function () {},
  getHostIdentity: function () {
    return Promise.resolve({ cliInstalled: true, hostId: "host_this_machine" });
  },
  onHostStatusChanged: function (callback) {
    window.__hostStatusListeners.push(callback);
    return () => {
      window.__hostStatusListeners = window.__hostStatusListeners.filter(cb => cb !== callback);
    };
  },
  connectArcaHost: function () {
    window.__connectArcaCalls += 1;
    return new Promise(resolve => {
      window.__finishArcaReconnect = result => {
        resolve(result);
        window.__hostStatusListeners.forEach(callback => callback());
      };
    });
  },
  getDesktopFeatures: function () { return Promise.resolve(null); },
};
"""

# Records whether the reconnect dialog ever mounted, not just its final state.
_DIALOG_WATCH_SCRIPT = """() => {
  window.__reconnectDialogShown = false;
  new MutationObserver(() => {
    if (document.querySelector('[data-testid="reconnect-session-dialog"]')) {
      window.__reconnectDialogShown = true;
    }
  }).observe(document.body, { childList: true, subtree: true });
}"""


@pytest.fixture(autouse=True)
def _drop_routes(page: Page) -> Iterator[None]:
    yield
    if not page.is_closed():
        page.unroute_all(behavior="ignoreErrors")


def _patch_offline_arca_host(page: Page, session_id: str, host_name: str) -> dict[str, bool]:
    host_state = {"online": False}
    host = {
        "host_id": _ARCA_HOST_ID,
        "name": host_name,
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
        payload["host_id"] = _ARCA_HOST_ID
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
            body=json.dumps(
                {"hosts": [{**host, "status": "online" if host_state["online"] else "offline"}]}
            ),
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
                row for row in rows if not (isinstance(row, dict) and row.get("id") == session_id)
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
        live = {"runner_online": False, "host_online": host_state["online"]}
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
    return host_state


def _open_offline_arca_chat(
    page: Page,
    seeded_session: tuple[str, str],
    *,
    remembered: bool,
    host_name: str,
) -> tuple[Locator, dict[str, bool]]:
    """Open the seeded chat bound to an offline Arca host in the desktop shell.

    :param page: Playwright page before navigation.
    :param seeded_session: ``(base_url, session_id)`` of the seeded chat.
    :param remembered: Whether this browser saved the host via Run on Arca.
    :param host_name: Host name the server reports, e.g. ``"e2e's arca"``.
    :returns: The composer host picker and the mutable host-online state.
    """
    base_url, session_id = seeded_session
    page.add_init_script(_DESKTOP_BRIDGE_INIT_SCRIPT)
    page.add_init_script(
        f"window.localStorage.setItem('omnigent:arca-host-id', {_ARCA_HOST_ID!r});"
        if remembered
        else "window.localStorage.removeItem('omnigent:arca-host-id');"
    )
    host_state = _patch_offline_arca_host(page, session_id, host_name)

    page.goto(f"{base_url}/c/{session_id}")
    host_picker = page.get_by_test_id("composer-host-select")
    expect(host_picker).to_be_visible(timeout=15_000)
    expect(host_picker).to_have_attribute(
        "aria-label", re.compile(rf"{re.escape(host_name)}, offline$")
    )
    return host_picker, host_state


@pytest.mark.parametrize(
    ("remembered", "host_name"),
    [(True, "arca-devbox"), (False, "e2e's arca")],
    ids=["remembered", "seeded"],
)
def test_desktop_reconnects_arca_from_current_chat(
    page: Page,
    seeded_session: tuple[str, str],
    remembered: bool,
    host_name: str,
) -> None:
    """One Reconnect host action starts Arca and reports progress without a dialog."""
    host_picker, host_state = _open_offline_arca_chat(
        page, seeded_session, remembered=remembered, host_name=host_name
    )
    page.evaluate(_DIALOG_WATCH_SCRIPT)
    page.wait_for_timeout(800)

    host_picker.click()
    page.get_by_role("menuitem", name="Reconnect host", exact=True).click()
    expect(page.get_by_text("Reconnecting Arca…", exact=True)).to_be_visible()
    assert page.evaluate("window.__connectArcaCalls") == 1
    expect(page.get_by_text("Arca is back online.", exact=True)).not_to_be_visible()
    page.wait_for_timeout(1500)

    host_state["online"] = True
    page.evaluate("window.__finishArcaReconnect({ ok: true })")
    expect(host_picker).to_have_attribute(
        "aria-label", re.compile(rf"{re.escape(host_name)}, online$")
    )
    expect(page.get_by_text("Arca is back online.", exact=True)).to_be_visible()
    assert page.evaluate("window.__reconnectDialogShown") is False
    assert page.evaluate("window.__connectArcaCalls") == 1
    page.wait_for_timeout(1500)


def test_desktop_arca_reconnect_failure_offers_retry(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A failed Arca start opens the dialog, and its retry reconnects the host."""
    host_picker, host_state = _open_offline_arca_chat(
        page, seeded_session, remembered=False, host_name="e2e's arca"
    )
    page.wait_for_timeout(800)

    host_picker.click()
    page.get_by_role("menuitem", name="Reconnect host", exact=True).click()
    expect(page.get_by_text("Reconnecting Arca…", exact=True)).to_be_visible()
    dialog = page.get_by_test_id("reconnect-session-dialog")
    expect(dialog).not_to_be_visible()
    page.wait_for_timeout(1200)

    page.evaluate("window.__finishArcaReconnect({ ok: false, error: 'Arca did not start.' })")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_test_id("reconnect-session-arca-error")).to_have_text(
        "Arca did not start."
    )
    page.wait_for_timeout(1200)

    dialog.get_by_role("button", name="Reconnect Arca", exact=True).click()
    expect(dialog.get_by_role("button", name="Reconnecting Arca…", exact=True)).to_be_disabled()
    assert page.evaluate("window.__connectArcaCalls") == 2
    page.wait_for_timeout(1200)

    host_state["online"] = True
    page.evaluate("window.__finishArcaReconnect({ ok: true })")
    expect(dialog).not_to_be_visible()
    expect(host_picker).to_have_attribute("aria-label", re.compile(r"e2e's arca, online$"))
    expect(page.get_by_text("Arca is back online.", exact=True)).to_be_visible()
    page.wait_for_timeout(1500)

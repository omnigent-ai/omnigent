"""An offline Arca session reconnects explicitly from the current chat."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_ARCA_HOST_ID = "host_arca"
_OLD_CREATED_AT = 1_700_000_000

_DESKTOP_BRIDGE_INIT_SCRIPT = f"""
window.localStorage.setItem("omnigent:arca-host-id", {_ARCA_HOST_ID!r});
window.__connectArcaCalls = 0;
window.__hostStatusListeners = [];
window.omnigentDesktop = {{
  kind: "electron",
  setBadgeCount: function () {{}},
  notify: function () {{ return Promise.resolve(false); }},
  onNotificationActivated: function () {{ return function () {{}}; }},
  getServerPicker: function () {{ return Promise.resolve(null); }},
  switchServer: function () {{ return Promise.resolve(); }},
  openServerSetup: function () {{}},
  getHostIdentity: function () {{
    return Promise.resolve({{ cliInstalled: true, hostId: "host_this_machine" }});
  }},
  onHostStatusChanged: function (callback) {{
    window.__hostStatusListeners.push(callback);
    return () => {{
      window.__hostStatusListeners = window.__hostStatusListeners.filter(cb => cb !== callback);
    }};
  }},
  connectArcaHost: function () {{
    window.__connectArcaCalls += 1;
    return new Promise(resolve => {{
      window.__finishArcaReconnect = result => {{
        resolve(result);
        window.__hostStatusListeners.forEach(callback => callback());
      }};
    }});
  }},
  getDesktopFeatures: function () {{ return Promise.resolve(null); }},
}};
"""


@pytest.fixture(autouse=True)
def _drop_routes(page: Page) -> Iterator[None]:
    yield
    if not page.is_closed():
        page.unroute_all(behavior="ignoreErrors")


def _patch_offline_arca_host(page: Page, session_id: str) -> dict[str, bool]:
    host_state = {"online": False}
    host = {
        "host_id": _ARCA_HOST_ID,
        "name": "arca-devbox",
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


def test_desktop_reconnects_arca_from_current_chat(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The dialog waits for the explicit Arca action and reports progress."""
    base_url, session_id = seeded_session
    page.add_init_script(_DESKTOP_BRIDGE_INIT_SCRIPT)
    host_state = _patch_offline_arca_host(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    host_picker = page.get_by_test_id("composer-host-select")
    expect(host_picker).to_be_visible(timeout=15_000)
    expect(host_picker).to_have_attribute("aria-label", re.compile(r"arca-devbox, offline$"))
    page.wait_for_timeout(800)

    host_picker.click()
    page.get_by_role("menuitem", name="Reconnect host", exact=True).click()
    dialog = page.get_by_test_id("reconnect-session-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("This session's Arca host is offline")
    expect(dialog.get_by_role("button", name="Reconnect Arca", exact=True)).to_be_visible()
    assert page.evaluate("window.__connectArcaCalls") == 0
    page.wait_for_timeout(1200)

    dialog.get_by_role("button", name="Reconnect Arca", exact=True).click()
    expect(dialog.get_by_role("button", name="Reconnecting Arca…", exact=True)).to_be_disabled()
    assert page.evaluate("window.__connectArcaCalls") == 1
    page.wait_for_timeout(1500)

    host_state["online"] = True
    page.evaluate("window.__finishArcaReconnect({ ok: true })")
    expect(dialog).not_to_be_visible()
    expect(host_picker).to_have_attribute("aria-label", re.compile(r"arca-devbox, online$"))
    expect(page.get_by_text("Arca reconnect requested.", exact=True)).to_be_visible()
    page.wait_for_timeout(1500)

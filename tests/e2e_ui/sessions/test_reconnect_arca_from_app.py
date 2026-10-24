"""Reconnect host starts Arca directly from the current chat."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, Route, expect

from tests._helpers.desktop_host_recovery import (
    ARCA_DESKTOP_BRIDGE_INIT_SCRIPT as _DESKTOP_BRIDGE_INIT_SCRIPT,
)
from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry
from tests.e2e_ui.sessions.test_reconnect_local_host_from_app import (
    _DESKTOP_BRIDGE_INIT_SCRIPT as _LOCAL_DESKTOP_BRIDGE_INIT_SCRIPT,
)
from tests.e2e_ui.sessions.test_reconnect_local_host_from_app import (
    _patch_local_offline_host,
)

_ARCA_HOST_ID = "host_arca"
_OLD_CREATED_AT = 1_700_000_000


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
    """One reconnect action starts Arca, reports progress, and never opens a dialog."""
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
    page.wait_for_timeout(800)

    host_picker.click()
    page.get_by_role("menuitem", name="Reconnect host", exact=True).click()
    dialog = page.get_by_test_id("reconnect-session-dialog")
    expect(page.get_by_text("Reconnecting Arca…", exact=True)).to_be_visible()
    expect(dialog).not_to_be_visible()
    assert page.evaluate("window.__connectArcaCalls") == 1
    expect(page.get_by_text("Arca reconnect requested.", exact=True)).not_to_be_visible()
    page.wait_for_timeout(1500)

    host_state["online"] = True
    page.evaluate("window.__finishArcaReconnect({ ok: true })")
    expect(dialog).not_to_be_visible()
    expect(host_picker).to_have_attribute(
        "aria-label", re.compile(rf"{re.escape(host_name)}, online$")
    )
    expect(page.get_by_text("Arca reconnect requested.", exact=True)).to_be_visible()
    assert page.evaluate("window.__reconnectDialogShown") is False
    page.wait_for_timeout(1500)


@pytest.mark.parametrize(
    ("host_kind", "first_attempt_fails", "observed_offline"),
    [
        pytest.param("arca", False, True, id="success-arca"),
        pytest.param("laptop", False, True, id="success-laptop"),
        pytest.param("arca", True, True, id="retry-arca"),
        pytest.param("laptop", True, True, id="retry-laptop"),
        pytest.param("arca", False, False, id="stale-health-arca"),
    ],
)
def test_desktop_send_reconnects_host_and_delivers_message(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
    host_kind: str,
    first_attempt_fails: bool,
    observed_offline: bool,
) -> None:
    """Sending retains the text and file until the host, not just the bridge, is ready."""
    base_url, session_id = seeded_session
    is_arca = host_kind == "arca"
    page.add_init_script(
        _DESKTOP_BRIDGE_INIT_SCRIPT if is_arca else _LOCAL_DESKTOP_BRIDGE_INIT_SCRIPT
    )
    page.add_init_script("window.localStorage.removeItem('omnigent:arca-host-id');")
    host_state = (
        _patch_offline_arca_host(page, session_id, "e2e's arca")
        if is_arca
        else _patch_local_offline_host(page, session_id)
    )
    host_state["online"] = not observed_offline
    prompt = f"Continue with the attached notes after reconnecting. recovery-{session_id[:8]}"
    reply = "Reconnected and received your notes."
    configure_mock_llm(mock_llm_server_url, [{"text": reply}], match=prompt)
    posts: list[Request] = []
    page.on(
        "request",
        lambda request: (
            posts.append(request)
            if request.method == "POST"
            and urlparse(request.url).path == f"/v1/sessions/{session_id}/events"
            else None
        ),
    )
    if not observed_offline:

        def _refuse_first_send(route: Route) -> None:
            host_state["online"] = False
            route.fulfill(
                status=503,
                json={"error": {"code": "host_offline", "message": "Waiting for the host."}},
            )

        page.route(re.compile(rf"/v1/sessions/{session_id}/events$"), _refuse_first_send, times=1)
    page.goto(f"{base_url}/c/{session_id}")
    host_picker = page.get_by_test_id("composer-host-select")
    initial_status = "offline" if observed_offline else "online"
    expect(host_picker).to_have_attribute("aria-label", re.compile(rf", {initial_status}$"))
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    composer.hover()
    expect(composer).to_be_enabled()
    page.locator('input[type="file"][accept*="image/"]').set_input_files(
        {"name": "reconnect-notes.txt", "mimeType": "text/plain", "buffer": b"Keep these notes.\n"}
    )
    composer.fill(prompt)
    if is_arca:
        composer.press("Enter")
    else:
        page.get_by_role("button", name="Send", exact=True).click()

    expect(page.get_by_text("Waiting for host", exact=True)).to_be_visible()
    expect(page.get_by_text("reconnect-notes.txt", exact=True)).to_be_visible()
    progress = "Reconnecting Arca…" if is_arca else "Reconnecting this machine…"
    expect(page.get_by_text(progress, exact=True)).to_be_visible()
    expect(page.get_by_test_id("reconnect-session-dialog")).not_to_be_visible()
    expect(composer).to_have_value("")
    calls = "window.__connectArcaCalls" if is_arca else "window.__controlHostCalls.length"
    assert page.evaluate(calls) == 1
    refused_posts = 0 if observed_offline else 1
    assert len(posts) == refused_posts

    finish = "window.__finishArcaReconnect" if is_arca else "window.__finishReconnect"
    if first_attempt_fails:
        page.evaluate(f"{finish}({{ ok: false, error: 'Connection interrupted. Try again.' }})")
        dialog = page.get_by_test_id("reconnect-session-dialog")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("alert")).to_contain_text("Connection interrupted")
        assert len(posts) == refused_posts
        retry = "reconnect-session-arca" if is_arca else "reconnect-session-this-machine"
        dialog.get_by_test_id(retry).click()
        page.wait_for_function(f"() => {calls} === 2")
        expect(dialog.get_by_test_id(retry)).to_be_disabled()

    page.evaluate(f"{finish}({{ ok: true }})")
    page.wait_for_timeout(1000)
    expect(page.get_by_text("Waiting for host", exact=True)).to_be_visible()
    assert len(posts) == refused_posts

    host_state["online"] = True
    page.evaluate("window.__hostStatusListeners.forEach(callback => callback())")
    expect(page.get_by_text(reply, exact=True)).to_be_visible(timeout=45_000)
    expect(page.get_by_text("Waiting for host", exact=True)).not_to_be_visible()
    expect(page.locator('[data-testid="message-bubble"][data-role="user"]')).to_have_count(1)
    assert len(posts) == refused_posts + 1
    content = posts[-1].post_data_json["data"]["content"]
    assert (
        posts[0].post_data_json["data"]["stable_id"]
        == posts[-1].post_data_json["data"]["stable_id"]
    )
    assert {"type": "input_text", "text": prompt} in content
    attachment = next(block for block in content if block["type"] == "input_file")
    assert attachment["file_id"]
    items = page.request.get(f"{base_url}/v1/sessions/{session_id}/items?limit=100&order=asc")
    assert items.ok, items.text()
    users = [item for item in items.json()["data"] if item.get("role") == "user"]
    assert len(users) == 1
    assert users[0]["content"] == content
    assert page.evaluate(calls) == (2 if first_attempt_fails else 1)
    page.wait_for_timeout(1000)

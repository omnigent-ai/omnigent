"""E2E: a paused terminal-first session resumes without a chat message."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, Route, expect

_OLD_CREATED_AT = 1_700_000_000


def _save_demo(page: Page, name: str) -> None:
    """Save optional PR evidence without making screenshots test artifacts."""
    output_dir = os.environ.get("E2E_SCREENSHOT_DIR")
    if not output_dir:
        return
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path / f"{name}.png"), full_page=True)


def _message_count(base_url: str, session_id: str) -> int:
    """Count committed chat messages, excluding terminal resource events."""
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items?order=asc&limit=1000",
        timeout=30.0,
    )
    response.raise_for_status()
    return sum(item.get("type") == "message" for item in response.json().get("data", []))


def test_terminal_view_resumes_paused_session_without_message(
    page: Page,
    terminal_session: tuple[str, str],
) -> None:
    """Terminal stays selectable while paused and resumes through its prompt."""
    base_url, session_id = terminal_session
    messages_before = _message_count(base_url, session_id)

    state = {"asleep": True}

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["created_at"] = _OLD_CREATED_AT
        payload["host_id"] = "host_demo"
        payload["labels"] = {**payload.get("labels", {}), "omnigent.ui": "terminal"}
        route.fulfill(
            status=response.status,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_session_list(route: Route) -> None:
        response = route.fetch()
        payload = response.json()
        for row in payload.get("data", []):
            if row.get("id") != session_id:
                continue
            row["created_at"] = _OLD_CREATED_AT
            row["host_id"] = "host_demo"
            row["labels"] = {**row.get("labels", {}), "omnigent.ui": "terminal"}
        route.fulfill(
            status=response.status,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_health(route: Route) -> None:
        response = route.fetch()
        if not state["asleep"]:
            route.fulfill(status=response.status, headers=response.headers, body=response.body())
            return
        payload = response.json()
        live = {"runner_online": False, "host_online": True}
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = live
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **live}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _hide_terminals_while_asleep(route: Route) -> None:
        if route.request.method != "GET" or not state["asleep"]:
            route.continue_()
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"object": "list", "data": [], "has_more": False}),
        )

    def _observe_resume(route: Route) -> None:
        response = route.fetch()
        if response.ok:
            created = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "zsh", "session_key": "main"},
                timeout=30.0,
            )
            created.raise_for_status()
            state["asleep"] = False
        route.fulfill(status=response.status, headers=response.headers, body=response.body())

    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(
        re.compile(rf"/v1/sessions/{re.escape(session_id)}/resources/terminals(\?|$)"),
        _hide_terminals_while_asleep,
    )
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}/resume$"), _observe_resume)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route(re.compile(r"/v1/sessions(\?|$)"), _patch_session_list)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)

    page.goto(f"{base_url}/c/{session_id}")

    page.get_by_role("button", name="Switch between chat and terminal").click()
    terminal_option = page.get_by_role("menuitemradio", name="Terminal", exact=True)
    expect(terminal_option).not_to_have_attribute("aria-disabled", "true", timeout=20_000)
    terminal_option.click()

    prompt = page.get_by_test_id("terminal-resume-prompt")
    expect(prompt).to_be_visible()
    expect(terminal_option).to_be_hidden()
    _save_demo(page, "terminal-resume-prompt")

    prompt.get_by_role("button", name="Resume session").click()

    expect(prompt).to_be_hidden(timeout=30_000)
    terminal = page.get_by_test_id("terminal-view").last
    expect(terminal).to_be_visible(timeout=20_000)
    expect(terminal).to_have_attribute("data-state", "connected", timeout=20_000)
    assert _message_count(base_url, session_id) == messages_before
    _save_demo(page, "terminal-resumed")

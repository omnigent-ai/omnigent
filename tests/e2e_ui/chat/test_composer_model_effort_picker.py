"""Browser e2e: claude-native composer picker shows effective model + effort.

Issue #1463 is user-facing UI behavior: a fresh Claude Code session with no
explicit model/effort override should still tell the user what is active. The
unit tests cover the pure picker logic; this e2e stubs the session bind APIs and
proves the routed chat surface renders the same defaults on the real composer
trigger and marks them inside the dropdown.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, Route, expect

_SESSION_ID = "conv_picker_e2e"
_AGENT_ID = "ag_claude_picker_e2e"
_AGENT_NAME = "claude-native-ui"
_HOST_ID = "host_picker_e2e"

_SESSIONS_LIST_RE = re.compile(r"/v1/sessions(\?.*)?$")
_FILESYSTEM_RE = re.compile(r"/v1/hosts/[^/]+/filesystem")
_SESSION_DETAIL_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}(\?.*)?$")
_ITEMS_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}/items")
_STREAM_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}/stream")
_AGENT_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}/agent")
_SUBRESOURCE_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}/(child_sessions|resources)")
_HEALTH_RE = re.compile(r"/health(\?.*)?$")

_EMPTY_LIST_BODY = {"object": "list", "data": [], "has_more": False}
_DONE_SSE = "data: [DONE]\n\n"

_AGENTS_BODY = {
    "data": [
        {
            "id": _AGENT_ID,
            "name": _AGENT_NAME,
            "display_name": "Claude Code",
            "description": "Anthropic's coding agent",
            "harness": None,
            "skills": [],
        }
    ]
}
_HOSTS_BODY = {
    "hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]
}
_ITEMS_BODY = {"object": "list", "data": [], "first_id": None, "last_id": None, "has_more": False}
_SESSION_BODY = {
    "id": _SESSION_ID,
    "agent_id": _AGENT_ID,
    "agent_name": _AGENT_NAME,
    "status": "idle",
    "created_at": 1704067200,
    "updated_at": 1704067200,
    "title": None,
    "labels": {"omnigent.wrapper": "claude-code-native-ui", "omnigent.ui": "terminal"},
    # Fresh native session: no explicit overrides and no resolved snapshot model yet.
    "reasoning_effort": None,
    "llm_model": None,
    "model_override": None,
    "items": [],
    "permission_level": 4,
}
_AGENT_BODY = {
    "id": _AGENT_ID,
    "object": "agent",
    "name": _AGENT_NAME,
    "description": "Anthropic's coding agent",
    "harness": None,
    "mcp_servers": [],
    "policies": [],
    "terminals": [],
}
_HEALTH_BODY = {"sessions": {_SESSION_ID: {"runner_online": True, "host_online": True}}}


def _fulfill_json(route: Route, payload: object) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))


def _register_stubbed_chat_routes(page: Page) -> None:
    page.route("**/v1/agents", lambda r: _fulfill_json(r, _AGENTS_BODY))
    page.route("**/v1/hosts", lambda r: _fulfill_json(r, _HOSTS_BODY))
    page.route(_FILESYSTEM_RE, lambda r: _fulfill_json(r, _EMPTY_LIST_BODY))
    page.route(_SESSIONS_LIST_RE, lambda r: _fulfill_json(r, _EMPTY_LIST_BODY))
    page.route(_ITEMS_RE, lambda r: _fulfill_json(r, _ITEMS_BODY))
    page.route(_AGENT_RE, lambda r: _fulfill_json(r, _AGENT_BODY))
    page.route(_SUBRESOURCE_RE, lambda r: _fulfill_json(r, _EMPTY_LIST_BODY))
    page.route(_SESSION_DETAIL_RE, lambda r: _fulfill_json(r, _SESSION_BODY))
    page.route(_HEALTH_RE, lambda r: _fulfill_json(r, _HEALTH_BODY))
    page.route(
        _STREAM_RE,
        lambda r: r.fulfill(status=200, content_type="text/event-stream", body=_DONE_SSE),
    )


def test_claude_native_picker_shows_default_model_and_effort(
    page: Page,
    live_server: str,
) -> None:
    """Fresh claude-native session shows Sonnet/Medium on trigger and dropdown."""
    _register_stubbed_chat_routes(page)

    page.goto(f"{live_server}/c/{_SESSION_ID}")

    trigger = page.get_by_test_id("agent-picker-trigger")
    expect(trigger).to_be_visible(timeout=30_000)
    expect(trigger).to_contain_text("Sonnet")
    expect(trigger).to_contain_text("Medium")
    expect(trigger).not_to_contain_text("Claude")

    trigger.click()

    sonnet = page.locator('[data-testid="model-picker-item"][data-model-id="sonnet"]')
    expect(sonnet).to_have_attribute("data-active", "true")
    expect(sonnet.get_by_text("Current", exact=True)).to_be_visible()

    medium = page.locator('[data-testid="effort-picker-item"][data-effort-level="medium"]')
    expect(medium).to_have_attribute("data-active", "true")
    expect(medium.get_by_text("Current", exact=True)).to_be_visible()

"""Exercise retained failed submissions through the real composer and transcript.

The browser API boundary is mocked, so no runner or provider is needed. Use
``--ui-base-url`` to run against an existing SPA dev server, or omit it to use
the suite's isolated server and built SPA.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.chat.test_model_flows_contract import _install_stream_controller

_SESSION_ID = "failed-send-recovery"
_SESSION_PATH = f"/v1/sessions/{_SESSION_ID}"


def _mock_session(page: Page, posts: list[dict[str, Any]]) -> None:
    """Serve one idle chat and capture retried messages without dispatching them."""
    session = {
        "id": _SESSION_ID,
        "object": "conversation",
        "title": "Failed submission recovery",
        "agent_id": "claude",
        "agent_name": "Claude Code",
        "runner_id": "runner_test",
        "status": "idle",
        "created_at": 1_790_035_140,
        "labels": {},
        "permission_level": 4,
        "owner": "local",
        "harness": "claude-native",
        "llm_model": "claude-sonnet-4-6",
        "workspace": "/workspace/project",
        "runner_online": True,
        "model_options": [{"id": "claude-sonnet-4-6", "display_name": "Sonnet 4.6"}],
        "items": [],
        "pending_inputs": [],
    }

    def api(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        body: Any = {"data": [], "has_more": False}
        if path == "/v1/info":
            body = {
                "accounts_enabled": False,
                "single_user": True,
                "databricks_features": False,
                "server_version": "0.15.0.dev0",
                "features": {},
                "enabled_connections": [],
                "sharing_mode": "off",
            }
        elif path == "/v1/me":
            body = {"user_id": "local", "login_url": None}
        elif path == "/health":
            body = {"sessions": {_SESSION_ID: {"runner_online": True, "host_online": None}}}
        elif path == "/v1/hosts":
            body = {"hosts": []}
        elif path == "/v1/sessions/projects":
            body = []
        elif path == "/v1/skills":
            body = {"skills": []}
        elif path == "/v1/sessions":
            body = {"object": "list", "data": [session], "has_more": False}
        elif path == _SESSION_PATH:
            body = session
        elif path == f"{_SESSION_PATH}/agent":
            body = {
                "id": "claude",
                "name": "Claude Code",
                "harness": "claude-native",
                "terminals": [],
                "mcp_servers": [],
                "policies": [],
            }
        elif path == f"{_SESSION_PATH}/resources/files" and request.method == "POST":
            body = {
                "id": "file_notes",
                "name": "notes.txt",
                "metadata": {"filename": "notes.txt", "bytes": 12, "created_at": 1_790_035_140},
            }
        elif path == f"{_SESSION_PATH}/events" and request.method == "POST":
            posts.append(request.post_data_json)
            route.fulfill(status=202, json={"queued": True, "pending_id": "pending_test"})
            return
        elif path.endswith("/goal"):
            body = {"goal": None}
        elif path.endswith("/workspace"):
            body = {"workspace": "/workspace/project", "git_branch": "main"}
        elif path.endswith("/resources/terminals"):
            body = {"terminals": []}
        elif path.endswith("/resources/environments"):
            body = {"environments": []}
        elif path.endswith("/filesystem/git/status"):
            body = {"is_git_repo": True, "branch": "main", "files": []}
        route.fulfill(json=body)

    _install_stream_controller(page, _SESSION_ID)
    for pattern in ("**/v1/**", "**/health*", "**/api/**", "**/auth/**"):
        page.route(pattern, api)
    page.route_web_socket("**/v1/sessions/updates*", lambda _: None)


def test_offline_submission_can_be_edited_and_retried_without_losing_newer_draft(
    page: Page,
    request: pytest.FixtureRequest,
) -> None:
    """Offline sends retain files; explicit retry leaves a newer composer draft intact."""
    # All API calls are intercepted; an explicit SPA URL needs no runner fixture.
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    posts: list[dict[str, Any]] = []
    _mock_session(page, posts)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{base_url}/c/{_SESSION_ID}")
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_editable(timeout=30_000)
    page.locator('input[type="file"]').set_input_files(
        {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"Review notes"}
    )
    composer.fill("Please review the attached notes.")

    page.context.set_offline(True)
    page.get_by_role("button", name="Send", exact=True).click()
    failed = page.get_by_test_id("failed-send-message")
    expect(failed).to_have_attribute("data-delivery-status", "not_sent")
    expect(failed).to_contain_text("Please review the attached notes.")
    expect(failed).to_contain_text("notes.txt")
    expect(failed).to_contain_text("Failed to send")
    expect(failed).to_contain_text("Offline")
    expect(failed.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
    assert posts == [], "Offline preflight must not submit the message"

    newer_draft = "A newer draft must stay in the composer."
    composer.fill(newer_draft)
    page.context.set_offline(False)
    retry = failed.get_by_role("button", name="Retry", exact=True)
    expect(retry).to_be_enabled()
    expect(failed).not_to_contain_text("Offline")
    failed.hover()
    failed.get_by_role("button", name="Edit", exact=True).click()
    editor = failed.get_by_label("Edit unsent message")
    expect(editor).to_be_focused()
    editor_id = editor.get_attribute("id")
    assert editor_id is not None
    stable_id = editor_id.removeprefix("edit-")
    edited_text = "Review the notes and suggest a shorter introduction."
    editor.fill(edited_text)
    failed.get_by_role("button", name="Save changes", exact=True).click()
    expect(failed).to_contain_text(edited_text)
    expect(failed.get_by_role("button", name="Edit", exact=True)).to_be_focused()
    expect(composer).to_have_text(newer_draft)
    assert posts == [], "Reconnecting and editing must not resend automatically"

    with page.expect_response(
        lambda response: (
            urlparse(response.url).path == f"{_SESSION_PATH}/events"
            and response.request.method == "POST"
        )
    ) as accepted:
        retry.click()
    assert accepted.value.status == 202
    expect(failed).to_have_count(0)
    assert len(posts) == 1
    assert posts[0]["data"]["stable_id"] == stable_id
    assert posts[0]["data"]["content"] == [
        {"type": "input_file", "file_id": "file_notes", "filename": "notes.txt"},
        {"type": "input_text", "text": edited_text},
    ]
    expect(composer).to_have_text(newer_draft)

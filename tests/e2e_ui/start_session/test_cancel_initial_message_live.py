r"""Cancel and correct the first prompt while a real model selection is pending.

Run against an isolated local server + host with Claude Code and real Smart
Routing/inference credentials. No API responses, browser state, or routing
timing are replaced. Without an explicit live URL these tests skip::

    OMNIGENT_E2E_ALLOW_DEV_BASE_URL=1 uv run --no-sync pytest \
      tests/e2e_ui/start_session/test_cancel_initial_message_live.py \
      --ui-base-url http://127.0.0.1:5173 --video=on --tracing=on \
      --output=/tmp/cancel-initial-message
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, Response, expect

pytestmark = [pytest.mark.workspace_panel_product_default, pytest.mark.timeout(240)]

_TEMP_ROUTE = re.compile(r"/c/temp:[0-9a-f]{32}$")
_USER = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'


def _choose_workspace_and_routing(page: Page, workspace: Path) -> None:
    """Choose an actual host folder and Claude's Smart Routing model in the UI."""
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    expect(picker).to_be_enabled(timeout=60_000)
    page.get_by_test_id("new-chat-landing-workspace-chip").click()
    page.get_by_test_id("new-chat-landing-workspace-open-folder").click()
    page.get_by_test_id("workspace-picker-breadcrumbs").get_by_role("button").last.click()
    folder = page.get_by_test_id("workspace-picker-path-input")
    folder.fill(str(workspace))
    folder.press("Enter")
    page.get_by_test_id("workspace-picker-select").click()
    expect(page.get_by_test_id("workspace-picker")).to_be_hidden()

    picker.click()
    claude = page.get_by_role("menuitem", name="Claude Code", exact=True)
    expect(claude).to_be_visible()
    claude.hover()
    claude.get_by_text("Edit", exact=True).click()
    page.get_by_role("menuitemcheckbox", name="Smart Routing", exact=True).click()
    page.keyboard.press("Escape")
    if picker.get_attribute("aria-expanded") == "true":
        page.keyboard.press("Escape")
    expect(picker).to_have_attribute("aria-expanded", "false")
    expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)


def _wait_for_create(page: Page, responses: list[Response]) -> Response:
    """Wait for the observed, unmodified session-create response."""
    deadline = time.monotonic() + 60
    while not responses and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(responses) == 1, "The real session creation did not finish exactly once"
    response = responses[0]
    assert response.ok, f"Session creation failed: {response.status} {response.text()}"
    return response


def _assert_no_canceled_input(
    page: Page, base_url: str, session_id: str, marker: str
) -> list[dict[str, object]]:
    """Inspect durable user messages and native inputs not yet consumed."""
    items_response = page.request.get(
        f"{base_url}/v1/sessions/{session_id}/items?limit=100&order=asc"
    )
    assert items_response.ok, items_response.text()
    items = items_response.json()["data"]
    user_messages = [
        item for item in items if item.get("type") == "message" and item.get("role") == "user"
    ]
    assert all(marker not in str(item.get("content")) for item in user_messages), user_messages

    snapshot_response = page.request.get(f"{base_url}/v1/sessions/{session_id}")
    assert snapshot_response.ok, snapshot_response.text()
    pending_inputs = snapshot_response.json().get("pending_inputs", [])
    assert marker not in str(pending_inputs), pending_inputs
    return items


@pytest.mark.parametrize("interrupt_with", ["button", "escape"])
def test_cancel_initial_message_during_real_model_selection(
    page: Page, request: pytest.FixtureRequest, tmp_path: Path, interrupt_with: str
) -> None:
    """Cancel before create resolves, edit immediately, and send only the correction."""
    base_url = request.config.getoption("--ui-base-url")
    if not base_url:
        pytest.skip("Requires --ui-base-url for a real server, host, router, and Claude Code")
    base_url = base_url.rstrip("/")
    info_response = page.request.get(f"{base_url}/v1/info")
    assert info_response.ok, info_response.text()
    assert info_response.json()["smart_routing_enabled"], "Configure real Smart Routing first"

    nonce = uuid.uuid4().hex
    original_marker = f"ROUTING_CANCEL_ORIGINAL_{nonce}"
    corrected_marker = f"ROUTING_CANCEL_CORRECTED_{nonce}"
    original = f"Reply with exactly {original_marker}. Do not use tools."
    corrected = f"Reply with exactly {corrected_marker}. Do not use tools."
    creates: list[Request] = []
    responses: list[Response] = []
    dispatched_original: list[str] = []

    def record_request(network_request: Request) -> None:
        if network_request.method != "POST":
            return
        if urlparse(network_request.url).path == "/v1/sessions":
            creates.append(network_request)
        elif original_marker in (network_request.post_data or ""):
            dispatched_original.append(network_request.url)

    def record_response(response: Response) -> None:
        if response.request.method == "POST" and urlparse(response.url).path == "/v1/sessions":
            responses.append(response)

    page.on("request", record_request)
    page.on("response", record_response)
    try:
        page.goto(f"{base_url}/")
        _choose_workspace_and_routing(page, tmp_path)
        page.get_by_test_id("new-chat-landing-input").fill(original)
        page.get_by_test_id("new-chat-landing-submit").click()
        expect(page).to_have_url(_TEMP_ROUTE)
        composer = page.get_by_role("textbox", name="Message the agent")
        expect(composer).to_be_editable()
        expect(page.locator(_USER, has_text=original_marker)).to_be_visible()
        assert len(creates) == 1
        create_body = creates[0].post_data_json
        assert create_body["smart_routing_message"] == original, create_body
        assert create_body["cost_control_mode_override"] == "on", create_body
        assert not create_body.get("model_override"), create_body
        assert not responses, "Model selection finished before the cancellation attempt"
        assert _TEMP_ROUTE.search(page.url), (
            "Cancellation must happen before a real session exists"
        )

        # Do not wait for creation to enable Interrupt: that would test a later turn.
        if interrupt_with == "button":
            interrupt = page.get_by_role("button", name="Interrupt", exact=True)
            assert interrupt.count() == 1, "No Interrupt button while model selection is pending"
            assert interrupt.is_enabled(), "Interrupt is disabled while model selection is pending"
            interrupt.click(timeout=1_000)
        else:
            composer.press("Escape")

        expect(composer).to_have_value(original, timeout=1_000)
        assert not responses, "Model selection finished before the restored prompt could be edited"
        composer.fill(corrected)
        created = _wait_for_create(page, responses).json()
        session_id = created["id"]
        expect(page).to_have_url(f"{base_url}/c/{session_id}", timeout=30_000)
        expect(composer).to_have_value(corrected)
        expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
        assert not dispatched_original, dispatched_original
        _assert_no_canceled_input(page, base_url, session_id, original_marker)

        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.locator(_ASSISTANT, has_text=corrected_marker)).to_be_visible(timeout=120_000)
        expect(page.locator(_USER, has_text=corrected_marker)).to_have_count(1)
        expect(page.locator(_USER, has_text=original_marker)).to_have_count(0)
        items = _assert_no_canceled_input(page, base_url, session_id, original_marker)
        assert any(
            item.get("type") == "message"
            and item.get("role") == "user"
            and corrected_marker in str(item.get("content"))
            for item in items
        ), items
        assert any(
            item.get("type") == "routing_decision" and item.get("applied") is True
            for item in items
        ), "The real model selector must have applied its decision"
        assert not dispatched_original, dispatched_original

        page.reload()
        expect(page.locator(_ASSISTANT, has_text=corrected_marker)).to_be_visible(timeout=30_000)
        expect(page.locator(_USER, has_text=corrected_marker)).to_have_count(1)
        expect(page.locator(_USER, has_text=original_marker)).to_have_count(0)
    finally:
        # Clean up only sessions created by this browser, including a failed repro.
        if creates:
            response = _wait_for_create(page, responses)
            page.request.delete(f"{base_url}/v1/sessions/{response.json()['id']}")
        page.remove_listener("request", record_request)
        page.remove_listener("response", record_response)

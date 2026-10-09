r"""Cancel a new native session's first prompt while its real runner starts.

Requires an isolated real server + host with Claude Code and working inference.
The model picker, session creation, startup, interruption, and corrected reply
all run normally, without intercepted requests or injected application state::

    OMNIGENT_E2E_ALLOW_DEV_BASE_URL=1 uv run --no-sync pytest \
      tests/e2e_ui/start_session/test_cancel_initial_message_startup_live.py \
      --ui-base-url http://127.0.0.1:5173 --video=on --tracing=on \
      --output=/tmp/cancel-initial-message-startup
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, Response, expect

from tests.e2e_ui.start_session.test_cancel_initial_message_live import (
    _ASSISTANT,
    _USER,
    _assert_no_canceled_input,
    _wait_for_create,
)

pytestmark = [pytest.mark.workspace_panel_product_default, pytest.mark.timeout(240)]

_REAL_ROUTE = re.compile(r"/c/(?!temp:)[\w-]+$")
_MODEL_SPINNER = '[data-testid="composer-model-pending"], [data-testid="composer-model-loading"]'


def _choose_workspace_and_haiku(page: Page, workspace: Path) -> None:
    """Choose the real host folder and a concrete model through the new-chat UI."""
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
    page.get_by_test_id("new-chat-landing-agent-model-haiku").click()
    page.keyboard.press("Escape")
    if picker.get_attribute("aria-expanded") == "true":
        page.keyboard.press("Escape")
    expect(picker).to_have_attribute("aria-expanded", "false")
    expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)


@pytest.mark.parametrize("interrupt_with", ["button", "escape"])
def test_cancel_initial_message_during_real_native_startup(
    page: Page, request: pytest.FixtureRequest, tmp_path: Path, interrupt_with: str
) -> None:
    """Cancel after the real ID binds but before the native first turn starts."""
    base_url = request.config.getoption("--ui-base-url")
    if not base_url:
        pytest.skip("Requires --ui-base-url for a real server, host, and Claude Code")
    base_url = base_url.rstrip("/")
    nonce = uuid.uuid4().hex
    original_marker = f"NATIVE_STARTUP_ORIGINAL_{nonce}"
    corrected_marker = f"NATIVE_STARTUP_CORRECTED_{nonce}"
    original = f"Reply with exactly {original_marker}. Do not use tools."
    corrected = f"Reply with exactly {corrected_marker}. Do not use tools."
    creates: list[Request] = []
    responses: list[Response] = []

    def record_request(network_request: Request) -> None:
        if (
            network_request.method == "POST"
            and urlparse(network_request.url).path == "/v1/sessions"
        ):
            creates.append(network_request)

    def record_response(response: Response) -> None:
        if response.request.method == "POST" and urlparse(response.url).path == "/v1/sessions":
            responses.append(response)

    page.on("request", record_request)
    page.on("response", record_response)
    try:
        page.goto(f"{base_url}/")
        _choose_workspace_and_haiku(page, tmp_path)
        page.get_by_test_id("new-chat-landing-input").fill(original)
        page.get_by_test_id("new-chat-landing-submit").click()
        expect(page).to_have_url(_REAL_ROUTE, timeout=60_000)
        session_id = urlparse(page.url).path.rsplit("/", 1)[1]
        composer = page.get_by_role("textbox", name="Message the agent")
        expect(composer).to_be_editable()
        initial_bubble = page.locator(_USER, has_text=original_marker)
        expect(initial_bubble).to_be_visible()
        assert len(creates) == 1
        create_body = creates[0].post_data_json
        assert create_body["model_override"] == "haiku", create_body
        assert create_body.get("cost_control_mode_override") != "on", create_body

        # Observe actual startup; waiting for Interrupt would skip the broken window.
        snapshot = page.request.get(f"{base_url}/v1/sessions/{session_id}")
        assert snapshot.ok, snapshot.text()
        assert snapshot.json()["status"] not in {"running", "waiting"}, snapshot.json()
        assert page.locator(_MODEL_SPINNER).is_visible(), "The real model spinner already settled"
        assert (initial_bubble.get_attribute("data-message-id") or "").startswith("pend_")
        assert page.locator(_ASSISTANT, has_text=original_marker).count() == 0

        if interrupt_with == "button":
            interrupt = page.get_by_role("button", name="Interrupt", exact=True)
            assert interrupt.count() == 1, "No Interrupt button during real-session model startup"
            assert interrupt.is_enabled(), (
                "Interrupt is disabled during real-session model startup"
            )
            interrupt.click(timeout=1_000)
        else:
            composer.press("Escape")

        expect(composer).to_have_value(original, timeout=1_000)
        composer.fill(corrected)
        expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
        page.get_by_role("button", name="Send", exact=True).click()

        expect(page.locator(_ASSISTANT, has_text=corrected_marker)).to_be_visible(timeout=120_000)
        expect(page.locator(_USER, has_text=corrected_marker)).to_have_count(1)
        expect(page.locator(_USER, has_text=original_marker)).to_have_count(0)
        expect(page.locator(_ASSISTANT, has_text=original_marker)).to_have_count(0)
        _assert_no_canceled_input(page, base_url, session_id, original_marker)

        page.reload()
        expect(page.locator(_ASSISTANT, has_text=corrected_marker)).to_be_visible(timeout=30_000)
        expect(page.locator(_USER, has_text=corrected_marker)).to_have_count(1)
        expect(page.locator(_USER, has_text=original_marker)).to_have_count(0)
        expect(page.locator(_ASSISTANT, has_text=original_marker)).to_have_count(0)
        _assert_no_canceled_input(page, base_url, session_id, original_marker)
    finally:
        # Delete only the real session created through this browser's Submit.
        if creates:
            response = _wait_for_create(page, responses)
            page.request.delete(f"{base_url}/v1/sessions/{response.json()['id']}")
        page.remove_listener("request", record_request)
        page.remove_listener("response", record_response)

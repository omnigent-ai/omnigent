r"""Cancel a message sent while a real native model switch is pending.

Run against an isolated server + host with Claude Code and real inference.
No requests, application state, or model-switch timing are replaced::

    OMNIGENT_E2E_ALLOW_DEV_BASE_URL=1 .venv/bin/python -m pytest \
      tests/e2e_ui/chat/test_cancel_message_model_switch_live.py \
      --ui-base-url http://127.0.0.1:5173 --video=on --tracing=on \
      --output=/tmp/cancel-message-model-switch
"""

from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.start_session.test_cancel_initial_message_live import (
    _ASSISTANT,
    _USER,
    _assert_no_canceled_input,
)
from tests.e2e_ui.start_session.test_cancel_initial_message_startup_live import (
    _choose_workspace_and_haiku,
)

pytestmark = [pytest.mark.workspace_panel_product_default, pytest.mark.timeout(300)]


def test_cancel_message_during_real_model_switch(
    page: Page, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Switch Haiku to Sonnet, send and cancel during the switch, then correct it."""
    base_url = request.config.getoption("--ui-base-url")
    if not base_url:
        pytest.skip("Requires --ui-base-url for a real server, host, and Claude Code")
    base_url = base_url.rstrip("/")
    nonce = uuid.uuid4().hex
    warm_marker = f"MODEL_SWITCH_READY_{nonce}"
    original_marker = f"MODEL_SWITCH_ORIGINAL_{nonce}"
    corrected_marker = f"MODEL_SWITCH_CORRECTED_{nonce}"
    original = f"Reply with exactly {original_marker}. Do not use tools."
    corrected = f"Reply with exactly {corrected_marker}. Do not use tools."
    session_id: str | None = None

    try:
        page.goto(f"{base_url}/")
        _choose_workspace_and_haiku(page, tmp_path)
        page.get_by_test_id("new-chat-landing-input").fill(
            f"Reply with exactly {warm_marker}. Do not use tools."
        )
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and urlparse(response.url).path == "/v1/sessions"
            )
        ) as creating:
            page.get_by_test_id("new-chat-landing-submit").click()
        created = creating.value
        assert created.ok, created.text()
        session_id = created.json()["id"]
        expect(page).to_have_url(f"{base_url}/c/{session_id}")
        expect(page.locator(_ASSISTANT, has_text=warm_marker)).to_be_visible(timeout=120_000)
        expect(page.get_by_role("button", name="Interrupt", exact=True)).to_have_count(
            0, timeout=30_000
        )
        expect(
            page.locator(
                '[data-testid="composer-model-pending"], [data-testid="composer-model-loading"]'
            )
        ).to_have_count(0, timeout=30_000)

        gear = page.get_by_test_id("composer-config-gear")
        gear.click()
        page.get_by_test_id("composer-agent-edit").click()
        with page.expect_request(
            lambda network_request: (
                network_request.method == "PATCH"
                and urlparse(network_request.url).path == f"/v1/sessions/{session_id}"
            )
        ) as switching:
            page.get_by_test_id("composer-agent-model-sonnet").click()
        assert switching.value.post_data_json["model_override"] == "sonnet"
        for _ in range(3):
            if gear.get_attribute("aria-expanded") != "true":
                break
            page.keyboard.press("Escape")

        composer = page.get_by_role("textbox", name="Message the agent")
        spinner = page.get_by_test_id("composer-model-pending")
        composer.fill(original)
        assert spinner.is_visible(), "The real model switch finished before Send"
        page.get_by_role("button", name="Send", exact=True).click(timeout=1_000)
        initial_bubble = page.locator(_USER, has_text=original_marker)
        expect(initial_bubble).to_be_visible()
        assert spinner.is_visible(), "The real model switch finished before cancellation"
        assert (initial_bubble.get_attribute("data-message-id") or "").startswith("pend_")
        assert page.locator(_ASSISTANT, has_text=original_marker).count() == 0

        # Check immediately; waiting for a later turn would miss the recorded race.
        interrupt = page.get_by_role("button", name="Interrupt", exact=True)
        assert interrupt.count() == 1 and interrupt.is_enabled()
        interrupt.click(timeout=1_000)
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
        if session_id is not None:
            page.request.delete(f"{base_url}/v1/sessions/{session_id}")

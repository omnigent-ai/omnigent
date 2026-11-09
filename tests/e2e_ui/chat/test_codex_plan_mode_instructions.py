"""Plan mode at creation and composer toggles reach the real Codex model request."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.stores.conversation_store import CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)


@pytest.mark.nightly
@pytest.mark.timeout(360)
@pytest.mark.parametrize(
    "native_codex_mock_session",
    [{CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY: "plan"}],
    indirect=True,
)
def test_codex_plan_mode_creation_and_toggles_preserve_instructions(
    request: pytest.FixtureRequest,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_codex_mock_session
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    for turn_index, mode in enumerate(("Plan", "Default", "Plan")):
        if turn_index:
            page.get_by_test_id("composer-attach").click()
            plan_action = page.get_by_test_id("composer-plan-action")
            action = "Enter" if mode == "Plan" else "Exit"
            expect(plan_action).to_have_attribute("aria-label", f"{action} Plan mode")
            plan_action.click()
        expect(page.get_by_test_id("composer-plan-mode")).to_have_count(
            int(mode == "Plan"), timeout=30_000
        )

        marker = f"plan-mode-{uuid.uuid4().hex[:8]}"
        reply = f"reply-{marker}"
        reset_mock_llm(mock_llm_server_url)
        configure_mock_llm(mock_llm_server_url, [{"text": reply}], key=marker, match=marker)
        set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")
        _send(page, f"Add a hello.txt file with a greeting. Marker {marker}.")
        expect(page.locator(_ASSISTANT, has_text=reply).first).to_be_visible(
            timeout=_MOCK_TURN_TIMEOUT_MS
        )
        expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

        captured = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10).json()["requests"]
        model_request = next(item for item in captured if marker in json.dumps(item))
        developer_messages = [
            json.dumps(item) for item in model_request["input"] if item.get("role") == "developer"
        ]
        collaboration = [text for text in developer_messages if "<collaboration_mode>" in text]
        heading = "# Plan Mode" if mode == "Plan" else "# Collaboration Mode: Default"
        assert collaboration and heading in collaboration[-1], collaboration
        assert any(
            "Codex is running in the session terminal" in text for text in developer_messages
        )

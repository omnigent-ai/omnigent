"""E2E: a Codex session stays usable after the provider safety-blocks a turn.

The mock provider blocks every request whose user input still contains the
flagged message, standing in for a content-based safety classifier.
"""

from __future__ import annotations

import json
import shutil
import uuid

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _CODEX_MOCK_MODEL,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="codex CLI is required for native Codex e2e",
)

_TURN_TIMEOUT_MS = 180_000
_SAFETY_BLOCK = (
    "This request was blocked by our safety systems. Reason: Potentially unintended activity."
)
_ERROR_HEADLINE = "Codex ran into an error during this turn."
_FOLLOW_UP_REPLY = "Hello again! What would you like to do next?"
_FOLLOW_UPS = ("hellO??", "what is the activity? i intended")
# The forwarder drops the refused turn over an app-server round trip that lands
# after the error card, and reports no client-visible signal of its own.
_HISTORY_REVERT_SETTLE_MS = 1_500


def _send_and_wait_for_outcome(
    page: Page, text: str, *, errors_before: int, replies_before: int = 0
) -> None:
    """Send *text* and wait until its turn shows a new error card or the follow-up reply."""
    user_bubbles = page.locator(_USER)
    users_before = user_bubbles.count()
    _send(page, text)
    expect(user_bubbles).to_have_count(users_before + 1, timeout=_TURN_TIMEOUT_MS)
    new_error = page.get_by_test_id("error-pill").nth(errors_before)
    reply = page.locator(_ASSISTANT, has_text=_FOLLOW_UP_REPLY).nth(replies_before)
    expect(new_error.or_(reply)).to_be_visible(timeout=_TURN_TIMEOUT_MS)


def _show_error_details(page: Page, index: int) -> None:
    pill = page.get_by_test_id("error-pill").nth(index)
    expect(pill.get_by_test_id("error-headline")).to_have_text(_ERROR_HEADLINE)
    pill.get_by_role("button", name=_ERROR_HEADLINE, exact=True).click()
    details = pill.get_by_test_id("error-message-content")
    expect(details).to_contain_text(_SAFETY_BLOCK)
    details.scroll_into_view_if_needed()
    page.wait_for_timeout(1_500)


@pytest.mark.timeout(600)
def test_codex_native_follow_up_after_safety_block_gets_reply(
    request: pytest.FixtureRequest,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Follow-up messages after a safety-blocked turn get a normal reply."""
    base_url, session_id = native_codex_mock_session
    reset_mock_llm(mock_llm_server_url)
    marker = f"SAFETY-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-{marker}",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "echo scanning workspace"}),
                    }
                ]
            },
            *({"error": _SAFETY_BLOCK, "status_code": 400} for _ in range(20)),
        ],
        key=marker,
        match=marker,
        required_tools=["exec_command"],
    )

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    _send_and_wait_for_outcome(
        page, f"Scan this workspace and tidy up anything unused. {marker}", errors_before=0
    )
    _show_error_details(page, 0)
    page.wait_for_timeout(_HISTORY_REVERT_SETTLE_MS)

    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, _FOLLOW_UP_REPLY)
    try:
        replies = page.locator(_ASSISTANT, has_text=_FOLLOW_UP_REPLY)
        for answered, follow_up in enumerate(_FOLLOW_UPS):
            _send_and_wait_for_outcome(page, follow_up, errors_before=1, replies_before=answered)
            expect(replies).to_have_count(answered + 1)
        expect(page.get_by_test_id("error-pill")).to_have_count(1)
        replies.last.scroll_into_view_if_needed()
        page.wait_for_timeout(1_500)
    finally:
        # Fallbacks survive reset_mock_llm; restore the empty reply other Codex tests use.
        set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

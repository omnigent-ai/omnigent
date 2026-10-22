"""E2E: an old Codex model must not block the first web message."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import MockedCodexNativeSession
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_OLD_MODEL = "gpt-5.4"
_TIMEOUT_MS = 60_000


@pytest.mark.timeout(180)
@pytest.mark.parametrize("mocked_native_codex_session", [_OLD_MODEL], indirect=True)
def test_codex_old_model_does_not_block_first_web_message(
    page: Page,
    mocked_native_codex_session: MockedCodexNativeSession,
) -> None:
    """A pinned legacy model starts without the terminal migration prompt."""
    session = mocked_native_codex_session
    page.goto(f"{session.base_url}/c/{session.session_id}")

    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    prompt = "Process this first web message without waiting for terminal input."
    _send(page, prompt)
    expect(page.locator(_ASSISTANT, has_text="E2E_GOAL_BOOTSTRAP").first).to_be_visible(
        timeout=_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TIMEOUT_MS)

    requests = session.sidecar.requests(min_count=1, timeout_ms=30_000)
    assert requests[0]["body"]["model"] == _OLD_MODEL
    assert prompt in str(requests[0]["body"]["input"])

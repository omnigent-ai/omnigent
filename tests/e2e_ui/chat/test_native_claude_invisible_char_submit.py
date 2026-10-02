r"""A claude-native draft with an invisible character must still submit.

Regression guard for the blind-submit path in ``_paste_and_submit``
(``omnigent/harnesses/claude_native/bridge.py``). The bridge injects a web-composer
message into the real Claude Code TUI with a bracketed paste, then looks for the
draft's needle on the input row to confirm the paste landed. When it can't find
the needle (``draft_seen=False``) it sends a single blind Enter and returns
success WITHOUT ``_verify_submit_accepted``.

A message carrying an embedded U+FEFF triggers Claude Code's "Removed N invisible
characters . review and press Enter to send" confirmation, which swallows that one
Enter, so the draft stays stuck in the composer, the message never reaches the
model, and no reply ever comes back. The web composer trims *leading*/trailing
invisibles, so the character rides here embedded MID-string (which ``String.trim``
cannot remove) through the real composer, exactly as a user pasting text copied
from a page/PDF would hit it.

A correct submit path re-verifies delivery and re-sends when the first Enter was
consumed, so the mock's reply token comes back and this test passes.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

# U+FEFF between two visible words, inside the first characters the bridge uses
# as its submit needle, so it reaches the input box unstripped by String.trim.
_BOM = "﻿"
_MESSAGE = f"pasted{_BOM}invisible char reply now"
_REPLY_TOKEN = "INVISIBLE-CHAR-DELIVERED"

# Covers Claude boot + terminal attach + inject + (post-fix) re-submit + mock reply.
_DELIVERY_TIMEOUT_MS = 120_000


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_invisible_char_draft_still_submits(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A composer message with an embedded invisible char reaches the model and replies."""
    base_url, session_id = native_claude_mock_session
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", _REPLY_TOKEN)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, _REPLY_TOKEN)

    _send(page, _MESSAGE)

    expect(page.locator(_ASSISTANT, has_text=_REPLY_TOKEN)).to_be_visible(
        timeout=_DELIVERY_TIMEOUT_MS
    )

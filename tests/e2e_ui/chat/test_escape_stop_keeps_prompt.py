"""E2E: pressing Esc to stop a running turn must KEEP the just-sent prompt.

Journey: type a prompt, press Enter (it renders immediately as a user bubble
and the agent starts running), then press Escape to stop the agent — the
just-sent prompt must stay in the chat, matching the composer Stop button.

Why this drives a native (claude-native) session rather than a plain SDK one:
the composer's Escape shortcut fires the interrupt only once the server
reports the session working (``sessionStatus`` running/waiting). On the SDK
path the user message is promoted to a committed transcript bubble
(``session.input.consumed``) before that point, so it survives a stop. On the
native path the prompt is committed only after the terminal round-trip, so
there is a real window where it is still an optimistic ``pendingUserMessages``
entry while the session is already working — pressing Esc promptly wins that
race, and a stop that wipes pending messages deletes the bubble for the rest
of the live session (only a page reload re-fetches it).
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

_COMPOSER_LABEL = "Message the agent"
# Distinctive so the user bubble is unambiguous in the transcript.
_PROMPT = "sentinel prompt keep me when Esc stops the agent"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'


@pytest.mark.timeout(300)
def test_escape_stop_keeps_the_just_sent_prompt(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Esc-to-stop must not delete the prompt the user just sent."""
    base_url, session_id = native_claude_mock_session

    # A slow reply for the sentinel turn keeps the session "working" (so Esc
    # arms) long enough to interrupt before the terminal round-trip commits
    # the prompt. A short fallback answers Claude's own background requests.
    set_fallback_mock_llm(mock_llm_server_url, "default", "ok")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "ok")
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "slow sentinel reply", "delay": 12}],
        match="sentinel prompt",
    )

    interrupt_posts: list[str] = []

    def record(request: Request) -> None:
        if request.method != "POST":
            return
        if urlparse(request.url).path != f"/v1/sessions/{session_id}/events":
            return
        body = request.post_data_json
        if isinstance(body, dict) and body.get("type") == "interrupt":
            interrupt_posts.append("interrupt")

    page.on("request", record)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    prompt_bubble = page.locator(_USER_BUBBLE, has_text=_PROMPT)

    # Type the prompt and press Enter — it renders as a user bubble and the
    # agent starts running.
    composer.fill(_PROMPT)
    composer.press("Enter")
    expect(prompt_bubble).to_be_visible(timeout=30_000)

    # Press Esc to stop the running agent, as promptly as a user would. The
    # composer shortcut fires the interrupt only once the session is working,
    # so tap Esc until the interrupt POST is observed (bounded).
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and not interrupt_posts:
        composer.press("Escape")
        page.wait_for_timeout(50)
    assert interrupt_posts, "Esc never triggered an interrupt (session never armed the stop)"

    # Let the post-interrupt transcript settle.
    page.wait_for_timeout(3_000)

    # Contract: the just-sent prompt must survive the Esc-stop, matching the
    # Stop button's behavior.
    expect(prompt_bubble).to_be_visible(timeout=4_000)

"""E2E: a send the server holds open says "Still sending…" until the server answers.

On a slow phone network the send POST can go unanswered for a long time. The bubble
must not look delivered in the meantime, and the note must clear once the server
answers. Desktop Chromium at a phone profile stands in for the native WebView.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm

_PHONE = pytest.mark.browser_context_args(
    viewport={"width": 390, "height": 844},
    is_mobile=True,
    has_touch=True,
)

_COMPOSER = 'textarea[aria-label="Message the agent"]'
_STILL_SENDING = '[data-testid="message-still-sending"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'

_PROMPT = "held-send-sentinel list the overnight alerts"
_REPLY = "held-send-alert-list-reply"

_TIMEOUT_MS = 30_000


@_PHONE
def test_held_send_says_still_sending_until_the_server_answers(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """The bubble of a send whose POST is held shows the note, and loses it once answered."""
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _REPLY}],
        key="held-send-still-sending",
        match=_PROMPT,
    )
    held: list[Route] = []

    def _hold_send(route: Route) -> None:
        # Keep the POST open without answering until the test releases it.
        if route.request.method == "POST" and _PROMPT in (route.request.post_data or ""):
            held.append(route)
            return
        route.continue_()

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.locator(_COMPOSER)
    expect(composer).to_be_visible(timeout=_TIMEOUT_MS)
    page.route(f"**/v1/sessions/{session_id}/events", _hold_send)

    composer.tap()
    composer.fill(_PROMPT)
    page.get_by_role("button", name="Send", exact=True).tap()

    expect(page.locator(_STILL_SENDING)).to_be_visible(timeout=_TIMEOUT_MS)
    assert len(held) == 1, "the send POST was never held; the note cannot be checked"

    held[0].continue_()
    expect(page.locator(_STILL_SENDING)).to_have_count(0, timeout=_TIMEOUT_MS)
    expect(page.locator(_ASSISTANT_BUBBLE).filter(has_text=_REPLY)).to_be_visible(
        timeout=_TIMEOUT_MS
    )

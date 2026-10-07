"""A turn the user watched start and stop must never end with no feedback.

Mid-session, a completed-but-empty model response (the stream completes, but its
only output item is an empty assistant message) used to resolve silently: no
reply, no error, no notice. The executor now retries once and then surfaces a
retryable error notice that names the empty completion.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'
_WORKING = '[data-testid="working-indicator"]'

_TURN1_TOKEN = "SILENT-STOP-TURN-ONE"
_TURN2_TOKEN = "SILENT-STOP-TURN-TWO"
# Deeper than the executor's retry window (2 attempts), so the fault stays active
# until the turn fails loud and the mock's non-empty fallback is never reached.
_EMPTY_REPLIES = 5


def _send(page: Page, text: str) -> None:
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.timeout(300)
def test_turn_that_stops_must_leave_feedback(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session

    # Turn 1 is queued deep too, so a background title request cannot drain it.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "hello from turn one"}] * 3,
        key="silent-stop-t1",
        match=_TURN1_TOKEN,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": ""}] * _EMPTY_REPLIES,
        key="silent-stop-t2",
        match=_TURN2_TOKEN,
    )

    page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, f"{_TURN1_TOKEN} say hello")
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    _send(page, f"{_TURN2_TOKEN} tell me more")
    expect(page.locator(_USER)).to_have_count(2, timeout=15_000)
    expect(page.locator(_WORKING).first).to_be_visible(timeout=30_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=90_000)

    pill = page.get_by_test_id("error-pill").first
    expect(
        pill,
        "turn 2 started and stopped but surfaced no error notice (silent stop)",
    ).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("error-headline").first).to_contain_text(
        "ran into an error during this turn"
    )
    pill.click()
    expect(page.get_by_test_id("error-message-content").first).to_contain_text("empty completion")

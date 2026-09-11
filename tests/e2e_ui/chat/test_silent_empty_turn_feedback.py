"""UI journey: a turn that starts must never end with NO feedback.

Reported journey: mid-session, the user enters a prompt, Omnigent starts
running (the working indicator appears), then it suddenly stops — no
assistant reply, no error pill, no banner. The user is left staring at
their own message with zero indication of what happened.

Live reproduction on the unfixed build: a model response that completes
with an *empty* message output item (a realistic gateway/model hiccup —
the response is well-formed but contentless) ends the turn silently. The
openai-agents executor's empty-turn guard (``_is_empty_turn`` in
``omnigent/inner/openai_agents_sdk_executor.py``) counts the empty
message item as "output", so neither the empty-turn retry nor the
fail-loud gate fires, and the turn resolves as ``TurnComplete("")``: no
assistant item is persisted, no error item is persisted, and the SPA's
working indicator simply disappears.

Journey driven here, on the real web SPA against a live server + runner:

1. open a session and send a prompt; a normal reply arrives (mid-session)
2. send a second prompt; the agent starts working (indicator visible)
3. the model returns a completed-but-empty response
4. the working indicator clears and the turn is over

Regression guard: the final assertion — the user must receive SOME
feedback for a turn that ran (an assistant reply OR an error/notice pill)
— FAILS on the unfixed build (nothing at all renders) and passes once a
fix surfaces the empty turn (retry to success, error pill, or an explicit
"no output" notice all satisfy it).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'
_WORKING = '[data-testid="working-indicator"]'

# Unique routing tokens so each turn draws from its own mock queue and a
# stray request from another test cannot contaminate this journey.
_TURN1_TOKEN = "SILENT-STOP-TURN-ONE"
_TURN2_TOKEN = "SILENT-STOP-TURN-TWO"


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.timeout(300)
def test_turn_that_stops_must_leave_feedback(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A turn that visibly started must not end with zero user feedback.

    The mock scripts turn 2 as a completed-but-empty model response
    (``text: ""`` — the SSE stream opens and completes normally but the
    only output item is an empty message). Queued three deep so a fix
    that retries the empty turn keeps hitting the same fault and must
    still surface *something* to the user.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :param mock_llm_server_url: Mock LLM base URL for scripting responses.
    """
    base_url, session_id = seeded_session

    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "hello from turn one"}],
        key="silent-stop-t1",
        match=_TURN1_TOKEN,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": ""}] * 3,
        key="silent-stop-t2",
        match=_TURN2_TOKEN,
    )

    page.goto(f"{base_url}/c/{session_id}")

    # Turn 1: a normal exchange, so turn 2 happens "in the middle of a
    # session" exactly as reported.
    _send(page, f"{_TURN1_TOKEN} say hello")
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # Turn 2: the prompt whose turn dies into a completed-but-empty
    # response. The turn must visibly START (working indicator) so the
    # final assertion fails specifically on the silent STOP, not on a
    # send that never dispatched.
    _send(page, f"{_TURN2_TOKEN} tell me more")
    expect(page.locator(_USER)).to_have_count(2, timeout=15_000)
    expect(page.locator(_WORKING).first).to_be_visible(timeout=30_000)

    # ... and it must STOP (indicator gone) — "starts running, then stops".
    expect(page.locator(_WORKING)).to_have_count(0, timeout=90_000)

    # THE BUG: after a turn the user watched start and stop, the
    # transcript must show some outcome for it — a second assistant
    # reply (a fix that retries into output) OR an error/notice pill (a
    # fix that fails loud). On the unfixed build NEITHER exists: the
    # user's message just sits there with no reply, no error, no notice.
    # ``.first`` keeps the or_ locator strict-safe when both render.
    feedback = page.locator(_ASSISTANT).nth(1).or_(page.get_by_test_id("error-pill").first).first
    expect(
        feedback,
        "turn 2 started and stopped but left no feedback at all: no assistant "
        "reply and no error/notice pill rendered (silent stop)",
    ).to_be_visible(timeout=15_000)

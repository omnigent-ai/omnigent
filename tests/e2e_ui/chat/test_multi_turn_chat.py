"""UI journey: a second turn typed into the composer sees turn-1 context.

The smoke test proves one prompt round-trips; this proves the part
users actually depend on: history threading. Turn 1 establishes a
random token, turn 2 asks for it back, and the token can only appear
in turn 2's bubble if UI -> server -> runner -> LLM replayed the
conversation.

"""

from __future__ import annotations

import uuid

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Ask the agent anything…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# The recall prompt's stable substring — present only in turn 2, so the mock
# can content-route turn 2 to the token-echo response.
_RECALL_PROMPT = "What token did I ask you to remember? Reply with the token only."


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def test_multi_turn_recall_through_ui(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    token = f"ui-recall-{uuid.uuid4().hex[:8]}"

    # The mock routes by message content: turn 1 (the only message carrying
    # the token) replies "stored"; turn 2 (the recall prompt) replies with the
    # token. Both match strings are unique to their turn, so each queue fires
    # exactly once and the recall assertion is deterministic.
    configure_mock_llm(mock_llm_server_url, [{"text": "stored"}], key="recall-store", match=token)
    configure_mock_llm(
        mock_llm_server_url, [{"text": token}], key="recall-echo", match=_RECALL_PROMPT
    )

    page.goto(f"{base_url}/c/{session_id}")

    _send(
        page,
        f"Remember this token exactly, you will be asked to repeat it "
        f"verbatim later: {token}. Reply with just the word 'stored'.",
    )
    # Turn 1 terminal: an assistant bubble rendered AND the working
    # shimmer is gone (sending turn 2 mid-stream would test steering,
    # not history replay).
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    _send(page, _RECALL_PROMPT)
    # Two user bubbles = turn 2 actually left the composer.
    expect(page.locator('[data-testid="message-bubble"][data-role="user"]')).to_have_count(
        2, timeout=15_000
    )
    # The literal token in an assistant bubble is only producible from
    # turn-1 history; a fresh-context reply cannot contain it.
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(timeout=60_000)

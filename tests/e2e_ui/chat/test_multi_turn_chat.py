"""UI journey: a second turn typed into the composer sees turn-1 context.

The smoke test proves one prompt round-trips; this proves the part
users actually depend on: history threading. Turn 1 establishes a
random token, turn 2 asks for it back, and the token can only appear
in turn 2's bubble if UI -> server -> runner -> LLM replayed the
conversation.

"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


# Real-LLM nondeterminism: the model must reply "stored" then echo the token
# verbatim. llm_flaky reruns rotate the model per attempt (databricks-gpt-5-4 ->
# -5-5), which is exactly the right retry for a recall flake. Safe here because
# e2e-ui.yml runs serially (no xdist) with no --timeout=180 cap.
@pytest.mark.llm_flaky(reruns=2, reruns_delay=1)
def test_multi_turn_recall_through_ui(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    token = f"ui-recall-{uuid.uuid4().hex[:8]}"
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

    _send(page, "What token did I ask you to remember? Reply with the token only.")
    # Two user bubbles = turn 2 actually left the composer.
    expect(page.locator('[data-testid="message-bubble"][data-role="user"]')).to_have_count(
        2, timeout=15_000
    )
    # The literal token in an assistant bubble is only producible from
    # turn-1 history; a fresh-context reply cannot contain it.
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(timeout=60_000)

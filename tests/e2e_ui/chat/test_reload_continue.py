"""UI journey: reload hydrates history from the snapshot, then continue.

A reload kills the SPA's in-memory state, so the turn-1 bubble can only
re-render from ``GET /v1/sessions/{id}``; the post-reload turn proves
the re-established stream still dispatches and sees the old context.

"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'
_WORKING = '[data-testid="working-indicator"]'


# Back on nightly burn-in: flaked on its first PR-gate run (turn-2
# reply paraphrased instead of echoing the token verbatim).
@pytest.mark.nightly
def test_reload_hydrates_history_and_continues(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    token = f"ui-reload-{uuid.uuid4().hex[:8]}"
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(
        f"Remember this token exactly, you will be asked to repeat it "
        f"verbatim later: {token}. Reply with just the word 'stored'."
    )
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    page.reload()

    # Hydration: the optimistic in-memory bubble died with the reload,
    # so a visible turn-1 user bubble must have come from the snapshot.
    expect(page.locator(_USER, has_text=token).first).to_be_visible(timeout=15_000)

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill("Repeat the token from my first message, exactly, and nothing else.")
    page.get_by_role("button", name="Send", exact=True).click()

    # Turn 2 rendered (2 user bubbles) and produced a second assistant
    # bubble; asserting the count first keeps the `.last` content check
    # from matching a turn-1 echo of the token.
    expect(page.locator(_USER)).to_have_count(2, timeout=15_000)
    expect(page.locator(_ASSISTANT).nth(1)).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    # Post-reload context retention: only server-side history can
    # supply the token after the in-memory state was destroyed.
    expect(page.locator(_ASSISTANT, has_text=token).last).to_be_visible(timeout=15_000)

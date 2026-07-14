"""Browser coverage for labeled arguments in a policy approval card."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_APPROVAL_CARD = '[data-testid="approval-card"]'
_ARGS_BLOCK = '[data-testid="approval-args"]'

# Allow for the agent cold start before the gated tool call appears.
_AGENT_TURN_TIMEOUT_MS = 120_000


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_approval_card_renders_labeled_args(
    page: Page,
    approval_session: tuple[str, str],
) -> None:
    """Render labeled arguments while retaining the raw call details."""
    base_url, session_id = approval_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Run the command now.")
    page.get_by_role("button", name="Send", exact=True).click()

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)

    args_block = card.locator(_ARGS_BLOCK)
    expect(args_block).to_be_visible()
    expect(args_block.get_by_text("command")).to_be_visible()
    expect(args_block.get_by_text("git push origin main")).to_be_visible()

    raw_toggle = card.get_by_text("Raw call")
    expect(raw_toggle).to_be_visible()
    raw_toggle.click()
    expect(card.locator("details pre")).to_be_visible()

    card.get_by_role("button", name="Approve").click()
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=30_000)

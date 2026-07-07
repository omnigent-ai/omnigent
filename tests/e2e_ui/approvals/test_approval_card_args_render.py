"""E2E: a policy-ASK card renders the gated call as labeled arguments.

A tool-call preview that parses as ``{name, arguments}`` now renders as a
labeled argument block (``data-testid="approval-args"``) with the raw JSON
demoted to a collapsed "Raw call" details element, instead of a raw JSON
dump (``web/src/components/blocks/ApprovalCard.tsx``). When the arguments
carry the optional ``approval_context`` convention, that text is promoted
to prose above the block — covered unit-side in ``ApprovalCard.test.tsx``;
this test drives the browser-rendered path on the standard gated-push
fixture, whose ``sys_os_shell`` call already produces the parseable shape.

Same ``approval_session`` fixture and marks as ``test_approval_card.py``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_APPROVAL_CARD = '[data-testid="approval-card"]'
_ARGS_BLOCK = '[data-testid="approval-args"]'

# The agent must boot, take a turn, and emit the gated tool call before the
# card appears — cold-start can be slow, so allow well past the streaming
# default but under the test's 600s ceiling.
_AGENT_TURN_TIMEOUT_MS = 120_000


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_approval_card_renders_labeled_args(
    page: Page,
    approval_session: tuple[str, str],
) -> None:
    """Gated tool call → labeled argument block + collapsed raw call."""
    base_url, session_id = approval_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Run the command now.")
    page.get_by_role("button", name="Send", exact=True).click()

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)

    # The gated call renders as labeled arguments: the shell command
    # appears as a labeled value, not inside a raw JSON dump.
    args_block = card.locator(_ARGS_BLOCK)
    expect(args_block).to_be_visible()
    expect(args_block.get_by_text("command")).to_be_visible()
    expect(args_block.get_by_text("git push origin main")).to_be_visible()

    # The verbatim preview stays reachable behind the collapsed details.
    raw_toggle = card.get_by_text("Raw call")
    expect(raw_toggle).to_be_visible()
    raw_toggle.click()
    expect(card.locator("details pre")).to_be_visible()

    # The card still resolves normally through the labeled render.
    card.get_by_role("button", name="Approve").click()
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=30_000)

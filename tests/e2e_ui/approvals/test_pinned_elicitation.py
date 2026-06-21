"""E2E: a pending choice/approval prompt pins to the bottom of the conversation.

Issue #206: when Omnigent presents a choice/selection box it rendered inline
mid-conversation and scrolled out of view as the conversation grew, so users
missed that a decision was pending and the turn silently blocked. The fix
pins the ACTIVE/pending card just above the composer (``PinnedElicitationBar``
in ``ap-web/src/pages/ChatPage.tsx``) and suppresses its inline copy via
``PinnedElicitationIdContext`` (``BlockRenderer.tsx``) so the actionable
prompt lives in exactly one place — where attention lands. Once resolved,
the bar disappears and the responded summary card returns to its inline place
in the scroll history.

This drives the full loop on the openai-agents harness via the
``approval_session`` fixture (a ``blast_radius`` guardrail that gates pushes):
send a turn that makes the agent attempt a gated ``git push``, wait for the
pending card, assert it is pinned (and the inline copy is suppressed), then
Approve and assert the responded card returns inline. Real LLM in the loop →
nightly + a generous timeout, matching the other agent-driven UI suites.
"""

from __future__ import annotations

import re
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_APPROVAL_CARD = '[data-testid="approval-card"]'
_PINNED_SLOT = '[data-testid="pinned-elicitation"]'
# A pending card that lives INSIDE the pinned slot (descendant combinator).
_PINNED_PENDING = f'{_PINNED_SLOT} [data-testid="approval-card"][data-state="pending"]'

# The agent must boot, take a turn, and emit the gated tool call before the
# card appears — cold-start can be slow, so allow well past the streaming
# default but under the test's 600s ceiling.
_AGENT_TURN_TIMEOUT_MS = 120_000


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.25) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_pending_choice_box_pins_above_composer(
    page: Page,
    approval_session: tuple[str, str],
) -> None:
    """Gated tool call → pending card pinned above composer → Approve → inline."""
    base_url, session_id = approval_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Run the command now.")
    page.get_by_role("button", name="Send", exact=True).click()

    # The agent calls the gated push; the policy ASK surfaces a pending card.
    # Issue #206: that card must render inside the pinned slot above the
    # composer, not inline in the scroll history.
    pinned_card = page.locator(_PINNED_PENDING).first
    expect(pinned_card).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)
    expect(pinned_card.get_by_text("Approval required")).to_be_visible()
    # The server is genuinely parked on this prompt, not just an optimistic UI.
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"

    # The pinned slot itself is visible and labels the hoisted prompt.
    expect(page.locator(_PINNED_SLOT)).to_be_visible()
    expect(
        page.locator(_PINNED_SLOT).get_by_text(re.compile(r"Pinned · decision required"))
    ).to_be_visible()

    # The inline copy is suppressed: there is exactly ONE pending approval card
    # in the DOM, and it is the one inside the pinned slot. Without the
    # PinnedElicitationIdContext suppression there would be two (pinned + inline).
    expect(page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')).to_have_count(1)

    # Resolve the prompt. The pinned card's Approve button is the one in view.
    pinned_card.get_by_role("button", name="Approve").click()

    # Once resolved, the pinned slot vanishes (no pending elicitation to pin)
    # and the responded summary card returns to its INLINE place in the
    # transcript — i.e. it is NOT a descendant of the pinned slot.
    expect(page.locator(_PINNED_SLOT)).to_have_count(0)
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=30_000)
    expect(responded.get_by_text("Approved", exact=False).first).to_be_visible()
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))

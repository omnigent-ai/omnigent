"""E2E: a pending approval surfaces on the /inbox page and resolves there.

The Inbox page (``ap-web/src/pages/InboxPage.tsx``) gathers every pending
``response.elicitation_request`` across the user's sessions and renders each
as the same ``ApprovalCard`` the chat uses, with a local submit handler that
posts the verdict to the owning session. This test raises a gated-push
approval in a session, navigates to ``/inbox``, asserts the prompt is listed,
approves it from the inbox, and asserts the item drains (the row's pending
count drops to zero, so it falls out of the inbox).

Driven by the same ``approval_session`` fixture as the in-chat card test;
real LLM → nightly + generous timeout.
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_APPROVAL_CARD = '[data-testid="approval-card"]'
_INBOX_ITEM = '[data-testid="inbox-item"]'
_AGENT_TURN_TIMEOUT_MS = 120_000


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.5) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_pending_approval_surfaces_and_resolves_in_inbox(
    page: Page,
    approval_session: tuple[str, str],
) -> None:
    """Gated tool call → /inbox lists the prompt → Approve there → it drains."""
    base_url, session_id = approval_session

    # Raise the approval from the chat surface, then leave it pending.
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Run the command now.")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first).to_be_visible(
        timeout=_AGENT_TURN_TIMEOUT_MS
    )
    # Confirm the server is parked before we navigate away.
    _wait_for(lambda: bool(_pending_elicitations(base_url, session_id)))

    # The inbox gathers the prompt from the session's snapshot.
    page.goto(f"{base_url}/inbox")
    item = page.locator(_INBOX_ITEM).first
    expect(item).to_be_visible(timeout=30_000)
    card = item.locator(_APPROVAL_CARD)
    expect(card).to_be_visible()
    expect(card.get_by_text("Approval required")).to_be_visible()

    # Approve from the inbox: the verdict routes to the owning session, the
    # server drains the prompt, and the row's pending count drops to zero so
    # the item falls out of the inbox.
    card.get_by_role("button", name="Approve").click()
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))
    expect(page.locator(_INBOX_ITEM)).to_have_count(0, timeout=30_000)

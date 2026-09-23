"""E2E: an in-chat approval prompt renders and resolves.

When a tool call trips a policy that returns ASK, the runner forwards the
gate to the server, which publishes a ``response.elicitation_request`` the
chat renders as an ``ApprovalCard``. These tests drive the full loop on the
openai-agents harness: send a turn that makes the agent attempt a gated
``git push``, wait for the pending card, resolve it, and assert the server
clears the prompt.

The ``approval_session`` fixture supplies an agent whose ``blast_radius``
guardrail gates pushes; the gate fires on the tool call, so the push never has
to succeed. Real LLM in the loop means nightly coverage and a generous
timeout.

Fast component coverage owns native-vendor labels and the composer's pending
approval state. The full-stack cases here stay focused on the real policy ASK
boundary: an agent tool call parks on the server until the browser approves or
rejects it.
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Send a message…"
_APPROVAL_CARD = '[data-testid="approval-card"]'

# The agent must boot, take a turn, and emit the gated tool call before the
# card appears — cold-start can be slow, so allow well past the streaming
# default but under the test's 600s ceiling.
_AGENT_TURN_TIMEOUT_MS = 120_000


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_approval_card_renders_and_approves(
    page: Page,
    approval_session: tuple[str, str],
) -> None:
    """Gated tool call → pending approval card → Approve → resolved."""
    base_url, session_id = approval_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Run the command now.")
    page.get_by_role("button", name="Send", exact=True).click()

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)
    expect(card.get_by_text("Approval required")).to_be_visible()
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"

    card.get_by_role("button", name="Approve").click()

    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=30_000)
    expect(responded.get_by_text("Approved", exact=False).first).to_be_visible()
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_approval_card_reject(
    page: Page,
    approval_session: tuple[str, str],
) -> None:
    """Rejecting a pending approval flips the card to the rejected state."""
    base_url, session_id = approval_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Run the command now.")
    page.get_by_role("button", name="Send", exact=True).click()

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)
    card.get_by_role("button", name="Reject").click()

    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=30_000)
    expect(responded.get_by_text("Rejected", exact=False).first).to_be_visible()
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))


def _wait_for(predicate, *, timeout_s: float = 15.0, interval_s: float = 0.25) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")

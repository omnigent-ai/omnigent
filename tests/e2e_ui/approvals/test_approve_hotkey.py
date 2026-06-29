"""E2E: Cmd/Ctrl+Enter accepts the pending in-chat approval prompt.

Covers ``useApproveHotkey`` (``web/src/hooks/useApproveHotkey.ts``), bound
once at the app shell: when a tool call trips a policy that returns ASK, the
chat renders a pending ``ApprovalCard``, and Cmd+Enter (Ctrl+Enter on
Win/Linux) is the keyboard equivalent of clicking **Approve** on that card.

This drives the full loop on the openai-agents harness rather than the unit
test's mocked store: send a turn that makes the agent attempt a gated
``git push``, wait for the pending card, press the hotkey, and assert the card
flips to its "Approved" responded state and the server clears the parked
prompt. The hook binds on ``window`` in the capture phase, so the keystroke
resolves the approval regardless of where focus sits after Send.

The ``approval_session`` fixture (conftest) supplies an agent whose
``blast_radius`` guardrail gates pushes; the gate fires on the *tool call*, so
the push never has to succeed. Real LLM in the loop -> nightly + a generous
timeout, matching the other agent-driven UI suites.
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
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
def test_ctrl_enter_accepts_pending_approval(
    page: Page,
    approval_session: tuple[str, str],
) -> None:
    """Gated tool call → pending card → Ctrl+Enter → resolved 'Approved'."""
    base_url, session_id = approval_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Run the command now.")
    page.get_by_role("button", name="Send", exact=True).click()

    # The agent calls the gated push; the policy ASK surfaces a pending card.
    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)
    expect(card.get_by_text("Approval required")).to_be_visible()
    # The server is genuinely parked on this prompt, not just an optimistic UI.
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"

    # Accept via the keyboard instead of the Approve button. Ctrl+Enter is the
    # Win/Linux chord (CI runs Linux chromium); the hook also accepts Cmd+Enter
    # via metaKey on macOS. The window-level capture handler fires wherever
    # focus landed after Send, so no element needs explicit focus first.
    page.keyboard.press("Control+Enter")

    # Card transitions to the responded "Approved" state and the parked
    # server-side prompt drains — same resolution the button click produces.
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=30_000)
    expect(responded.get_by_text("Approved", exact=False).first).to_be_visible()
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))


def _wait_for(predicate, *, timeout_s: float = 15.0, interval_s: float = 0.25) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")

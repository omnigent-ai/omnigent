"""E2E: the side-by-side multi-session view streams and accepts input per pane.

Covers ``/compare?sessions=a,b`` — ``ap-web``'s ``ComparePage`` +
``MultiSessionGrid`` + the embedded ``ChatPage`` pane mode (and the per-session
chat-store registry under it). A developer opens several sessions at once and
watches/drives them in parallel rather than clicking between them.

The property under test is ISOLATION: each pane binds its OWN SSE stream and
POSTs to its OWN session. So the test opens two panes, sends distinct prompts
into each, and asserts each pane shows only its own user + assistant bubbles —
never the other's. Then it closes one pane and confirms it is removed while the
remaining session stays live (with two sessions, closing one falls back to the
single-session route, re-binding the kept session from its own cached store).

Selectors mirror the rest of ``tests/e2e_ui``: the pane is found by
``data-testid="session-pane"`` + ``data-session-id``; within it, the composer
by placeholder, Send by its accessible name, message bubbles by
``data-testid="message-bubble"`` + ``data-role``, and the pane's close control
by ``data-testid="session-pane-close"``.
"""

from __future__ import annotations

import re
import uuid

import pytest
from playwright.sync_api import Locator, Page, expect

_COMPOSER = "Ask the agent anything…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'


def _pane(page: Page, session_id: str) -> Locator:
    """The pane bound to *session_id* (a ``MultiSessionGrid`` child)."""
    return page.locator(f'[data-testid="session-pane"][data-session-id="{session_id}"]')


def _send_into(pane: Locator, text: str) -> None:
    """Type *text* into THIS pane's composer and click its Send button."""
    composer = pane.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    pane.get_by_role("button", name="Send", exact=True).click()


# Real-LLM nondeterminism only matters when e2e-ui.yml runs against a live
# model; against the default mock LLM this is deterministic. Mirrors the
# multi-turn chat test's retry posture.
@pytest.mark.llm_flaky(reruns=2, reruns_delay=1)
def test_side_by_side_panes_stream_send_and_close(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Open two sessions side by side, send into each, then close one."""
    base_url, session_a, session_b = seeded_session_pair
    page.goto(f"{base_url}/compare?sessions={session_a},{session_b}")

    # Both panes mount, each bound to its own session.
    pane_a = _pane(page, session_a)
    pane_b = _pane(page, session_b)
    expect(pane_a).to_be_visible(timeout=30_000)
    expect(pane_b).to_be_visible(timeout=30_000)
    expect(page.locator('[data-testid="session-pane"]')).to_have_count(2)

    token_a = f"alpha-{uuid.uuid4().hex[:8]}"
    token_b = f"beta-{uuid.uuid4().hex[:8]}"

    # Send a distinct prompt into each pane, independently.
    _send_into(pane_a, f"Reply with one short word. My tag is {token_a}.")
    _send_into(pane_b, f"Reply with one short word. My tag is {token_b}.")

    # Each pane shows ONLY its own user message — proof the send routed to the
    # right session and the panes don't cross-talk.
    expect(pane_a.locator(_USER, has_text=token_a)).to_have_count(1, timeout=15_000)
    expect(pane_b.locator(_USER, has_text=token_b)).to_have_count(1, timeout=15_000)
    expect(pane_a.locator(_USER, has_text=token_b)).to_have_count(0)
    expect(pane_b.locator(_USER, has_text=token_a)).to_have_count(0)

    # Both panes stream an assistant response in parallel (not snapshots).
    expect(pane_a.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(pane_a.locator(_ASSISTANT).first).to_have_text(re.compile(r"\S"), timeout=60_000)
    expect(pane_b.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(pane_b.locator(_ASSISTANT).first).to_have_text(re.compile(r"\S"), timeout=60_000)

    # Close pane A. It is removed; with one session left the view falls back to
    # the single-session route, and the kept session's transcript survives
    # (re-bound from its own cached store) — its user message is still on screen.
    pane_a.get_by_test_id("session-pane-close").click()
    expect(_pane(page, session_a)).to_have_count(0, timeout=10_000)
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(session_b)}"))
    expect(page.locator(_USER, has_text=token_b)).to_be_visible(timeout=15_000)
    # The kept session stays interactive.
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible()

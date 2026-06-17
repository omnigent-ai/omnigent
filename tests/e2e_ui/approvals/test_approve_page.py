"""E2E: the standalone ``/approve/<sid>/<eid>`` page resolves an elicitation.

A policy ASK can be answered three ways: the in-chat ``ApprovalCard``
(``test_approval_card.py``), the ``/inbox`` page (``test_inbox_approval.py``),
and the standalone approval page (``pages/ApprovePage.tsx``) — the URL the REPL
prints when a policy returns ASK in URL mode, openable by anyone with the link
and no surrounding app shell. This suite covers that third surface: park a real
gated-push ASK, navigate straight to ``/approve/<sid>/<eid>``, and resolve it
there.

The page fetches the elicitation from ``GET /v1/sessions/<sid>/elicitations/<eid>``
and posts the verdict to the matching ``/resolve`` endpoint — the same backing
calls the inline card uses, just on a bare route. Driven by the same
``approval_session`` fixture as the in-chat card test (real LLM emits the gated
``git push``), so it carries a generous per-test timeout.

The load-bearing assertion is that the server's parked prompt drains after the
page's Approve / Reject — proof the standalone route resolves the *same*
server-side elicitation the chat would, not a detached copy.
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_APPROVAL_CARD = '[data-testid="approval-card"]'
# The agent must boot, take a turn, and emit the gated tool call before the
# elicitation parks — cold-start can be slow, under the test's 600s ceiling.
_AGENT_TURN_TIMEOUT_MS = 120_000


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.25):
    """Poll *predicate* until it returns a truthy value, then return it."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


def _park_elicitation(page: Page, base_url: str, session_id: str) -> str:
    """Drive the agent to a parked gated-push ASK and return its elicitation id.

    Sends the deterministic "run the command" turn the ``approval_session``
    agent answers with a gated ``git push``, waits for the in-chat pending card
    (proof the gate fired), then reads the elicitation id from the session
    snapshot's ``pending_elicitations`` (each event carries ``elicitation_id``).

    :param page: Playwright page used to send the chat turn.
    :param base_url: Spawned server base URL.
    :param session_id: The approval session id.
    :returns: The parked elicitation's id, e.g. ``"elicit_ab12..."``.
    """
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Run the command now.")
    page.get_by_role("button", name="Send", exact=True).click()

    expect(page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first).to_be_visible(
        timeout=_AGENT_TURN_TIMEOUT_MS
    )
    pending = _wait_for(lambda: _pending_elicitations(base_url, session_id))
    elicitation_id = pending[0].get("elicitation_id")
    assert isinstance(elicitation_id, str) and elicitation_id, f"no elicitation_id in {pending[0]}"
    return elicitation_id


@pytest.mark.timeout(600)
def test_approve_page_approves(
    page: Page,
    approval_session: tuple[str, str],
) -> None:
    """Standalone page renders the pending prompt and Approve drains it."""
    base_url, session_id = approval_session
    elicitation_id = _park_elicitation(page, base_url, session_id)

    page.goto(f"{base_url}/approve/{session_id}/{elicitation_id}")
    expect(page.get_by_text("Approval required")).to_be_visible(timeout=30_000)

    page.get_by_role("button", name="Approve").click()

    # The page confirms the verdict and the server drains the parked prompt.
    expect(page.get_by_text("Approved", exact=False).first).to_be_visible(timeout=30_000)
    expect(page.get_by_text("You can close this page.")).to_be_visible()
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))


@pytest.mark.timeout(600)
def test_approve_page_rejects(
    page: Page,
    approval_session: tuple[str, str],
) -> None:
    """Reject on the standalone page also drains the parked prompt."""
    base_url, session_id = approval_session
    elicitation_id = _park_elicitation(page, base_url, session_id)

    page.goto(f"{base_url}/approve/{session_id}/{elicitation_id}")
    expect(page.get_by_text("Approval required")).to_be_visible(timeout=30_000)

    page.get_by_role("button", name="Reject").click()

    expect(page.get_by_text("Rejected", exact=False).first).to_be_visible(timeout=30_000)
    expect(page.get_by_text("You can close this page.")).to_be_visible()
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))


@pytest.mark.timeout(600)
def test_approve_page_resolved_for_unknown_elicitation(
    page: Page,
    approval_session: tuple[str, str],
) -> None:
    """An already-resolved / unknown elicitation id shows the resolved state.

    Reuses the fixture only for a live session id; no turn is sent. The page
    must not present approve/reject controls for an id the server has no parked
    prompt for — it renders the terminal "resolved" alert instead.
    """
    base_url, session_id = approval_session
    page.goto(f"{base_url}/approve/{session_id}/elicit_does_not_exist")

    expect(page.get_by_text("Elicitation resolved")).to_be_visible(timeout=30_000)
    expect(page.get_by_role("button", name="Approve")).to_have_count(0)

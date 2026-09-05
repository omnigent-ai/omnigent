"""Interrupting a turn must clear the Working indicator.

The bug: when ``session.interrupted`` fires (marking the active response as
``cancelled`` so the "Interrupted" label appears on the bubble), the client
does NOT receive a matching ``session.status = idle`` edge.  With no idle
edge the ``sessionStatus`` field stays ``"running"`` and the
"Working…" / "Brewing…" shimmer — which reads ``sessionStatus`` — stays lit.
The user sees both "Interrupted" on the bubble AND the shimmer, which is
contradictory: the bubble says the turn is over, the shimmer says it is still
in progress.

The fix must ensure that after a ``session.interrupted`` event the shimmer
clears.  Two acceptable approaches:

1. The server co-emits a ``session.status = idle`` edge alongside the
   ``session.interrupted`` event (symmetric with how every other terminal path
   emits an idle edge).
2. The client clears ``sessionStatus`` when it handles ``session_interrupted``
   (similar to the ``stop()`` local-optimistic path).

This test drives the exact turn-start → interrupt edge shape that exposes the
inconsistency and asserts the Working indicator is gone after the interrupt.
Both server-side and client-side fixes make the test pass; the test is
deliberately agnostic about which layer fixes it.

The test mirrors the shape of ``test_working_indicator_idle_clears.py``:
publish a ``running`` edge to light the shimmer, then publish a
``session.interrupted`` event, and assert the shimmer is gone.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

_WORKING = '[data-testid="working-indicator"]'
_INTERRUPTED = '[data-testid="assistant-interrupted-indicator"]'


def _publish_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    response_id: str | None = None,
) -> None:
    """Publish a session status through the native-harness events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param status: Session status to publish, e.g. ``"running"``.
    :param response_id: Optional in-flight turn id.  Set on the turn-start
        ``running`` edge (which opens the streaming ``activeResponse``) and
        omitted on a bare terminal ``idle``.
    :returns: None.
    """
    data: dict[str, str] = {"status": status}
    if response_id is not None:
        data["response_id"] = response_id
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def _publish_interrupted(
    base_url: str,
    session_id: str,
    *,
    response_id: str,
) -> None:
    """Publish a ``session.interrupted`` event via the native-harness events route.

    This is the server-side signal that marks the active response as
    cancelled.  The client stores the ``response_id`` in
    ``interruptedResponseIds`` and calls ``finalizeCurrentActive("cancelled")``,
    which decorates the bubble with the "Interrupted" label.  What it does NOT
    do (the bug) is update ``sessionStatus``, so the Working shimmer stays lit.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param response_id: The response id the interrupted event targets.
    :returns: None.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_interrupted",
            "data": {"response_id": response_id},
        },
        timeout=10.0,
    )
    resp.raise_for_status()


@pytest.mark.compat_smoke
def test_interrupted_clears_working_indicator(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Working shimmer clears when the server reports session.interrupted.

    Journey (the exact edge shape that exposes the bug):
    1. Navigate to the session page — shimmer is off.
    2. Publish a turn-start ``running`` edge with a ``response_id`` — shimmer
       appears and the active response opens (``sessionStatus = "running"``,
       ``activeResponse.state = "streaming"``).
    3. Publish ``session.interrupted`` with the same ``response_id`` — the
       bubble's "Interrupted" label appears (``activeResponse.state = "cancelled"``).
       The shimmer **must** also go out (``sessionStatus`` must become ``"idle"``).

    Before the fix: ``sessionStatus`` stays ``"running"`` after step 3, so
    the shimmer keeps running even though the bubble says "Interrupted".

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)
    response_id = "resp_interrupted_turn_1"

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=20_000)

    # The shimmer is off before any turn starts.
    expect(working).to_have_count(0, timeout=10_000)

    # Step 1: start a turn — shimmer appears.
    _publish_status(base_url, session_id, "running", response_id=response_id)
    expect(working).to_be_visible(timeout=15_000)

    # Step 2: interrupt arrives — shimmer must go out.
    # Before the fix the shimmer stays lit because sessionStatus is never
    # updated to "idle" by the session_interrupted event handler.
    _publish_interrupted(base_url, session_id, response_id=response_id)
    expect(working).to_have_count(0, timeout=15_000)

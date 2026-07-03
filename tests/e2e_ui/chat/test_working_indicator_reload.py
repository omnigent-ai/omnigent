"""Reload behavior for the main chat Working indicator.

The regression covered here is specific to an active main session whose
snapshot hydrates as ``running`` before any committed or pending chat
bubble exists locally. The UI must keep showing Working across a full
reload instead of falling back to the empty-session start screen.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect


def _publish_status(base_url: str, session_id: str, status: str) -> None:
    """Publish a session status through the same Omnigent route native harnesses use.

    :param base_url: Base URL of the local e2e server, e.g.
        ``"http://127.0.0.1:51234"``.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param status: Session status to publish, e.g. ``"running"``.
    :returns: None.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": {"status": status}},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_running_empty_session_reload_keeps_working_indicator(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Keep Working visible after reload when the main session is running.

    This reproduces the Nessie/custom-agent reload shape without a slow
    LLM turn: the local server owns the durable ``session.status`` cache,
    the session has no persisted chat bubbles, and the browser hydrates
    from ``GET /v1/sessions/{id}`` after a fresh page load.

    :param page: Playwright page fixture.
    :param seeded_session_pair: ``(base_url, session_a_id, session_b_id)``
        from the local server fixture. This fixture respawns the shared
        runner when a prior UI test killed it.
    :returns: None.
    """
    base_url, session_id, _other_session_id = seeded_session_pair
    _publish_status(base_url, session_id, "running")

    try:
        page.goto(f"{base_url}/c/{session_id}")
        working = page.locator('[data-testid="working-indicator"]')
        expect(working).to_be_visible(timeout=15_000)
        # Old behavior rendered the empty-state headline instead of Working.
        expect(page.get_by_text("What should we work on?")).to_have_count(0)

        page.reload()
        expect(working).to_be_visible(timeout=15_000)
        # Reload used to lose Working and fall back to the new-chat headline.
        expect(page.get_by_text("What should we work on?")).to_have_count(0)
    finally:
        _publish_status(base_url, session_id, "idle")

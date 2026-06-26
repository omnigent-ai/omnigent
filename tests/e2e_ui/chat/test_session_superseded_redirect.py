"""Auto-redirect the active viewer when a conversation is superseded.

A claude-native ``/clear`` rotates the live session to a fresh one; the forwarder
posts a ``session.superseded`` event to the OLD conversation. A client actively
viewing that conversation must follow the redirect to the new one.

e2e_ui has no real ``claude`` binary — native sessions are mocked and native
behaviors are exercised by POSTing the same ``external_*`` events the forwarder
emits (see ``test_working_indicator_reload`` / ``test_author_label``). So this
drives ``external_session_superseded`` (what ``_post_clear_supersession`` posts
on ``/clear``) and asserts the browser-side redirect, which is the user-facing
behavior this PR adds.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

_COMPOSER_PLACEHOLDER = "Ask the agent anything…"


def _publish_superseded(base_url: str, old_session_id: str, new_session_id: str) -> None:
    """Post the supersession event the claude-native forwarder emits on ``/clear``.

    :param base_url: Base URL of the local e2e server, e.g.
        ``"http://127.0.0.1:51234"``.
    :param old_session_id: Superseded (old) conversation id, e.g. ``"conv_old"``.
    :param new_session_id: Conversation to redirect to, e.g. ``"conv_new"``.
    :returns: None.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{old_session_id}/events",
        json={
            "type": "external_session_superseded",
            "data": {"target_conversation_id": new_session_id},
        },
        timeout=10.0,
    )
    resp.raise_for_status()


def test_session_superseded_redirects_active_viewer(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Viewing the superseded conversation auto-redirects to the new one.

    :param page: Playwright page fixture.
    :param seeded_session_pair: ``(base_url, old_session_id, new_session_id)``
        from the local server fixture.
    :returns: None.
    """
    base_url, old_session_id, new_session_id = seeded_session_pair

    page.goto(f"{base_url}/c/{old_session_id}")
    # Wait until the chat is interactive: by the time the composer renders,
    # switchTo has bound the live SSE stream, so the transient superseded event
    # posted below is delivered (it is live-only, with no snapshot replay).
    expect(page.get_by_placeholder(_COMPOSER_PLACEHOLDER)).to_be_visible(timeout=15_000)
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(old_session_id)}"))
    # Brief settle so the SSE stream connection is established server-side before
    # we publish — the superseded event has no replay, so a post that races ahead
    # of the subscription would be missed.
    page.wait_for_timeout(1_000)

    _publish_superseded(base_url, old_session_id, new_session_id)

    # The client follows the redirect to the new conversation.
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(new_session_id)}"), timeout=15_000)

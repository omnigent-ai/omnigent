"""Sending a message while only background work is running.

A claude-native turn can settle into the user-visible ``waiting`` state:
the turn already ended and the server's turn gate is free, but background
shells (or a still-running sub-agent) outlive it. The claude/cursor-native
Stop hook reports this as an ``external_session_status`` edge carrying
``status: "waiting"``, the ended turn's ``response_id``, and a positive
``background_task_count``.

The web composer must treat that as free-to-send — a new message starts a
fresh turn immediately — NOT queue it behind the background work. The
regression this guards: the ``waiting``+``response_id`` edge used to force
the local send lifecycle into ``streaming``, so the composer showed
"Send a follow-up (queued)" and held every message in the client-side
queue strip until the background work finished.

The ``waiting`` edge is published LIVE (after navigation) so it arrives via
the SSE ``session.status`` path — the one the fix targets. Publishing it
before navigation is not equivalent: this suite's ``openai-agents`` runner
collapses a posted ``waiting`` to ``running`` in the snapshot projection, so
a pre-navigation post would not reproduce the ``waiting`` state at all.

Like ``test_working_indicator_background_tasks``, this drives the real
status edge through the Sessions events route (the same path the
claude-native forwarder posts to), so it is deterministic — no live LLM
turn whose timing would make the assertions flaky.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

_QUEUED_STRIP = '[data-testid="composer-queued-strip"]'
_WORKING = '[data-testid="working-indicator"]'
_COMPOSER_PLACEHOLDER_IDLE = "Ask the agent anything…"

_SEND_MSG = "sentinel-bg-send-2a9c sent while a background task runs"


def _publish_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    response_id: str | None = None,
    background_task_count: int | None = None,
) -> None:
    """Publish a session status through the native-harness events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param status: Session status to publish, e.g. ``"waiting"``.
    :param response_id: Ended turn's response id, as the native Stop hook
        attaches it. ``None`` omits the field.
    :param background_task_count: Background shells still running as of this
        edge. ``None`` omits the field (leaves the sticky tally untouched).
    :returns: None.
    """
    data: dict[str, object] = {"status": status}
    if response_id is not None:
        data["response_id"] = response_id
    if background_task_count is not None:
        data["background_task_count"] = background_task_count
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def _user_bubble(page: Page, text: str):
    """Locator for the user-message bubble carrying ``text``."""
    return page.locator('[data-testid="message-bubble"][data-role="user"]').filter(has_text=text)


def test_message_sends_directly_while_background_task_runs(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A message sends immediately while the session is only ``waiting``.

    Drives the native Stop-hook edge live (``waiting`` + ``response_id`` +
    a positive ``background_task_count``), then asserts the composer is
    free to send: the placeholder reads the idle prompt (not the queued
    follow-up), sending renders the user bubble immediately, and the
    message never lands in the client-side queued strip.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    composer = page.get_by_label("Message the agent")
    page.goto(f"{base_url}/c/{session_id}")
    expect(composer).to_be_visible()

    # The turn ended but a background shell outlives it: the Stop hook posts
    # `waiting` with the ended turn's response_id and a positive count. The
    # working indicator stays lit ("1 background task still running").
    _publish_status(
        base_url,
        session_id,
        "waiting",
        response_id="resp_bg_1",
        background_task_count=1,
    )
    expect(page.locator(_WORKING)).to_contain_text(
        "1 background task still running", timeout=15_000
    )

    # The composer must be free to send — NOT stuck on the queued follow-up
    # placeholder. This is the exact regression: `waiting`+response_id used
    # to leave the local send lifecycle "streaming", showing the queued hint.
    expect(composer).to_have_attribute("placeholder", _COMPOSER_PLACEHOLDER_IDLE, timeout=15_000)

    # Sending must dispatch directly (a fresh turn), not enqueue: the user
    # bubble renders immediately and nothing appears in the queued strip.
    composer.fill(_SEND_MSG)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(_user_bubble(page, _SEND_MSG)).to_be_visible(timeout=10_000)
    expect(page.locator(_QUEUED_STRIP)).to_have_count(0)

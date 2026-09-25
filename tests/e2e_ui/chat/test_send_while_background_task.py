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

Both the live SSE path and the snapshot path are covered. The snapshot one
is the load-bearing case: the server records a background-task ``waiting``
as ``idle`` (the turn ended), so opening the session fresh serves a status
the composer can send against. When it recorded ``waiting``, the snapshot
projection collapsed that to ``running`` while still carrying the ended
turn's ``active_response_id`` — so every fresh open reopened the settled
turn as "streaming" and stranded the composer on the queue until the
background work finished.

Like ``test_working_indicator_background_tasks``, this drives the real
status edge through the Sessions events route (the same path the
claude-native forwarder posts to), so it is deterministic — no live LLM
turn whose timing would make the assertions flaky.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_working_indicator_background_tasks import (
    _MONITOR_TASK,
    _pill_badge,
    _publish_status,
)

_QUEUED_STRIP = '[data-testid="composer-queued-strip"]'
_COMPOSER_PLACEHOLDER_IDLE = "Send a message…"


_SEND_MSG = "sentinel-bg-send-2a9c sent while a background task runs"
_RELOAD_SEND_MSG = "sentinel-bg-send-7f31 sent after reopening the session"


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

    # A monitor outlives the turn, but does not make the composer busy.
    _publish_status(
        base_url,
        session_id,
        "waiting",
        response_id="resp_bg_1",
        background_task_count=1,
        background_tasks=[_MONITOR_TASK],
    )
    expect(_pill_badge(page, 1)).to_have_text("1", timeout=15_000)

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


def test_message_sends_directly_after_reopening_with_background_task(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A message sends immediately when the session is opened mid-background-work.

    Same Stop-hook edge, but published BEFORE navigation so the composer
    hydrates from the snapshot instead of a live SSE edge. This is the path
    the live test cannot reach: the snapshot used to project the recorded
    ``waiting`` as ``running`` while still carrying the ended turn's
    ``active_response_id``, reopening it as "streaming" — so every fresh open
    or reload queued every message behind the background work.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _publish_status(
        base_url,
        session_id,
        "waiting",
        response_id="resp_bg_2",
        background_task_count=1,
        background_tasks=[_MONITOR_TASK],
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()
    expect(_pill_badge(page, 1)).to_have_text("1", timeout=15_000)
    # The turn is over, so the composer is free.
    expect(composer).to_have_attribute("placeholder", _COMPOSER_PLACEHOLDER_IDLE, timeout=15_000)

    composer.fill(_RELOAD_SEND_MSG)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(_user_bubble(page, _RELOAD_SEND_MSG)).to_be_visible(timeout=10_000)
    expect(page.locator(_QUEUED_STRIP)).to_have_count(0)

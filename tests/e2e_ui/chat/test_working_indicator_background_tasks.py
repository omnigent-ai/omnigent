"""Working indicator behavior when background shells outlive a turn.

A claude-native turn can settle to ``idle`` while background shells keep
running. The forwarder reports that as ``external_session_status: idle``
carrying a positive ``background_task_count``, and the web chat must keep
the working indicator lit — labelled ``"N background tasks still
running"`` — instead of falling idle like the TUI's "N shells still
running" banner.

This drives the three user-visible states end to end:

1. Background tasks running (idle + ``background_task_count``) → the
   indicator shows ``"N background tasks still running"``.
2. The user sends a message while they're still running → the new turn
   supersedes the tally and the indicator flips to ``"Working…"``.
3. The turn finishes with no background tasks left → the indicator
   disappears.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Ask the agent anything…"
_WORKING = '[data-testid="working-indicator"]'


def _publish_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    background_task_count: int | None = None,
) -> None:
    """Publish a session status through the native-harness events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param status: Session status to publish, e.g. ``"idle"``.
    :param background_task_count: Background shells still running as of this
        status edge. Omitted (``None``) clears the tally, matching a Stop
        hook with no background tasks.
    :returns: None.
    """
    data: dict[str, object] = {"status": status}
    if background_task_count is not None:
        data["background_task_count"] = background_task_count
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def _release_gate(mock_url: str) -> None:
    """Release the oldest blocked mock-LLM response so the turn completes."""
    resp = httpx.post(f"{mock_url}/gate/release", timeout=10.0)
    resp.raise_for_status()


def test_background_tasks_keep_indicator_then_send_then_clear(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Indicator tracks background tasks → Working → gone across a turn.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local
        server fixture.
    :param mock_llm_server_url: Base URL of the in-process mock LLM, used
        to hold the user's turn in-flight so ``"Working…"`` is observable.
    :returns: None.
    """
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)

    # The user message is held in-flight (``block``) so the turn stays
    # ``running`` long enough to assert ``"Working…"``; ``match`` claims a
    # private queue so only this message blocks (title/routing calls fall
    # through to the live_server fallback).
    prompt = f"bg-indicator probe {uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "done", "block": True}],
        key="bg-indicator",
        match=prompt,
    )

    # 1. Background shells outlive the turn: idle + a positive count. The
    #    snapshot caches the count, so a fresh page load hydrates it.
    _publish_status(base_url, session_id, "idle", background_task_count=2)
    page.goto(f"{base_url}/c/{session_id}")
    expect(working).to_be_visible(timeout=15_000)
    expect(working).to_contain_text("2 background tasks still running")

    # 2. Sending a message starts a new turn that supersedes the tally:
    #    the label must flip from the background-task count to "Working…".
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()

    expect(working).to_contain_text("Working", timeout=15_000)
    expect(working).not_to_contain_text("background task", timeout=15_000)

    # 3. The turn finishes with no background tasks left → indicator gone.
    _release_gate(mock_llm_server_url)
    expect(working).to_have_count(0, timeout=30_000)


def test_sidebar_spinner_tracks_background_tasks(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The sidebar row's running spinner tracks background shells too.

    A claude-native turn settles to ``idle`` while shells keep running; the
    sidebar row must show the grey running spinner (``SessionStateBadge``
    ``data-state="running"``), matching the in-chat indicator — not fall idle.
    When the last shell finishes, the ``Stop`` hook's authoritative ``0``
    clears the tally and both the spinner and the chat indicator go out.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)
    # The badge sits in the row's time-marker slot (a sibling of the row link),
    # and `seeded_session` holds exactly one session — so the lone running badge
    # is this session's. Idle rows render no badge at all.
    running_badge = page.locator('[data-testid="session-state-badge"][data-state="running"]')

    # 1. Background shells outlive the turn → both the chat indicator and the
    #    sidebar row's running spinner light up.
    _publish_status(base_url, session_id, "idle", background_task_count=1)
    page.goto(f"{base_url}/c/{session_id}")
    expect(working).to_contain_text("1 background task still running", timeout=15_000)
    expect(running_badge).to_have_count(1, timeout=15_000)

    # 2. The last shell finishes: the Stop hook reports an authoritative `0`,
    #    which clears the tally — both the chat indicator and the sidebar
    #    spinner go out (idle rows render no badge).
    _publish_status(base_url, session_id, "idle", background_task_count=0)
    expect(working).to_have_count(0, timeout=15_000)
    expect(running_badge).to_have_count(0, timeout=15_000)

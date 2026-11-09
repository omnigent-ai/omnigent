"""E2E: the agent's own task list fills the chat's task preview and the Tasks tab.

The one-line preview (``ChatTasksPreview``) is pinned above the conversation
and, in a session with no plan, fed by the harness-agnostic ``session.todos``
contract — the claude-native forwarder posts it from Claude's task tools, the
codex-native forwarder from plan updates. These tests drive that list straight
through the Sessions events route (the same path the forwarders post to), so
they're deterministic: no live agent turn is needed to produce a task list.

Seed before load to prove the snapshot path, then re-publish the idempotent
full-state list until the live SSE subscription catches it.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
from playwright.sync_api import Page, expect

_PREVIEW = '[data-testid="tasks-preview"]'
_TASKS_PANEL = '[data-testid="tasks-panel"]'

_PLAN = [
    {"content": "Read the code", "status": "completed", "activeForm": "Reading the code"},
    {"content": "Write the tracker", "status": "in_progress", "activeForm": "Writing the tracker"},
    {"content": "Ship it", "status": "pending", "activeForm": "Shipping it"},
]


def _publish_todos(
    base_url: str,
    session_id: str,
    todos: list[dict[str, str]],
) -> None:
    """Publish the full todo list through the events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param todos: Full list; each item is ``{"content", "status", "activeForm"}``.
        An empty list clears the preview.
    :returns: None.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_todos", "data": {"todos": todos}},
        timeout=10.0,
    )
    resp.raise_for_status()


def _publish_until(
    base_url: str,
    session_id: str,
    todos: list[dict[str, str]],
    expectation: Callable[[], None],
) -> None:
    """Re-publish the idempotent full list until the preview reflects it.

    The session stream is snapshot-plus-live-tail with no replay buffer, so a
    list published in the window between the page's snapshot load and its live
    SSE subscription is dropped. The list is full-state and idempotent, so
    re-publishing until the assertion passes closes the connect race without
    weakening the check.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param todos: Full list to publish each attempt.
    :param expectation: Playwright ``expect`` assertion for the state the
        published list should drive; polled between re-publishes.
    :returns: None.
    """
    deadline = time.monotonic() + 30.0
    while True:
        _publish_todos(base_url, session_id, todos)
        try:
            expectation()
            return
        except AssertionError:
            if time.monotonic() >= deadline:
                raise


def test_tasks_preview_opens_tasks_tab_tracks_updates_and_clears(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The preview seeds from the snapshot, opens the Tasks tab on click, tracks
    a live completion in both places, and disappears when the list is cleared.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session
    preview = page.locator(_PREVIEW)
    panel = page.locator(_TASKS_PANEL)

    # 1. The list exists BEFORE the page opens: the snapshot cache must seed the
    #    preview on load (the refresh / reconnect channel). 1 of 3 done, and it
    #    names the task in progress.
    _publish_todos(base_url, session_id, _PLAN)
    page.goto(f"{base_url}/c/{session_id}")
    expect(preview).to_be_visible(timeout=15_000)
    expect(preview).to_contain_text("Tasks")
    expect(preview).to_contain_text("1/3")
    expect(preview).to_contain_text("Writing the tracker")

    # 2. A preview, not a disclosure: clicking it opens the Tasks tab, which
    #    lists every task with where it stands.
    preview.click()
    expect(panel).to_be_visible(timeout=10_000)
    rows = panel.locator('[data-testid="tasks-row"]')
    expect(rows).to_have_count(3)
    expect(rows.nth(0)).to_have_attribute("data-state", "done")
    expect(rows.nth(2)).to_contain_text("Ship it")

    # 3. Live update: the agent completes the in-progress task and the count
    #    advances without a reload, in the preview and the tab. First live-tail-dependent step, so
    #    re-publish the idempotent list until the SSE subscription receives it.
    completed_plan = [
        {"content": "Read the code", "status": "completed", "activeForm": "Reading the code"},
        {
            "content": "Write the tracker",
            "status": "completed",
            "activeForm": "Writing the tracker",
        },
        {"content": "Ship it", "status": "pending", "activeForm": "Shipping it"},
    ]
    _publish_until(
        base_url,
        session_id,
        completed_plan,
        lambda: expect(preview).to_contain_text("2/3", timeout=3_000),
    )
    expect(rows.nth(1)).to_have_attribute("data-state", "done")

    # 4. An empty list clears the preview entirely — a session with no tasks
    #    shows nothing above the chat.
    _publish_until(
        base_url,
        session_id,
        [],
        lambda: expect(preview).to_have_count(0, timeout=3_000),
    )

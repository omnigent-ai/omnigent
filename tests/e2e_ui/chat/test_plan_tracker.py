"""E2E: the in-chat Plan tracker renders, expands, tracks live updates, clears.

The task/plan tracker (``ChatPlanAccordion``) is pinned above the conversation
and fed by the harness-agnostic ``session.todos`` contract — the claude-native
forwarder posts it from ``TodoWrite``, the codex-native forwarder from plan
updates. These tests drive that list straight through the Sessions events route
(the same path the forwarders post to), so they're deterministic: no live agent
turn is needed to produce a plan.

Mirrors ``chat/test_mcp_startup_indicator.py`` — seed before load to prove the
snapshot path, then re-publish the idempotent full-state list until the live SSE
subscription catches it.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
from playwright.sync_api import Page, expect

_TRACKER = '[data-testid="plan-tracker"]'

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
        An empty list clears the tracker.
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
    """Re-publish the idempotent full list until the tracker reflects it.

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


def test_plan_tracker_renders_expands_updates_and_clears(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The Plan card seeds from the snapshot, expands on click, tracks a live
    completion, and disappears when the list is cleared.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session
    tracker = page.locator(_TRACKER)

    # 1. The plan exists BEFORE the page opens: the snapshot cache must seed the
    #    card on load (the refresh / reconnect channel).
    _publish_todos(base_url, session_id, _PLAN)
    page.goto(f"{base_url}/c/{session_id}")
    expect(tracker).to_be_visible(timeout=15_000)
    expect(tracker).to_contain_text("Plan")
    # 1 completed of 3 total.
    expect(tracker).to_contain_text("(1/3)")

    # 2. Collapsed by default: the task list is hidden until the user expands.
    in_progress = tracker.get_by_text("Write the tracker")
    expect(in_progress).to_be_hidden()

    # 3. Clicking the summary opens the disclosure and reveals the tasks.
    tracker.locator("summary").click()
    expect(in_progress).to_be_visible()
    expect(tracker).to_contain_text("Read the code")
    expect(tracker).to_contain_text("Ship it")

    # 4. Live update: the agent completes the in-progress task and the count
    #    advances without a reload. First live-tail-dependent step, so
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
        lambda: expect(tracker).to_contain_text("(2/3)", timeout=3_000),
    )

    # 5. An empty list clears the tracker entirely — a session with no plan
    #    shows no card.
    _publish_until(
        base_url,
        session_id,
        [],
        lambda: expect(tracker).to_have_count(0, timeout=3_000),
    )

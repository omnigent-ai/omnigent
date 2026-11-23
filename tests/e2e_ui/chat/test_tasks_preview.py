"""E2E: the agent's own task list fills the chat's task preview and the Tasks tab.

The one-line preview (``ChatTasksPreview``) is pinned above the conversation
and, in a session with no plan, fed by the harness-agnostic ``session.todos``
contract — the claude-native forwarder posts it from Claude's task tools, the
codex-native forwarder from plan updates. These tests drive that list straight
through the Sessions events route (the same path the forwarders post to), so
they're deterministic: no live agent turn is needed to produce a task list.

Seed before load to prove the snapshot path, then re-publish the idempotent
full-state list until the live SSE subscription catches it. The last journey
runs a scripted turn instead, whose journal writes mark the chat between its
tool calls.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

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
    #    names the task in progress, Stopped because the session isn't working.
    _publish_todos(base_url, session_id, _PLAN)
    page.goto(f"{base_url}/c/{session_id}")
    expect(preview).to_be_visible(timeout=15_000)
    expect(preview).to_contain_text("Tasks")
    expect(preview).to_contain_text("1/3")
    expect(preview).to_contain_text("Stopped: Write the tracker")

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


@pytest.mark.min_server_version("0.23.0")
def test_task_markers_and_the_tasks_tab_link_both_ways(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The server marks the chat where the agent's own tasks start and finish;
    a marker's task opens the Tasks tab at its row, and a row scrolls the chat
    back to its marker.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session
    # From an empty list, one task completed and one started: a marker each.
    _publish_todos(base_url, session_id, _PLAN)
    page.goto(f"{base_url}/c/{session_id}")
    markers = page.locator('[data-testid="task-activity"]')
    expect(markers).to_have_count(2, timeout=15_000)
    expect(markers.nth(0)).to_have_text("Completed Read the code")
    expect(markers.nth(1)).to_have_text("Started Write the tracker")

    # Marker to tab: the task's name opens the Tasks tab, highlighting its row.
    markers.nth(1).get_by_role("button", name="Write the tracker").click()
    panel = page.locator(_TASKS_PANEL)
    expect(panel).to_be_visible(timeout=10_000)
    row = panel.locator('[data-testid="tasks-row"]').nth(1)
    expect(row).to_have_attribute("data-flash", "true", timeout=5_000)

    # Tab to chat: a finished task's row scrolls the chat to where it ended.
    panel.locator('[data-testid="tasks-row"]').nth(0).get_by_test_id(
        "tasks-row-show-in-chat"
    ).click()
    expect(page).to_have_url(re.compile(r"[?&]message=todo-task-1-done"), timeout=5_000)
    expect(markers.nth(0)).to_be_in_viewport(timeout=5_000)
    # A task not started yet has no marker, so its row doesn't link.
    expect(
        panel.locator('[data-testid="tasks-row"]').nth(2).get_by_test_id("tasks-row-show-in-chat")
    ).to_have_count(0)


# A settled turn's rows in order before anything is expanded: its "Worked" rows,
# task markers and messages. A closed row's work isn't mounted.
_TURN_OUTLINE_JS = """() => Array.from(
  document.querySelectorAll(
    '.turn-fold-row button, [data-testid="task-activity"], [data-testid="assistant-text-section"]'
  ),
  (node) => node.textContent.trim(),
)"""


def _one_call(call_id: str, tool: str, /, **arguments: object) -> dict[str, object]:
    """A scripted model response that makes one tool call."""
    return {"tool_calls": [{"call_id": call_id, "name": tool, "arguments": json.dumps(arguments)}]}


@pytest.mark.min_server_version("0.23.0")
def test_a_turn_that_moves_a_task_folds_its_work_around_the_markers(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A settled turn folds the work on either side of its task markers into
    "Worked for" rows, and the markers stay in view, live and after a reload.

    The agent writes its plan and the task's statuses with the journal tools
    mid-turn, so the journal route marks the chat between the turn's shell
    calls, as in a workflow run.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :param mock_llm_server_url: Mock LLM server that scripts the turn.
    :returns: None.
    """
    base_url, session_id = seeded_session
    prompt_marker = "task-marker-folds-run"
    answer = "task-marker-folds-done"
    step = {"id": "explain", "name": "Explain the stage", "depends_on": []}
    done = _one_call(
        "call_tm_done", "sys_journal_write_status", step="explain", status="done", text="Explained"
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            _one_call("call_tm_before", "sys_os_shell", command="echo before-the-task"),
            _one_call("call_tm_plan", "sys_journal_write_plan", name="Stage checks", steps=[step]),
            _one_call(
                "call_tm_start", "sys_journal_write_status", step="explain", status="started"
            ),
            _one_call("call_tm_during", "sys_os_shell", command="echo during-the-task"),
            # The task takes a measurable span before it completes.
            {**done, "delay": 2.0},
            {"text": answer},
        ],
        key="task_marker_folds",
        match=prompt_marker,
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    composer.fill(f"Explain the stage, tracking it as a task. ({prompt_marker})")
    page.get_by_role("button", name="Send", exact=True).click()

    folds = page.locator('[data-testid="turn-worked-fold"]')
    markers = page.locator('[data-testid="task-activity"]')
    worked = re.compile(r"^Worked for \d+s$")

    def expect_folded_turn() -> None:
        expect(page.get_by_text(answer, exact=True)).to_be_visible(timeout=60_000)
        # The journal route marked where the plan was made and the task started and ended.
        expect(markers).to_have_text(
            [
                re.compile(r"^Made a plan: Stage checks"),
                "Started Explain the stage",
                "Completed Explain the stage",
            ]
        )
        # Each run of work around them folds once the turn settles.
        expect(folds).to_have_count(2, timeout=15_000)
        outline = page.evaluate(_TURN_OUTLINE_JS)
        expected = [
            worked,
            "Made a plan: Stage checks",
            "Started Explain the stage",
            worked,
            "Completed Explain the stage",
            answer,
        ]
        assert len(outline) == len(expected), outline
        for row, want in zip(outline, expected, strict=True):
            matches = want.match(row) if isinstance(want, re.Pattern) else row.startswith(want)
            assert matches, f"expected {want!r} in {outline!r}"
        expect(page.get_by_text("Ran 1 shell command")).to_have_count(0)

        # The task's own work opens from the row between its markers.
        folds.nth(1).get_by_role("button", name=worked).click()
        expect(folds.nth(1).get_by_text("Ran 1 shell command")).to_be_visible()

    expect_folded_turn()
    # Reloaded history folds the same way.
    page.reload()
    expect_folded_turn()

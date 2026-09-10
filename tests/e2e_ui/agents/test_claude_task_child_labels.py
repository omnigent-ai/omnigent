"""UI journey: Claude Code Task children are labeled by their task, not by
opaque runtime IDs.

Claude Code spawns sub-agents internally via its Task tool; the claude-native
forwarder registers each one with the server through the
``external_subagent_start`` event, which titles the child row
``"{agent_type}:{subagent_id}"`` and stores the Task tool's forwarded
``description`` as the ``omnigent.claude_native.description`` label. The web
UI is expected to label each child by that task description (agent type as a
fallback), keeping the runtime ID only for correlation — but both the Agents
rail (``childPrimaryLabel`` in ``SubagentsPanel``) and the composer's
sub-agent tray (``subAgentComposerLabel`` in ``ChatPage``) fall through to the
title's post-colon half, which for claude-native children is the opaque
Claude-side runtime ID (e.g. ``a5c7effd3b12…``). With two parallel workers on
screen the rows are indistinguishable.

The children are registered through the real ``external_subagent_start``
contract the claude-native forwarder POSTs (mirroring
``test_subagent_tab_title.claude_native_subagent``), so the rows carry exactly
the title/labels shape a live Claude Code session produces, without an LLM run.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# Two parallel Task dispatches with distinct descriptions — the report's
# journey. The ``subagent_id`` is the opaque Claude-side runtime ID that the
# buggy UI surfaces as the visible label.
_TASKS = (
    {
        "subagent_id": "a5c7effd3b12f4e8a0144c2e",
        "agent_type": "Explore",
        "description": "Investigate the authentication flow",
        "tool_use_id": "toolu_01ReproExploreAAAAAAAAAA",
    },
    {
        "subagent_id": "b6d8ff0e4c23a5f9b1255d31",
        "agent_type": "Code",
        "description": "Write unit tests",
        "tool_use_id": "toolu_01ReproCodeBBBBBBBBBBBB",
    },
)


@pytest.fixture
def claude_task_children(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, list[tuple[str, dict[str, str]]]]]:
    """Register two claude-native Task children; yield ``(base_url, parent_id, children)``.

    Each child goes through the real ``external_subagent_start`` contract the
    claude-native forwarder POSTs when Claude Code's Task tool spawns a
    sub-agent — that is what stamps the ``claude-code-native-ui-subagent``
    wrapper label, the ``"{agent_type}:{subagent_id}"`` title, and the
    ``omnigent.claude_native.description`` label the UI must surface.

    :returns: ``(base_url, parent_id, [(child_id, task_payload), ...])``.
    """
    base_url, parent_id = seeded_session
    children: list[tuple[str, dict[str, str]]] = []
    try:
        for task in _TASKS:
            started = httpx.post(
                f"{base_url}/v1/sessions/{parent_id}/events",
                json={"type": "external_subagent_start", "data": dict(task)},
                timeout=30.0,
            )
            started.raise_for_status()
            children.append((started.json()["child_session_id"], dict(task)))
        yield (base_url, parent_id, children)
    finally:
        for child_id, _ in children:
            httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)


def test_agents_rail_labels_task_children_by_description(
    page: Page,
    claude_task_children: tuple[str, str, list[tuple[str, dict[str, str]]]],
) -> None:
    """Each Task child row in the Agents rail reads as its task, not a hex ID.

    Journey: open the parent (claude-native) session → expand the Workspace
    rail → open the Agents tab. Two parallel Task workers were dispatched with
    distinct descriptions; each row's label must show its task description
    (the report's expected behavior), and the opaque Claude runtime ID must
    not be the visible row text — otherwise parallel workers cannot be told
    apart.
    """
    base_url, parent_id, children = claude_task_children

    # Guard the premise: the children really are claude-native Task rows
    # carrying the runtime-ID title shape the UI must not surface.
    for child_id, task in children:
        child = httpx.get(f"{base_url}/v1/sessions/{child_id}", timeout=10.0).json()
        assert child["labels"]["omnigent.wrapper"] == "claude-code-native-ui-subagent"
        assert child["title"] == f"{task['agent_type']}:{task['subagent_id']}"
        assert child["labels"]["omnigent.claude_native.description"] == task["description"]

    page.goto(f"{base_url}/c/{parent_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()

    rows = rail.locator('[data-testid="subagent-row"]')
    expect(rows).to_have_count(len(children), timeout=30_000)

    for child_id, task in children:
        row = rail.locator(f'[data-testid="subagent-row"][data-child-session-id="{child_id}"]')
        # The visible label is the forwarded task description…
        expect(row).to_contain_text(task["description"], timeout=30_000)
        # …not the opaque Claude-side runtime ID.
        expect(row).not_to_contain_text(task["subagent_id"])


def test_composer_tray_names_task_child_by_description(
    page: Page,
    claude_task_children: tuple[str, str, list[tuple[str, dict[str, str]]]],
) -> None:
    """Selecting a Task child, the composer tray names the task, not a hex ID.

    Journey: navigate into one of the dispatched Task children. The composer's
    "chatting with sub-agent" tray labels the routing target; it must read as
    the child's task description (agent type as fallback), not the opaque
    runtime ID.
    """
    base_url, _parent_id, children = claude_task_children
    child_id, task = children[0]

    page.goto(f"{base_url}/c/{child_id}")
    expect(page.get_by_role("link", name="Back to parent session")).to_be_visible(timeout=30_000)

    tray = page.get_by_test_id("composer-subagent-tray")
    expect(tray).to_be_visible(timeout=30_000)
    # The routing label is the forwarded task description…
    expect(tray).to_contain_text(task["description"], timeout=30_000)
    # …not the opaque Claude-side runtime ID.
    expect(tray).not_to_contain_text(task["subagent_id"])

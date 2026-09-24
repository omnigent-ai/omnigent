"""UI journey: a Claude Task sub-agent's Agents-rail row must carry a
human-readable name, not the raw sub-agent hash id.

A managed Claude session spawns sub-agents through Claude Code's own Task
tool. The claude-native forwarder registers each spawn with the server via
the ``external_subagent_start`` contract, carrying the Claude-side sub-agent
hash id, the agent type (e.g. ``"Explore"``), and the human-readable task
description. The server mints the child conversation titled
``"<agent_type>:<subagent_id>"`` and keeps the description as a label.

The OSS naming stack (``task_summary`` + the Agents-rail label chain) is
supposed to give every child row a human-readable name. For these children
it does not: no task summary is ever generated for them (the only scheduler,
in the session-events route, is unreachable for externally mirrored
children), and the rail's fallback chain lands on ``session_name`` — the
post-colon half of the title, i.e. the raw hash — so the user reads
``a5c7effac5a9a35ab`` instead of a name. The codex/opencode/antigravity
sub-agent wrappers each have a display special-case that avoids this;
claude's wrapper historically did not.

This test drives the real contract end-to-end against the live server and
SPA: register a Task sub-agent exactly as the forwarder does, open the
Agents rail, and require the child row's primary label to be something other
than the internal hash. It fails while the naming gap is live and passes once
any legitimate fix lands (a task summary derived from the stored description,
an agent-type display fallback like the other native wrappers, or any other
human-readable label source).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# Realistic Claude-side identifiers, shaped like the values the claude-native
# forwarder reads from ``agent-<id>.meta.json`` (see
# ``omnigent/harnesses/claude_native/forwarder.py``).
_SUBAGENT_ID = "a5c7effac5a9a35ab"
_AGENT_TYPE = "Explore"
_DESCRIPTION = "Investigate web UI session data flow"
_TOOL_USE_ID = "toolu_01P8fodgyqp4yQcPWCNNdrGJ"

_SUBAGENT_ROW = '[data-testid="subagent-row"]'


@pytest.fixture
def claude_task_subagent(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Register one Claude Task child under the parent, as the forwarder does.

    POSTs the same ``external_subagent_start`` event the claude-native
    forwarder sends when Claude Code's Task tool spawns a sub-agent (the
    contract ``_post_external_subagent_start`` implements), so the child row
    carries the real wrapper/description labels and the
    ``"<agent_type>:<subagent_id>"`` title anatomy.

    :returns: ``(base_url, parent_session_id, child_session_id)``.
    """
    base_url, parent_id = seeded_session
    started = httpx.post(
        f"{base_url}/v1/sessions/{parent_id}/events",
        json={
            "type": "external_subagent_start",
            "data": {
                "subagent_id": _SUBAGENT_ID,
                "agent_type": _AGENT_TYPE,
                "description": _DESCRIPTION,
                "tool_use_id": _TOOL_USE_ID,
            },
        },
        timeout=30.0,
    )
    started.raise_for_status()
    child_id = started.json()["child_session_id"]
    try:
        yield (base_url, parent_id, child_id)
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)


def test_claude_task_subagent_row_is_named_not_hash(
    page: Page,
    claude_task_subagent: tuple[str, str, str],
) -> None:
    """The Agents-rail row for a Claude Task child shows a name, not the hash."""
    base_url, parent_id, child_id = claude_task_subagent

    # Premise guard: the child really is a claude-native Task sub-agent (the
    # wrapper label is stamped) and the human-readable description reached the
    # server, so a name source exists. If these fail, the seeding contract
    # changed and this test no longer exercises the naming journey.
    children = httpx.get(
        f"{base_url}/v1/sessions/{parent_id}/child_sessions", timeout=10.0
    ).json()["data"]
    (child,) = [c for c in children if c["id"] == child_id]
    assert child["labels"]["omnigent.wrapper"] == "claude-code-native-ui-subagent"
    assert child["labels"]["omnigent.claude_native.description"] == _DESCRIPTION

    # The user journey: open the parent session, expand the Workspace rail,
    # and switch to the Agents tab where the sub-agent tree renders.
    page.goto(f"{base_url}/c/{parent_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()

    row = rail.locator(_SUBAGENT_ROW)
    expect(row).to_have_count(1, timeout=30_000)
    label = row.locator("span.font-medium").first
    expect(label).to_be_visible()

    # The defect this guards against: the row's primary label renders the
    # raw Claude-side sub-agent hash id instead of a human-readable name.
    expect(label).not_to_have_text(_SUBAGENT_ID)

    # Belt and braces for any future title anatomy: whatever the label is, it
    # must never be a bare internal identifier — not a hex hash, and not the
    # child conversation id.
    label_text = label.inner_text().strip()
    assert label_text, "sub-agent row rendered an empty primary label"
    assert label_text != child_id, (
        f"sub-agent row shows the child conversation id {child_id!r} instead of a name"
    )
    assert not re.fullmatch(r"[0-9a-f]{12,}", label_text), (
        f"sub-agent row shows a raw hash id {label_text!r} instead of a human-readable name"
    )

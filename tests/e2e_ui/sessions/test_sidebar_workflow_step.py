"""E2E: a workflow run's sidebar row names the step it's on.

The agent writes its plan and step statuses with the journal tools in a
scripted turn. Each write records where the plan stands on the session's
journal heads row, and the session list carries it to the row's line under
the title, live and after a reload, without the sidebar reading the journal.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm


def _one_call(call_id: str, tool: str, /, **arguments: object) -> dict[str, object]:
    """A scripted model response that makes one tool call."""
    return {"tool_calls": [{"call_id": call_id, "name": tool, "arguments": json.dumps(arguments)}]}


@pytest.mark.min_server_version("0.24.0")
def test_a_runs_sidebar_row_follows_its_steps(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """The row counts finished steps and names the one in progress, then says the run is done.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :param mock_llm_server_url: Mock LLM server that scripts the turn.
    :returns: None.
    """
    base_url, session_id = seeded_session
    prompt_marker = "sidebar-workflow-step-run"
    steps = [
        {"id": "inventory", "name": "Inventory dependencies", "depends_on": []},
        {"id": "open-prs", "name": "Open PRs for safe upgrades", "depends_on": ["inventory"]},
    ]

    def status(call_id: str, step: str, value: str, **text: str) -> dict[str, object]:
        return _one_call(call_id, "sys_journal_write_status", step=step, status=value, **text)

    configure_mock_llm(
        mock_llm_server_url,
        [
            _one_call(
                "call_sw_plan", "sys_journal_write_plan", name="Dependency audit", steps=steps
            ),
            status("call_sw_start_1", "inventory", "started"),
            status("call_sw_done_1", "inventory", "done", text="37 are behind"),
            status("call_sw_start_2", "open-prs", "started"),
            # The step stays open across the session list's live-update rescans.
            {**status("call_sw_done_2", "open-prs", "done", text="Opened 3 PRs"), "delay": 12.0},
            {"text": "sidebar-workflow-step-done"},
        ],
        key="sidebar_workflow_step",
        match=prompt_marker,
    )

    page.goto(f"{base_url}/c/{session_id}")
    line = page.locator(f'[data-sidebar-session-id="{session_id}"]').get_by_test_id(
        "session-row-plan"
    )
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    # A session with no plan has no line.
    expect(line).to_have_count(0)
    composer.fill(f"Audit the dependencies, tracking each step. ({prompt_marker})")
    page.get_by_role("button", name="Send", exact=True).click()

    expect(line).to_contain_text("1/2·Open PRs for safe upgrades", timeout=30_000)
    expect(line).to_contain_text("2/2·All done", timeout=60_000)
    # The list a reload reads carries it too.
    page.reload()
    expect(line).to_contain_text("2/2·All done", timeout=15_000)

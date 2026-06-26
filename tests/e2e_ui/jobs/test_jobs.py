"""E2E: the Jobs/Workflows journey — create a flow, add a step, and run it.

The Jobs page (``/jobs``) lists saved workflows; "Create flow" mints a job and
opens the flow builder (``/jobs/flow/:id``). The builder is a top-down stepper:
it starts with a lone Start step, and a "+" adds the next step. The user picks
the agent to run as and hits "Run now" — which renders the flow's narrative,
creates an agent session seeded with it, and navigates to that session
(``/c/:id``).

This pins the happy path end to end against the real ``/v1/jobs`` API the
builder now talks to (it was localStorage-only in the initial flows UI):
create → add a step → pick agent → run → land on the session. ``live_server``
runs ``examples/hello_world.yaml``, so the agent picker offers the "Hello_world"
agent.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect


def test_create_flow_pick_agent_and_run(page: Page, live_server: str) -> None:
    """Create a job, add a step, bind an agent, and run it — landing on the session.

    No LLM turn is asserted (the run only needs to *create* the session and
    navigate); the mock LLM keeps the spawned session deterministic.
    """
    base_url = live_server
    page.goto(f"{base_url}/jobs")

    # Empty state → create a flow. The button appears in both the header and
    # the empty-state card; the header one is always present.
    create = page.get_by_role("button", name="Create flow").first
    expect(create).to_be_visible(timeout=30_000)
    create.click()

    # We land in the flow builder on the new job (a lone Start step).
    expect(page).to_have_url(re.compile(r"/jobs/flow/job_[0-9a-f]+"), timeout=30_000)

    # Add a step so the flow is runnable (Run is disabled on a Start-only flow):
    # click the "+" below Start, then pick "Process".
    page.get_by_role("button", name="Add step").first.click()
    page.get_by_role("button", name="Process").click()

    # Bind the run agent (the picker lists the server's built-in agents).
    agent_select = page.get_by_test_id("job-agent-select")
    expect(agent_select).to_be_visible(timeout=30_000)
    # Wait for the agent list to populate (index 0 is the "Pick an agent…"
    # placeholder), then bind the first real agent.
    expect(agent_select.locator("option")).not_to_have_count(1, timeout=30_000)
    agent_select.select_option(index=1)

    # Run now persists the flow, then runs it.
    run_button = page.get_by_test_id("job-run-button")
    expect(run_button).to_be_enabled(timeout=30_000)
    run_button.click()

    # Run now creates a session and navigates to it.
    expect(page).to_have_url(re.compile(r"/c/conv_[0-9a-f]+"), timeout=60_000)


def test_jobs_list_shows_created_job(page: Page, live_server: str) -> None:
    """A created job appears back on the Jobs list.

    Guards the list round-trip through the API: after creating a flow and
    navigating back, the job row is present (newly created, so "never run").
    """
    base_url = live_server
    page.goto(f"{base_url}/jobs")

    page.get_by_role("button", name="Create flow").first.click()
    expect(page).to_have_url(re.compile(r"/jobs/flow/job_[0-9a-f]+"), timeout=30_000)

    # Back to the list — the new job should be listed.
    page.get_by_role("link", name="Jobs").first.click()
    expect(page).to_have_url(re.compile(r"/jobs$"), timeout=30_000)
    rows = page.get_by_test_id("job-row")
    expect(rows.first).to_be_visible(timeout=30_000)
    expect(rows.first).to_contain_text("never run")

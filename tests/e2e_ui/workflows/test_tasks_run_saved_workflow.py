"""Run a saved workflow from a session's empty Tasks tab.

A real host launches the session, so the composer gets the workflow skill from
skill discovery, as it does for users; only the model is mocked.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from playwright.sync_api import Page, ViewportSize, expect

from tests._helpers.host import Host, isolated_host
from tests._helpers.polling import poll_until
from tests._helpers.session import bundle_files, post_session_bundle

pytestmark = pytest.mark.min_server_version("0.23.0")

_AGENT_YAML = b"""\
name: tasks_run_workflow
prompt: You are a terse assistant.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""
_PHONE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def workflow_host(
    live_server: str, mock_llm_server_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Host]:
    """A real host whose sessions use the suite's mock model."""
    with (
        httpx.Client(base_url=live_server, trust_env=False, timeout=30.0) as client,
        isolated_host(
            tmp_path_factory.mktemp("tasks_run_workflow_host"),
            live_server,
            env={"OPENAI_BASE_URL": f"{mock_llm_server_url}/v1", "OPENAI_API_KEY": "mock-key"},
        ) as host,
    ):
        host.wait_online(client, timeout=180, poll_interval=1)
        yield host


@pytest.fixture
def host_session(live_server: str, workflow_host: Host, tmp_path: Path) -> Iterator[str]:
    """A session the host launched in its own workspace, once its runner is online."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with httpx.Client(base_url=live_server, trust_env=False, timeout=30.0) as client:
        created = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle_files({"tasks_run_workflow.yaml": _AGENT_YAML}),
            metadata={"workspace": str(workspace), "host_id": workflow_host.host_id},
            timeout=120.0,
        )
        created.raise_for_status()
        session_id = str(created.json()["session_id"])
        try:
            poll_until(
                lambda: client.get(f"/v1/sessions/{session_id}").json().get("runner_online"),
                timeout_s=120,
                interval_s=1,
            )
            yield session_id
        finally:
            with contextlib.suppress(httpx.HTTPError):
                client.delete(f"/v1/sessions/{session_id}")


@pytest.mark.parametrize("phone", [False, True], ids=["rail-keyboard", "phone-drawer-touch"])
def test_tasks_tab_runs_a_saved_workflow(
    page: Page, live_server: str, host_session: str, phone: bool
) -> None:
    """The empty Tasks tab's "run a workflow" lists saved workflows, as the "+" menu does.

    The rail runs one from the keyboard; the phone drawer by tapping.
    """
    name = f"Triage flaky tests {uuid4().hex[:8]}"
    details = f"Only the upload suite {uuid4().hex[:8]}"
    with httpx.Client(base_url=live_server, trust_env=False, timeout=10.0) as client:
        response = client.post(
            "/v1/workflows",
            json={
                "name": name,
                "description": "Find the flaky tests and fix them.",
                "steps": [
                    {
                        "id": "find",
                        "name": "Find flaky tests",
                        "goal": "List them",
                        "depends_on": [],
                    }
                ],
            },
        )
        response.raise_for_status()
        workflow_id = response.json()["id"]
        try:
            if phone:
                page.set_viewport_size(_PHONE_VIEWPORT)
            page.goto(f"{live_server}/c/{host_session}")
            if phone:
                page.get_by_role("button", name="Conversation actions").click()
                page.get_by_role("menuitem", name="Tasks", exact=True).click()
                panel = page.get_by_test_id("tasks-panel-drawer")
                expect(panel).to_have_attribute("data-state", "open")
            else:
                panel = page.get_by_role("complementary", name="Workspace")
                panel.get_by_role("tab", name=re.compile("^Tasks")).click()
            expect(panel.get_by_text("No tasks yet", exact=True)).to_be_visible()
            # The link shows once skill discovery gives the composer the workflow skill.
            link = panel.get_by_role("button", name="run a workflow", exact=True)
            expect(link).to_be_visible(timeout=60_000)
            picker = page.get_by_test_id("composer-workflow-picker")
            row = picker.get_by_test_id("composer-saved-workflow").filter(has_text=name)
            dialog = page.get_by_test_id("workflow-dialog")
            run_details = dialog.get_by_test_id("workflow-dialog-description")
            if phone:
                link.click()
                expect(picker).to_be_visible()
                expect(picker.get_by_test_id("composer-describe-workflow")).to_be_visible()
                row.click()
                expect(
                    dialog.get_by_role("heading", name=f"Run {name}", exact=True)
                ).to_be_visible()
                expect(panel).to_have_attribute("data-state", "closed")
                run_details.fill(details)
                dialog.get_by_test_id("workflow-dialog-run").click()
            else:
                link.focus()
                page.keyboard.press("Enter")
                expect(picker).to_be_visible()
                expect(picker.get_by_test_id("composer-describe-workflow")).to_be_visible()
                page.keyboard.type(name)
                page.keyboard.press("ArrowDown")
                expect(row).to_be_focused()
                page.keyboard.press("Enter")
                expect(
                    dialog.get_by_role("heading", name=f"Run {name}", exact=True)
                ).to_be_visible()
                expect(run_details).to_be_focused()
                page.keyboard.type(details)
                page.keyboard.press("Control+Enter")
            expect(dialog).to_be_hidden()

            run = f'Run the saved workflow "{name}" (ID {workflow_id}).'

            def received() -> bool:
                items = client.get(
                    f"/v1/sessions/{host_session}/items", params={"limit": 100, "order": "asc"}
                )
                items.raise_for_status()
                return any(
                    item.get("type") == "slash_command"
                    and item.get("name") == "workflow"
                    and run in str(item.get("arguments"))
                    and details in str(item.get("arguments"))
                    for item in items.json()["data"]
                )

            poll_until(received)
        finally:
            client.delete(f"/v1/workflows/{workflow_id}")

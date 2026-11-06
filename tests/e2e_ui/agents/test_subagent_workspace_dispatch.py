"""UI journeys: a worker sub-agent must run inside the workspace assigned to its child session.

An orchestrator dispatches a ``worker`` sub-agent through ``sys_session_send``
and names a per-task directory inside the worker's configured ``os_env.cwd`` as
the child's workspace. The user then opens the worker from the Workspace rail's
Agents tab, expands Worked › Ran 1 shell command › pwd, and reads where the
worker actually ran. A second journey creates the named child directly with a
persisted workspace and checks that the runner initializes it there.

The orchestrator and worker models are scripted on the mock LLM, so the
dispatch arguments and the worker's ``pwd`` call are deterministic.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    configure_mock_llm,
    open_right_rail,
    set_fallback_mock_llm,
)

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_PARENT_REPLY = "Dispatched the worker for task A."
_WORKER_REPLY = "Working directory printed."

_ORCHESTRATOR_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You coordinate one `worker` sub-agent. When the user asks you to run a task
  in a directory, call sys_session_send once with agent "worker", the task
  name as the title, and args {{input: <task>, workspace: <directory>}}, then
  finish and wait for the worker's result.
executor:
  model: {model}
  config:
    harness: openai-agents
tools:
  agents:
    - worker
"""

_WORKER_YAML = """\
spec_version: 1
name: worker
description: Runs one task inside the project directory.
prompt: Run the requested shell command in your working directory and report its output.
executor:
  model: {model}
  config:
    harness: openai-agents
os_env:
  type: caller_process
  cwd: {project}
  sandbox:
    type: none
"""


@dataclass
class WorkspaceSubagents:
    """Orchestrator session whose ``worker`` sub-agent is rooted at ``project``."""

    base_url: str
    session_id: str
    agent_id: str
    project: Path
    task_a: Path
    task_b: Path
    children: list[str] = field(default_factory=list)


@pytest.fixture
def workspace_subagents(
    live_server: str,
    runner_id: str,
    mock_llm_server_url: str,
    _recover_shared_runner: Callable[[], None],
) -> Iterator[WorkspaceSubagents]:
    uid = uuid.uuid4().hex[:8]
    orchestrator_model = f"ws-orchestrator-{uid}"
    worker_model = f"ws-worker-{uid}"
    # The runner can live in another sandbox that shares only the worktree, not /tmp.
    project = _REPO_ROOT / ".omnigent" / "e2e-workspaces" / f"ws-{uid}" / "project"
    task_a = project / "task-a"
    task_b = project / "task-b"
    task_a.mkdir(parents=True)
    task_b.mkdir()

    try:
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "call_dispatch_task_a",
                            "name": "sys_session_send",
                            "arguments": json.dumps(
                                {
                                    "agent": "worker",
                                    "title": "task-a",
                                    "args": {
                                        "input": "Print your working directory.",
                                        "workspace": str(task_a),
                                    },
                                }
                            ),
                        }
                    ]
                },
            ],
            key=orchestrator_model,
        )
        set_fallback_mock_llm(mock_llm_server_url, orchestrator_model, _PARENT_REPLY)
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "call_pwd",
                            "name": "sys_os_shell",
                            "arguments": json.dumps({"command": "pwd"}),
                        }
                    ]
                },
            ],
            key=worker_model,
        )
        set_fallback_mock_llm(mock_llm_server_url, worker_model, _WORKER_REPLY)
        _recover_shared_runner()

        bundle = bundle_files(
            {
                "config.yaml": _ORCHESTRATOR_YAML.format(
                    name=f"ws_orchestrator_{uid}", model=orchestrator_model
                ).encode(),
                "agents/worker/config.yaml": _WORKER_YAML.format(
                    model=worker_model, project=project
                ).encode(),
            }
        )
        created = post_session_bundle(
            httpx.post, f"{live_server}/v1/sessions", bundle, timeout=30.0
        )
        created.raise_for_status()
        session_id = created.json()["session_id"]
        agent_id = created.json()["agent_id"]
        handle = WorkspaceSubagents(
            base_url=live_server,
            session_id=session_id,
            agent_id=agent_id,
            project=project,
            task_a=task_a,
            task_b=task_b,
        )
        try:
            bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)
            yield handle
        finally:
            listed = httpx.get(
                f"{live_server}/v1/sessions/{session_id}/child_sessions",
                params={"limit": 100},
                timeout=10.0,
            )
            if listed.status_code == 200:
                handle.children.extend(
                    str(child.get("id") or child.get("session_id"))
                    for child in listed.json().get("data", [])
                )
            for child_id in dict.fromkeys(handle.children):
                httpx.delete(f"{live_server}/v1/sessions/{child_id}", timeout=10.0)
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        shutil.rmtree(project.parent, ignore_errors=True)


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _read_pwd_output(page: Page) -> str:
    """Expand Worked › Ran 1 shell command › pwd and return the printed directory."""
    worked = page.get_by_test_id("turn-worked-fold")
    expect(worked).to_be_visible(timeout=120_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
    worked.locator('[data-slot="collapsible-trigger"]').first.click()

    fold = page.get_by_text("Ran 1 shell command", exact=True)
    expect(fold).to_be_visible(timeout=15_000)
    fold.click()

    row = page.locator('[data-slot="collapsible-trigger"]', has_text="pwd").first
    expect(row).to_be_visible(timeout=15_000)
    row.click()

    output = page.locator("[data-language]", has=page.get_by_text("Output", exact=True)).first
    expect(output).to_be_visible(timeout=15_000)
    text = output.locator("pre").inner_text()
    # Keep the expanded output on screen long enough to read in the recording.
    page.wait_for_timeout(1_500)
    try:
        return str(json.loads(text)["stdout"]).strip()
    except (ValueError, KeyError, TypeError):
        return text.strip()


def _persisted_workspace(base_url: str, session_id: str) -> str | None:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("workspace")


@pytest.mark.timeout(300)
def test_dispatched_worker_runs_in_assigned_workspace(
    request: pytest.FixtureRequest,
    workspace_subagents: WorkspaceSubagents,
) -> None:
    ws = workspace_subagents
    page = request.getfixturevalue("page")
    assert isinstance(page, Page)
    page.goto(f"{ws.base_url}/c/{ws.session_id}")
    _send(page, f"Run task A in {ws.task_a}: print your working directory.")
    expect(page.locator(_ASSISTANT, has_text=_PARENT_REPLY).first).to_be_visible(timeout=120_000)

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    row = rail.get_by_test_id("subagent-row").filter(has_text="task-a").first
    expect(row).to_be_visible(timeout=60_000)
    child_id = row.get_attribute("data-child-session-id")
    assert child_id, "task-a row is missing data-child-session-id"
    ws.children.append(child_id)
    row.click()
    page.wait_for_url(re.compile(re.escape(f"/c/{child_id}")))

    printed = _read_pwd_output(page)
    persisted = _persisted_workspace(ws.base_url, child_id)
    assert (persisted, printed) == (str(ws.task_a), str(ws.task_a)), (
        f"worker child {child_id} did not run in its assigned workspace {ws.task_a}: "
        f"persisted workspace {persisted!r}, pwd printed {printed!r}"
    )


@pytest.mark.timeout(300)
def test_directly_created_child_runs_in_persisted_workspace(
    request: pytest.FixtureRequest,
    workspace_subagents: WorkspaceSubagents,
) -> None:
    ws = workspace_subagents
    created = httpx.post(
        f"{ws.base_url}/v1/sessions",
        json={
            "agent_id": ws.agent_id,
            "parent_session_id": ws.session_id,
            "sub_agent_name": "worker",
            "title": "worker:task-b",
            "workspace": str(ws.task_b),
        },
        timeout=30.0,
    )
    created.raise_for_status()
    child_id = created.json()["id"]
    ws.children.append(child_id)
    assert created.json()["workspace"] == str(ws.task_b)

    page = request.getfixturevalue("page")
    assert isinstance(page, Page)
    page.goto(f"{ws.base_url}/c/{child_id}")
    _send(page, "Print your working directory.")

    printed = _read_pwd_output(page)
    assert printed == str(ws.task_b), (
        f"directly created child {child_id} persisted workspace {ws.task_b} "
        f"but pwd printed {printed!r}"
    )

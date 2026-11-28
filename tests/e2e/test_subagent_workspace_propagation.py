"""E2E: a per-dispatch child workspace must survive sub-agent dispatch.

Guards against the workspace-drop regression: the object form of
``sys_session_send`` accepts a create-time ``workspace``, and an
orchestrator that assigns a per-child workspace at dispatch time must see
it persisted on the child session row — the runner's session-init
snapshot and ``_session_workspace_value`` read exactly that persisted
field as the child's runtime cwd, so a dropped value means the child runs
in the runner's default directory instead of the assigned one.

Journey (mock-LLM, full live server + runner):

1. Register a parent agent with a named ``worker`` sub-agent whose
   ``os_env.cwd`` points at a project root.
2. Create a nested per-task directory ``<project>/task-a``.
3. The mock parent brain dispatches ``sys_session_send`` naming the child
   workspace inside the object ``args``.
4. The child session is created — with the assigned workspace persisted.

Run::

    pytest tests/e2e/test_subagent_workspace_propagation.py -v
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S

# Dispatch + child turn + auto-wake are three serial mock turns; generous
# cap absorbs slow CI boot without masking a real hang (signal method is
# safe here — the test blocks only in main-thread httpx polls).
pytestmark = pytest.mark.timeout(600, method="signal")

# How long to wait for the dispatched child session to appear under the
# parent. The dispatch is async (runs after the parent's turn ends).
_CHILD_APPEAR_TIMEOUT_S = 120.0


def _wait_for_child_session(
    http_client: httpx.Client,
    parent_session_id: str,
    *,
    title: str,
    timeout_s: float = _CHILD_APPEAR_TIMEOUT_S,
) -> dict[str, Any]:
    """
    Poll the parent's child_sessions until the named child exists.

    :param http_client: HTTP client pointed at the live server.
    :param parent_session_id: The dispatching parent session id.
    :param title: Full child title, e.g. ``"worker:task-a"``.
    :param timeout_s: Max seconds to wait for the child to appear.
    :returns: The child-session summary dict.
    :raises AssertionError: When the child never appears (dispatch failed).
    """
    deadline = time.monotonic() + timeout_s
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        resp = http_client.get(f"/v1/sessions/{parent_session_id}/child_sessions")
        resp.raise_for_status()
        last = resp.json().get("data", [])
        for child in last:
            if child.get("title") == title:
                return child
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"child session {title!r} never appeared under parent "
        f"{parent_session_id} within {timeout_s:.0f}s — the sys_session_send "
        f"dispatch itself failed (children seen: "
        f"{[c.get('title') for c in last]!r}). Check the mock-LLM wiring "
        f"before reading this as the workspace bug."
    )


def test_sys_session_send_workspace_reaches_child_session(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    A per-dispatch child workspace must be persisted on the child session.

    The orchestrator dispatches ``sys_session_send(agent="worker",
    title="task-a")`` naming ``<project>/task-a`` as the child's
    workspace. The child session must be created with exactly that
    workspace persisted on its row — that persisted field is what the
    runner's session-init snapshot and ``_session_workspace_value``
    consume as the child's runtime cwd, so ``null`` here means the child
    runs in the runner's default directory instead of the assigned one.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Registered runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM base URL (without ``/v1``).
    :param tmp_path: Per-test temp dir for the project/task directories.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-ws-parent-{uid}"
    worker_model = f"mock-ws-worker-{uid}"
    mock_base = f"{mock_llm_server_url}/v1"

    # A project root the worker's os_env.cwd points at, plus a nested
    # per-task directory intended for this one child.
    project_root = tmp_path / "project"
    task_dir = project_root / "task-a"
    task_dir.mkdir(parents=True)

    reset_mock_llm(mock_llm_server_url)

    parent_name = register_inline_agent(
        http_client,
        name=f"ws-parent-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "Dispatch the worker sub-agent via sys_session_send with the "
            "workspace the user names, then stop."
        ),
        mock_llm_base_url=mock_base,
        extra_config={
            "tools": {
                "worker": {
                    "type": "agent",
                    "description": "Test-fixture worker for the workspace test.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": worker_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": mock_base,
                        },
                    },
                    "os_env": {
                        "type": "caller_process",
                        "cwd": str(project_root),
                        "sandbox": {"type": "none"},
                    },
                    "prompt": "You are the test-fixture worker.",
                },
            },
        },
    )

    # Parent brain: one dispatch that names the child workspace inside the
    # object args (the create-time contract). Then an ack and an auto-wake
    # continuation so no queue slot starves.
    dispatch_arguments = json.dumps(
        {
            "agent": "worker",
            "title": "task-a",
            "args": {
                "input": "Run the task in your assigned workspace.",
                "workspace": str(task_dir),
            },
        }
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_1",
                        "name": "sys_session_send",
                        "arguments": dispatch_arguments,
                    }
                ],
            },
            {"text": "Dispatched worker into task-a."},
            {"text": "Worker finished."},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "WORKER_TASK_A_DONE"}],
        key=worker_model,
    )

    parent_session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )
    send_user_message_to_session(
        http_client,
        session_id=parent_session_id,
        content=f"Dispatch the worker sub-agent with workspace {task_dir}.",
    )

    child = _wait_for_child_session(http_client, parent_session_id, title="worker:task-a")

    snap = http_client.get(f"/v1/sessions/{child['id']}")
    snap.raise_for_status()
    persisted_workspace = snap.json().get("workspace")
    # Dispatch persists the canonical (resolved) path; compare against the
    # resolved dir so a symlinked tmp_path (e.g. macOS /var) can't flake.
    expected_workspace = str(task_dir.resolve())
    assert persisted_workspace == expected_workspace, (
        f"child session {child['id']} was created WITHOUT its assigned "
        f"workspace: expected {expected_workspace!r}, persisted "
        f"{persisted_workspace!r}. The sys_session_send dispatch dropped "
        f"the per-child workspace, so the runner will fall back to its "
        f"default directory instead of the assigned per-task workspace."
    )

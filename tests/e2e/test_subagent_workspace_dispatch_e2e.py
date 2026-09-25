"""E2E: a per-dispatch child workspace is persisted and used by the child.

Reproduces the workspace-drop defect: when an orchestrator
dispatches a named sub-agent and passes an explicit per-task workspace in the
object form of ``args``, the runner's dispatch handler silently drops the key.
The child session is created without a persisted workspace, so the child agent
runs in its configured default directory instead of the workspace assigned to
that child task.

Journey (mirrors the report's steps to reproduce):

1. A parent orchestrator declares a named ``worker`` sub-agent whose
   ``os_env.cwd`` points at a project root (a temp dir made by this test).
2. The test creates a nested per-task directory ``<project>/task-a``.
3. The user asks the orchestrator to run task A in that workspace; the
   scripted parent LLM dispatches ``sys_session_send`` with
   ``args: {input: ..., workspace: "<project>/task-a"}``.
4. The child session is created and the worker runs a ``pwd`` probe.
5. EXPECTED: the child session persists the canonical ``<project>/task-a``
   workspace and the worker's shell runs inside it.
   ACTUAL (bug): the dispatch silently drops ``workspace`` — no error is
   returned to the orchestrator — the child row persists ``workspace: null``
   and the worker runs in the runner's default directory.

Topology mirrors tests/e2e/test_subagent_tool_limit_e2e.py: real server +
real runner from this working tree, mock LLM scripted per-agent (parent and
child each route to their own mock model queue via a per-agent
``executor.auth.base_url``).

Run::

    pytest tests/e2e/test_subagent_workspace_dispatch_e2e.py -v
"""

from __future__ import annotations

import io
import json
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.e2e.conftest import (
    OMNIGENT_INTERNAL_WS_ORIGIN,
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S

# Per-child mock-LLM routing (each agent on its own mock model + auth
# base_url) requires a server >= 0.3.0 — same constraint as
# tests/e2e/test_subagent_tool_limit_e2e.py.
pytestmark = [
    pytest.mark.min_server_version("0.3.0"),
    # Serial mock turns (parent dispatch, child shell turn, parent
    # auto-wake), so allow headroom under signal-based timeout.
    pytest.mark.timeout(600, method="signal"),
]

# Sentinel in the worker mock's final text, so the test can wait for the
# child turn to be fully finished before asserting persisted state.
_WORKER_DONE = "WORKER_WORKSPACE_TEST_DONE"


def _build_agent_yaml(
    *,
    name: str,
    model: str,
    prompt: str,
    mock_llm_base_url: str,
    cwd: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build one spec_version-1 agent config dict for the bundle.

    :param name: Agent name, e.g. ``"worker"``.
    :param model: Mock model key selecting this agent's response queue.
    :param prompt: System prompt (scripted mocks drive the turns anyway).
    :param mock_llm_base_url: Mock server base URL including ``/v1``.
    :param cwd: The agent's configured ``os_env.cwd`` root.
    :param extra: Optional top-level keys merged into the config
        (e.g. ``{"tools": {"agents": ["worker"]}}``).
    :returns: The config dict ready for YAML serialization.
    """
    config: dict[str, Any] = {
        "spec_version": 1,
        "name": name,
        "executor": {
            "type": "omnigent",
            "model": model,
            "config": {"harness": "openai-agents"},
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": mock_llm_base_url,
            },
        },
        "prompt": prompt,
        # ``caller_process`` + explicit cwd: the configured filesystem root
        # the report says a per-dispatch workspace must resolve inside.
        # ``sandbox: none`` keeps the worker's pwd probe deterministic on CI.
        "os_env": {"type": "caller_process", "cwd": cwd, "sandbox": {"type": "none"}},
    }
    if extra:
        config.update(extra)
    return config


def _register_workspace_bundle(
    client: httpx.Client,
    *,
    name: str,
    parent_model: str,
    child_model: str,
    project_root: Path,
    mock_llm_base_url: str,
) -> str:
    """
    Upload a parent orchestrator + ``agents/worker`` child bundle.

    The worker's ``os_env.cwd`` is the temp project root, so a per-dispatch
    workspace naming ``<project_root>/task-a`` is a valid nested directory
    inside the child's configured root — exactly the report's topology.

    :param client: HTTP client pointed at the live server.
    :param name: Unique parent agent name for this registration.
    :param parent_model: Mock model key for the parent's response queue.
    :param child_model: Mock model key for the worker's response queue.
    :param project_root: The worker's configured project root directory.
    :param mock_llm_base_url: Mock server base URL including ``/v1``.
    :returns: The registered parent agent name.
    """
    parent_cfg = _build_agent_yaml(
        name=name,
        model=parent_model,
        prompt=(
            "You are a supervisor. When asked to run a task, dispatch the "
            "worker sub-agent with sys_session_send, assigning the task's "
            "workspace, and wait for its result."
        ),
        mock_llm_base_url=mock_llm_base_url,
        cwd=".",
        extra={"tools": {"agents": ["worker"]}},
    )
    child_cfg = _build_agent_yaml(
        name="worker",
        model=child_model,
        prompt=(
            "You are a careful worker. When asked to state your working "
            "directory, run `pwd` with sys_os_shell and then report it."
        ),
        mock_llm_base_url=mock_llm_base_url,
        cwd=str(project_root),
    )

    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:

            def _add_yaml(arcname: str, config: dict[str, Any]) -> None:
                data = yaml.dump(config).encode()
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

            _add_yaml("config.yaml", parent_cfg)
            _add_yaml("agents/worker/config.yaml", child_cfg)
        bundle = buf.getvalue()

    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code not in (200, 201, 409):
        raise RuntimeError(f"bundle register failed: {resp.status_code} {resp.text[:500]}")
    return name


def _configure_parent_and_child_mocks(
    mock_llm_server_url: str,
    *,
    parent_model: str,
    child_model: str,
    task_workspace: str,
) -> None:
    """
    Script both mock queues: the parent dispatches the worker once with an
    explicit per-task ``workspace``; the worker probes its cwd and reports.

    :param mock_llm_server_url: Mock server base URL.
    :param parent_model: Parent queue key.
    :param child_model: Worker queue key.
    :param task_workspace: The absolute per-task workspace the dispatch
        assigns to the child, e.g. ``"<project>/task-a"``.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_worker_ws",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "worker",
                                "title": "task-a",
                                "args": {
                                    "input": (
                                        "State your working directory: run pwd "
                                        "and report the result."
                                    ),
                                    # The report's step 3: assign the nested
                                    # per-task directory as the child session's
                                    # create-time workspace.
                                    "workspace": task_workspace,
                                },
                            }
                        ),
                    },
                ],
            },
            {"text": "Dispatched worker task-a; waiting for its report."},
            # Inbox auto-wake continuation after the worker finishes.
            {"text": "PARENT_WAKE_DONE"},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_worker_pwd",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": "pwd"}),
                    }
                ]
            },
            {"text": _WORKER_DONE},
        ],
        key=child_model,
    )


def _find_child_session_id(
    http_client: httpx.Client,
    *,
    parent_session_id: str,
    child_title: str,
    timeout: float = 180.0,
) -> str:
    """
    Poll the sub-agent session list until the parent's child appears.

    ``sys_session_send`` spawns the child asynchronously; the runner mints
    the child title as ``"{agent}:{title}"``.

    :param http_client: HTTP client pointed at the live server.
    :param parent_session_id: The dispatching parent session id.
    :param child_title: The minted child title, e.g. ``"worker:task-a"``.
    :param timeout: Max seconds to wait for the child to appear.
    :returns: The child (sub-agent) session id.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = http_client.get("/v1/sessions", params={"kind": "sub_agent", "limit": 1000})
        resp.raise_for_status()
        for item in resp.json().get("data", []):
            if item.get("title") != child_title:
                continue
            candidate = str(item["id"])
            snap = http_client.get(f"/v1/sessions/{candidate}")
            snap.raise_for_status()
            if snap.json().get("parent_session_id") == parent_session_id:
                return candidate
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"No sub-agent child session titled {child_title!r} for parent "
        f"{parent_session_id!r} appeared within {timeout:.0f}s."
    )


def _wait_for_child_done(
    http_client: httpx.Client,
    *,
    child_session_id: str,
    timeout: float = 240.0,
) -> list[dict[str, Any]]:
    """
    Poll the child session until its scripted final text lands, then return
    its conversation items.

    :param http_client: HTTP client pointed at the live server.
    :param child_session_id: The spawned worker session id.
    :param timeout: Max seconds to wait.
    :returns: The child session's conversation items.
    """
    deadline = time.monotonic() + timeout
    items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        resp = http_client.get(f"/v1/sessions/{child_session_id}")
        resp.raise_for_status()
        body = resp.json()
        items = body.get("items", [])
        if _WORKER_DONE in json.dumps(items):
            return items
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Worker session {child_session_id} did not finish within {timeout:.0f}s; "
        f"last items: {json.dumps(items)[:2000]}"
    )


def _flattened(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Flatten session items to Responses-style dicts (payload under ``data``).

    :param items: Session conversation items.
    :returns: Flat dicts keeping the item type.
    """
    return [{"type": item.get("type"), **(item.get("data") or {})} for item in items]


def _tool_outputs(items: list[dict[str, Any]], tool_name: str) -> list[str]:
    """
    Extract a named tool's outputs from session items, in order.

    :param items: Session conversation items.
    :param tool_name: The function tool name, e.g. ``"sys_os_shell"``.
    :returns: Output payloads of matching ``function_call_output`` items.
    """
    flat = _flattened(items)
    call_ids = {
        p.get("call_id")
        for p in flat
        if p.get("type") == "function_call" and p.get("name") == tool_name
    }
    return [
        str(p.get("output", ""))
        for p in flat
        if p.get("type") == "function_call_output" and p.get("call_id") in call_ids
    ]


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_dispatch_workspace_is_persisted_and_used_by_child(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    tmp_path: Path,
) -> None:
    """
    A ``sys_session_send`` dispatch naming ``<project>/task-a`` as the child's
    workspace must create the child session WITH that canonical workspace
    persisted, and the worker's shell must run inside it.

    On the buggy build the dispatch handler has no ``workspace`` plumbing:
    the key is silently ignored (the orchestrator gets no error), the child
    row persists ``workspace: null``, the worker's ``pwd`` prints the
    runner's default directory, and this test FAILS.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-ws-parent-{uid}"
    child_model = f"mock-ws-child-{uid}"

    # The report's steps 1-2: a project root for the worker's configured
    # os_env.cwd, with a nested per-task directory for this one child task.
    project_root = (tmp_path / "project").resolve()
    task_dir = project_root / "task-a"
    task_dir.mkdir(parents=True)
    canonical_task_dir = str(task_dir.resolve())

    reset_mock_llm(mock_llm_server_url)
    assert mock_llm_server_url is not None
    agent_name = _register_workspace_bundle(
        http_client,
        name=f"subagent-workspace-{uid}",
        parent_model=parent_model,
        child_model=child_model,
        project_root=project_root,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    _configure_parent_and_child_mocks(
        mock_llm_server_url,
        parent_model=parent_model,
        child_model=child_model,
        task_workspace=canonical_task_dir,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="RUN: do task A inside the task-a workspace of the project.",
    )
    poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=180
    )

    child_session_id = _find_child_session_id(
        http_client,
        parent_session_id=session_id,
        child_title="worker:task-a",
    )
    child_items = _wait_for_child_done(http_client, child_session_id=child_session_id)

    # The dispatch must not have errored: on the buggy build the workspace
    # key is dropped SILENTLY (no rejection reaches the orchestrator), and
    # on a fixed build this valid nested workspace is accepted. An error
    # here means dispatch plumbing regressed in a different way.
    parent_snap = http_client.get(f"/v1/sessions/{session_id}")
    parent_snap.raise_for_status()
    dispatch_outputs = _tool_outputs(parent_snap.json().get("items", []), "sys_session_send")
    assert dispatch_outputs, "no sys_session_send tool output found on the parent session"
    assert not any(o.startswith("Error:") for o in dispatch_outputs), (
        f"sys_session_send rejected the dispatch instead of honoring (or "
        f"silently dropping) the workspace: {dispatch_outputs!r}"
    )

    # THE BUG: the child session must persist the canonical per-task
    # workspace assigned at dispatch. On the buggy build this is None —
    # the assigned workspace was silently dropped.
    child_snap = http_client.get(f"/v1/sessions/{child_session_id}")
    child_snap.raise_for_status()
    persisted_workspace = child_snap.json().get("workspace")
    assert persisted_workspace == canonical_task_dir, (
        f"child session {child_session_id} should persist the workspace "
        f"assigned at dispatch ({canonical_task_dir!r}) but persisted "
        f"{persisted_workspace!r} — sys_session_send dropped the per-task "
        f"workspace"
    )

    # The user-visible consequence: the worker's shell runs inside the
    # assigned per-task workspace, not the configured default directory.
    pwd_outputs = _tool_outputs(child_items, "sys_os_shell")
    assert pwd_outputs, "worker never produced its sys_os_shell pwd output"
    assert any(canonical_task_dir in output for output in pwd_outputs), (
        f"worker ran in the wrong directory: pwd printed {pwd_outputs!r}, "
        f"expected the assigned workspace {canonical_task_dir!r}"
    )

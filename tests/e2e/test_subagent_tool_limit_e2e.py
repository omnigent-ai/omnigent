"""
E2E reproduction for the sub-agent ``max_tool_calls_per_session`` limit bug.

A native agent bundle declares a ``max_tool_calls_per_session`` guardrail on
BOTH the parent and its ``agents/worker`` child, with the child's limit
STRICTER than the parent's. When the parent dispatches the worker via
``sys_session_send`` and the worker performs sequential tool calls, the
worker must be denied once it exceeds ITS OWN limit — the effective limit is
``min(parent limit, child limit)``.

The defect: the policy engine for the child conversation is built from the
ROOT bundle spec only (``_load_agent_spec_for_session`` never resolves
``conv.sub_agent_name`` to the child's own ``AgentSpec``), so the child's
``guardrails.policies`` are silently dropped at runtime. The child then runs
under the parent's looser limit and sails past its configured cap without
ever receiving ``Denied by policy: Exceeded N tool calls this session``.

The control test (parent stricter than child) documents the valid behavior
the fix must preserve: the inherited parent policy still fences the child.

Topology mirrors tests/e2e/test_coder_subagent.py: real server + real runner,
mock LLM scripted per-agent (parent and child each route to their own mock
model queue via a per-agent ``executor.auth.base_url``).
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

# Fixture bundle: parent (looser limit) + agents/worker child (stricter
# limit) whose calculate tool ships as Python source under tools/python/.
_BUNDLE_DIR = Path(__file__).resolve().parents[1] / "resources" / "agents" / "subagent-tool-limit"

# Per-child mock-LLM routing (each agent on its own mock model + auth
# base_url) requires a server >= 0.3.0 — same constraint as
# tests/e2e/test_coder_subagent.py.
pytestmark = [
    pytest.mark.min_server_version("0.3.0"),
    # Several serial mock turns (parent dispatch, four child tool rounds,
    # parent auto-wake), so allow headroom under signal-based timeout.
    pytest.mark.timeout(600, method="signal"),
]

# Sentinel emitted by the child mock's final text response, so the test can
# wait for the child turn to be fully finished before asserting.
_WORKER_DONE = "WORKER_LIMIT_TEST_DONE"


def _register_limit_bundle(
    client: httpx.Client,
    *,
    name: str,
    parent_model: str,
    child_model: str,
    parent_limit: int,
    child_limit: int,
    mock_llm_base_url: str,
) -> str:
    """
    Upload the parent+worker bundle with per-agent models, auth and limits.

    Stamps ``executor.model`` + an ``executor.auth`` api-key block onto BOTH
    the parent config.yaml and the child agents/worker/config.yaml so each
    agent routes to its own mock-LLM queue, and stamps the two
    ``max_tool_calls_per_session`` limits so one fixture dir serves both the
    defect test and the control test.

    :param client: HTTP client pointed at the live server.
    :param name: Unique agent name for this registration.
    :param parent_model: Mock model key for the parent's response queue.
    :param child_model: Mock model key for the worker's response queue.
    :param parent_limit: ``max_tool_calls_per_session`` limit for the parent.
    :param child_limit: ``max_tool_calls_per_session`` limit for the worker.
    :param mock_llm_base_url: Mock server base URL including ``/v1``.
    :returns: The registered agent name.
    """
    auth = {"type": "api_key", "api_key": "mock-key", "base_url": mock_llm_base_url}

    parent_cfg = yaml.safe_load((_BUNDLE_DIR / "config.yaml").read_text())
    parent_cfg["name"] = name
    parent_cfg.setdefault("executor", {})["model"] = parent_model
    parent_cfg["executor"]["auth"] = auth
    parent_cfg["guardrails"]["policies"]["parent_tool_limit"]["function"]["arguments"]["limit"] = (
        parent_limit
    )

    child_cfg = yaml.safe_load((_BUNDLE_DIR / "agents" / "worker" / "config.yaml").read_text())
    child_cfg.setdefault("executor", {})["model"] = child_model
    child_cfg["executor"]["auth"] = auth
    child_cfg["guardrails"]["policies"]["child_tool_limit"]["function"]["arguments"]["limit"] = (
        child_limit
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
            tar.add(
                str(_BUNDLE_DIR / "agents" / "worker" / "tools" / "python" / "calculate.py"),
                arcname="agents/worker/tools/python/calculate.py",
            )
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
) -> None:
    """
    Script both mock queues: the parent dispatches the worker once, the
    worker attempts FOUR sequential ``calculate`` calls then reports done.

    :param mock_llm_server_url: Mock server base URL.
    :param parent_model: Parent queue key.
    :param child_model: Worker queue key.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_worker",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "worker",
                                "title": "limit-check",
                                "args": (
                                    "Calculate 1+1, then 2+2, then 3+3, then 4+4 — "
                                    "one calculate call per expression."
                                ),
                            }
                        ),
                    },
                ],
            },
            {"text": "Dispatched worker; waiting for its results."},
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
                        "call_id": f"call_calc_{i}",
                        "name": "calculate",
                        "arguments": json.dumps({"expression": expr}),
                    }
                ]
            }
            for i, expr in enumerate(["1+1", "2+2", "3+3", "4+4"])
        ]
        + [{"text": _WORKER_DONE}],
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
    :param child_title: The minted child title, e.g. ``"worker:limit-check"``.
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
    Poll the child session until its final text (or a policy denial that
    ended the turn) lands, then return its conversation items.

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
        blob = json.dumps(items)
        # Terminal states: the worker's scripted final text, or an idle
        # session whose turn already produced tool outputs (a denial may
        # abort the scripted flow before the final text is reached).
        if _WORKER_DONE in blob:
            return items
        if body.get("status") in ("idle", "failed") and "function_call_output" in blob:
            # Give in-flight items a moment to settle, then re-read.
            time.sleep(2.0)
            resp = http_client.get(f"/v1/sessions/{child_session_id}")
            resp.raise_for_status()
            return resp.json().get("items", [])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Worker session {child_session_id} did not finish within {timeout:.0f}s; "
        f"last items: {json.dumps(items)[:2000]}"
    )


def _calculate_outputs(items: list[dict[str, Any]]) -> list[str]:
    """
    Extract the ``calculate`` tool outputs from child session items, in order.

    :param items: Child session conversation items.
    :returns: Output payloads of ``function_call_output`` items whose call_id
        matches a ``calculate`` function_call.
    """
    flattened: list[dict[str, Any]] = []
    for item in items:
        # Session items carry the payload under "data"; keep the item type.
        data = item.get("data") or {}
        flattened.append({"type": item.get("type"), **data})
    calc_call_ids = {
        p.get("call_id")
        for p in flattened
        if p.get("type") == "function_call" and p.get("name") == "calculate"
    }
    return [
        str(p.get("output", ""))
        for p in flattened
        if p.get("type") == "function_call_output" and p.get("call_id") in calc_call_ids
    ]


def _is_denial(output: str) -> bool:
    """Return whether a tool output is a policy denial payload."""
    return "Denied by policy" in output or "Exceeded" in output


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_child_stricter_tool_limit_is_enforced(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    A worker child with ``max_tool_calls_per_session: 3`` under a parent
    with limit 10 must be denied on its 4th tool call.

    The effective limit for the child is ``min(parent, child) = 3``. The
    worker's mock script attempts four sequential ``calculate`` calls, so:

    - at most 3 calls may execute successfully (numeric outputs), and
    - the 4th call's output must be the policy denial
      (``Exceeded 3 tool calls this session``).

    On the buggy build the child's own guardrail policy is dropped at
    runtime (the engine is built from the root bundle spec only), all four
    calls execute, and this test FAILS.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-limit-parent-{uid}"
    child_model = f"mock-limit-child-{uid}"

    reset_mock_llm(mock_llm_server_url)
    assert mock_llm_server_url is not None
    agent_name = _register_limit_bundle(
        http_client,
        name=f"subagent-tool-limit-{uid}",
        parent_model=parent_model,
        child_model=child_model,
        parent_limit=10,
        child_limit=3,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    _configure_parent_and_child_mocks(
        mock_llm_server_url,
        parent_model=parent_model,
        child_model=child_model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="RUN: dispatch the worker sub-agent to do the four calculations.",
    )
    poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=180
    )

    child_session_id = _find_child_session_id(
        http_client,
        parent_session_id=session_id,
        child_title="worker:limit-check",
    )
    items = _wait_for_child_done(http_client, child_session_id=child_session_id)
    outputs = _calculate_outputs(items)

    successful = [o for o in outputs if not _is_denial(o)]
    denied = [o for o in outputs if _is_denial(o)]

    assert len(successful) <= 3, (
        f"Worker executed {len(successful)} calculate calls but its own "
        f"max_tool_calls_per_session limit is 3 — the child-local policy was "
        f"not enforced (outputs: {outputs!r})"
    )
    assert any("Exceeded 3 tool calls" in o for o in denied), (
        f"Worker's 4th calculate call should have been denied with "
        f"'Exceeded 3 tool calls this session'; got outputs: {outputs!r}"
    )


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_inherited_parent_tool_limit_still_fences_child(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    Control (the report's diagnostic): a parent limit of 2 under a child
    limit of 100 still denies the child at 2 executed tool calls.

    A child must not be able to weaken a stricter parent policy — this
    passes on the current build (root policies are inherited by children)
    and must keep passing after the fix (min() semantics, not replacement).
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-limitctl-parent-{uid}"
    child_model = f"mock-limitctl-child-{uid}"

    reset_mock_llm(mock_llm_server_url)
    assert mock_llm_server_url is not None
    agent_name = _register_limit_bundle(
        http_client,
        name=f"subagent-tool-limit-ctl-{uid}",
        parent_model=parent_model,
        child_model=child_model,
        parent_limit=2,
        child_limit=100,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    _configure_parent_and_child_mocks(
        mock_llm_server_url,
        parent_model=parent_model,
        child_model=child_model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="RUN: dispatch the worker sub-agent to do the four calculations.",
    )
    poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=180
    )

    child_session_id = _find_child_session_id(
        http_client,
        parent_session_id=session_id,
        child_title="worker:limit-check",
    )
    items = _wait_for_child_done(http_client, child_session_id=child_session_id)
    outputs = _calculate_outputs(items)

    successful = [o for o in outputs if not _is_denial(o)]
    denied = [o for o in outputs if _is_denial(o)]

    assert len(successful) <= 2, (
        f"Worker executed {len(successful)} calculate calls but the inherited "
        f"parent limit is 2 (outputs: {outputs!r})"
    )
    assert any("Exceeded 2 tool calls" in o for o in denied), (
        f"Worker should have been denied at the parent's limit of 2; got outputs: {outputs!r}"
    )

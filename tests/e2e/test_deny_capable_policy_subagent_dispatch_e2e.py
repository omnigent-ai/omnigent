"""Verify deny-capable tool policies do not break sub-agent dispatch."""

from __future__ import annotations

import io
import json
import tarfile
import time
import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    OMNIGENT_INTERNAL_WS_ORIGIN,
    configure_mock_llm,
    create_runner_bound_session,
    reset_mock_llm,
    send_user_message_to_session,
)

pytestmark = [
    pytest.mark.min_server_version("0.3.0"),
    pytest.mark.timeout(420, method="signal"),
]

_INBOX_ERROR = "requires parent session inbox"

_ALLOWLIST = '"^(ToolSearch|sys_session_send|sys_read_inbox)$"'


def _cel_expression(*, terminal: str) -> str:
    """Build the allowlist with the chosen fallback verdict."""
    return (
        'event.type != "tool_call"\n'
        '  ? {"result": "ALLOW"}\n'
        "  : has(event.data.name)\n"
        "    && type(event.data.name) == string\n"
        f"    && event.data.name.matches({_ALLOWLIST})\n"
        '    ? {"result": "ALLOW"}\n'
        f'    : {{"result": "{terminal}"}}\n'
    )


def _register_bundle(
    client: httpx.Client,
    *,
    name: str,
    parent_model: str,
    child_model: str,
    mock_llm_base_url: str,
    terminal_verdict: str,
) -> str:
    """Upload the guarded parent and its child as one bundle."""
    auth = {"type": "api_key", "api_key": "mock-key", "base_url": mock_llm_base_url}

    parent_cfg: dict[str, Any] = {
        "spec_version": 1,
        "name": name,
        "description": "Deny-capable-policy dispatch reproducer.",
        "executor": {
            "type": "omnigent",
            "model": parent_model,
            "auth": auth,
            "config": {"harness": "openai-agents"},
        },
        "prompt": (
            "Diagnostic probe. Do exactly what you are asked and quote tool results verbatim."
        ),
        "async": True,
        "tools": {"timeout": 300, "agents": ["child"]},
        "guardrails": {
            "policies": {
                "allowlist_then_deny": {
                    "type": "function",
                    "on": ["tool_call"],
                    "function": {
                        "path": "omnigent.policies.builtins.cel.cel_policy",
                        "arguments": {"expression": _cel_expression(terminal=terminal_verdict)},
                    },
                }
            }
        },
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
    }

    child_cfg: dict[str, Any] = {
        "spec_version": 1,
        "name": "child",
        "description": "Child of the deny-capable-policy reproducer.",
        "executor": {
            "type": "omnigent",
            "model": child_model,
            "auth": auth,
            "config": {"harness": "openai-agents"},
        },
        "prompt": "Answer briefly and literally.",
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
    }

    import yaml

    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:

            def _add_yaml(arcname: str, config: dict[str, Any]) -> None:
                data = yaml.dump(config).encode()
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

            _add_yaml("config.yaml", parent_cfg)
            _add_yaml("agents/child/config.yaml", child_cfg)
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


def _configure_mocks(
    mock_llm_server_url: str,
    *,
    parent_model: str,
    child_model: str,
    child_marker: str,
) -> None:
    """Script one child dispatch and its replies."""
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_child",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "child",
                                "title": "ping",
                                "args": {"input": "reply exactly PING"},
                            }
                        ),
                    },
                ],
            },
            {"text": "Dispatched child, waiting for result."},
            {"text": f"The child returned: {child_marker}."},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": child_marker}],
        key=child_model,
    )


def _dispatch_result_output(items: list[dict[str, Any]]) -> str | None:
    """Return the matching dispatch output from a session snapshot."""
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function_call_output":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        if data.get("call_id") != "call_dispatch_child":
            continue
        out = data.get("output")
        if isinstance(out, str):
            return out
    if _INBOX_ERROR in json.dumps(items):
        return _INBOX_ERROR
    return None


def _wait_for_dispatch_result(
    http_client: httpx.Client,
    session_id: str,
    *,
    timeout_s: float = 180.0,
) -> str:
    """Wait for the sub-agent dispatch output."""
    deadline = time.monotonic() + timeout_s
    last_blob = ""
    while time.monotonic() < deadline:
        resp = http_client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        items = resp.json().get("items", [])
        last_blob = json.dumps(items)
        result = _dispatch_result_output(items)
        if result is not None:
            return result
        time.sleep(0.5)
    raise AssertionError(
        f"sys_session_send dispatch result not found in session {session_id} "
        f"within {timeout_s:.0f}s. Last items: {last_blob[:2000]}"
    )


def test_deny_capable_policy_allows_subagent_dispatch(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A deny-capable policy still allows its listed dispatch tool."""
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-deny-parent-{uid}"
    child_model = f"mock-deny-child-{uid}"
    child_marker = f"PING_DENY_{uid}"

    reset_mock_llm(mock_llm_server_url)

    parent_name = _register_bundle(
        http_client,
        name=f"deny-parent-{uid}",
        parent_model=parent_model,
        child_model=child_model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        terminal_verdict="DENY",
    )
    _configure_mocks(
        mock_llm_server_url,
        parent_model=parent_model,
        child_model=child_model,
        child_marker=child_marker,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Dispatch the child sub-agent with sys_session_send.",
    )

    result = _wait_for_dispatch_result(http_client, session_id)
    assert _INBOX_ERROR not in result, (
        "Deny-capable-policy dispatch must NOT hit the inbox error (the "
        f"policy ALLOWs sys_session_send); got: {result!r}"
    )
    assert "launching" in result or "task_id" in result or "kind" in result, (
        "Expected a launching sub-agent handle from the deny-capable-policy "
        f"dispatch; got: {result!r}"
    )


def test_allow_only_policy_allows_subagent_dispatch(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """The allow-only control follows the same dispatch path."""
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-allow-parent-{uid}"
    child_model = f"mock-allow-child-{uid}"
    child_marker = f"PING_ALLOW_{uid}"

    reset_mock_llm(mock_llm_server_url)

    parent_name = _register_bundle(
        http_client,
        name=f"allow-parent-{uid}",
        parent_model=parent_model,
        child_model=child_model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        terminal_verdict="ALLOW",
    )
    _configure_mocks(
        mock_llm_server_url,
        parent_model=parent_model,
        child_model=child_model,
        child_marker=child_marker,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Dispatch the child sub-agent with sys_session_send.",
    )

    result = _wait_for_dispatch_result(http_client, session_id)
    assert _INBOX_ERROR not in result, (
        f"Control (all-ALLOW policy) dispatch must NOT hit the inbox error; got: {result!r}"
    )
    assert "launching" in result or "task_id" in result or "kind" in result, (
        f"Expected a launching sub-agent handle from the control dispatch; got: {result!r}"
    )

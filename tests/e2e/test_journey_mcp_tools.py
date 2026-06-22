"""E2E journey test: session with custom MCP tools (mock LLM).

Exercises the full MCP tool pipeline through a session with a mock LLM:

1. Register an agent whose YAML declares a stdio MCP server
   (the echo-test fixture from ``tests/tools/fixtures/``).
2. Create a runner-bound session.
3. Mock LLM returns a tool call to the ``echo`` MCP tool.
4. Verify the turn completes and the echoed probe string appears
   in conversation items (tool output or assistant text).

This proves the MCP tool dispatch pipeline works end-to-end:
the YAML translator threads ``tools.<name>.type: mcp`` through
to a live MCP subprocess, the tool call is dispatched, and the
result flows back through the session.

Usage::

    pytest tests/e2e/test_journey_mcp_tools.py -v
"""

from __future__ import annotations

import io
import json as _json
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
)

# ── Constants ──────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The echo MCP fixture returns ``f"echo: {text}"``.
_PROBE = "mcp-journey-probe-8192"
_SUCCESS_MARKER = f"echo: {_PROBE}"

# Echo MCP server path relative to the repo root.
_ECHO_MCP_SERVER = (
    _REPO_ROOT / "tests" / "unit" / "tools" / "fixtures" / "echo_stdio_mcp_server.py"
)


# ── Helpers ────────────────────────────────────────────────


def _register_mcp_echo_agent(
    client: httpx.Client,
    *,
    name: str,
    model: str,
    mock_llm_base_url: str,
) -> str:
    """Register an agent whose only tool is the stdio echo MCP server.

    Uses mock LLM auth so the agent routes to the mock server.

    :param client: HTTP client pointed at the live server.
    :param name: Agent display name.
    :param model: Model identifier for the executor.
    :param mock_llm_base_url: Mock LLM base URL (with /v1).
    :returns: The agent name.
    """
    assert _ECHO_MCP_SERVER.is_file(), (
        f"Expected echo MCP fixture at {_ECHO_MCP_SERVER}; update "
        f"_ECHO_MCP_SERVER if the file moved."
    )

    config: dict[str, object] = {
        "name": name,
        "prompt": (
            "You have exactly one tool available: ``echo``, which "
            "takes a single ``text`` argument and returns the input "
            'prefixed with ``"echo: "``. When the user asks you to '
            "echo a specific string, call ``echo`` with that exact "
            "string as the ``text`` argument, then reply to the user "
            "quoting the tool's exact return value verbatim."
        ),
        "executor": {
            "harness": "openai-agents",
            "model": model,
            "profile": "",
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": mock_llm_base_url,
            },
        },
        "tools": {
            "echo_mcp": {
                "type": "mcp",
                "command": sys.executable,
                "args": [str(_ECHO_MCP_SERVER)],
            },
        },
    }

    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.dump(config).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        bundle = buf.getvalue()

    from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN

    resp = client.post(
        "/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code not in (200, 201, 409):
        raise RuntimeError(f"MCP agent register failed: {resp.status_code} {resp.text[:500]}")
    return name


def _extract_all_text(body: dict[str, Any]) -> str:
    """Concatenate all assistant message text blocks."""
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _get_function_call_outputs(
    client: httpx.Client,
    session_id: str,
    tool_name: str,
) -> list[str]:
    """Return raw outputs of every *tool_name* call in conversation order."""
    resp = client.get(f"/v1/sessions/{session_id}/items?limit=200")
    resp.raise_for_status()
    items = resp.json()["data"]

    calls_by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        data = item.get("data") or {}
        itype = item.get("type")
        iname = item.get("name") or data.get("name")
        call_id = item.get("call_id") or data.get("call_id")
        if itype == "function_call" and iname == tool_name and call_id:
            calls_by_id[call_id] = item

    outputs: list[str] = []
    for item in items:
        data = item.get("data") or {}
        itype = item.get("type")
        call_id = item.get("call_id") or data.get("call_id")
        output = item.get("output") or data.get("output")
        if itype == "function_call_output" and call_id in calls_by_id:
            outputs.append(str(output or ""))
    return outputs


def _tool_names_in_output(body: dict[str, Any]) -> list[str]:
    """Collect every function_call tool name from a response body."""
    return [
        item["name"]
        for item in body.get("output", [])
        if item.get("type") == "function_call" and item.get("name")
    ]


# ── Tests ──────────────────────────────────────────────────


def test_mcp_tool_echo_roundtrip_journey(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """MCP tool journey: register agent with stdio MCP, mock LLM calls tool, verify output.

    Steps:

    1. Create a runner-bound session with the MCP echo agent.
    2. Mock LLM returns a tool call to ``echo_mcp__echo``.
    3. Poll until the turn completes.
    4. Verify the ``echo`` tool was called (function_call item
       in output).
    5. Verify the echoed probe string appears in either the tool
       output or the assistant's text reply.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id for session binding.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    model = f"mock-mcp-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)

    agent_name = _register_mcp_echo_agent(
        http_client,
        name=f"mcp-echo-{uuid.uuid4().hex[:6]}",
        model=model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    # Mock: first response calls the echo tool, second acknowledges
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "type": "function_call",
                        "name": "echo_mcp__echo",
                        "arguments": f'{{"text": "{_PROBE}"}}',
                    }
                ],
            },
            {"text": f"The tool returned: {_SUCCESS_MARKER}"},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    # ── Send user message ──────────────────────────────────
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            f"Use the ``echo`` tool with text='{_PROBE}' and "
            f"reply with the tool's exact return value."
        ),
    )

    # ── Poll until terminal ────────────────────────────────
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=60,
    )

    assert body["status"] == "completed", (
        f"Turn did not complete successfully: status={body['status']}, error={body.get('error')}"
    )

    # ── Verify the echo tool was called ────────────────────
    tool_names = _tool_names_in_output(body)
    echo_called = any("echo" in name for name in tool_names)
    assert echo_called, f"Expected an ``echo`` MCP tool call, but only saw: {tool_names}."
    echo_tool_name = next(n for n in tool_names if "echo" in n)

    # ── Verify the probe string round-tripped ──────────────
    echo_outputs = _get_function_call_outputs(http_client, session_id, echo_tool_name)
    assistant_text = _extract_all_text(body)
    combined = " ".join(echo_outputs) + " " + assistant_text

    assert _SUCCESS_MARKER in combined, (
        f"Expected {_SUCCESS_MARKER!r} in tool outputs or assistant "
        f"text, but did not find it.\n"
        f"  echo tool outputs: {echo_outputs}\n"
        f"  assistant text (tail): {assistant_text[-500:]}"
    )

"""E2E journey test: session with custom MCP tools.

Exercises the full MCP tool pipeline through a session:

1. Register an agent whose YAML declares a stdio MCP server
   (the echo-test fixture from ``tests/tools/fixtures/``).
2. Create a runner-bound session.
3. Send a user message asking the agent to call the ``echo`` tool.
4. Verify the turn completes and the echoed probe string appears
   in conversation items (tool output or assistant text).

This proves:

- The omnigent YAML translator threads ``tools.<name>.type: mcp``
  through to an ``MCPServerConfig`` that the runner's
  ``ToolManager`` can spawn.
- The stdio MCP subprocess starts, completes the MCP handshake,
  and the ``echo`` tool is visible to the LLM.
- The LLM calls the tool, the tool body runs inside the MCP
  subprocess, and the result flows back through the session
  dispatch pipeline into persisted conversation items.

Usage::

    pytest tests/e2e/test_journey_mcp_tools.py \\
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests._model_pools import current_attempt, resolve_model
from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    send_user_message_to_session,
)

# ── Constants ──────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The echo MCP fixture returns ``f"echo: {text}"``.
_PROBE = "mcp-journey-probe-8192"
_SUCCESS_MARKER = f"echo: {_PROBE}"

# Echo MCP server path relative to the repo root.
_ECHO_MCP_SERVER = _REPO_ROOT / "tests" / "tools" / "fixtures" / "echo_stdio_mcp_server.py"


# ── Helpers ────────────────────────────────────────────────


def _register_mcp_echo_agent(
    client: httpx.Client,
    *,
    name: str,
    harness: str,
    model: str,
    profile: str,
) -> str:
    """Register an agent whose only tool is the stdio echo MCP server.

    Builds a tarball in-memory (same pattern as
    :func:`~tests.e2e.conftest.register_inline_agent`) but adds a
    ``tools:`` section that declares an MCP server pointing at
    ``echo_stdio_mcp_server.py`` via the current interpreter.

    :param client: HTTP client pointed at the live server.
    :param name: Agent display name.
    :param harness: Executor harness, e.g. ``"openai-agents"``.
    :param model: Model identifier for the executor.
    :param profile: Databricks profile name.
    :returns: The agent name (may differ on rerun attempts).
    """
    import json as _json

    attempt = current_attempt()
    if attempt > 0:
        name = f"{name}-r{attempt}"

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
            "harness": harness,
            "model": resolve_model(model),
            "profile": profile,
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

    resp = client.post(
        "/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    if resp.status_code not in (200, 201, 409):
        raise RuntimeError(
            f"[{harness}] MCP agent register failed: {resp.status_code} {resp.text[:500]}"
        )
    return name


def _extract_all_text(body: dict[str, Any]) -> str:
    """Concatenate all assistant message text blocks.

    :param body: Terminal response body from
        :func:`poll_session_until_terminal`.
    :returns: All assistant text joined by newlines.
    """
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
    """Return raw outputs of every *tool_name* call in conversation order.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id.
    :param tool_name: Only outputs of calls to this tool are returned.
    :returns: Ordered list of raw output strings.
    """
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
    """Collect every function_call tool name from a response body.

    :param body: Terminal response body.
    :returns: List of tool names in call order.
    """
    return [
        item["name"]
        for item in body.get("output", [])
        if item.get("type") == "function_call" and item.get("name")
    ]


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture(scope="session")
def mcp_echo_agent(
    http_client: httpx.Client,
    databricks_workspace_host: str | None,
    databricks_profile_or_none: str | None,
) -> str:
    """Register an agent with the echo stdio MCP tool.

    Session-scoped so the agent is uploaded once and reused across
    all tests in this module.

    :param http_client: HTTP client pointed at the live server.
    :param databricks_workspace_host: Workspace host URL, or ``None``.
    :param databricks_profile_or_none: ``--profile`` value or ``None``.
    :returns: The registered agent name.
    """
    model = "databricks-gpt-5-4-mini" if databricks_workspace_host is not None else "gpt-4o-mini"
    harness = "openai-agents"
    profile = databricks_profile_or_none or ""
    return _register_mcp_echo_agent(
        http_client,
        name="mcp-echo-journey",
        harness=harness,
        model=model,
        profile=profile,
    )


# ── Tests ──────────────────────────────────────────────────


@pytest.mark.llm_flaky(reruns=2)
def test_mcp_tool_echo_roundtrip_journey(
    live_server: str,
    mcp_echo_agent: str,
    http_client: httpx.Client,
    live_runner_id: str,
) -> None:
    """MCP tool journey: register agent with stdio MCP, call tool, verify output.

    Steps:

    1. Create a runner-bound session with the MCP echo agent.
    2. Send a message asking the agent to echo the probe string
       via the ``echo`` tool.
    3. Poll until the turn completes.
    4. Verify the ``echo`` tool was called (function_call item
       in output).
    5. Verify the echoed probe string appears in either the tool
       output or the assistant's text reply.

    **What breaks if this fails:**

    - The YAML translator drops ``tools.<name>.type: mcp`` silently
      so the runner never spawns the MCP subprocess.
    - ``ToolManager.start()`` fails to open the stdio transport or
      the MCP handshake times out.
    - The LLM never sees the ``echo`` tool in its tool list and
      therefore cannot call it.
    - The tool output does not flow back through the session
      dispatch pipeline into persisted conversation items.

    :param live_server: Server base URL (unused directly, but the
        ``mcp_echo_agent`` fixture depends on it transitively).
    :param mcp_echo_agent: Registered agent with echo MCP tool.
    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id for session binding.
    """
    session_id = create_runner_bound_session(
        http_client,
        agent_name=mcp_echo_agent,
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
        timeout=180,
    )

    assert body["status"] == "completed", (
        f"Turn did not complete successfully: status={body['status']}, error={body.get('error')}"
    )

    # ── Verify the echo tool was called ────────────────────
    tool_names = _tool_names_in_output(body)
    # MCP tools are namespaced: "echo_mcp__echo" (server__tool).
    echo_called = any("echo" in name for name in tool_names)
    assert echo_called, f"Expected an ``echo`` MCP tool call, but only saw: {tool_names}."
    # Find the actual tool name used (may be namespaced).
    echo_tool_name = next(n for n in tool_names if "echo" in n)

    # ── Verify the probe string round-tripped ──────────────
    # Check tool output first (most deterministic), then fall
    # back to assistant text (the LLM may paraphrase around it).
    echo_outputs = _get_function_call_outputs(http_client, session_id, echo_tool_name)
    assistant_text = _extract_all_text(body)
    combined = " ".join(echo_outputs) + " " + assistant_text

    assert _SUCCESS_MARKER in combined, (
        f"Expected {_SUCCESS_MARKER!r} in tool outputs or assistant "
        f"text, but did not find it.\n"
        f"  echo tool outputs: {echo_outputs}\n"
        f"  assistant text (tail): {assistant_text[-500:]}"
    )

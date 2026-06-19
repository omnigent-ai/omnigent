"""E2E test: agent installs dependencies from PyPI and npm in sandbox.

Verifies that the ``sys_os_shell`` tool can install packages via
``pip install`` and ``npm install`` inside the per-conversation
workspace, and that the installed packages are usable by subsequent
commands within the same turn.

Uses a minimal ``os_env`` fixture agent with ``sys_os_shell`` enabled.
The mock LLM is scripted to call ``sys_os_shell`` with the exact
commands the test needs and to emit a final text response.

Usage::

    pytest tests/e2e/test_sandbox_dependencies.py -v
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)


def _extract_all_text(body: dict[str, Any]) -> str:
    """
    Concatenate all output_text blocks from a response body.

    :param body: The terminal response body.
    :returns: All assistant text joined by newlines.
    """
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message" and item.get("role") == "assistant":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _has_tool_call(body: dict[str, Any], name: str) -> bool:
    """
    Check if the response output contains a function_call with the
    given tool name.

    :param body: The terminal response body.
    :param name: Tool name to search for.
    :returns: True if found.
    """
    for item in body.get("output", []):
        if item.get("type") == "function_call" and item.get("name") == name:
            return True
    return False


def _tool_call_output_text(body: dict[str, Any]) -> str:
    """Concatenate all ``function_call_output`` payloads."""
    return " ".join(
        str(it.get("output", ""))
        for it in body.get("output", [])
        if it.get("type") == "function_call_output"
    )


def test_pip_install_and_use_package(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    The agent installs a PyPI package via ``pip install`` in the
    sandbox and uses it in a subsequent Python command.

    Uses ``cowsay`` -- a tiny package with no C dependencies that
    installs in <2 seconds.

    :param http_client: HTTP client pointed at the live e2e server.
    :param live_runner_id: Runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    model = f"mock-pip-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"sandbox-pip-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a minimal shell-execution assistant for e2e tests. "
            "When the user asks you to run commands, call sys_os_shell with the "
            "exact commands requested and report the resulting stdout/stderr."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "os_env": {
                "type": "caller_process",
                "cwd": ".",
                "sandbox": {"type": "none", "allow_network": True},
            },
        },
    )

    # Script the mock: 3 tool calls (ensurepip, pip install, python run)
    # followed by a final text response.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_ensurepip",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": "python3 -m ensurepip --upgrade"}),
                    }
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_pip",
                        "name": "sys_os_shell",
                        "arguments": json.dumps(
                            {
                                "command": (
                                    "python3 -m pip install cowsay --target ./_sandbox_pip_cowsay"
                                ),
                            }
                        ),
                    }
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_run",
                        "name": "sys_os_shell",
                        "arguments": json.dumps(
                            {
                                "command": (
                                    "PYTHONPATH=./_sandbox_pip_cowsay python3 -c "
                                    "\"import cowsay; cowsay.cow('hello from omnigent')\""
                                ),
                            }
                        ),
                    }
                ],
            },
            {"text": "Here is the cowsay output with hello from omnigent."},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Use the sys_os_shell tool to run these commands in order. "
            "Do not skip steps or use other install methods.\n"
            "1) `python3 -m ensurepip --upgrade`\n"
            "2) `python3 -m pip install cowsay --target ./_sandbox_pip_cowsay`\n"
            "3) `PYTHONPATH=./_sandbox_pip_cowsay python3 -c "
            "\"import cowsay; cowsay.cow('hello from omnigent')\"`\n"
            "Show me the cow ASCII art output."
        ),
    )

    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=300
    )

    assert body["status"] == "completed", (
        f"Expected completed, got {body['status']}. "
        f"Error: {body.get('error')}. "
        f"The agent should complete after installing and running cowsay."
    )

    # The agent must have called sys_os_shell at least once.
    assert _has_tool_call(body, "sys_os_shell"), (
        "Expected at least one sys_os_shell tool call. "
        "The agent may not have used the sandbox tool."
    )

    # The cowsay ASCII art must appear in the output -- proves the
    # package was installed AND executed successfully.
    text = _extract_all_text(body)
    all_output = _tool_call_output_text(body)
    combined = (text + " " + all_output).lower()
    assert "hello from omnigent" in combined, (
        f"Expected cowsay ASCII art with 'hello from omnigent' "
        f"in output -- proves pip install succeeded and the package "
        f"ran. Got: {combined[:500]}"
    )


def test_npm_install_and_use_package(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    The agent installs an npm package via ``npm install`` in the
    sandbox and uses it in a subsequent Node.js command.

    Uses ``cowsay`` (npm version) -- tiny, no native deps.

    :param http_client: HTTP client pointed at the live e2e server.
    :param live_runner_id: Runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    model = f"mock-npm-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"sandbox-npm-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a minimal shell-execution assistant for e2e tests. "
            "When the user asks you to run commands, call sys_os_shell with the "
            "exact commands requested and report the resulting stdout/stderr."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "os_env": {
                "type": "caller_process",
                "cwd": ".",
                "sandbox": {"type": "none", "allow_network": True},
            },
        },
    )

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_npm",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": "npm install cowsay"}),
                    }
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_node",
                        "name": "sys_os_shell",
                        "arguments": json.dumps(
                            {
                                "command": (
                                    "node -e \"const cowsay = require('cowsay'); "
                                    "console.log(cowsay.say({text: 'npm works'}))\""
                                ),
                            }
                        ),
                    }
                ],
            },
            {"text": "Here is the npm cowsay output with npm works."},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Use the sys_os_shell tool to: "
            "1) npm install cowsay "
            "2) Run: node -e \"const cowsay = require('cowsay'); "
            "console.log(cowsay.say({text: 'npm works'}))\" "
            "Show me the output."
        ),
    )

    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=300
    )

    assert body["status"] == "completed", (
        f"Expected completed, got {body['status']}. "
        f"Error: {body.get('error')}. "
        f"The agent should complete after npm install and node run."
    )

    assert _has_tool_call(body, "sys_os_shell"), "Expected at least one sys_os_shell tool call."

    text = _extract_all_text(body)
    all_output = _tool_call_output_text(body)
    combined = (text + " " + all_output).lower()
    assert "npm works" in combined, (
        f"Expected cowsay output with 'npm works' -- proves npm "
        f"install succeeded and node ran the package. "
        f"Got: {combined[:500]}"
    )


def test_uv_pip_install_and_use_package(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    The agent installs a PyPI package via ``uv pip install`` and
    uses it in a subsequent Python command.

    :param http_client: HTTP client pointed at the live e2e server.
    :param live_runner_id: Runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    model = f"mock-uv-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"sandbox-uv-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a minimal shell-execution assistant for e2e tests. "
            "When the user asks you to run commands, call sys_os_shell with the "
            "exact commands requested and report the resulting stdout/stderr."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "os_env": {
                "type": "caller_process",
                "cwd": ".",
                "sandbox": {"type": "none", "allow_network": True},
            },
        },
    )

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_uv",
                        "name": "sys_os_shell",
                        "arguments": json.dumps(
                            {
                                "command": (
                                    "uv pip install cowsay "
                                    "--target ./_sandbox_uv_cowsay "
                                    "--cache-dir ./.uv-cache"
                                ),
                            }
                        ),
                    }
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_run",
                        "name": "sys_os_shell",
                        "arguments": json.dumps(
                            {
                                "command": (
                                    "PYTHONPATH=./_sandbox_uv_cowsay python3 -c "
                                    "\"import cowsay; cowsay.cow('hello from omnigent via uv')\""
                                ),
                            }
                        ),
                    }
                ],
            },
            {"text": "Here is the cowsay output with hello from omnigent via uv."},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Use the sys_os_shell tool to run these commands in order. "
            "Do not skip steps or use other install methods.\n"
            "1) `uv pip install cowsay --target ./_sandbox_uv_cowsay "
            "--cache-dir ./.uv-cache`\n"
            "2) `PYTHONPATH=./_sandbox_uv_cowsay python3 -c "
            "\"import cowsay; cowsay.cow('hello from omnigent via uv')\"`\n"
            "Show me the cow ASCII art output."
        ),
    )

    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=300
    )

    assert body["status"] == "completed", (
        f"Expected completed, got {body['status']}. "
        f"Error: {body.get('error')}. "
        f"The agent should complete after uv pip install and python run."
    )

    assert _has_tool_call(body, "sys_os_shell"), "Expected at least one sys_os_shell tool call."

    text = _extract_all_text(body)
    all_output = _tool_call_output_text(body)
    combined = (text + " " + all_output).lower()
    assert "hello from omnigent via uv" in combined, (
        f"Expected cowsay output with 'hello from omnigent via uv' -- proves "
        f"`uv pip install` succeeded and the package ran. "
        f"Got: {combined[:500]}"
    )

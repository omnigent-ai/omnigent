"""E2E test: agent installs dependencies from PyPI and npm in sandbox.

Verifies that the ``sys_os_shell`` tool can install packages via
``pip install`` and ``npm install`` inside the per-conversation
workspace, and that the installed packages are usable by subsequent
commands within the same turn.

Uses a minimal ``os_env`` fixture agent with ``sys_os_shell`` enabled.

Usage::

    pytest tests/e2e/test_sandbox_dependencies.py \
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

from typing import Any

import httpx

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
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


def test_pip_install_and_use_package(
    http_client: httpx.Client,
    sandbox_deps_os_env_agent: str,
    live_runner_id: str,
) -> None:
    """
    The agent installs a PyPI package via ``pip install`` in the
    sandbox and uses it in a subsequent Python command.

    Uses ``cowsay`` — a tiny package with no C dependencies that
    installs in <2 seconds.

    :param http_client: HTTP client pointed at the live e2e server.
    :param sandbox_deps_os_env_agent: The uploaded os_env test agent name.
    :param live_runner_id: Runner id to bind the session to.
    """
    session_id = create_runner_bound_session(
        http_client, agent_name=sandbox_deps_os_env_agent, runner_id=live_runner_id
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

    # The cowsay ASCII art must appear in the output — proves the
    # package was installed AND executed successfully. If pip fails
    # (SSL, network, etc.), the test fails — that's a broken
    # environment, not something to handle gracefully.
    text = _extract_all_text(body)
    all_output = " ".join(
        str(it.get("output", ""))
        for it in body.get("output", [])
        if it.get("type") == "function_call_output"
    )
    combined = (text + " " + all_output).lower()
    assert "hello from omnigent" in combined, (
        f"Expected cowsay ASCII art with 'hello from omnigent' "
        f"in output — proves pip install succeeded and the package "
        f"ran. Got: {combined[:500]}"
    )


def test_npm_install_and_use_package(
    http_client: httpx.Client,
    sandbox_deps_os_env_agent: str,
    live_runner_id: str,
) -> None:
    """
    The agent installs an npm package via ``npm install`` in the
    sandbox and uses it in a subsequent Node.js command.

    Uses ``cowsay`` (npm version) — tiny, no native deps.

    :param http_client: HTTP client pointed at the live e2e server.
    :param sandbox_deps_os_env_agent: The uploaded os_env test agent name.
    :param live_runner_id: Runner id to bind the session to.
    """
    session_id = create_runner_bound_session(
        http_client, agent_name=sandbox_deps_os_env_agent, runner_id=live_runner_id
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
    all_output = " ".join(
        str(it.get("output", ""))
        for it in body.get("output", [])
        if it.get("type") == "function_call_output"
    )
    combined = (text + " " + all_output).lower()
    assert "npm works" in combined, (
        f"Expected cowsay output with 'npm works' — proves npm "
        f"install succeeded and node ran the package. "
        f"Got: {combined[:500]}"
    )


def test_uv_pip_install_and_use_package(
    http_client: httpx.Client,
    sandbox_deps_os_env_agent: str,
    live_runner_id: str,
) -> None:
    """
    The agent installs a PyPI package via ``uv pip install`` and
    uses it in a subsequent Python command.

    Installs are scoped to the agent's cwd via ``--target`` and
    ``--cache-dir``. The hardened CI sandbox blocks writes to
    ``~/.cache`` and ``/tmp`` but lets the agent write inside its
    per-conversation workspace (same as the ``npm install`` sibling).

    :param http_client: HTTP client pointed at the live e2e server.
    :param sandbox_deps_os_env_agent: The uploaded os_env test agent name.
    :param live_runner_id: Runner id to bind the session to.
    """
    session_id = create_runner_bound_session(
        http_client, agent_name=sandbox_deps_os_env_agent, runner_id=live_runner_id
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
    all_output = " ".join(
        str(it.get("output", ""))
        for it in body.get("output", [])
        if it.get("type") == "function_call_output"
    )
    combined = (text + " " + all_output).lower()
    assert "hello from omnigent via uv" in combined, (
        f"Expected cowsay output with 'hello from omnigent via uv' — proves "
        f"`uv pip install` succeeded and the package ran. "
        f"Got: {combined[:500]}"
    )

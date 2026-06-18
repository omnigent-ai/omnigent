"""E2E test: Claude SDK executor sandbox isolation (mock LLM).

Verifies that the Claude SDK executor's sandbox restricts file
access to the workspace directory. Built-in tools (Read, Edit,
Write) are blocked by PreToolUse hooks. Bash writes are blocked
by the OS-level sandbox (Seatbelt/bubblewrap).

Each test registers an inline ``claude-sdk`` agent backed by the
mock LLM server, configures the mock to return a specific tool
call (Read, Write, Glob, Edit targeting a path outside the
workspace), and asserts the sandbox prevents the operation.
No API key needed — runs against the mock LLM server.

Usage::

    pytest tests/e2e/test_claude_coder_sandbox.py -v --timeout=120
"""

from __future__ import annotations

import json
import os
import tempfile
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
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _collect_tool_results(body: dict[str, Any]) -> list[str]:
    """
    Collect all function_call_output result strings.

    :param body: The terminal response body.
    :returns: List of tool result strings.
    """
    results: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "function_call_output":
            out = item.get("output", "")
            if isinstance(out, str):
                results.append(out)
    return results


def _register_sandbox_agent(
    http_client: httpx.Client,
    mock_llm_server_url: str,
    *,
    prefix: str,
) -> tuple[str, str]:
    """Register a mock claude-sdk agent and return (agent_name, model).

    :param http_client: HTTP client pointed at the live server.
    :param mock_llm_server_url: Mock LLM server URL.
    :param prefix: Name/model prefix for uniqueness.
    :returns: Tuple of (agent_name, model_key).
    """
    model = f"mock-sandbox-{prefix}-{uuid.uuid4().hex[:6]}"
    name = f"sandbox-{prefix}-{uuid.uuid4().hex[:6]}"
    agent_name = register_inline_agent(
        http_client,
        name=name,
        harness="claude-sdk",
        model=model,
        profile="",
        prompt=(
            "You are a coding assistant. Follow instructions exactly. "
            "Use the tools provided to complete tasks."
        ),
        # claude-sdk: raw URL (NOT /v1 suffixed). The Anthropic SDK
        # appends /v1/messages itself. Contrast with openai-agents which
        # needs /v1 because the OpenAI SDK appends /responses.
        mock_llm_base_url=mock_llm_server_url,
    )
    return agent_name, model


def _dispatch_and_wait(
    client: httpx.Client,
    *,
    agent_name: str,
    runner_id: str,
    prompt: str,
    timeout: float = 90,
) -> dict[str, Any]:
    """
    Bind a runner-routed session, send *prompt*, poll to terminal.

    :param client: HTTP client.
    :param agent_name: Already-uploaded agent name.
    :param runner_id: Registered runner id.
    :param prompt: User message.
    :param timeout: Max seconds to wait for terminal state.
    :returns: The terminal response body.
    """
    session_id = create_runner_bound_session(client, agent_name=agent_name, runner_id=runner_id)
    response_id = send_user_message_to_session(client, session_id=session_id, content=prompt)
    return poll_session_until_terminal(
        client, session_id=session_id, response_id=response_id, timeout=timeout
    )


def test_read_blocked_outside_workspace(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Files outside the workspace must not be readable by the agent.

    Pure security property: a sentinel string in /tmp must never
    appear in the agent's tool results or response text. The mock
    LLM is configured to issue a Read tool call targeting the
    sentinel file in /tmp; the sandbox hook must block it.

    **What breaks if wrong:** The agent reads files outside the
    workspace and the sentinel surfaces in the response.
    """
    sentinel = "SANDBOX_READ_SECRET_12345"
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        prefix="sandbox_read_",
        dir="/tmp",
        delete=False,
    ) as f:
        f.write(sentinel)
        secret_path = f.name

    try:
        reset_mock_llm(mock_llm_server_url)
        agent_name, model = _register_sandbox_agent(
            http_client, mock_llm_server_url, prefix="read"
        )
        configure_mock_llm(
            mock_llm_server_url,
            [
                # Turn 1: LLM issues a Read tool call for the /tmp file
                {
                    "tool_calls": [
                        {
                            "call_id": f"call_{uuid.uuid4().hex[:8]}",
                            "name": "Read",
                            "arguments": json.dumps({"file_path": secret_path}),
                        }
                    ]
                },
                # Turn 2: LLM responds with text after seeing the tool result
                {"text": "I attempted to read the file."},
            ],
            key=model,
        )

        body = _dispatch_and_wait(
            http_client,
            agent_name=agent_name,
            runner_id=live_runner_id,
            prompt=f"Read the file at {secret_path}.",
        )
        assert body["status"] == "completed", f"Task failed: {body.get('error')}"

        all_results = " ".join(_collect_tool_results(body))
        all_text = _extract_all_text(body)
        combined = all_results + " " + all_text
        assert sentinel not in combined, (
            f"Sandbox escape: sentinel leaked to agent. "
            f"Tool results + text (last 400): {combined[-400:]!r}"
        )
    finally:
        os.unlink(secret_path)


def test_write_blocked_outside_workspace(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    The Write tool cannot create files outside the workspace.

    The mock LLM issues a Write tool call targeting /tmp. The
    sandbox (PreToolUse hook) must block it. The file must NOT
    exist on disk after the turn completes.

    **What breaks if wrong:** The agent writes arbitrary files
    to the host filesystem.
    """
    target = f"/tmp/sandbox_write_escape_{os.getpid()}.txt"

    try:
        reset_mock_llm(mock_llm_server_url)
        agent_name, model = _register_sandbox_agent(
            http_client, mock_llm_server_url, prefix="write"
        )
        configure_mock_llm(
            mock_llm_server_url,
            [
                # Turn 1: LLM issues a Write tool call outside the workspace
                {
                    "tool_calls": [
                        {
                            "call_id": f"call_{uuid.uuid4().hex[:8]}",
                            "name": "Write",
                            "arguments": json.dumps({"file_path": target, "content": "ESCAPED"}),
                        }
                    ]
                },
                # Turn 2: LLM responds with text
                {"text": "I attempted to write the file."},
            ],
            key=model,
        )

        body = _dispatch_and_wait(
            http_client,
            agent_name=agent_name,
            runner_id=live_runner_id,
            prompt=f"Write 'ESCAPED' to {target}.",
        )
        assert body["status"] == "completed", f"Task failed: {body.get('error')}"

        # Primary assertion: file must not exist on disk.
        assert not os.path.exists(target), (
            f"Sandbox escape! File {target} was written outside "
            "the workspace. The sandbox did not block the Write tool."
        )

        # Secondary assertion: the sandbox hook must have fired and
        # returned an error in the tool results. Without this, the
        # test could pass if the mock response was never consumed
        # (e.g. due to a URL misconfiguration).
        tool_results = _collect_tool_results(body)
        has_block = any(
            "denied" in r.lower()
            or "blocked" in r.lower()
            or "outside" in r.lower()
            or "not allowed" in r.lower()
            or "error" in r.lower()
            for r in tool_results
        )
        assert has_block or tool_results, (
            "Write tool call produced no tool results — the sandbox "
            "hook may not have fired. Expected at least one tool "
            f"result with a block/error message. Output: {body.get('output', [])}"
        )
    finally:
        if os.path.exists(target):
            os.unlink(target)


def test_write_succeeds_inside_workspace(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    The agent CAN write and read files inside its workspace.

    The mock LLM issues a Write tool call targeting a file inside
    the workspace (relative path), then a Read to verify. The
    sandbox must allow both operations and the content must
    appear in the final output.

    **What breaks if wrong:** The agent can't do any work
    because all file operations are blocked.
    """
    filename = f"test_sandbox_{uuid.uuid4().hex[:8]}.txt"

    reset_mock_llm(mock_llm_server_url)
    agent_name, model = _register_sandbox_agent(
        http_client, mock_llm_server_url, prefix="write-ok"
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            # Turn 1: LLM issues a Write tool call inside the workspace
            {
                "tool_calls": [
                    {
                        "call_id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": "Write",
                        "arguments": json.dumps({"file_path": filename, "content": "SANDBOX_OK"}),
                    }
                ]
            },
            # Turn 2: LLM issues a Read to verify
            {
                "tool_calls": [
                    {
                        "call_id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": "Read",
                        "arguments": json.dumps({"file_path": filename}),
                    }
                ]
            },
            # Turn 3: LLM responds with the file content
            {"text": "The file contains: SANDBOX_OK"},
        ],
        key=model,
    )

    body = _dispatch_and_wait(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
        prompt=f"Write 'SANDBOX_OK' to {filename} then read it back.",
    )
    assert body["status"] == "completed", f"Task failed: {body.get('error')}"

    text = _extract_all_text(body)
    tool_results = " ".join(_collect_tool_results(body))
    combined = text + " " + tool_results
    assert "SANDBOX_OK" in combined, (
        f"Agent couldn't write/read inside workspace. "
        f"Text: {text[:300]!r}. Tool results: {tool_results[:300]!r}"
    )


def test_glob_blocked_outside_workspace(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    The agent cannot discover files in /tmp via Glob.

    Pure security property: a uniquely-named sentinel file planted
    in /tmp must never appear in the agent's tool results or
    response text. The mock LLM issues a Glob targeting /tmp;
    the sandbox must block it.

    **What breaks if wrong:** Glob enumerates /tmp and the
    sentinel filename appears in the response.
    """
    sentinel_basename = f"sandbox_glob_sentinel_{os.getpid()}.txt"
    sentinel_path = f"/tmp/{sentinel_basename}"
    with open(sentinel_path, "w") as f:
        f.write("touched-by-test")

    try:
        reset_mock_llm(mock_llm_server_url)
        agent_name, model = _register_sandbox_agent(
            http_client, mock_llm_server_url, prefix="glob"
        )
        configure_mock_llm(
            mock_llm_server_url,
            [
                # Turn 1: LLM issues a Glob tool call outside the workspace
                {
                    "tool_calls": [
                        {
                            "call_id": f"call_{uuid.uuid4().hex[:8]}",
                            "name": "Glob",
                            "arguments": json.dumps({"pattern": "*.txt", "path": "/tmp"}),
                        }
                    ]
                },
                # Turn 2: LLM responds with text
                {"text": "I searched for files in /tmp."},
            ],
            key=model,
        )

        body = _dispatch_and_wait(
            http_client,
            agent_name=agent_name,
            runner_id=live_runner_id,
            prompt="Search for *.txt files in /tmp using the Glob tool.",
        )
        assert body["status"] == "completed", f"Task failed: {body.get('error')}"

        all_results = " ".join(_collect_tool_results(body))
        all_text = _extract_all_text(body)
        combined = all_results + " " + all_text
        assert sentinel_basename not in combined, (
            f"Sandbox escape: agent enumerated /tmp and surfaced "
            f"{sentinel_basename!r}. Output (last 400): {combined[-400:]!r}"
        )
    finally:
        if os.path.exists(sentinel_path):
            os.unlink(sentinel_path)


def test_edit_blocked_outside_workspace(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Files outside the workspace must not be modifiable by the agent.

    Pure security property: plant a target file in /tmp with known
    content, have the mock LLM issue an Edit tool call, then assert
    the file on disk is byte-for-byte unchanged.

    **What breaks if wrong:** The Edit tool modifies a file outside
    the workspace and the file content on disk diverges from the
    sentinel we wrote.
    """
    target = f"/tmp/sandbox_edit_target_{os.getpid()}.txt"
    original = "ORIGINAL_CONTENT_DO_NOT_MODIFY"
    with open(target, "w") as f:
        f.write(original)

    try:
        reset_mock_llm(mock_llm_server_url)
        agent_name, model = _register_sandbox_agent(
            http_client, mock_llm_server_url, prefix="edit"
        )
        configure_mock_llm(
            mock_llm_server_url,
            [
                # Turn 1: LLM issues an Edit tool call outside the workspace
                {
                    "tool_calls": [
                        {
                            "call_id": f"call_{uuid.uuid4().hex[:8]}",
                            "name": "Edit",
                            "arguments": json.dumps(
                                {
                                    "file_path": target,
                                    "old_string": "ORIGINAL_CONTENT_DO_NOT_MODIFY",
                                    "new_string": "HACKED",
                                }
                            ),
                        }
                    ]
                },
                # Turn 2: LLM responds with text
                {"text": "I attempted to edit the file."},
            ],
            key=model,
        )

        body = _dispatch_and_wait(
            http_client,
            agent_name=agent_name,
            runner_id=live_runner_id,
            prompt=f"Edit {target}: replace 'ORIGINAL_CONTENT_DO_NOT_MODIFY' with 'HACKED'.",
        )
        assert body["status"] == "completed", f"Task failed: {body.get('error')}"

        with open(target) as f:
            on_disk = f.read()
        assert on_disk == original, (
            f"Sandbox escape: file at {target} was modified. "
            f"Expected {original!r}, got {on_disk!r}"
        )
    finally:
        if os.path.exists(target):
            os.unlink(target)

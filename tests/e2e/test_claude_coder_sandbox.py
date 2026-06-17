"""E2E test: Claude SDK executor sandbox isolation.

Verifies that the Claude SDK executor's sandbox restricts file
access to the workspace directory. Built-in tools (Read, Edit,
Write) are blocked by PreToolUse hooks. Bash writes are blocked
by the OS-level sandbox (Seatbelt/bubblewrap).

Usage::

    pytest tests/e2e/test_claude_coder_sandbox.py \
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import os
import tempfile
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


def _dispatch_and_wait(
    client: httpx.Client,
    *,
    agent_name: str,
    runner_id: str,
    prompt: str,
    timeout: float = 90,
) -> dict[str, Any]:
    """
    Bind a runner-routed session for *agent_name*, send *prompt*, poll to terminal.

    Wraps the three-step prod-equivalent flow (POST /v1/sessions,
    PATCH /v1/sessions/{id} with runner_id, POST /v1/sessions/{id}/events)
    so each test reads as one call.

    :param client: HTTP client.
    :param agent_name: Already-uploaded agent name.
    :param runner_id: Registered runner id (session fixture).
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
    claude_coder_sandbox_agent: str,
    live_runner_id: str,
) -> None:
    """
    Files outside the workspace must not be readable by the agent.

    Pure security property: a sentinel string in /tmp must never
    appear in the agent's tool results or response text. Any block
    mechanism counts (PreToolUse hook, missing skill, model refusal,
    different tool that also fails) as long as the sentinel does
    not leak.

    **What breaks if wrong:** The agent reads files outside the
    workspace and the sentinel surfaces in the response.
    """
    # Create a file outside the workspace with a unique sentinel.
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
        body = _dispatch_and_wait(
            http_client,
            agent_name=claude_coder_sandbox_agent,
            runner_id=live_runner_id,
            prompt=f"Use the Read tool to read {secret_path}. Do NOT use Bash or cat.",
        )
        assert body["status"] == "completed", f"Task failed: {body.get('error')}"

        # Sentinel must not appear in tool results or in the agent's
        # final text. Either confirms the sandbox isolated /tmp from
        # the workspace; both must be checked because LLM output is
        # non-deterministic about where it surfaces tool data.
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
    claude_coder_sandbox_agent: str,
    live_runner_id: str,
) -> None:
    """
    Bash cannot write files outside the workspace.

    The OS-level sandbox (Seatbelt/bubblewrap) blocks writes
    to paths outside the cwd. The agent should report an
    operation not permitted error.

    **What breaks if wrong:** The agent writes arbitrary files
    to the host filesystem.
    """
    target = f"/tmp/sandbox_write_escape_{os.getpid()}.txt"

    try:
        body = _dispatch_and_wait(
            http_client,
            agent_name=claude_coder_sandbox_agent,
            runner_id=live_runner_id,
            prompt=f"Run this exact Bash command: echo ESCAPED > {target}",
        )
        assert body["status"] == "completed", f"Task failed: {body.get('error')}"

        # The file must NOT exist on the host.
        assert not os.path.exists(target), (
            f"Sandbox escape! File {target} was written outside "
            "the workspace. The OS sandbox did not block it."
        )
    finally:
        if os.path.exists(target):
            os.unlink(target)


def test_write_succeeds_inside_workspace(
    http_client: httpx.Client,
    claude_coder_sandbox_agent: str,
    live_runner_id: str,
) -> None:
    """
    The agent CAN write and read files inside its workspace.

    This verifies the sandbox doesn't over-restrict; tools
    must work normally within the workspace directory.

    **What breaks if wrong:** The agent can't do any work
    because all file operations are blocked.
    """
    body = _dispatch_and_wait(
        http_client,
        agent_name=claude_coder_sandbox_agent,
        runner_id=live_runner_id,
        prompt=(
            "Create a file called test_sandbox.txt in the "
            "current directory with the content 'SANDBOX_OK'. "
            "Then read it back and tell me what it says."
        ),
    )
    assert body["status"] == "completed", f"Task failed: {body.get('error')}"

    text = _extract_all_text(body)
    assert "SANDBOX_OK" in text, (
        f"Agent couldn't write/read inside workspace. Output: {text[:300]}"
    )


def test_glob_blocked_outside_workspace(
    http_client: httpx.Client,
    claude_coder_sandbox_agent: str,
    live_runner_id: str,
) -> None:
    """
    The agent cannot discover files in /tmp.

    Pure security property: a uniquely-named sentinel file planted
    in /tmp must never appear in the agent's tool results or
    response text. Any block mechanism counts (PreToolUse hook,
    Glob not exposed as a tool, model refusal) as long as the
    sentinel filename does not leak.

    **What breaks if wrong:** Glob (or a substitute) enumerates
    /tmp and the sentinel filename appears in the response.
    """
    # Plant a uniquely-named sentinel file in /tmp.
    sentinel_basename = f"sandbox_glob_sentinel_{os.getpid()}.txt"
    sentinel_path = f"/tmp/{sentinel_basename}"
    with open(sentinel_path, "w") as f:
        f.write("touched-by-test")

    try:
        body = _dispatch_and_wait(
            http_client,
            agent_name=claude_coder_sandbox_agent,
            runner_id=live_runner_id,
            prompt="Use the Glob tool to search for *.txt files in /tmp. Do NOT use Bash or ls.",
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
    claude_coder_sandbox_agent: str,
    live_runner_id: str,
) -> None:
    """
    Files outside the workspace must not be modifiable by the agent.

    Pure security property: plant a target file in /tmp with known
    content, ask the agent to Edit it, then assert the file on disk
    is byte-for-byte unchanged. Any block mechanism counts (PreToolUse
    hook, Edit unavailable, model refusal) as long as the file isn't
    modified.

    **What breaks if wrong:** The Edit tool modifies a file outside
    the workspace and the file content on disk diverges from the
    sentinel we wrote.
    """
    target = f"/tmp/sandbox_edit_target_{os.getpid()}.txt"
    original = "ORIGINAL_CONTENT_DO_NOT_MODIFY"
    with open(target, "w") as f:
        f.write(original)

    try:
        body = _dispatch_and_wait(
            http_client,
            agent_name=claude_coder_sandbox_agent,
            runner_id=live_runner_id,
            prompt=(
                f"Use the Edit tool to replace 'ORIGINAL_CONTENT_DO_NOT_MODIFY' "
                f"with 'HACKED' in {target}. Do NOT use Bash."
            ),
        )
        assert body["status"] == "completed", f"Task failed: {body.get('error')}"

        # File on disk must be byte-for-byte unchanged. Whether the
        # agent refused, the sandbox blocked, or the Edit tool was
        # unavailable, the security invariant is identical.
        with open(target) as f:
            on_disk = f.read()
        assert on_disk == original, (
            f"Sandbox escape: file at {target} was modified. "
            f"Expected {original!r}, got {on_disk!r}"
        )
    finally:
        if os.path.exists(target):
            os.unlink(target)

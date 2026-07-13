"""E2E: os_env write-boundary enforcement via the ``worktree_guard`` policy.

The companion to ``test_claude_coder_sandbox.py``. That file checks the
claude-sdk native file tools, where the Claude Code CLI confines writes to
its cwd *silently* (no tool result is surfaced for a dropped out-of-workspace
write). This file checks the path real agents use for file output — the
``sys_os_write`` MCP builtin — gated by the ``worktree_guard`` policy on the
**openai-agents** harness, which DOES surface tool results.

``worktree_guard`` (omnigent/inner/nessie/policies.py) denies
``sys_os_write`` / ``sys_os_edit`` whose path is absolute or contains a ``..``
segment (an escape), and ALLOWS relative in-tree paths. Because the deny is a
TOOL_CALL-phase policy verdict, it surfaces as an error tool result even under
the default ``bypassPermissions`` permission mode (the policy half of the
``can_use_tool`` gate runs in every mode).

No API key needed — runs against the mock LLM server.

Usage::

    pytest tests/e2e/test_os_env_write_boundary_e2e.py -v --profile oss
"""

from __future__ import annotations

import json
import os
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

# An os_env (so ``sys_os_write`` is exposed) plus the worktree_guard policy
# confining writes to the workspace. ``sandbox: type: none`` keeps it
# host-portable; the boundary here is enforced by the policy, not the kernel
# sandbox. The factory-handler policy shape is ``type: function`` +
# ``handler:`` (dotted path) + ``factory_params:`` (→ kwargs); worktree_guard
# self-selects sys_os_write/sys_os_edit by tool name, so no ``on:`` clause is
# needed. ``allowed_root`` / ``deny_reason`` only shape the deny message.
_WORKTREE_GUARD_CONFIG: dict[str, Any] = {
    "os_env": {
        "type": "caller_process",
        "cwd": ".",
        "sandbox": {"type": "none"},
    },
    "policies": {
        "confine_writes_to_workspace": {
            "type": "function",
            "handler": "omnigent.inner.nessie.policies.worktree_guard",
            "factory_params": {
                "allowed_root": "workspace",
                "deny_reason": "Writes must stay inside the workspace.",
            },
        },
    },
}


def _extract_all_text(body: dict[str, Any]) -> str:
    """Concatenate all assistant output_text blocks from a terminal body.

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
    """Collect all function_call_output result strings from a terminal body.

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


def _register_guarded_agent(
    http_client: httpx.Client,
    mock_llm_server_url: str,
    *,
    prefix: str,
) -> tuple[str, str]:
    """Register an openai-agents agent with an os_env + worktree_guard policy.

    :param http_client: HTTP client pointed at the live server.
    :param mock_llm_server_url: Mock LLM server URL.
    :param prefix: Name/model prefix for uniqueness.
    :returns: Tuple of (agent_name, model_key).
    """
    model = f"mock-wtguard-{prefix}-{uuid.uuid4().hex[:6]}"
    name = f"wtguard-{prefix}-{uuid.uuid4().hex[:6]}"
    agent_name = register_inline_agent(
        http_client,
        name=name,
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a coding assistant. Use sys_os_write to create files exactly as instructed."
        ),
        # openai-agents: /v1-suffixed URL (the OpenAI SDK appends /responses).
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config=_WORKTREE_GUARD_CONFIG,
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
    """Bind a runner-routed session, send *prompt*, poll to terminal.

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


def test_sys_os_write_outside_workspace_denied(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """An out-of-workspace ``sys_os_write`` is denied AND the deny is surfaced.

    The mock LLM issues a ``sys_os_write`` to an absolute ``/tmp`` path. The
    ``worktree_guard`` policy denies it (absolute path escapes the workspace),
    so the file is never created and a deny error is surfaced as a tool result.

    **What breaks if wrong:** the agent writes arbitrary files to the host, or
    a workspace-escape is silently allowed with no signal to the caller.
    """
    target = f"/tmp/sandbox_write_escape_{os.getpid()}_{uuid.uuid4().hex[:6]}.txt"

    try:
        reset_mock_llm(mock_llm_server_url)
        agent_name, model = _register_guarded_agent(
            http_client, mock_llm_server_url, prefix="deny"
        )
        configure_mock_llm(
            mock_llm_server_url,
            [
                # Turn 1: sys_os_write to an absolute out-of-workspace path.
                {
                    "tool_calls": [
                        {
                            "call_id": f"call_{uuid.uuid4().hex[:8]}",
                            "name": "sys_os_write",
                            "arguments": json.dumps({"path": target, "content": "ESCAPED"}),
                        }
                    ]
                },
                # Turn 2: the LLM acknowledges after seeing the deny result.
                {"text": "I attempted to write the file."},
            ],
            key=model,
        )

        body = _dispatch_and_wait(
            http_client,
            agent_name=agent_name,
            runner_id=live_runner_id,
            prompt=f"Write 'ESCAPED' to {target} using sys_os_write.",
        )
        assert body["status"] == "completed", f"Task failed: {body.get('error')}"

        # Primary security property: the file must not exist on disk.
        assert not os.path.exists(target), (
            f"Workspace escape! File {target} was written outside the "
            "workspace — the worktree_guard policy did not deny the write."
        )

        # Surfaced-deny property: at least one tool result must carry the
        # policy deny (worktree_guard returns an error result containing the
        # deny reason + 'outside <root>/: <path>').
        tool_results = _collect_tool_results(body)
        assert any(
            "denied" in r.lower() or "outside" in r.lower() or "error" in r.lower()
            for r in tool_results
        ), (
            "Expected a surfaced policy-deny tool result for the "
            f"out-of-workspace sys_os_write. Tool results: {tool_results!r}. "
            f"Output: {body.get('output', [])}"
        )
    finally:
        if os.path.exists(target):
            os.unlink(target)


def test_sys_os_write_inside_workspace_allowed(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A relative, in-workspace ``sys_os_write`` is allowed (control case).

    Proves ``worktree_guard`` isn't a blanket deny: a relative path stays in
    the workspace, so the write succeeds and the tool result reports the
    bytes written. The file lands in the session's per-conversation workspace
    (an ephemeral tmpdir), so no repo cleanup is needed.

    **What breaks if wrong:** the policy over-blocks and the agent can't write
    anything, even inside its own workspace.
    """
    filename = f"notes_{uuid.uuid4().hex[:8]}.md"

    reset_mock_llm(mock_llm_server_url)
    agent_name, model = _register_guarded_agent(http_client, mock_llm_server_url, prefix="allow")
    configure_mock_llm(
        mock_llm_server_url,
        [
            # Turn 1: sys_os_write to a relative in-workspace path.
            {
                "tool_calls": [
                    {
                        "call_id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": "sys_os_write",
                        "arguments": json.dumps({"path": filename, "content": "INSIDE_OK"}),
                    }
                ]
            },
            # Turn 2: the LLM reports success.
            {"text": "Wrote the file inside the workspace."},
        ],
        key=model,
    )

    body = _dispatch_and_wait(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
        prompt=f"Write 'INSIDE_OK' to {filename} using sys_os_write.",
    )
    assert body["status"] == "completed", f"Task failed: {body.get('error')}"

    tool_results = _collect_tool_results(body)
    assert tool_results, f"sys_os_write produced no tool result. Output: {body.get('output', [])}"
    joined = " ".join(tool_results).lower()
    assert "denied" not in joined and "outside" not in joined, (
        f"In-workspace write was wrongly denied by the policy. Results: {tool_results!r}"
    )
    assert "bytes_written" in joined or "created" in joined, (
        f"Expected a successful write result (bytes_written / created). Results: {tool_results!r}"
    )

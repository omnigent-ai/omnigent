"""
E2E proof that a YAML ``tool_call`` DENY policy blocks a user
function tool end-to-end under the mock-LLM session-native path.

Drives the full openai-agents runner topology: an inline agent
declares a ``type: function`` tool (``calculate``, a dotted
callable) plus a ``type: function`` policy that DENYs the
``calculate`` tool at the ``tool_call`` phase. The mock LLM is
scripted to call ``calculate`` and then acknowledge the denial.

The bug this guards against: the runner's ``_spec_with_workdir_paths``
used to join the agent workdir onto every local tool's ``path`` —
including the dotted import path of a callable-backed tool. That
corrupted ``tests.x.calculate`` into ``<workdir>/tests.x.calculate``,
the import failed, the tool never registered, and the LLM's call hit
"Tool calculate not found" — so the TOOL_CALL policy never even ran
(no tool to deny). The fix leaves dotted callable paths untouched, the
tool registers, and the policy DENY surfaces as the tool output.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_dir_agent_with_mock_llm,
    reset_mock_llm,
    send_user_message_to_session,
)

# Fixture agent: the calculate tool ships as Python source under tools/python/
# (auto-discovered) and the tool_call DENY policy lives in its config.yaml, so
# both resolve from the uploaded bundle on any server version (no tests/ dep).
_TOOL_CALL_POLICY_DIR = (
    Path(__file__).resolve().parents[1] / "resources" / "agents" / "tool-call-policy"
)

# Unique reason string — chosen so its presence proves OUR policy fired, not an
# incidental denial from another path. Must match the `reason:` in
# tests/resources/agents/tool-call-policy/config.yaml.
_DENY_REASON = "TOOL_BAN_TEST_SENTINEL_XYZQ"


def _tool_outputs(body: dict[str, Any]) -> list[str]:
    """Pull every ``function_call_output`` payload from a response body."""
    return [
        item.get("output", "")
        for item in body.get("output", [])
        if item.get("type") == "function_call_output"
    ]


def _all_text(body: dict[str, Any]) -> str:
    """Concatenate every assistant message text block in a response body."""
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def test_tool_call_deny_blocks_callable_tool(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    A ``tool_call:calculate`` DENY policy intercepts the tool, returns
    the deny sentinel as the tool output, and the real callable never
    runs.

    Asserts:

    - The session completes (the deny path doesn't crash the turn).
    - The deny sentinel is the ``calculate`` tool output.
    - The real answer ``12`` never appears in any tool output (the
      callable was short-circuited, not executed).
    """
    model = f"mock-toolpolicy-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_dir_agent_with_mock_llm(
        http_client,
        agent_dir=_TOOL_CALL_POLICY_DIR,
        name=f"toolpolicy-{uuid.uuid4().hex[:6]}",
        model=model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_calc1",
                        "name": "calculate",
                        "arguments": '{"expression": "6 + 6"}',
                    },
                ],
            },
            {"text": "the calculation was denied"},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client, session_id=session_id, content="What is 6 + 6?"
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=60
    )

    assert body.get("status") == "completed", (
        f"turn did not complete cleanly: status={body.get('status')!r} error={body.get('error')!r}"
    )

    outs = _tool_outputs(body)
    assert any(_DENY_REASON in o for o in outs), (
        f"DENY sentinel missing from tool outputs — the tool_call policy "
        f"did not fire (tool may have failed to register).\n"
        f"tool outputs: {outs}\ntext: {_all_text(body)}"
    )
    assert not any("12" in o for o in outs), (
        f"the real calculate ran (answer '12' leaked) — the DENY did not "
        f"short-circuit dispatch.\ntool outputs: {outs}"
    )

"""E2E test for ``web_fetch`` built-in tool wiring.

Verifies that an agent with ``web_fetch`` configured can be registered
and a turn can be driven through the session. Uses the mock LLM to
script a deterministic response -- the focus is on the agent-registration
and turn-dispatch path, not on actually fetching live web content (which
requires real network and a real LLM).

Usage::

    pytest tests/e2e/test_web_fetch_e2e.py -v
"""

from __future__ import annotations

import json
import uuid

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text


def test_web_fetch_returns_live_content(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    An agent can call ``sys_os_shell`` to fetch web content and produce a response.

    The mock LLM is scripted to call ``sys_os_shell`` with a curl command
    and then emit a final text response. The test verifies the full chain
    works: agent -> tool call -> shell execution -> result -> text.

    :param http_client: HTTP client pointed at the live e2e server.
    :param live_runner_id: Runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    marker = "WEB_FETCH_E2E_OK"
    model = f"mock-webfetch-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)

    agent_name = register_inline_agent(
        http_client,
        name=f"web-fetch-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a research assistant. Use sys_os_shell to fetch web "
            "content when asked. Report the findings clearly."
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

    # Script the mock LLM:
    # Turn 1: call sys_os_shell with an echo command (simulating a curl)
    # Turn 2: after tool result, emit final text with marker
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_fetch",
                        "name": "sys_os_shell",
                        "arguments": json.dumps(
                            {"command": "echo 'mlflow/mlflow has 20500 stars'"}
                        ),
                    }
                ],
            },
            {
                "text": (
                    f"{marker} The mlflow/mlflow repository currently has "
                    "approximately 20,500 GitHub stars."
                ),
            },
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
            "Use sys_os_shell to find how many GitHub stars the "
            "mlflow/mlflow repository currently has. Report the "
            "exact number."
        ),
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=180
    )

    # "completed" means the chain worked: agent -> tool -> result -> text.
    assert body["status"] == "completed", (
        f"Response status is {body['status']!r}, expected 'completed'. "
        f"Output: {body.get('output', [])}"
    )

    full_text = final_assistant_text(body)
    assert marker in full_text, f"Marker {marker!r} missing from assistant text: {full_text!r}"
    assert len(full_text) > 20, f"Response too short ({len(full_text)} chars)."

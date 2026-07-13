"""End-to-end test for ``examples/agents/agent_with_subagent_session``.

The example demonstrates the ``sys_session_*`` builtin tool family:
``sys_session_send`` / ``sys_session_get_history`` /
``sys_session_cancel_turn`` / ``sys_read_inbox``. A supervisor
agent delegates work to a persistent worker sub-agent via a named
session.

**What breaks if this fails:**
- ``tools.<name>.type: agent`` translation regresses (sub-agent
  spec no longer converts into an :class:`AgentTool`).
- The ``sys_session_*`` builtin registrations drop from the
  effective tool set when an agent declares sub-agents.
- Session dispatch wiring in the runtime loses the ability to
  route messages to a named session.

The prompt asks the supervisor to start worker session alpha and
run a trivial calculation, so the session tools fire during the
turn — a reply that doesn't mention the worker would mean the
supervisor handled it directly, bypassing the feature under test.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.omnigent._example_helpers import (
    assert_completed_one_shot,
    run_one_shot,
)
from tests.e2e.omnigent.conftest import configure_mock_llm

_PROMPT = "Start worker session alpha and ask it to calculate 2 + 2."


def test_agent_with_subagent_session_one_shot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Run the subagent-session example one-shot and assert the run
    finishes cleanly. Sub-agent tool invocations land inside the
    captured stdout stream.

    Uses the mock LLM server for deterministic responses.

    :param omnigent_python: Interpreter with omnigent +
        openai-agents installed.
    :param omnigent_repo_root: Repo root for subprocess cwd.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    # The supervisor may make a tool call then get a follow-up
    # response. Provide several mock responses to cover multi-turn.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "I started worker session alpha. The result of 2 + 2 is 4."},
            {"text": "4"},
            {"text": "The answer is 4."},
        ],
    )
    result = run_one_shot(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        omnigent_credentials_env=mock_credentials_env,
        example_name="agent_with_subagent_session",
        prompt=_PROMPT,
        model="mock-model",
    )
    assert_completed_one_shot(result, "agent_with_subagent_session")

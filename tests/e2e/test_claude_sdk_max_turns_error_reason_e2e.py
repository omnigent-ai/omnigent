"""E2E regression: claude-sdk names the CLI's reason when an ``is_error`` result is empty.

When the Claude CLI ends a turn with ``ResultMessage(is_error=True)`` and no
``result`` text (``error_max_turns``, ``error_during_execution``, ...), the
executor must surface the structured reason the CLI reported rather than a
detail-free placeholder. The runner shows that string to the user as
``inner executor error: <reason>``, so a placeholder leaves nothing actionable.

This drives the real ``claude`` CLI through the real ``ClaudeSDKExecutor``
against the mock LLM server. With ``max_turns=1`` and a scripted tool call the
CLI runs the tool, hits its turn cap, and returns
``ResultMessage(subtype="error_max_turns", is_error=True, result=None)``
deterministically, with no mid-stream timing involved.

The empty-result ``is_error`` state is only reachable at the executor boundary:
the runner never forwards ``max_turns`` into ``ExecutorConfig`` and coalesces a
user cancel into a clean cancellation, so the web/CLI surfaces cannot drive it.

Usage::

    uv run --group test pytest tests/e2e/test_claude_sdk_max_turns_error_reason_e2e.py -v
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from shutil import which

import pytest

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.inner.executor import ExecutorConfig, ExecutorError, TurnComplete
from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

# The detail-free string the executor falls back to when it drops the CLI's reason.
_PLACEHOLDER = "claude-sdk harness error"


def _claude_sdk_available() -> bool:
    """True when both the ``claude_agent_sdk`` package and the ``claude`` CLI exist."""
    return importlib.util.find_spec("claude_agent_sdk") is not None and which("claude") is not None


@pytest.mark.skipif(
    not _claude_sdk_available(),
    reason="claude-sdk harness prerequisites missing (claude_agent_sdk package + claude CLI)",
)
async def test_claude_sdk_max_turns_error_result_surfaces_cli_reason(
    mock_llm_server_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token = f"max-turns-{uuid.uuid4().hex[:8]}"
    model = f"mock-max-turns-{uuid.uuid4().hex[:8]}"
    session_key = f"max-turns-error-reason-{uuid.uuid4().hex[:8]}"

    reset_mock_llm(mock_llm_server_url)
    # One scripted tool call per request: with max_turns=1 the CLI runs the tool
    # once, hits its turn cap, and returns an empty-result error_max_turns result.
    configure_mock_llm(
        mock_llm_server_url,
        [{"tool_calls": [{"name": "ToolSearch", "arguments": '{"query": "x"}'}]}] * 8,
        key=model,
        match=token,
    )

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (tmp_path / "claude-config").mkdir()

    monkeypatch.setenv("ANTHROPIC_BASE_URL", mock_llm_server_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "mock-key")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    # bwrap namespaces are unavailable in CI sandboxes; the ResultMessage handling
    # under test is identical with or without the CLI process sandbox.
    monkeypatch.setenv("OMNIGENT_CLAUDE_SDK_NO_SANDBOX", "1")

    executor = ClaudeSDKExecutor(
        model=model,
        cwd=str(cwd),
        api_key_helper="printf %s mock-key",
    )
    config = ExecutorConfig(model=model, extra={"max_turns": 1})
    messages = [
        {
            "role": "user",
            "content": f"Please use the ToolSearch tool. {token}",
            "session_id": session_key,
        }
    ]

    error: str | None = None
    completed = False
    try:
        async for event in executor.run_turn(
            messages=messages,
            tools=[],
            system_prompt="You are a terse assistant.",
            config=config,
        ):
            if isinstance(event, ExecutorError):
                error = event.message
            elif isinstance(event, TurnComplete):
                completed = True
    finally:
        await executor.close_session(session_key)

    assert error is not None, (
        "expected the max_turns=1 turn to surface an ExecutorError; "
        f"turn completed instead: {completed}"
    )
    assert error != _PLACEHOLDER, (
        "claude-sdk surfaced the detail-free placeholder instead of the CLI's "
        f"is_error reason (subtype=error_max_turns): {error!r}"
    )
    assert "error_max_turns" in error or "maximum number of turns" in error.lower(), (
        f"surfaced harness error lacks the CLI's max-turns reason: {error!r}"
    )

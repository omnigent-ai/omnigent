from __future__ import annotations

import pytest

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.executor import ExecutorConfig, ExecutorError
from omnigent.inner.openai_agents_sdk_executor import OpenAIAgentsSDKExecutor
from omnigent.llms.adapters.anthropic import _effort_to_budget
from omnigent.llms.errors import PermanentLLMError


@pytest.mark.parametrize("effort", ["none", "minimal"])
def test_anthropic_effort_rejects_openai_only_values(effort: str) -> None:
    with pytest.raises(PermanentLLMError, match="not supported by Anthropic"):
        _effort_to_budget(effort, 10000)


@pytest.mark.asyncio
async def test_claude_sdk_rejects_none_before_sdk_call() -> None:
    executor = ClaudeSDKExecutor(gateway=False)
    events = [
        e
        async for e in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(extra={"reasoning_effort": "none"}),
        )
    ]
    assert any(
        isinstance(e, ExecutorError) and "not supported by Claude" in e.message for e in events
    )


@pytest.mark.asyncio
async def test_codex_rejects_max_without_cli() -> None:
    executor = CodexExecutor(codex_path="/bin/echo")
    events = [
        e
        async for e in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(extra={"reasoning_effort": "max"}),
        )
    ]
    assert any(
        isinstance(e, ExecutorError) and "not supported by codex" in e.message for e in events
    )


@pytest.mark.asyncio
async def test_openai_agents_rejects_max_without_sdk_call(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    fake_agents = types.SimpleNamespace()
    monkeypatch.setattr(
        "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk", lambda: fake_agents
    )
    executor = OpenAIAgentsSDKExecutor(client=object())
    events = [
        e
        async for e in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(extra={"reasoning_effort": "max"}),
        )
    ]
    assert any(
        isinstance(e, ExecutorError) and "not supported by OpenAI Agents SDK" in e.message
        for e in events
    )

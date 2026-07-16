from __future__ import annotations

import pytest

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.executor import ExecutorConfig, ExecutorError, TurnComplete
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
async def test_codex_coerces_max_to_xhigh() -> None:
    """The deprecated ``max`` alias is coerced to ``xhigh`` for codex.

    codex's ladder tops out at ``xhigh`` (no ``max``); ``EFFORT_ALIASES``
    coerces the deprecated value rather than rejecting it. The app-session
    factory is stubbed so the turn never spawns a real codex process — it
    only needs to record the ``reasoning_effort`` it was started with.
    """
    calls: list[dict] = []

    class _FakeAppSession:
        async def run_turn(self, **kwargs):
            calls.append(kwargs)
            yield TurnComplete(response="done")

        async def close(self) -> None:
            return None

    executor = CodexExecutor(
        codex_path="/bin/echo",
        app_session_factory=lambda **kwargs: _FakeAppSession(),
    )
    events = [
        e
        async for e in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(extra={"reasoning_effort": "max"}),
        )
    ]
    assert not any(isinstance(e, ExecutorError) for e in events)
    assert calls[0]["reasoning_effort"] == "xhigh"


@pytest.mark.asyncio
async def test_openai_agents_coerces_max_to_xhigh(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deprecated ``max`` alias is coerced to ``xhigh`` for the OpenAI Agents SDK.

    Its ladder tops out at ``xhigh`` (no ``max``); ``EFFORT_ALIASES`` coerces
    the deprecated value rather than rejecting it. The session state is
    stubbed to a fresh, un-started ``_AgentsSessionState`` (skipping the real
    ``SQLiteSession``) and ``_get_or_create_agent`` — the first call that
    consumes the resolved effort — is stubbed to capture its
    ``reasoning_effort`` kwarg and raise a sentinel exception, so the turn
    never reaches the real SDK/network call.
    """
    import types

    from omnigent.inner.openai_agents_sdk_executor import _AgentsSessionState

    fake_agents = types.SimpleNamespace(OpenAIProvider=lambda **kwargs: types.SimpleNamespace())
    monkeypatch.setattr(
        "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk", lambda: fake_agents
    )
    executor = OpenAIAgentsSDKExecutor(client=object())
    monkeypatch.setattr(
        executor,
        "_get_or_create_session_state",
        lambda agents_sdk, session_key: _AgentsSessionState(sdk_session=None),
    )

    captured: dict[str, object] = {}

    class _StopTurn(Exception):
        pass

    def _fake_get_or_create_agent(self, agents_sdk, state, **kwargs):
        captured["reasoning_effort"] = kwargs.get("reasoning_effort")
        raise _StopTurn()

    monkeypatch.setattr(OpenAIAgentsSDKExecutor, "_get_or_create_agent", _fake_get_or_create_agent)

    with pytest.raises(_StopTurn):
        async for _ in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(extra={"reasoning_effort": "max"}),
        ):
            pass

    assert captured["reasoning_effort"] == "xhigh"

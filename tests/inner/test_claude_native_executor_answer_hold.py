"""
A Claude question or permission prompt holds a web message instead of failing it.

When the prompt refuses the message before anything was typed, the turn waits
for the person to answer it and then delivers the message. A refusal after
typing began, or a hold that runs out, still fails as before.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import ClaudeUserPromptPending
from omnigent.inner import claude_native_executor
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete

_REFUSAL = "Answer the pending Claude question or permission request before sending a message."


class _Pane:
    """
    Stand-in for the bridge: a pane that holds a prompt for some polls.

    :param refusals: ``nothing_typed`` flags of the refusals the first deliveries raise.
    :param pending_polls: How many ``has_pending_user_prompt`` polls still see the prompt.
    """

    def __init__(self, refusals: list[bool], pending_polls: int) -> None:
        self.refusals = list(refusals)
        self.pending_polls = pending_polls
        self.delivered: list[str] = []
        self.deliveries = 0
        self.polls = 0
        self.kills = 0

    def inject_user_message(
        self, bridge_dir: Path, *, content: str, timeout_s: float = 30.0
    ) -> None:
        del bridge_dir, timeout_s
        self.deliveries += 1
        if self.refusals:
            refusal = ClaudeUserPromptPending(_REFUSAL)
            refusal.nothing_typed = self.refusals.pop(0)
            raise refusal
        self.delivered.append(content)

    def has_pending_user_prompt(self, bridge_dir: Path) -> bool:
        del bridge_dir
        self.polls += 1
        if self.pending_polls > 0:
            self.pending_polls -= 1
            return True
        return False

    def kill_session(self, bridge_dir: Path, *, timeout_s: float) -> None:
        del bridge_dir, timeout_s
        self.kills += 1


@pytest.fixture
def pane(monkeypatch: pytest.MonkeyPatch) -> _Pane:
    fake = _Pane(refusals=[True], pending_polls=2)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake.inject_user_message)
    # raising=False: main has neither, so these tests fail there on behaviour.
    monkeypatch.setattr(
        claude_native_executor,
        "has_pending_user_prompt",
        fake.has_pending_user_prompt,
        raising=False,
    )
    monkeypatch.setattr(claude_native_executor, "kill_session", fake.kill_session)
    monkeypatch.setattr(claude_native_executor, "_ANSWER_POLL_S", 0.0, raising=False)
    return fake


async def _run(tmp_path: Path, executor: ClaudeNativeExecutor | None = None) -> list[object]:
    executor = executor or ClaudeNativeExecutor(tmp_path / "bridge")
    return [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello from the web"}],
            tools=[],
            system_prompt="",
        )
    ]


@pytest.mark.asyncio
async def test_a_message_refused_before_typing_waits_for_the_answer(
    pane: _Pane, tmp_path: Path
) -> None:
    """
    The turn waits out the prompt, then delivers the same message once.

    On main the refusal failed the turn at once with "Answer the pending Claude
    question or permission request before sending a message."
    """
    events = await _run(tmp_path)

    assert events == [TurnComplete(response=None)]
    assert pane.delivered == ["hello from the web"]
    assert pane.deliveries == 2
    assert pane.polls == 3, "The hold ends on the first poll that sees no prompt."
    assert pane.kills == 0, "A held message must not reap the pane."


@pytest.mark.asyncio
async def test_repeated_prompts_share_one_hold(pane: _Pane, tmp_path: Path) -> None:
    """A second prompt after the first answer is waited out too, then the message goes."""
    pane.refusals = [True, True]

    events = await _run(tmp_path)

    assert events == [TurnComplete(response=None)]
    assert pane.delivered == ["hello from the web"]
    assert pane.deliveries == 3


@pytest.mark.asyncio
async def test_a_refusal_after_typing_began_is_reported_at_once(
    pane: _Pane, tmp_path: Path
) -> None:
    """A prompt that appeared once keystrokes went out may own the message, so no retry."""
    pane.refusals = [False]

    events = await _run(tmp_path)

    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert events[0].message == _REFUSAL
    assert events[0].undelivered is True
    assert pane.deliveries == 1
    assert pane.polls == 0


@pytest.mark.asyncio
async def test_the_hold_ends_with_the_refusal_when_nobody_answers(
    pane: _Pane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the hold the turn fails exactly as it did without one."""
    monkeypatch.setattr(claude_native_executor, "_ANSWER_HOLD_S", 0.05, raising=False)
    monkeypatch.setattr(claude_native_executor, "_ANSWER_POLL_S", 0.01, raising=False)
    pane.pending_polls = 10**6

    events = await _run(tmp_path)

    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert events[0].message == _REFUSAL
    assert events[0].undelivered is True
    assert pane.deliveries == 1
    assert pane.delivered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "clear_pending", "clear_answered"])
async def test_interrupting_a_held_turn_keeps_the_pane_and_input(
    pane: _Pane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    """Stopping or changing the conversation ends old work without touching the pane."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    state = bridge_dir / "bridge.json"
    state.write_text('{"active_session_id": "session-original"}')
    monkeypatch.setenv(claude_native_executor.REQUEST_SESSION_ID_ENV_VAR, "session-original")
    monkeypatch.setattr(claude_native_executor, "_ANSWER_POLL_S", 0.01, raising=False)
    pane.pending_polls = 10**6
    turn = asyncio.create_task(_run(tmp_path))
    for _ in range(200):
        if pane.polls >= 3 or turn.done():
            break
        await asyncio.sleep(0.01)
    assert not turn.done(), "The turn must still be waiting for the answer."

    try:
        if interruption == "cancel":
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn
        else:
            state.write_text('{"active_session_id": "session-replacement"}')
            if interruption == "clear_answered":
                pane.pending_polls = 0
            events = await asyncio.wait_for(turn, 1)
            assert len(events) == 1 and isinstance(events[0], ExecutorError)
            assert events[0].undelivered is True
            assert "no longer active after /clear" in events[0].message
    finally:
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)

    assert pane.kills == 0
    assert pane.deliveries == 1
    assert pane.delivered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["message", "steering", "model"])
async def test_conversation_changes_while_input_waits_for_the_injection_lock(
    pane: _Pane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delivery: str
) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    state = bridge_dir / "bridge.json"
    state.write_text('{"active_session_id": "session-original"}')
    monkeypatch.setenv(claude_native_executor.REQUEST_SESSION_ID_ENV_VAR, "session-original")
    executor = ClaudeNativeExecutor(bridge_dir)
    monkeypatch.setattr(
        executor, "_model_command_arg", lambda _model: "sonnet" if delivery == "model" else None
    )
    model_commands: list[str] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_slash_command",
        lambda _bridge, *, command, **_kwargs: model_commands.append(command),
    )
    checked = asyncio.Event()
    original_check = claude_native_executor._session_is_active

    def check(*args) -> bool:
        result = original_check(*args)
        checked.set()
        return result

    monkeypatch.setattr(claude_native_executor, "_session_is_active", check)
    pane.refusals = []
    async with executor._inject_lock:
        task = asyncio.create_task(
            executor.enqueue_session_message("main", "old steering")
            if delivery == "steering"
            else _run(tmp_path, executor)
        )
        await asyncio.wait_for(checked.wait(), 1)
        assert not task.done()
        state.write_text('{"active_session_id": "session-replacement"}')
        context = bridge_dir / claude_native_executor.CLAUDE_FRAMEWORK_CONTEXT_FILE
        context.write_text("replacement context")
    result = await asyncio.wait_for(task, 1)

    if delivery == "steering":
        assert result is False
    else:
        assert len(result) == 1 and isinstance(result[0], ExecutorError)
        assert result[0].undelivered is True
    assert pane.deliveries == 0
    assert pane.delivered == []
    assert model_commands == []
    assert context.read_text() == "replacement context"
    assert pane.kills == 0

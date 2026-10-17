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


async def _run(tmp_path: Path) -> list[object]:
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
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
async def test_stopping_a_held_turn_keeps_the_pane(
    pane: _Pane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop during the hold cancels the wait; it neither types nor reaps the pane."""
    monkeypatch.setattr(claude_native_executor, "_ANSWER_POLL_S", 0.01, raising=False)
    pane.pending_polls = 10**6
    turn = asyncio.create_task(_run(tmp_path))
    for _ in range(200):
        if pane.polls >= 3 or turn.done():
            break
        await asyncio.sleep(0.01)
    assert not turn.done(), "The turn must still be waiting for the answer."

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert pane.kills == 0
    assert pane.deliveries == 1
    assert pane.delivered == []

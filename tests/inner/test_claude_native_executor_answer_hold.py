"""
A refusal before a web message was typed holds the message instead of failing it.

Questions, permission prompts and answerable terminal dialogs hold the message
until the person answers them. An uncleared input box waits until clear; shell
mode and history search wait until chat input is available. Refusals after typing
and expired holds still fail as before.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import (
    ClaudeComposerNotCleared,
    ClaudeInputBoxOccupied,
    ClaudeTerminalDialog,
    ClaudeUserPromptPending,
)
from omnigent.inner import claude_native_executor
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete

_REFUSAL = "Answer the pending Claude question or permission request before sending a message."
_NOT_CLEARED = (
    "Claude Code's input box could not be verified clear, so nothing was sent. "
    "Open the terminal, expand it and clear the input box, then retry."
)
_DIALOG = "Claude Code is waiting for an answer in its terminal (Settings Error)"
_INSTALL_QUESTION = (
    "Claude Code's launch command is waiting for installation confirmation in its "
    "terminal, so the message was not delivered. Open the terminal, answer the install "
    "prompt, then resend your message."
)
_SHELL_MODE = (
    "Claude Code's input box is occupied by shell mode, so the message was not "
    "delivered. Open the terminal, dismiss it, then resend your message."
)


class _Pane:
    """
    Stand-in for the bridge: a pane that refuses the first deliveries for some polls.

    :param refusals: The refusals the first deliveries raise: a ``bool`` is the
        ``nothing_typed`` flag of a pending-prompt refusal, an exception is raised as is.
    :param pending_polls: How many polls still see the prompt, or an input box
        that does not read clear.
    """

    def __init__(self, refusals: list[bool | Exception], pending_polls: int) -> None:
        self.refusals = list(refusals)
        self.pending_polls = pending_polls
        self.dialog_polls = 0
        self.delivered: list[str] = []
        self.deliveries = 0
        self.polls = 0
        self.repaints = 0
        self.kills = 0

    def inject_user_message(
        self, bridge_dir: Path, *, content: str, timeout_s: float = 30.0
    ) -> None:
        del bridge_dir, timeout_s
        self.deliveries += 1
        if self.refusals:
            refusal = self.refusals.pop(0)
            if isinstance(refusal, Exception):
                raise refusal
            prompt = ClaudeUserPromptPending(_REFUSAL)
            prompt.nothing_typed = refusal
            raise prompt
        self.delivered.append(content)

    def has_pending_user_prompt(self, bridge_dir: Path) -> bool:
        del bridge_dir
        return self._still_refusing()

    def input_box_reads_clear(self, bridge_dir: Path, *, repaint: bool = False) -> bool:
        del bridge_dir
        self.repaints += repaint
        return not self._still_refusing()

    def terminal_dialog_pending(self, bridge_dir: Path) -> bool:
        del bridge_dir
        if self.dialog_polls > 0:
            self.dialog_polls -= 1
            return True
        return False

    def claude_pane_ready(self, bridge_dir: Path) -> bool:
        del bridge_dir
        return not self._still_refusing()

    def _still_refusing(self) -> bool:
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
    monkeypatch.setattr(
        claude_native_executor,
        "input_box_reads_clear",
        fake.input_box_reads_clear,
        raising=False,
    )
    monkeypatch.setattr(
        claude_native_executor, "claude_pane_ready", fake.claude_pane_ready, raising=False
    )
    monkeypatch.setattr(
        claude_native_executor, "terminal_dialog_pending", fake.terminal_dialog_pending
    )
    monkeypatch.setattr(claude_native_executor, "kill_session", fake.kill_session)
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_POLL_S", 0.0, raising=False)
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
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_S", 0.05, raising=False)
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_POLL_S", 0.01, raising=False)
    pane.pending_polls = 10**6

    events = await _run(tmp_path)

    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert events[0].message == _REFUSAL
    assert events[0].undelivered is True
    assert pane.deliveries == 1
    assert pane.delivered == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refusal_kind", "interruption"),
    [("prompt", "cancel")]
    + [
        (kind, interruption)
        for kind in ("prompt", "input_box")
        for interruption in ("clear_pending", "clear_answered")
    ],
)
async def test_interrupting_a_held_turn_keeps_the_pane_and_input(
    pane: _Pane,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
    refusal_kind: str,
) -> None:
    """Stopping or changing the conversation ends old work without touching the pane."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    state = bridge_dir / "bridge.json"
    state.write_text('{"active_session_id": "session-original"}')
    monkeypatch.setenv(claude_native_executor.REQUEST_SESSION_ID_ENV_VAR, "session-original")
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_POLL_S", 0.01, raising=False)
    if refusal_kind == "input_box":
        pane.refusals = [ClaudeComposerNotCleared(_NOT_CLEARED)]
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
async def test_a_message_the_input_box_refused_waits_for_it_to_read_clear(
    pane: _Pane, tmp_path: Path
) -> None:
    """
    The turn holds the message until the input box reads clear, then delivers it once.

    On main the refusal failed the turn at once with "Claude Code's input box could
    not be verified clear, so nothing was sent."
    """
    pane.refusals = [ClaudeComposerNotCleared(_NOT_CLEARED)]

    events = await _run(tmp_path)

    assert events == [TurnComplete(response=None)]
    assert pane.delivered == ["hello from the web"]
    assert pane.deliveries == 2
    assert pane.polls == 3, "The hold ends on the first poll that reads the box clear."
    assert pane.repaints == 1, "The first poll asks for a repaint; the next is an interval away."
    assert pane.kills == 0, "A held message must not reap the pane."


@pytest.mark.asyncio
async def test_a_held_message_asks_for_a_repaint_each_interval(
    pane: _Pane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        claude_native_executor, "_INPUT_BOX_REPAINT_INTERVAL_S", 0.0, raising=False
    )
    pane.refusals = [ClaudeComposerNotCleared(_NOT_CLEARED)]

    await _run(tmp_path)

    assert pane.repaints == pane.polls == 3


@pytest.mark.asyncio
async def test_an_input_box_and_a_prompt_share_one_hold(pane: _Pane, tmp_path: Path) -> None:
    """A prompt that opens once the box reads clear is waited out too, then the message goes."""
    pane.refusals = [ClaudeComposerNotCleared(_NOT_CLEARED), True]

    events = await _run(tmp_path)

    assert events == [TurnComplete(response=None)]
    assert pane.delivered == ["hello from the web"]
    assert pane.deliveries == 3


@pytest.mark.asyncio
async def test_an_input_box_that_never_reads_clear_fails_with_the_refusal(
    pane: _Pane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the hold the turn fails exactly as it did without one."""
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_S", 0.05, raising=False)
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_POLL_S", 0.01, raising=False)
    pane.refusals = [ClaudeComposerNotCleared(_NOT_CLEARED)]
    pane.pending_polls = 10**6

    events = await _run(tmp_path)

    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert events[0].message == _NOT_CLEARED
    assert events[0].undelivered is True
    assert pane.deliveries == 1
    assert pane.delivered == []


@pytest.mark.asyncio
async def test_a_message_sent_during_the_hold_queues_behind_the_held_one(
    pane: _Pane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A live message sent during the hold is refused back to the runner, typing nothing.

    The runner delivers a refused live message as a later turn, so it cannot reach
    Claude Code ahead of the held one.
    """
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_POLL_S", 0.01, raising=False)
    pane.refusals = [ClaudeComposerNotCleared(_NOT_CLEARED)]
    pane.pending_polls = 10**6
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    turn = asyncio.create_task(_run(tmp_path, executor))
    for _ in range(200):
        if pane.polls >= 2 or turn.done():
            break
        await asyncio.sleep(0.01)
    assert not turn.done(), "The turn must still be holding its message."

    assert await executor.enqueue_session_message("session", "a later message") is False
    assert pane.deliveries == 1

    pane.pending_polls = 0
    assert await turn == [TurnComplete(response=None)]
    assert await executor.enqueue_session_message("session", "a later message") is True
    assert pane.delivered == ["hello from the web", "a later message"]


@pytest.mark.asyncio
async def test_stopping_a_turn_held_for_the_input_box_keeps_the_pane(
    pane: _Pane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop during the hold cancels the wait; it neither types nor reaps the pane."""
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_POLL_S", 0.01, raising=False)
    pane.refusals = [ClaudeComposerNotCleared(_NOT_CLEARED)]
    pane.pending_polls = 10**6
    turn = asyncio.create_task(_run(tmp_path))
    for _ in range(200):
        if pane.polls >= 3 or turn.done():
            break
        await asyncio.sleep(0.01)
    assert not turn.done(), "The turn must still be holding its message."

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert pane.kills == 0
    assert pane.deliveries == 1
    assert pane.delivered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["shell mode", "the prompt-history search"])
async def test_a_message_an_occupied_input_box_refused_waits_for_chat_input(
    pane: _Pane, tmp_path: Path, surface: str
) -> None:
    """
    The turn holds the message until the box takes chat input again, then delivers it once.

    Shell mode or the prompt-history search owns the box when the message arrives;
    nothing was typed, so the refusal is a wait rather than a failed turn.
    """
    pane.refusals = [ClaudeInputBoxOccupied(_SHELL_MODE, surface=surface)]

    events = await _run(tmp_path)

    assert events == [TurnComplete(response=None)]
    assert pane.delivered == ["hello from the web"]
    assert pane.deliveries == 2
    assert pane.polls == 3, "The hold ends on the first poll that finds chat input."
    assert pane.repaints == 0, "Leaving the mode is the person's step; no repaint is asked."
    assert pane.kills == 0, "A held message must not reap the pane."


@pytest.mark.asyncio
async def test_an_occupied_input_box_and_a_prompt_share_one_hold(
    pane: _Pane, tmp_path: Path
) -> None:
    """A prompt that opens once the box leaves shell mode is waited out too, then it goes."""
    pane.refusals = [ClaudeInputBoxOccupied(_SHELL_MODE, surface="shell mode"), True]

    events = await _run(tmp_path)

    assert events == [TurnComplete(response=None)]
    assert pane.delivered == ["hello from the web"]
    assert pane.deliveries == 3


@pytest.mark.asyncio
async def test_an_input_box_kept_in_shell_mode_fails_with_the_refusal(
    pane: _Pane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the hold the turn fails exactly as it did without one."""
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_S", 0.05, raising=False)
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_POLL_S", 0.01, raising=False)
    pane.refusals = [ClaudeInputBoxOccupied(_SHELL_MODE, surface="shell mode")]
    pane.pending_polls = 10**6

    events = await _run(tmp_path)

    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert events[0].message == _SHELL_MODE
    assert events[0].undelivered is True
    assert pane.deliveries == 1
    assert pane.delivered == []
    assert pane.kills == 0


@pytest.mark.asyncio
async def test_a_terminal_dialog_holds_the_message_until_it_closes(
    pane: _Pane, tmp_path: Path
) -> None:
    """A dialog only the terminal shows, such as a settings error, holds the message."""
    pane.refusals = [ClaudeTerminalDialog(_DIALOG, awaits_answer=True)]
    pane.pending_polls = 0
    pane.dialog_polls = 2

    events = await _run(tmp_path)

    assert events == [TurnComplete(response=None)]
    assert pane.delivered == ["hello from the web"]
    assert pane.deliveries == 2
    assert pane.kills == 0, "A held message must not reap the pane."


@pytest.mark.asyncio
async def test_a_launcher_install_question_holds_the_message_until_it_is_answered(
    pane: _Pane, tmp_path: Path
) -> None:
    """
    The launcher's install question, printed before Claude Code runs, holds the message.

    On main the refusal failed the turn at once; the person then had to answer in
    the terminal and resend.
    """
    pane.refusals = [ClaudeTerminalDialog(_INSTALL_QUESTION, awaits_answer=True)]
    pane.pending_polls = 0
    pane.dialog_polls = 2

    events = await _run(tmp_path)

    assert events == [TurnComplete(response=None)]
    assert pane.delivered == ["hello from the web"]
    assert pane.deliveries == 2
    assert pane.kills == 0, "A held message must not reap the pane."


@pytest.mark.asyncio
async def test_a_terminal_dialog_that_awaits_no_answer_is_reported_at_once(
    pane: _Pane, tmp_path: Path
) -> None:
    """A surface refusal that names no answer for the person is not held."""
    pane.refusals = [ClaudeTerminalDialog(_DIALOG)]

    events = await _run(tmp_path)

    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert events[0].message.startswith(_DIALOG)
    assert events[0].undelivered is True
    assert pane.deliveries == 1
    assert pane.polls == 0


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

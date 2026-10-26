"""
Messages sent while Claude is busy reach the model in the order they were sent.

Claude Code folds queued messages into the running turn at its next tool call,
except a message starting with ``/``, which waits for the turn's end. The
executor holds later messages until Claude has taken that one.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

import pytest

from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.inner import claude_native_executor
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError, ExecutorEvent, TurnComplete

_SESSION = "11111111-2222-4333-8444-555555555555"
_SLASH_LED = "/home/me/notes.txt summarize these notes"
_SECOND_SLASH_LED = "/home/me/todo.txt add these to the list"
_Result = TypeVar("_Result")


class _ClaudeCode:
    """
    Claude Code's prompt queue behind the bridge, logged to its transcript.

    :param transcript: Native transcript the queue log is appended to.
    """

    def __init__(self, transcript: Path) -> None:
        self.transcript = transcript
        self.busy = False
        self.alive = True
        self.queue: list[str] = []
        self.typed: list[str] = []
        self.model_saw: list[str] = []
        self.replays = 0
        self.kills = 0
        self._lock = threading.Lock()

    def inject_user_message(
        self, bridge_dir: Path, *, content: str, timeout_s: float = 30.0
    ) -> None:
        del bridge_dir, timeout_s
        with self._lock:
            self.typed.append(content)
        self.submit(content)

    def submit(self, content: str) -> None:
        """Submit *content* in the pane: queued while a turn runs, else a new turn."""
        with self._lock:
            if self.busy:
                self.queue.append(content)
                self._log("enqueue", content=content)
            else:
                self.busy = True
                self.model_saw.append(content)

    def tool_call(self) -> None:
        """Fold every queued message that does not start with ``/`` into the turn."""
        with self._lock:
            for prompt in [p for p in self.queue if not p.strip().startswith("/")]:
                self.queue.remove(prompt)
                self._log("remove", content=prompt, reason="absorbed_mid_turn")
                self.model_saw.append(prompt)

    def end_turn(self, take: str | None = None) -> None:
        """Start the next turn with *take*, by default the oldest queued message, or go idle."""
        with self._lock:
            if self.queue:
                taken = take if take is not None else self.queue[0]
                self.queue.remove(taken)
                self.model_saw.append(taken)
                self._log("dequeue")
            else:
                self.busy = False

    def pane_state(self, socket_path: str, tmux_target: str) -> claude_bridge._ClaudePaneState:
        del socket_path, tmux_target
        self.replays += 1
        return claude_bridge._ClaudePaneState(
            alive=self.alive, exited=not self.alive, exit_status=None if self.alive else "0"
        )

    def kill_session(self, bridge_dir: Path, *, timeout_s: float) -> None:
        del bridge_dir, timeout_s
        self.kills += 1

    def _log(self, operation: str, **fields: str) -> None:
        record = {
            "type": "queue-operation",
            "operation": operation,
            "timestamp": "2026-10-26T12:00:00.000Z",
            "sessionId": _SESSION,
            **fields,
        }
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _ClaudeCode:
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path)
    transcript = tmp_path / "native.jsonl"
    transcript.write_text('{"type":"user","sessionId":"' + _SESSION + '"}\n', encoding="utf-8")
    bridge_dir = tmp_path / "bridge"
    claude_bridge.record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": _SESSION,
            "transcript_path": str(transcript),
        },
    )
    claude_bridge.write_tmux_target(
        bridge_dir, socket_path=tmp_path / "tmux.sock", tmux_target="claude:0.0"
    )
    fake = _ClaudeCode(transcript)
    monkeypatch.setattr(claude_bridge, "_claude_pane_state", fake.pane_state)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake.inject_user_message)
    monkeypatch.setattr(claude_native_executor, "kill_session", fake.kill_session)
    monkeypatch.setattr(claude_native_executor, "_ANSWER_POLL_S", 0.01)
    return fake


Send = Callable[[ClaudeNativeExecutor, str], Awaitable[None]]


async def _steer(executor: ClaudeNativeExecutor, text: str) -> None:
    assert await executor.enqueue_session_message("session", text) is True


async def _turn_events(executor: ClaudeNativeExecutor, text: str) -> list[ExecutorEvent]:
    return [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": text}], tools=[], system_prompt=""
        )
    ]


async def _turn(executor: ClaudeNativeExecutor, text: str) -> None:
    assert await _turn_events(executor, text) == [TurnComplete(response=None)]


async def _settle(claude: _ClaudeCode, task: asyncio.Future[_Result]) -> None:
    """Return once *task* finished or the executor read Claude's queue a few more times."""
    replays = claude.replays
    for _ in range(200):
        if task.done() or claude.replays >= replays + 3:
            return
        await asyncio.sleep(0.01)


async def _held_send(claude: _ClaudeCode, send: Awaitable[_Result]) -> asyncio.Task[_Result]:
    """Start *send* and return once it is done or the queue was read a few times."""
    task = asyncio.ensure_future(send)
    await _settle(claude, task)
    return task


@pytest.fixture(params=[_steer, _turn], ids=["steer", "queued-turn"])
def send(request: pytest.FixtureRequest) -> Send:
    return request.param


@pytest.mark.asyncio
async def test_a_message_waits_for_the_queued_slash_led_message(
    claude: _ClaudeCode, send: Send, tmp_path: Path
) -> None:
    """The model sees a ``/``-led message and the two sent after it in send order."""
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    claude.busy = True
    await send(executor, _SLASH_LED)

    bravo = await _held_send(claude, send(executor, "bravo"))
    claude.tool_call()
    claude.end_turn()
    await asyncio.wait_for(bravo, timeout=5)
    await send(executor, "charlie")
    claude.tool_call()

    assert claude.model_saw == [_SLASH_LED, "bravo", "charlie"]
    assert claude.kills == 0


@pytest.mark.asyncio
async def test_a_message_waits_while_claude_may_still_hold_the_slash_led_message(
    claude: _ClaudeCode, send: Send, tmp_path: Path
) -> None:
    """Claude can take a later message first, and its log does not say which one it took."""
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    claude.busy = True
    await send(executor, _SLASH_LED)
    claude.submit("typed into the pane")

    bravo = await _held_send(claude, send(executor, "bravo"))
    claude.end_turn(take="typed into the pane")
    await _settle(claude, bravo)
    claude.tool_call()
    claude.end_turn()
    await asyncio.wait_for(bravo, timeout=5)
    claude.tool_call()

    assert claude.model_saw == ["typed into the pane", _SLASH_LED, "bravo"]


@pytest.mark.asyncio
async def test_plain_messages_are_not_held(
    claude: _ClaudeCode, send: Send, tmp_path: Path
) -> None:
    """Messages without a ``/`` lead are typed at once, so Claude folds them in order."""
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    claude.busy = True
    for text in ("alpha", "bravo", "charlie"):
        await asyncio.wait_for(send(executor, text), timeout=5)
    claude.tool_call()

    assert claude.model_saw == ["alpha", "bravo", "charlie"]


@pytest.mark.parametrize("loss", ["new-transcript", "pane-exited"])
@pytest.mark.asyncio
async def test_the_hold_ends_when_claude_loses_its_queue(
    claude: _ClaudeCode, tmp_path: Path, loss: str
) -> None:
    """A new transcript or an exited Claude process drops the queue."""
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    claude.busy = True
    await _steer(executor, _SLASH_LED)
    bravo = await _held_send(claude, _steer(executor, "bravo"))
    assert claude.typed == [_SLASH_LED], "bravo must wait while the /-led message is queued"

    if loss == "pane-exited":
        claude.alive = False
    else:
        claude_bridge.record_hook_event(
            tmp_path / "bridge",
            {
                "hook_event_name": "SessionStart",
                "session_id": _SESSION,
                "transcript_path": str(tmp_path / "restarted.jsonl"),
            },
        )
    await asyncio.wait_for(bravo, timeout=5)

    assert claude.typed == [_SLASH_LED, "bravo"]


@pytest.mark.asyncio
async def test_a_message_held_past_the_limit_is_delivered(
    claude: _ClaudeCode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A ``/``-led message Claude does not take delays only the next message, up to the limit.

    A later ``/``-led message still holds the messages after it once Claude takes the first.
    """
    monkeypatch.setattr(claude_native_executor, "_ANSWER_HOLD_S", 0.05)
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    claude.busy = True
    await _steer(executor, _SLASH_LED)

    await asyncio.wait_for(_steer(executor, "bravo"), timeout=5)
    monkeypatch.setattr(claude_native_executor, "_ANSWER_HOLD_S", 60.0)
    await asyncio.wait_for(_steer(executor, "charlie"), timeout=5)
    await asyncio.wait_for(_steer(executor, _SECOND_SLASH_LED), timeout=5)
    assert claude.typed == [_SLASH_LED, "bravo", "charlie", _SECOND_SLASH_LED]

    claude.tool_call()
    claude.end_turn()
    delta = await _held_send(claude, _steer(executor, "delta"))
    claude.tool_call()
    claude.end_turn()
    await asyncio.wait_for(delta, timeout=5)
    claude.tool_call()

    assert claude.model_saw == ["bravo", "charlie", _SLASH_LED, _SECOND_SLASH_LED, "delta"]


@pytest.mark.parametrize("disruption", ["large-tool-output", "unreadable-transcript"])
@pytest.mark.asyncio
async def test_the_hold_outlasts_a_log_it_cannot_read_at_once(
    claude: _ClaudeCode, tmp_path: Path, disruption: str
) -> None:
    """A large or briefly unreadable transcript does not count as an empty queue."""
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    claude.busy = True
    await _steer(executor, _SLASH_LED)
    if disruption == "large-tool-output":
        output = {"type": "user", "sessionId": _SESSION, "content": "x" * (17 * 1024 * 1024)}
        with claude.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(output) + "\n")
        bravo = await _held_send(claude, _steer(executor, "bravo"))
    else:
        bravo = await _held_send(claude, _steer(executor, "bravo"))
        moved = claude.transcript.rename(tmp_path / "moved.jsonl")
        claude.transcript.mkdir()
        await _settle(claude, bravo)
        claude.transcript.rmdir()
        moved.rename(claude.transcript)
    assert claude.typed == [_SLASH_LED], "bravo must wait while the /-led message is queued"

    claude.tool_call()
    claude.end_turn()
    await asyncio.wait_for(bravo, timeout=5)
    claude.tool_call()

    assert claude.model_saw == [_SLASH_LED, "bravo"]


@pytest.mark.parametrize(
    "earlier,taken_at_turn_end",
    [("typed into the pane", False), ("/home/me/earlier.txt read this first", True)],
    ids=["folded", "taken-at-turn-end"],
)
@pytest.mark.asyncio
async def test_a_message_queued_before_the_executor_leaves_later_holds_working(
    claude: _ClaudeCode, tmp_path: Path, earlier: str, taken_at_turn_end: bool
) -> None:
    """Claude taking a message queued before the executor followed its log is not a stuck entry."""
    claude.busy = True
    claude.submit(earlier)
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    await _steer(executor, "alpha")
    claude.tool_call()
    if taken_at_turn_end:
        claude.end_turn()
    await _steer(executor, _SLASH_LED)

    bravo = await _held_send(claude, _steer(executor, "bravo"))
    assert claude.typed == ["alpha", _SLASH_LED], (
        "bravo must wait while the /-led message is queued"
    )
    claude.tool_call()
    claude.end_turn()
    await asyncio.wait_for(bravo, timeout=5)
    claude.tool_call()

    assert claude.model_saw[-2:] == [_SLASH_LED, "bravo"]


@pytest.mark.parametrize("path", ["steer", "queued-turn"])
@pytest.mark.asyncio
async def test_a_message_held_through_clear_is_not_typed_into_the_new_conversation(
    claude: _ClaudeCode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """
    ``/clear`` during the hold hands the pane to a new conversation, so nothing is typed.

    Claude starts its new session before Omnigent switches the pane's active session.
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "bridge.json").write_text('{"active_session_id": "conv_old"}', encoding="utf-8")
    monkeypatch.setenv(claude_bridge.REQUEST_SESSION_ID_ENV_VAR, "conv_old")
    executor = ClaudeNativeExecutor(bridge_dir)
    claude.busy = True
    await _steer(executor, _SLASH_LED)
    held = await _held_send(
        claude,
        executor.enqueue_session_message("session", "bravo")
        if path == "steer"
        else _turn_events(executor, "bravo"),
    )
    assert not held.done(), "bravo must wait while the /-led message is queued"

    claude_bridge.record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "22222222-2222-4333-8444-555555555555",
            "transcript_path": str(tmp_path / "cleared.jsonl"),
        },
    )
    result = await asyncio.wait_for(held, timeout=5)

    assert claude.typed == [_SLASH_LED]
    if path == "steer":
        assert result is False
    else:
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], ExecutorError)
        assert "no longer active after /clear" in result[0].message
        assert result[0].undelivered is True


@pytest.mark.asyncio
async def test_stopping_a_held_message_keeps_the_pane(claude: _ClaudeCode, tmp_path: Path) -> None:
    """Stop during the hold types nothing and does not reap the pane."""
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    claude.busy = True
    await _turn(executor, _SLASH_LED)
    bravo = await _held_send(claude, _turn(executor, "bravo"))
    assert not bravo.done(), "bravo must still be waiting for the /-led message"

    bravo.cancel()
    with pytest.raises(asyncio.CancelledError):
        await bravo

    assert claude.typed == [_SLASH_LED]
    assert claude.kills == 0

"""Reader loss must fail promptly without replaying unobserved native work."""

import asyncio
import gc
import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnigent.errors import HarnessTransportClosedError
from omnigent.inner.codex_executor import (
    CodexExecutor,
    _CodexAppServerSession,
    _CodexSessionState,
    _tool_signature,
)
from omnigent.inner.executor import TurnComplete
from omnigent.models.model_fallbacks import CODEX_DEFAULT_MODEL
from omnigent.runtime.harnesses import _executor_adapter
from tests.inner.test_codex_executor import _FakeAppSession, _FakeProcess


def _session() -> tuple[_CodexAppServerSession, asyncio.StreamReader]:
    session = _CodexAppServerSession(
        codex_path="/bin/echo", cwd="/tmp", env={}, tool_executor=None
    )
    session.start = AsyncMock()
    session._proc = _FakeProcess()
    stream = asyncio.StreamReader()
    session._proc.stdout = stream
    session.thread_id = "thread-1"
    return session, stream


async def _collect_turn(
    session: _CodexAppServerSession,
    *,
    content: str = "check the workspace",
    reasoning_effort: str | None = None,
) -> list[object]:
    return [
        event
        async for event in session.run_turn(
            messages=[{"role": "user", "content": content}],
            tools=[],
            system_prompt="",
            model="test-model",
            cwd="/tmp",
            sandbox="workspace-write",
            reasoning_effort=reasoning_effort,
        )
    ]


async def _collect_executor_turn(
    executor: CodexExecutor,
    messages: list[dict[str, object]],
) -> list[object]:
    return [event async for event in executor.run_turn(messages, [], "")]


async def _pending_request(session: _CodexAppServerSession, method: str) -> int:
    for _ in range(100):
        proc = session._proc
        writes = proc.stdin.writes if proc is not None and proc.stdin is not None else []
        for wire in reversed(writes):
            request = json.loads(wire.decode("utf-8"))
            request_id = request.get("id")
            if request.get("method") == method and request_id in session._pending_requests:
                return request_id
        await asyncio.sleep(0)
    pytest.fail(f"{method} was not sent")


async def _pending_start(session: _CodexAppServerSession) -> int:
    return await _pending_request(session, "turn/start")


def _frame(message: dict[str, object]) -> bytes:
    return (json.dumps(message) + "\n").encode()


async def _complete_turn(
    session: _CodexAppServerSession,
    stream: asyncio.StreamReader,
    turn_id: str,
) -> list[object]:
    turn = asyncio.create_task(_collect_turn(session))
    request_id = await _pending_start(session)
    stream.feed_data(
        b"".join(
            [
                _frame({"id": request_id, "result": {"turn": {"id": turn_id}}}),
                _frame({"method": "turn/started", "params": {"turn": {"id": turn_id}}}),
                _frame({"method": "turn/completed", "params": {"turn": {"id": turn_id}}}),
            ]
        )
    )
    return await turn


@pytest.mark.parametrize(
    "activity",
    [
        {
            "method": "item/started",
            "params": {
                "turnId": "turn-1",
                "item": {"type": "commandExecution", "id": "shell-1", "command": "touch result"},
            },
        },
        {
            "method": "item/started",
            "params": {
                "turnId": "turn-1",
                "item": {"type": "fileChange", "id": "file-1", "changes": []},
            },
        },
        {
            "method": "item/started",
            "params": {"turnId": "turn-1", "item": {"type": "mcpToolCall", "id": "mcp-1"}},
        },
        {
            "method": "item/started",
            "params": {"turnId": "turn-1", "item": {"type": "futureNativeTool", "id": "future-1"}},
        },
        {
            "id": 987,
            "method": "item/tool/call",
            "params": {"turnId": "turn-1", "callId": "call-1", "tool": "write", "arguments": {}},
        },
        {
            "method": "item/agentMessage/delta",
            "params": {"turnId": "turn-1", "itemId": "message-1", "delta": "Working"},
        },
        {
            "method": "item/reasoning/summaryTextDelta",
            "params": {"turnId": "turn-1", "itemId": "reason-1", "delta": "Inspect first"},
        },
        {
            "method": "item/futureTool/outputDelta",
            "params": {"turnId": "turn-1", "delta": "changed"},
        },
        {"method": "remoteControl/futureActivity", "params": {}},
        {"method": "remoteControl/status/changed", "params": []},
    ],
)
async def test_activity_before_start_ack_disallows_replay(activity: dict[str, object]) -> None:
    session, stream = _session()
    reader = asyncio.create_task(session._reader_loop())
    turn = asyncio.create_task(_collect_turn(session))
    await _pending_start(session)
    stream.feed_data(_frame(activity))
    stream.feed_eof()
    async with asyncio.timeout(1):
        with pytest.raises(HarnessTransportClosedError) as caught:
            await turn
        await reader
    assert caught.value.replay_safe is False


async def test_user_input_before_start_ack_does_not_prevent_safe_recovery() -> None:
    session, stream = _session()
    reader = asyncio.create_task(session._reader_loop())
    turn = asyncio.create_task(_collect_turn(session))
    await _pending_start(session)
    stream.feed_data(
        _frame(
            {
                "method": "item/started",
                "params": {"turnId": "turn-1", "item": {"id": "input-1", "type": "userMessage"}},
            }
        )
    )
    stream.feed_eof()
    async with asyncio.timeout(1):
        with pytest.raises(HarnessTransportClosedError) as caught:
            await turn
        await reader
    assert caught.value.replay_safe is True


def _remote_control_status() -> dict[str, object]:
    return {
        "method": "remoteControl/status/changed",
        "params": {
            "status": "disabled",
            "serverName": "test-server",
            "installationId": "test-installation",
            "environmentId": None,
        },
    }


@pytest.mark.parametrize("setup_method", ["thread/start", "thread/settings/update"])
async def test_remote_control_status_during_setup_allows_safe_eof_recovery(
    setup_method: str,
) -> None:
    session, stream = _session()
    if setup_method == "thread/start":
        session.thread_id = None
    reader = asyncio.create_task(session._reader_loop())
    turn = asyncio.create_task(_collect_turn(session, reasoning_effort="high"))
    await _pending_request(session, setup_method)
    stream.feed_data(_frame(_remote_control_status()))
    stream.feed_eof()

    async with asyncio.timeout(1):
        with pytest.raises(HarnessTransportClosedError) as caught:
            await turn
        await reader
    assert caught.value.replay_safe is True


@pytest.mark.parametrize("activity", ["current", "unfinished-prior"])
async def test_remote_control_status_preserves_task_replay_hazards(activity: str) -> None:
    session, stream = _session()
    reader = asyncio.create_task(session._reader_loop())
    if activity == "unfinished-prior":
        stream.feed_data(
            _frame({"method": "turn/started", "params": {"turn": {"id": "prior-turn"}}})
        )
        await asyncio.sleep(0)
        assert session._reader_started_turn == "prior-turn"
    turn = asyncio.create_task(_collect_turn(session, reasoning_effort="high"))
    await _pending_request(session, "thread/settings/update")
    if activity == "current":
        stream.feed_data(
            _frame(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "turnId": "current-turn",
                        "itemId": "message-1",
                        "delta": "Working",
                    },
                }
            )
        )
    stream.feed_data(_frame(_remote_control_status()))
    stream.feed_eof()

    async with asyncio.timeout(1):
        with pytest.raises(HarnessTransportClosedError) as caught:
            await turn
        await reader
    assert caught.value.replay_safe is False


async def test_second_turn_eof_during_setup_rpc_ignores_prior_turn_activity() -> None:
    session, stream = _session()
    reader = asyncio.create_task(session._reader_loop())
    first_events = await _complete_turn(session, stream, "turn-1")
    assert [event.response for event in first_events if isinstance(event, TurnComplete)] == [""]

    turn = asyncio.create_task(_collect_turn(session, reasoning_effort="high"))
    await _pending_request(session, "thread/settings/update")
    stream.feed_eof()

    async with asyncio.timeout(1):
        with pytest.raises(HarnessTransportClosedError) as caught:
            await turn
        await reader
    assert caught.value.replay_safe is True


async def test_second_turn_setup_activity_before_start_ack_disallows_replay() -> None:
    session, stream = _session()
    reader = asyncio.create_task(session._reader_loop())
    first_events = await _complete_turn(session, stream, "turn-1")
    assert [event.response for event in first_events if isinstance(event, TurnComplete)] == [""]

    turn = asyncio.create_task(_collect_turn(session, reasoning_effort="high"))
    settings_id = await _pending_request(session, "thread/settings/update")
    stream.feed_data(
        b"".join(
            [
                _frame(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "turnId": "turn-2",
                            "itemId": "message-2",
                            "delta": "Working",
                        },
                    }
                ),
                _frame({"id": settings_id, "result": {"settings": {}}}),
            ]
        )
    )
    await _pending_start(session)
    stream.feed_eof()

    async with asyncio.timeout(1):
        with pytest.raises(HarnessTransportClosedError) as caught:
            await turn
        await reader
    assert caught.value.replay_safe is False


@pytest.mark.parametrize("terminal_order", ["final", "delta-completed", "completed-final"])
async def test_buffered_final_answer_wins_over_immediately_following_eof(
    terminal_order: str,
) -> None:
    session, stream = _session()
    reader = asyncio.create_task(session._reader_loop())
    turn = asyncio.create_task(_collect_turn(session))
    request_id = await _pending_start(session)
    final = {
        "method": "item/completed",
        "params": {
            "turnId": "turn-1",
            "item": {
                "id": "answer-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "Done",
            },
        },
    }
    completed = {
        "method": "turn/completed",
        "params": {"turn": {"id": "turn-1", "status": "completed"}},
    }
    delta = {
        "method": "item/agentMessage/delta",
        "params": {"turnId": "turn-1", "itemId": "answer-1", "delta": "Done"},
    }
    notifications = {
        "final": [final],
        "delta-completed": [delta, completed],
        "completed-final": [completed, final],
    }[terminal_order]
    stream.feed_data(
        b"".join(
            [
                _frame({"id": request_id, "result": {"turn": {"id": "turn-1"}}}),
                *(_frame(notification) for notification in notifications),
            ]
        )
    )
    stream.feed_eof()
    async with asyncio.timeout(1):
        events = await turn
        await reader
    completions = [event for event in events if isinstance(event, TurnComplete)]
    assert len(completions) == 1
    assert completions[0].response == "Done"
    with pytest.raises(HarnessTransportClosedError):
        await session._request("turn/start", {"threadId": "thread-1", "input": []})


@pytest.mark.parametrize("wire", [b'{"method": invalid}\n', b"[]\n"])
async def test_malformed_reader_input_fails_closed(wire: bytes) -> None:
    session, stream = _session()
    reader = asyncio.create_task(session._reader_loop())
    turn = asyncio.create_task(_collect_turn(session))
    await _pending_start(session)
    stream.feed_data(wire)
    async with asyncio.timeout(1):
        with pytest.raises(HarnessTransportClosedError) as caught:
            await turn
        await reader
    assert caught.value.replay_safe is False


async def test_request_after_reader_eof_does_not_register_an_unresolvable_future() -> None:
    session, stream = _session()
    stream.feed_eof()
    await session._reader_loop()
    async with asyncio.timeout(1):
        with pytest.raises(HarnessTransportClosedError):
            await session._request("turn/start", {"threadId": "thread-1", "input": []})
    assert not session._pending_requests


async def test_cancelling_reader_is_not_reported_as_transport_loss() -> None:
    session, _stream = _session()
    reader = asyncio.create_task(session._reader_loop())
    await asyncio.sleep(0)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert session._transport_error is None


async def test_success_then_eof_recreates_the_native_session_for_the_next_user_turn() -> None:
    first, stream = _session()
    first.close = AsyncMock()
    second = _FakeAppSession([[TurnComplete(response="Next answer")]])
    sessions = [first, second]
    executor = CodexExecutor(
        codex_path="/bin/echo", app_session_factory=lambda **kwargs: sessions.pop(0)
    )
    history = [{"role": "user", "content": "check workspace", "session_id": "session-1"}]

    async def collect(messages: list[dict[str, str]]) -> list[object]:
        return [event async for event in executor.run_turn(messages, [], "")]

    reader = asyncio.create_task(first._reader_loop())
    turn = asyncio.create_task(collect(history))
    request_id = await _pending_start(first)
    stream.feed_data(
        b"".join(
            [
                _frame({"id": request_id, "result": {"turn": {"id": "turn-1"}}}),
                _frame(
                    {
                        "method": "item/completed",
                        "params": {
                            "turnId": "turn-1",
                            "item": {
                                "id": "answer-1",
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "Done",
                            },
                        },
                    }
                ),
            ]
        )
    )
    stream.feed_eof()
    async with asyncio.timeout(1):
        first_events = await turn
        await reader
        history.extend(
            [
                {"role": "assistant", "content": "Done", "session_id": "session-1"},
                {"role": "user", "content": "next step", "session_id": "session-1"},
            ]
        )
        second_events = await collect(history)
    assert [event.response for event in first_events if isinstance(event, TurnComplete)] == [
        "Done"
    ]
    assert [event.response for event in second_events if isinstance(event, TurnComplete)] == [
        "Next answer"
    ]
    first.close.assert_awaited_once()
    assert not sessions
    assert second.calls[0]["messages"] == history


@pytest.mark.parametrize("failure", [OSError("close failed"), asyncio.CancelledError()])
async def test_close_session_retains_failed_cleanup_for_retry(failure: BaseException) -> None:
    session = _FakeAppSession([])
    session.close = AsyncMock(side_effect=failure)
    executor = CodexExecutor(codex_path="/bin/echo")
    state = _CodexSessionState(app_session=session)
    executor._session_states["session-1"] = state

    with pytest.raises(type(failure)):
        await executor.close_session("session-1")
    assert executor._session_states["session-1"] is state

    session.close = AsyncMock()
    await executor.close_session("session-1")
    session.close.assert_awaited_once()
    assert "session-1" not in executor._session_states


async def test_close_session_preserves_a_replacement_registered_during_cleanup() -> None:
    session = _FakeAppSession([])
    executor = CodexExecutor(codex_path="/bin/echo")
    executor._session_states["session-1"] = _CodexSessionState(app_session=session)
    replacement = _CodexSessionState(app_session=_FakeAppSession([]))

    async def replace_session() -> None:
        executor._session_states["session-1"] = replacement

    session.close = AsyncMock(side_effect=replace_session)
    await executor.close_session("session-1")
    assert executor._session_states["session-1"] is replacement


@pytest.mark.parametrize("waiters", [1, 2])
async def test_same_session_turn_waits_for_inflight_native_close(waiters: int) -> None:
    old_session, _stream = _session()
    close_started = asyncio.Event()
    close_released = asyncio.Event()

    async def close_old_session() -> None:
        old_session._closing = True
        close_started.set()
        await close_released.wait()
        await _CodexAppServerSession.close(old_session)

    old_session.close = close_old_session  # type: ignore[method-assign]

    replacements: list[_FakeAppSession] = []

    def factory(**kwargs: object) -> _FakeAppSession:
        new_session = _FakeAppSession([[TurnComplete(response="new")] for _ in range(waiters)])
        replacements.append(new_session)
        return new_session

    executor = CodexExecutor(codex_path="/bin/echo", cwd="/tmp", app_session_factory=factory)
    executor._session_states["session-1"] = _CodexSessionState(
        app_session=old_session,
        signature=(CODEX_DEFAULT_MODEL, "", "/tmp", _tool_signature([])),
    )

    close_task = asyncio.create_task(executor.close_session("session-1"))
    await close_started.wait()

    turn_tasks = [
        asyncio.create_task(
            _collect_executor_turn(
                executor,
                [{"role": "user", "content": "next", "session_id": "session-1"}],
            )
        )
        for _ in range(waiters)
    ]
    await asyncio.sleep(0)
    assert not any(
        json.loads(wire.decode("utf-8")).get("method") == "turn/start"
        for wire in old_session._proc.stdin.writes
    )
    assert not replacements

    close_released.set()
    async with asyncio.timeout(1):
        await close_task
        results = await asyncio.gather(*turn_tasks)

    for events in results:
        assert [event.response for event in events if isinstance(event, TurnComplete)] == ["new"]
    assert len(replacements) == 1
    new_session = replacements[0]
    assert executor._session_states["session-1"].app_session is new_session
    assert len(new_session.calls) == waiters
    assert all(
        call["messages"] == [{"role": "user", "content": "next", "session_id": "session-1"}]
        for call in new_session.calls
    )
    await executor.close()
    assert new_session.closed
    assert not executor._session_states


async def test_failed_native_cleanup_cannot_confirm_safe_recovery() -> None:
    session = _FakeAppSession([])
    session.close = AsyncMock(side_effect=OSError("close failed"))
    executor = CodexExecutor(codex_path="/bin/echo")
    state = _CodexSessionState(app_session=session)
    executor._session_states["session-1"] = state

    confirmed = await _executor_adapter.ExecutorAdapter._safe_interrupt(
        None, executor, "session-1"
    )
    assert confirmed is False
    assert executor._session_states["session-1"] is state


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX SIGTERM semantics")
@pytest.mark.parametrize("cleanup", ["cancel_close", "adapter"])
async def test_cancelled_native_close_kills_and_reaps_sigterm_ignoring_child(
    cleanup: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); os.close(1); time.sleep(60)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    session = _CodexAppServerSession(
        codex_path="/bin/echo", cwd=str(tmp_path), env={}, tool_executor=None
    )
    session._proc = child
    session._codex_home_dir = tmp_path / "codex-home"
    session._codex_home_dir.mkdir()
    try:
        assert child.stdout is not None
        async with asyncio.timeout(5):
            assert await child.stdout.readline() == b"ready\n"
            session._reader_task = asyncio.create_task(session._reader_loop())
            session._stderr_task = asyncio.create_task(session._stderr_loop())
            await session._reader_task
        assert child.returncode is None
        assert session._transport_error is not None

        if cleanup == "cancel_close":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(session.close(), timeout=0.02)
        else:
            executor = CodexExecutor(codex_path="/bin/echo")
            executor._session_states["session-1"] = _CodexSessionState(app_session=session)
            monkeypatch.setattr(_executor_adapter, "_INTERRUPT_SLICE_S", 0.02)
            assert await _executor_adapter.ExecutorAdapter._safe_interrupt(
                None, executor, "session-1"
            )
            assert not executor._session_states

        assert child.returncode == -signal.SIGKILL
        assert session._proc is None
        assert session._reader_task is None
        assert session._stderr_task is None
        assert not (tmp_path / "codex-home").exists()
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()
        await session.close()


@pytest.mark.parametrize("activity, malformed", [(False, False), (True, False), (False, True)])
async def test_stdin_failure_after_reader_eof_preserves_typed_replay_safety(
    activity: bool,
    malformed: bool,
) -> None:
    session, stream = _session()
    sending = asyncio.Event()
    release = asyncio.Event()

    async def fail_drain() -> None:
        sending.set()
        await release.wait()
        raise BrokenPipeError("stdin closed")

    session._proc.stdin.drain = fail_drain
    request = asyncio.create_task(session._request("turn/start", {}))
    await sending.wait()
    if activity:
        stream.feed_data(
            _frame(
                {
                    "method": "item/started",
                    "params": {"item": {"id": "shell-1", "type": "commandExecution"}},
                }
            )
        )
    if malformed:
        stream.feed_data(b"[]\n")
    stream.feed_eof()
    await session._reader_loop()
    release.set()
    with pytest.raises(HarnessTransportClosedError) as caught:
        await request
    assert caught.value is session._transport_error
    assert caught.value.replay_safe is (not activity and not malformed)
    assert not session._pending_requests


@pytest.mark.parametrize("eof", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_interrupted_send_settles_pending_response_without_future_warnings(
    eof: bool,
    cancel: bool,
) -> None:
    loop = asyncio.get_running_loop()
    warnings: list[str] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: warnings.append(context["message"]))

    async def exercise_send() -> None:
        session, stream = _session()
        sending = asyncio.Event()
        release = asyncio.Event()
        send_error = BrokenPipeError("stdin closed")

        async def fail_drain() -> None:
            sending.set()
            await release.wait()
            raise send_error

        session._proc.stdin.drain = fail_drain
        request = asyncio.create_task(session._request("turn/start", {}))
        await sending.wait()
        future = next(iter(session._pending_requests.values()))
        if eof:
            stream.feed_eof()
            await session._reader_loop()
        if cancel:
            request.cancel()
        else:
            release.set()
        expected = (
            asyncio.CancelledError
            if cancel
            else HarnessTransportClosedError
            if eof
            else BrokenPipeError
        )
        try:
            await request
        except BaseException as error:
            # Check exception type separately so warning collection also runs on regressions.
            raised_type = type(error)
            if not cancel and not eof:
                assert error is send_error
        else:
            pytest.fail("interrupted send unexpectedly succeeded")
        assert not session._pending_requests
        if not eof:
            assert future.cancelled()
        send_error.__traceback__ = None
        if session._transport_error is not None:
            session._transport_error.__traceback__ = None
        assert raised_type is expected

    try:
        await exercise_send()
        await asyncio.sleep(0)
        gc.collect()
        assert not warnings
    finally:
        loop.set_exception_handler(previous_handler)

"""Durable ACP sessions using a real, hermetic stdio agent."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.executor import ExecutorError, TextChunk, TurnCancelled, TurnComplete

_AGENT = r"""
import json
import sys

log, mode = sys.argv[1:]
active_prompt = None

def emit(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    with open(log, "a") as output:
        output.write(json.dumps(request) + "\n")
    method = request.get("method")
    params = request.get("params", {})
    result = {}
    if method == "initialize":
        result = {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": mode != "no-load"},
        }
    elif method == "session/new":
        result = {"sessionId": "native-1"}
    elif method == "session/load":
        if mode == "load-request":
            emit({"jsonrpc": "2.0", "id": "permission-1",
                  "method": "session/request_permission", "params": {
                      "sessionId": params["sessionId"], "options": [],
                      "toolCall": {"toolCallId": "tool-1", "title": "Write a file"},
                  }})
            reply = json.loads(sys.stdin.readline())
            with open(log, "a") as output:
                output.write(json.dumps(reply) + "\n")
        if mode == "missing":
            emit({"jsonrpc": "2.0", "id": request["id"],
                  "error": {"code": -32602, "message": "Session not found"}})
            continue
        for role in ("user", "agent"):
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": params["sessionId"], "update": {
                    "sessionUpdate": role + "_message_chunk",
                    "content": {"type": "text", "text": "old transcript"},
                },
            }})
        if mode == "pending-load":
            result = {"_meta": {
                "caipePendingInterrupt": {"type": "form_input", "id": "approval-1"},
                "caipeConversationUrl": "https://agent.example.invalid/chat/native-1",
            }}
    elif method == "session/prompt":
        if mode == "cancel":
            active_prompt = request["id"]
            continue
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": params["sessionId"], "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "new reply"},
            },
        }})
        result = {"stopReason": "refusal" if mode in ("refusal", "pending") else "end_turn"}
        if mode == "pending":
            result["_meta"] = {
                "caipeStatus": "input_required",
                "caipePendingInterrupt": {"type": "form_input", "id": "approval-1"},
                "caipeConversationUrl": "https://agent.example.invalid/chat/native-1",
            }
    elif method == "session/cancel":
        if active_prompt is not None:
            emit({"jsonrpc": "2.0", "id": active_prompt,
                  "result": {"stopReason": "cancelled"}})
            active_prompt = None
        continue
    if "id" in request:
        emit({"jsonrpc": "2.0", "id": request["id"], "result": result})
"""


def _executor(tmp_path: Path, mode: str = "normal") -> tuple[AcpExecutor, Path]:
    script = tmp_path / "agent.py"
    script.write_text(_AGENT)
    log = tmp_path / "requests.jsonl"
    executor = AcpExecutor(
        AcpAgentConfig(
            command=shlex.join([sys.executable, str(script), str(log), mode]),
            omnigent_mcp=False,
            inject_system_prompt=False,
        ),
        cwd=str(tmp_path),
    )
    return executor, log


def _requests(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


async def _turn(executor: AcpExecutor, text: str = "new question") -> list:
    return [
        event
        async for event in executor.run_turn(
            [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old transcript"},
                {"role": "user", "content": text},
            ],
            [],
            "system instructions",
        )
    ]


@pytest.mark.asyncio
async def test_checkpoint_acknowledged_before_prompt_and_restarts_load_without_replay(
    tmp_path: Path,
) -> None:
    executor, log = _executor(tmp_path)
    entered = asyncio.Event()
    acknowledged = asyncio.Event()
    saved: list[str] = []

    async def checkpoint(session_id: str) -> None:
        entered.set()
        await acknowledged.wait()
        saved.append(session_id)

    executor.configure_native_session(None, checkpoint)
    turn = asyncio.create_task(_turn(executor))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert [r["method"] for r in _requests(log)] == ["initialize", "session/new"]
        acknowledged.set()
        assert isinstance((await asyncio.wait_for(turn, 5))[-1], TurnComplete)
        assert saved == ["native-1"]
        await executor.close()

        # A fresh executor models a full harness/host restart, not only process reuse.
        restored, _ = _executor(tmp_path)
        restored.configure_native_session(saved[0], checkpoint)
        try:
            events = await asyncio.wait_for(_turn(restored, "follow up"), 5)
            assert [event.text for event in events if isinstance(event, TextChunk)] == [
                "new reply"
            ]
            requests = _requests(log)
            assert [r["method"] for r in requests].count("session/new") == 1
            assert [r["method"] for r in requests].count("session/load") == 1
            prompt = [r for r in requests if r["method"] == "session/prompt"][-1]
            assert prompt["params"] == {
                "sessionId": "native-1",
                "prompt": [{"type": "text", "text": "follow up"}],
            }
            assert saved == ["native-1"]
        finally:
            await restored.close()
    finally:
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await executor.close()


@pytest.mark.asyncio
async def test_checkpoint_failure_or_cancel_never_sends_prompt(tmp_path: Path) -> None:
    executor, log = _executor(tmp_path)
    checkpoint = AsyncMock(side_effect=RuntimeError("checkpoint rejected"))
    executor.configure_native_session(None, checkpoint)
    try:
        events = await asyncio.wait_for(_turn(executor), 5)
        assert isinstance(events[-1], ExecutorError)
        assert "checkpoint rejected" in events[-1].message
        assert all(r["method"] != "session/prompt" for r in _requests(log))

        checkpoint.side_effect = None
        entered = asyncio.Event()

        async def blocked(_session_id: str) -> None:
            entered.set()
            await asyncio.Event().wait()

        executor.configure_native_session(None, blocked)
        task = asyncio.create_task(_turn(executor))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(r["method"] != "session/prompt" for r in _requests(log))
    finally:
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "no-load"])
async def test_saved_session_failure_never_creates_replacement(tmp_path: Path, mode: str) -> None:
    executor, log = _executor(tmp_path, mode)
    executor.configure_native_session("native-1", AsyncMock())
    try:
        events = await asyncio.wait_for(_turn(executor), 5)
        assert isinstance(events[-1], ExecutorError)
        assert "saved session was not replaced" in events[-1].message
        assert not any(r["method"] in {"session/new", "session/prompt"} for r in _requests(log))
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_transport_eof_during_checkpoint_stops_before_prompt(tmp_path: Path) -> None:
    executor, log = _executor(tmp_path)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def checkpoint(_session_id: str) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    executor.configure_native_session(None, checkpoint)
    task = asyncio.create_task(_turn(executor))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert executor._proc is not None
        executor._proc.terminate()
        events = await asyncio.wait_for(task, 5)
        assert isinstance(events[-1], ExecutorError)
        assert "transport closed during native session checkpoint" in events[-1].message
        assert cancelled.is_set()
        assert executor._native_session_id is None
        assert not any(r["method"] == "session/prompt" for r in _requests(log))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await executor.close()


@pytest.mark.asyncio
async def test_request_during_load_fails_promptly_without_executing_action(tmp_path: Path) -> None:
    executor, log = _executor(tmp_path, "load-request")
    executor.configure_native_session("native-1", AsyncMock())
    try:
        events = await asyncio.wait_for(_turn(executor), 5)
        assert isinstance(events[-1], ExecutorError)
        assert "requested an action during session replay" in events[-1].message
        async with asyncio.timeout(5):
            while not any(r.get("id") == "permission-1" for r in _requests(log)):
                await asyncio.sleep(0.01)
        permission_reply = next(r for r in _requests(log) if r.get("id") == "permission-1")
        assert permission_reply["error"]["code"] == -32601
        assert not any(r.get("method") == "session/prompt" for r in _requests(log))
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_loaded_pending_form_is_not_prompted_or_approved(tmp_path: Path) -> None:
    executor, log = _executor(tmp_path, "pending-load")
    executor.configure_native_session("native-1", AsyncMock())
    try:
        events = await asyncio.wait_for(_turn(executor), 5)
        error = events[-1]
        assert isinstance(error, ExecutorError)
        assert error.code == "acp_input_required"
        assert error.preserve_session and error.undelivered
        assert error.remediation == "https://agent.example.invalid/chat/native-1"
        assert not any(isinstance(event, (TextChunk, TurnComplete)) for event in events)
        assert [r["method"] for r in _requests(log)] == ["initialize", "session/load"]
    finally:
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,code", [("refusal", "acp_refused"), ("pending", "acp_input_required")]
)
async def test_refusal_and_explicit_pending_input_have_distinct_outcomes(
    tmp_path: Path,
    mode: str,
    code: str,
) -> None:
    executor, _ = _executor(tmp_path, mode)
    executor.configure_native_session(None, AsyncMock())
    try:
        events = await asyncio.wait_for(_turn(executor), 5)
        assert isinstance(events[-1], ExecutorError)
        assert events[-1].code == code
        assert events[-1].preserve_session
        assert not any(isinstance(event, TurnComplete) for event in events)
        if mode == "refusal":
            assert "waiting" not in events[-1].message
        else:
            assert events[-1].remediation == "https://agent.example.invalid/chat/native-1"
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_cancel_uses_native_id_and_retains_reference_for_reconnect(tmp_path: Path) -> None:
    executor, log = _executor(tmp_path, "cancel")
    executor.configure_native_session(None, AsyncMock())
    task = asyncio.create_task(_turn(executor))
    try:
        async with asyncio.timeout(5):
            while not log.exists() or not any(
                r["method"] == "session/prompt" for r in _requests(log)
            ):
                await asyncio.sleep(0.01)
        assert await executor.interrupt_session("local-session")
        events = await asyncio.wait_for(task, 5)
        assert isinstance(events[-1], TurnCancelled)
        assert not any(isinstance(event, TurnComplete) for event in events)
        assert _requests(log)[-1]["params"] == {"sessionId": "native-1"}
        await executor.close()
        assert executor._native_session_id == "native-1"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("capabilities", [[], {"promptCapabilities": []}, {"loadSession": "true"}])
async def test_malformed_capabilities_are_rejected_clearly(capabilities: object) -> None:
    executor = AcpExecutor(AcpAgentConfig(command="fake"))
    executor._rpc = AsyncMock(return_value={"result": {"agentCapabilities": capabilities}})
    with pytest.raises(RuntimeError, match="ACP initialize returned invalid"):
        await executor._ensure_initialized()

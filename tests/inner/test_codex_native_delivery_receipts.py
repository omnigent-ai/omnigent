"""Native submission recovery requires an exact persisted input receipt."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from websockets.asyncio.server import ServerConnection, serve

import omnigent.inner.codex_native_executor as native_executor
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)
from omnigent.inner.codex_native_executor import CodexNativeExecutor
from omnigent.inner.executor import InjectionDeliveryAmbiguous
from omnigent.native.input_diagnostics import input_delivery_scope
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.server.schemas import CreateResponseRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["turn/start", "turn/steer"])
@pytest.mark.parametrize(
    "receipt_kind",
    [
        "exact",
        "native-failed",
        "old-cli-no-id",
        "same-text-other-id",
        "wrong-thread",
        "different-turn",
        "missing-item-id",
        "wrong-item-type",
        "session-before-read",
        "thread-during-read",
        "socket-during-read",
        "read-failure",
        "read-timeout",
        "cancel-read",
        "no-server-id",
        "explicit-rejection",
    ],
)
async def test_native_receipt_requires_exact_delivery_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, receipt_kind: str
) -> None:
    """Only an owned input receipt recovers a lost reply; text and refusal never do."""
    requests: list[str] = []
    ordering: list[str] = []
    read_waiting = asyncio.Event()
    receipt_file = tmp_path / "native-history.json"
    confirmed = receipt_kind in {"exact", "native-failed"} or (
        receipt_kind == "different-turn" and method == "turn/start"
    )
    if receipt_kind == "read-timeout":
        monkeypatch.setattr(native_executor, "_DELIVERY_RECEIPT_TIMEOUT_SECONDS", 0.05)

    async def handle(websocket: ServerConnection) -> None:
        async for raw in websocket:
            request = json.loads(raw)
            if "id" not in request:
                continue
            rpc_method = request["method"]
            requests.append(rpc_method)
            if rpc_method == method:
                native_id = request["params"].get("clientUserMessageId")
                if receipt_kind == "old-cli-no-id":
                    native_id = None
                elif receipt_kind == "same-text-other-id":
                    native_id = "d" * 32
                receipt_file.write_text(
                    json.dumps(
                        {
                            "thread": {
                                "id": "thread_other"
                                if receipt_kind == "wrong-thread"
                                else "thread_receipt",
                                "turns": [
                                    {
                                        "id": "turn_other"
                                        if receipt_kind == "different-turn"
                                        else "turn_accepted",
                                        "status": "failed"
                                        if receipt_kind == "native-failed"
                                        else "inProgress",
                                        "items": [
                                            {
                                                "type": "agentMessage"
                                                if receipt_kind == "wrong-item-type"
                                                else "userMessage",
                                                "id": ""
                                                if receipt_kind == "missing-item-id"
                                                else "item_accepted",
                                                "clientId": native_id,
                                                "content": [{"type": "text", "text": "same text"}],
                                            }
                                        ],
                                    }
                                ],
                            }
                        }
                    )
                )
                ordering.append("native_receipt_persisted")
                # The forwarder may already have moved to another turn before RPC recovery.
                state = read_bridge_state(tmp_path)
                assert state is not None
                state = replace(state, active_turn_id="turn_newer")
                if receipt_kind == "session-before-read":
                    state = replace(state, session_id="conv_other")
                write_bridge_state(tmp_path, state)
                if receipt_kind == "explicit-rejection":
                    await websocket.send(
                        json.dumps(
                            {
                                "id": request["id"],
                                "error": {"code": -32603, "message": "explicit refusal"},
                            }
                        )
                    )
                    continue
                ordering.append("rpc_reply_lost")
                websocket.transport.abort()
                return
            result = json.loads(receipt_file.read_text()) if rpc_method == "thread/read" else {}
            if rpc_method == "thread/read":
                ordering.append("receipt_read")
                read_waiting.set()
                if receipt_kind in {"read-timeout", "cancel-read"}:
                    await websocket.wait_closed()
                    return
                if receipt_kind == "read-failure":
                    websocket.transport.abort()
                    return
                state = read_bridge_state(tmp_path)
                assert state is not None
                if receipt_kind == "thread-during-read":
                    write_bridge_state(tmp_path, replace(state, thread_id="thread_other"))
                elif receipt_kind == "socket-during-read":
                    write_bridge_state(tmp_path, replace(state, socket_path="ws://127.0.0.1:9"))
            await websocket.send(json.dumps({"id": request["id"], "result": result}))

    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        write_bridge_state(
            tmp_path,
            CodexNativeBridgeState(
                session_id="conv_receipt",
                thread_id="thread_receipt",
                socket_path=f"ws://127.0.0.1:{port}",
                codex_home=str(tmp_path),
                active_turn_id="turn_accepted" if method == "turn/steer" else None,
            ),
        )
        executor = CodexNativeExecutor(bridge_dir=tmp_path)
        adapter = ExecutorAdapter(executor_factory=lambda: executor)
        request = CreateResponseRequest(
            model="test-agent",
            input="same text",
            input_stable_id="a" * 32,
            pending_id="pending_" + "b" * 32,
            delivery_attempt_id=None if receipt_kind == "no-server-id" else "c" * 32,
        )
        ctx = TurnContext(
            response_id="resp_receipt", event_queue=asyncio.Queue(), cancelled=asyncio.Event()
        )
        if receipt_kind == "cancel-read":
            task = asyncio.create_task(adapter.run_turn(request, ctx))
            await asyncio.wait_for(read_waiting.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif confirmed:
            await asyncio.wait_for(adapter.run_turn(request, ctx), timeout=5)
        else:
            expected = (
                "explicit refusal"
                if receipt_kind == "explicit-rejection"
                else "may already be running"
            )
            with pytest.raises(RuntimeError, match=expected):
                await asyncio.wait_for(adapter.run_turn(request, ctx), timeout=5)
        if adapter._abandoned_executor_cleanup is not None:
            await adapter._abandoned_executor_cleanup

    assert requests.count(method) == 1
    should_probe = receipt_kind not in {
        "session-before-read",
        "no-server-id",
        "explicit-rejection",
    }
    assert requests.count("thread/read") == int(should_probe)
    if receipt_kind not in {"explicit-rejection", "cancel-read"}:
        assert "turn/interrupt" not in requests
    if confirmed:
        assert ordering == ["native_receipt_persisted", "rpc_reply_lost", "receipt_read"]
    receipt = json.loads(receipt_file.read_text())
    if confirmed:
        assert receipt["thread"]["turns"][0]["items"][0]["clientId"] == "c" * 32
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_newer"


@pytest.mark.asyncio
async def test_mismatched_request_thread_cannot_reconcile_from_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrelated requested thread cannot borrow an exact receipt from bridge history."""
    from unittest.mock import AsyncMock

    from omnigent.harnesses.codex_native.app_server import CodexAppServerClient

    client = CodexAppServerClient(ws_url="ws://127.0.0.1:9")
    request = AsyncMock(side_effect=ConnectionResetError("reply lost"))
    monkeypatch.setattr(client, "request", request)
    state = CodexNativeBridgeState(
        session_id="conv_receipt",
        thread_id="thread_receipt",
        socket_path="ws://127.0.0.1:9",
        codex_home=str(tmp_path),
    )
    write_bridge_state(tmp_path, state)
    with input_delivery_scope({"delivery_attempt_id": "c" * 32}):
        with pytest.raises(InjectionDeliveryAmbiguous):
            await native_executor._native_delivery_request_once(
                client,
                "turn/start",
                {"threadId": "thread_other", "input": []},
                state,
                bridge_dir=tmp_path,
            )
    request.assert_awaited_once_with("turn/start", {"threadId": "thread_other", "input": []})

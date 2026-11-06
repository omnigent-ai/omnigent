"""Event-stream lifecycle regressions for the native Codex app-server client."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest
from websockets.asyncio.server import ServerConnection, serve

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.app_server import CodexAppServerClient
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)
from omnigent.inner.codex_native_executor import CodexNativeExecutor
from tests.harnesses.codex_native.forwarder._support import _RecordingClient

#: Seconds within which a waiting event consumer must settle once its
#: connection is gone. Generous against CI jitter, tiny against a hang.
_SETTLE_DEADLINE = 5.0


async def _serve_probe_scenario(websocket: ServerConnection) -> None:
    """Answer the handshake; emit an event or disconnect on probe notifications."""
    async for raw in websocket:
        message = json.loads(raw)
        method = message.get("method")
        if method == "initialize":
            await websocket.send(json.dumps({"id": message["id"], "result": {}}))
        elif method == "probe/emit":
            await websocket.send(json.dumps({"method": "thread/event", "params": {}}))
        elif method == "probe/disconnect":
            await websocket.close()
            return


async def test_concurrent_event_consumers_all_settle_on_disconnect() -> None:
    """Every waiting event consumer wakes when the connection dies, not just one."""
    async with serve(_serve_probe_scenario, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
        await client.connect()
        streams = [client.iter_events(), client.iter_events()]
        consumers = [asyncio.ensure_future(anext(stream)) for stream in streams]
        try:
            await asyncio.sleep(0)
            await client.notify("probe/disconnect")
            done, _ = await asyncio.wait(consumers, timeout=_SETTLE_DEADLINE)
            still_blocked = len(consumers) - len(done)
            assert still_blocked == 0, (
                f"{still_blocked} of {len(consumers)} event consumers still blocked "
                f"{_SETTLE_DEADLINE}s after the app-server disconnect"
            )
        finally:
            for consumer in consumers:
                consumer.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            for stream in streams:
                await stream.aclose()
            await client.close()


async def test_reconnected_client_streams_live_events() -> None:
    """A reconnected client's consumers get live events, not the old stream's end."""
    async with serve(_serve_probe_scenario, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")

        await client.connect()
        first_stream = client.iter_events()
        first = asyncio.ensure_future(anext(first_stream))
        try:
            await asyncio.sleep(0)
            await client.notify("probe/disconnect")
            done, _ = await asyncio.wait({first}, timeout=_SETTLE_DEADLINE)
            assert first in done, (
                f"event consumer still blocked {_SETTLE_DEADLINE}s after the app-server disconnect"
            )
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            await first_stream.aclose()
            await client.close()

        await client.connect()
        second_stream = client.iter_events()
        second = asyncio.ensure_future(anext(second_stream))
        try:
            await asyncio.sleep(0)
            await client.notify("probe/emit")
            done, _ = await asyncio.wait({second}, timeout=_SETTLE_DEADLINE)
            assert second in done, "reconnected consumer never received the live event"
            assert second.exception() is None, (
                "the reconnected stream ended instead of yielding the live event "
                "the new connection sent"
            )
            assert second.result().get("method") == "thread/event"
        finally:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
            await second_stream.aclose()
            with contextlib.suppress(Exception):
                await client.close()


@pytest.mark.parametrize("stop", [False, True])
@pytest.mark.parametrize("failure", ["output_limit", "stream_disconnect"])
async def test_recoverable_turn_continuation_over_loopback_rpc(
    tmp_path: Path, stop: bool, failure: str
) -> None:
    """Real client transport proves delivery and keeps Stop distinct from completion."""
    requests: list[dict] = []
    receipt: dict = {}
    error = {
        "message": (
            "Incomplete response returned, reason: max_output_tokens"
            if failure == "output_limit"
            else "stream disconnected before completion: stream closed before response.completed"
        )
    }
    source = {
        "method": "turn/failed" if failure == "stream_disconnect" else "turn/completed",
        "params": {
            "threadId": "thread",
            "turn": {"id": "source", "status": "failed", "error": error},
        },
    }

    async def handle(websocket: ServerConnection) -> None:
        async for raw in websocket:
            message = json.loads(raw)
            method = message.get("method")
            if method == "initialize":
                await websocket.send(
                    json.dumps(
                        {
                            "id": message["id"],
                            "result": {
                                "userAgent": "omnigent/0.166.0",
                            },
                        }
                    )
                )
            elif method == "probe/source":
                await websocket.send(json.dumps(source))
            elif method == "probe/complete":
                await websocket.send(
                    json.dumps(
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": "thread",
                                "turnId": "automatic",
                                "item": {
                                    "type": "agentMessage",
                                    "id": "answer",
                                    "text": "Final answer.",
                                },
                            },
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": "thread",
                                "turn": {
                                    "id": "automatic",
                                    "status": "interrupted" if stop else "completed",
                                },
                            },
                        }
                    )
                )
            elif method in {
                "turn/start",
                "thread/read",
                "turn/interrupt",
                "thread/backgroundTerminals/list",
            }:
                requests.append(message)
                if method == "turn/start":
                    receipt.update(
                        {
                            "id": "native-input",
                            "type": "userMessage",
                            "clientId": message["params"]["clientUserMessageId"],
                        }
                    )
                    result = {"turn": {"id": "ack-is-not-proof"}}
                elif method == "thread/read":
                    result = {
                        "thread": {
                            "id": "thread",
                            "turns": [
                                {
                                    "id": "automatic",
                                    "status": "inProgress",
                                    "items": [receipt],
                                }
                            ],
                        }
                    }
                elif method == "thread/backgroundTerminals/list":
                    result = {"data": []}
                else:
                    result = {}
                await websocket.send(json.dumps({"id": message["id"], "result": result}))

    async with serve(handle, "127.0.0.1", 0) as server:
        transport = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        write_bridge_state(
            tmp_path,
            CodexNativeBridgeState(
                session_id="conv",
                thread_id="thread",
                socket_path=transport,
                codex_home=str(tmp_path / "codex-home"),
                active_turn_id="source",
            ),
        )
        native = CodexAppServerClient(ws_url=transport)
        await native.connect()
        assert native.supports_user_message_ids
        client, state = _RecordingClient(), fwd._CodexForwarderState()
        stream = native.iter_events()

        async def forward_next() -> None:
            event = await asyncio.wait_for(anext(stream), 2)
            await fwd._handle_event(
                client,
                session_id="conv",
                bridge_dir=tmp_path,
                event=event,
                usage_coalescer=fwd._SessionUsageCoalescer(client, "conv"),
                elicitation_tracker=fwd._CodexElicitationTaskTracker(),
                expected_thread_id="thread",
                codex_client=native,
                forwarder_state=state,
            )

        try:
            await native.notify("probe/source")
            await forward_next()
            owner = read_bridge_state(tmp_path)
            assert owner is not None and owner.active_turn_id == "automatic"
            assert owner.automatic_input_client_message_id == receipt["clientId"]
            assert owner.automatic_input_outcome == "accepted"
            if stop:
                assert await CodexNativeExecutor(bridge_dir=tmp_path).interrupt_session("conv")
                assert read_bridge_state(tmp_path).active_turn_id == "automatic"
            await native.notify("probe/complete")
            await forward_next()
            await forward_next()
            await native.notify("probe/source")
            await forward_next()
            if failure == "stream_disconnect":
                continuation = requests[0]["params"]["input"][0]["text"]
                assert "stream disconnected" in continuation
                assert "Do not repeat completed tool calls" in continuation
            assert [request["method"] for request in requests] == (
                ["turn/start", "thread/read", "turn/interrupt", "thread/backgroundTerminals/list"]
                if stop
                else ["turn/start", "thread/read", "thread/backgroundTerminals/list"]
            )
            assert read_bridge_state(tmp_path).active_turn_id is None
            answers = [
                body
                for _, body in client.posts
                if body["type"] == "external_conversation_item"
                and "Final answer." in json.dumps(body)
            ]
            assert len(answers) == 1
            statuses = [
                body["data"]["status"]
                for _, body in client.posts
                if body["type"] == "external_session_status"
            ]
            assert statuses[-1] == "idle"
            background = [
                body["data"]
                for _, body in client.posts
                if body["type"] == "external_background_work"
            ]
            assert background == [
                {"running": False, "background_task_count": 0, "background_tasks": []}
            ]
        finally:
            await stream.aclose()
            await native.close()

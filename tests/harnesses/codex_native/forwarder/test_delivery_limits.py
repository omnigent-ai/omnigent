"""Oversized events must not block Codex completion forwarding."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)
from omnigent.runtime.tool_output import MAX_TOOL_OUTPUT_BYTES
from omnigent.session_event_batch import MAX_SESSION_EVENT_REQUEST_BYTES


@pytest.fixture(autouse=True)
def _isolated_forward_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fwd, "_forward_health", fwd._ForwardHealth())


@pytest.mark.asyncio
async def test_large_tool_output_is_capped_before_upload() -> None:
    """An early server rejection can arrive as a read error during upload."""
    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # Simulate a proxy closing the upload when the server rejects its size.
        if len(request.content) > MAX_SESSION_EVENT_REQUEST_BYTES:
            raise httpx.ReadError("upload rejected", request=request)
        return httpx.Response(202)

    output = "x" * (MAX_SESSION_EVENT_REQUEST_BYTES + 1)
    item = {"call_id": "call_1", "output": output}
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(receive)
    ) as client:
        assert await asyncio.wait_for(
            fwd._post_external_item(
                client,
                "conv_x",
                item_type="function_call_output",
                item_data=item,
                response_id="codex_turn_1",
                source_id="thread_1:turn_1:call_1:output",
            ),
            timeout=2,
        )
        await fwd._post_status(client, "conv_x", "idle", response_id="codex_turn_1")

    assert len(requests) == 2
    mirrored = json.loads(requests[0].content)["data"]["item_data"]
    assert mirrored["call_id"] == "call_1"
    assert mirrored["output"].startswith("x" * MAX_TOOL_OUTPUT_BYTES)
    assert "[output truncated by omnigent:" in mirrored["output"]
    assert item["output"] == output
    assert json.loads(requests[1].content)["data"]["status"] == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["\x00", "界"])
async def test_oversized_event_is_saved_without_blocking_later_events(
    tmp_path: Path, text: str
) -> None:
    """JSON escaping and UTF-8 bytes count toward the limit, not characters."""
    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(request.content) > MAX_SESSION_EVENT_REQUEST_BYTES:
            raise httpx.ReadError("upload rejected", request=request)
        return httpx.Response(202)

    content = text * (MAX_SESSION_EVENT_REQUEST_BYTES // 3 + 1)
    item = {"role": "assistant", "content": [{"type": "output_text", "text": content}]}
    token = fwd._dead_letter_dir.set(tmp_path)
    locks_token = fwd._conversation_item_locks.set({})
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_1",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_1",
        ),
    )
    tracker = fwd._CodexElicitationTaskTracker()
    try:
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(receive)
        ) as client:
            assert not await asyncio.wait_for(
                fwd._post_external_item(
                    client,
                    "conv_x",
                    item_type="message",
                    item_data=item,
                    response_id="codex_turn_1",
                    source_id="thread_1:turn_1:item_1",
                ),
                timeout=2,
            )
            assert await fwd._post_external_item(
                client,
                "conv_x",
                item_type="message",
                item_data={"role": "assistant", "content": []},
                response_id="codex_turn_1",
                source_id="thread_1:turn_1:item_2",
            )
            await fwd._handle_event(
                client,
                session_id="conv_x",
                bridge_dir=tmp_path,
                event={
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread_1",
                        "turn": {
                            "id": "turn_1",
                            "status": "completed",
                            "items": [{"type": "agentMessage", "id": "item_2", "text": "done"}],
                        },
                    },
                },
                usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),
                elicitation_tracker=tracker,
                expected_thread_id="thread_1",
            )
    finally:
        await tracker.close()
        fwd._conversation_item_locks.reset(locks_token)
        fwd._dead_letter_dir.reset(token)

    assert len(requests) == 2
    assert json.loads(requests[0].content)["data"]["source_id"] == "thread_1:turn_1:item_2"
    assert json.loads(requests[1].content)["data"]["status"] == "idle"
    state = read_bridge_state(tmp_path)
    assert state is not None and state.active_turn_id is None
    records = (tmp_path / "dead_letter.jsonl").read_text().splitlines()
    assert len(records) == 1
    record = json.loads(records[0])
    assert record["payload"]["item_data"] == item
    assert "exceeds" in record["reason"]
    assert record["delivered_ambiguous"] is False
    assert record["http_status"] is None
    assert record["transport_error"] is None


@pytest.mark.asyncio
async def test_event_exactly_at_wire_limit_is_delivered() -> None:
    data = {"item_type": "message", "item_data": {"text": ""}, "source_id": "item_1"}
    payload = {"type": "external_conversation_item", "data": data}
    envelope_bytes = len(httpx.Request("POST", "http://test", json=payload).content)
    data["item_data"]["text"] = "x" * (MAX_SESSION_EVENT_REQUEST_BYTES - envelope_bytes)
    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(receive)
    ) as client:
        response = await fwd._post_session_event(
            client, "conv_x", event_type="external_conversation_item", data=data
        )

    assert response is not None and response.status_code == 202
    assert len(requests) == 1
    assert len(requests[0].content) == MAX_SESSION_EVENT_REQUEST_BYTES

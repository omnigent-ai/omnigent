"""A live runner must not replay accepted messages when its tunnel reconnects."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from omnigent.runner.app import _session_histories_ref
from omnigent.server.schemas import SessionInputConsumedEvent
from tests.runner.conftest import _runner_client, _ScriptedHarnessClient, _sse
from tests.runner.test_suppress_recovery_turn import (
    AGENT_ID,
    SESSION_ID,
    _build_sdk_app,
    _HistoryServerClient,
    _session_init_payload,
)


def _user(item_id: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": item_id}],
    }


class _Server(_HistoryServerClient):
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.read_started = asyncio.Event()
        self.release_read = asyncio.Event()
        self.release_read.set()

    async def get(self, url: str, **kwargs: Any) -> _HistoryServerClient._Resp:
        if not url.endswith(f"/sessions/{SESSION_ID}/items"):
            return self._Resp({})
        after = kwargs.get("params", {}).get("after")
        start = next((i + 1 for i, item in enumerate(self.items) if item["id"] == after), 0)
        page = copy.deepcopy(self.items[start:])
        self.read_started.set()
        await self.release_read.wait()
        return self._Resp({"data": page, "has_more": False})


async def _forward(client: Any, item_id: str, *, stream: bool = True) -> Any:
    return await client.post(
        f"/v1/sessions/{SESSION_ID}/events",
        params={"stream": str(stream).lower()},
        json={**_user(item_id), "agent_id": AGENT_ID, "persisted_item_id": item_id},
    )


async def _settle(app: Any) -> None:
    # Turn completion can schedule the next buffered turn.
    for _ in range(100):
        await asyncio.sleep(0.01)
        if SESSION_ID not in app.state.active_turns and not app.state.session_message_buffers.get(
            SESSION_ID
        ):
            return
    pytest.fail("runner did not finish its turns")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["idle", "failed"])
async def test_reconnect_reasserts_a_lost_sdk_terminal_status(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    server = _Server()
    app, _, harness = _build_sdk_app(server)
    if status == "failed":
        harness._sse_frames[-1] = _sse(
            {
                "type": "response.failed",
                "response": {
                    "id": "resp_1",
                    "error": {"code": "test_failure", "message": "The turn failed."},
                },
            }
        )
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json=_session_init_payload(suppress_recovery_turn=True))
        monkeypatch.setitem(_session_histories_ref, SESSION_ID, [])
        assert (await _forward(client, "accepted")).status_code == 200
        await _settle(app)

        # The old server consumed the terminal status before losing its tunnel.
        queue = app.state.session_event_queues[SESSION_ID]
        previous = []
        while not queue.empty():
            previous.append(queue.get_nowait())
        terminal = [event for event in previous if event["type"] == "session.status"][-1]
        assert terminal["status"] == status
        if status == "failed":
            assert terminal["error"]["message"] == "The turn failed."

        await app.state.catch_up_scan()
        replay = []
        while not queue.empty():
            replay.append(queue.get_nowait())
        assert terminal in replay
        assert len(harness.posted_bodies) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect", ["scan", "initialize", "both"])
async def test_reconnect_does_not_replay_a_turn_whose_reply_is_not_yet_persisted(
    monkeypatch: pytest.MonkeyPatch,
    reconnect: str,
) -> None:
    server = _Server()
    app, _, harness = _build_sdk_app(server)
    async with _runner_client(app) as client:
        assert (
            await client.post(
                "/v1/sessions", json=_session_init_payload(suppress_recovery_turn=True)
            )
        ).status_code == 201
        monkeypatch.setitem(_session_histories_ref, SESSION_ID, [])
        for item_id in ("first", "second"):
            server.items.append(_user(item_id))
            assert (await _forward(client, item_id)).status_code == 200
            if item_id == "first":
                server.items.append(
                    {
                        "id": "first-reply",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hi"}],
                    }
                )
        history = copy.deepcopy(_session_histories_ref[SESSION_ID])

        if reconnect in ("scan", "both"):
            await app.state.catch_up_scan()
            await _settle(app)
        if reconnect in ("initialize", "both"):
            assert (
                await client.post(
                    "/v1/sessions", json=_session_init_payload(suppress_recovery_turn=False)
                )
            ).status_code == 201
        await _settle(app)

        assert len(harness.posted_bodies) == 2
        assert _session_histories_ref[SESSION_ID] == history


@pytest.mark.asyncio
async def test_reconnect_rechecks_delivery_after_a_slow_history_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server()
    app, _, harness = _build_sdk_app(server)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json=_session_init_payload(suppress_recovery_turn=True))
        monkeypatch.setitem(_session_histories_ref, SESSION_ID, [])
        server.items.append(_user("racing"))
        server.read_started.clear()
        server.release_read.clear()
        scan = asyncio.create_task(app.state.catch_up_scan())
        try:
            await asyncio.wait_for(server.read_started.wait(), 2)
            assert (await _forward(client, "racing")).status_code == 200
            history = copy.deepcopy(_session_histories_ref[SESSION_ID])
        finally:
            server.release_read.set()
            await asyncio.wait_for(scan, 2)
        await _settle(app)

        assert len(harness.posted_bodies) == 1
        assert _session_histories_ref[SESSION_ID] == history


@pytest.mark.asyncio
async def test_reconnect_recovers_an_earlier_missed_message_and_ignores_its_late_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server()
    app, _, harness = _build_sdk_app(server)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json=_session_init_payload(suppress_recovery_turn=True))
        monkeypatch.setitem(_session_histories_ref, SESSION_ID, [])
        server.items.extend([_user("missed"), _user("delivered")])
        assert (await _forward(client, "delivered")).status_code == 200

        await asyncio.gather(app.state.catch_up_scan(), app.state.catch_up_scan())
        await _settle(app)
        assert len(harness.posted_bodies) == 2

        assert (await _forward(client, "missed")).status_code == 202
        await _settle(app)
        assert len(harness.posted_bodies) == 2
        users = [item for item in _session_histories_ref[SESSION_ID] if item.get("role") == "user"]
        assert [item["content"] for item in users] == [
            _user(item_id)["content"] for item_id in ("delivered", "missed")
        ]
        events = []
        queue = app.state.session_event_queues[SESSION_ID]
        while not queue.empty():
            events.append(queue.get_nowait())
        receipts = [
            SessionInputConsumedEvent.model_validate(event).data
            for event in events
            if event["type"] == "session.input.consumed"
        ]
        # Only newly recovered input needs a receipt; don't replay older receipts.
        assert [receipt.item_id for receipt in receipts] == ["missed"]
        assert receipts[0].data["content"] == _user("missed")["content"]


@pytest.mark.asyncio
async def test_reconnect_buffers_missed_work_while_another_turn_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    original = _ScriptedHarnessClient._StreamHandle.aiter_text

    async def gated_stream(handle: Any) -> Any:
        started.set()
        await release.wait()
        async for frame in original(handle):
            yield frame

    monkeypatch.setattr(_ScriptedHarnessClient._StreamHandle, "aiter_text", gated_stream)
    server = _Server()
    app, _, harness = _build_sdk_app(server)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json=_session_init_payload(suppress_recovery_turn=True))
        monkeypatch.setitem(_session_histories_ref, SESSION_ID, [])
        server.items.append(_user("running"))
        assert (await _forward(client, "running", stream=False)).status_code == 202
        await asyncio.wait_for(started.wait(), 2)
        try:
            server.items.append(_user("missed"))
            await app.state.catch_up_scan()
            assert len(harness.posted_bodies) == 1
        finally:
            release.set()
            await _settle(app)

        assert len(harness.posted_bodies) == 2
        users = [item for item in _session_histories_ref[SESSION_ID] if item.get("role") == "user"]
        assert [item["content"] for item in users] == [
            _user(item_id)["content"] for item_id in ("running", "missed")
        ]

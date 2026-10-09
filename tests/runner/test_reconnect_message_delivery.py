"""A live runner must not replay accepted messages when its tunnel reconnects."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import httpx
import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import session_history
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
@pytest.mark.parametrize("cold_history", [False, True])
@pytest.mark.parametrize("legacy_init", [False, True])
async def test_resent_message_is_answered_as_accepted_without_another_turn(
    monkeypatch: pytest.MonkeyPatch, cold_history: bool, legacy_init: bool
) -> None:
    server = _Server()
    app, _, harness = _build_sdk_app(server)
    async with _runner_client(app) as client:
        init = _session_init_payload(suppress_recovery_turn=True)
        if legacy_init:
            init.pop("session_init")
        await client.post("/v1/sessions", json=init)
        if cold_history:
            monkeypatch.delitem(_session_histories_ref, SESSION_ID, raising=False)
        else:
            monkeypatch.setitem(_session_histories_ref, SESSION_ID, [])
        server.items.append(_user("accepted"))
        assert (await _forward(client, "accepted")).status_code == 200
        await _settle(app)
        queue = app.state.session_event_queues[SESSION_ID]
        while not queue.empty():
            queue.get_nowait()

        # A browser resends when the answer is lost; reconnect scans run around it.
        await app.state.catch_up_scan()
        resent = await _forward(client, "accepted")
        await app.state.catch_up_scan()
        await _settle(app)

        assert resent.status_code == 202
        assert resent.json()["detail"] == "Message already accepted."
        assert len(harness.posted_bodies) == 1
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert not any(event["type"] == "session.input.consumed" for event in events)


class _HeldReadsServer(_Server):
    """Holds the first two history reads until the test releases each one."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = [(asyncio.Event(), asyncio.Event()) for _ in range(2)]
        self._read_count = 0

    async def get(self, url: str, **kwargs: Any) -> _HistoryServerClient._Resp:
        if url.endswith(f"/sessions/{SESSION_ID}/items") and self._read_count < len(self.reads):
            started, release = self.reads[self._read_count]
            self._read_count += 1
            started.set()
            await release.wait()
        return await super().get(url, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("forward_reads_history", [False, True])
async def test_forward_of_a_prompt_that_a_recovery_turn_replays_runs_it_once(
    monkeypatch: pytest.MonkeyPatch, forward_reads_history: bool
) -> None:
    monkeypatch.delitem(_session_histories_ref, SESSION_ID, raising=False)
    server = _HeldReadsServer()
    server.items = [_user("saved")]
    app, _, harness = _build_sdk_app(server)
    async with _runner_client(app) as client:
        init = asyncio.create_task(
            client.post("/v1/sessions", json=_session_init_payload(suppress_recovery_turn=False))
        )
        await asyncio.wait_for(server.reads[0][0].wait(), 2)
        if forward_reads_history:
            # The forward finds no warm history and reads it while init is replaying.
            forward = asyncio.create_task(_forward(client, "saved"))
            await asyncio.wait_for(server.reads[1][0].wait(), 2)
            server.reads[0][1].set()
            assert (await asyncio.wait_for(init, 2)).status_code == 201
            server.reads[1][1].set()
            response = await asyncio.wait_for(forward, 2)
        else:
            server.reads[0][1].set()
            server.reads[1][1].set()
            assert (await asyncio.wait_for(init, 2)).status_code == 201
            response = await _forward(client, "saved")
        await _settle(app)

    assert response.status_code == 202
    assert response.json()["detail"] == "Message already accepted."
    assert len(harness.posted_bodies) == 1


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


@pytest.mark.asyncio
async def test_failed_history_resolution_does_not_claim_an_unaccepted_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server()
    app, _, harness = _build_sdk_app(server)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json=_session_init_payload(suppress_recovery_turn=True))
        monkeypatch.delitem(_session_histories_ref, SESSION_ID, raising=False)
        server.items = [_user("previous"), _user("retryable")]
        fail = True

        async def resolve_history(content: Any, **kwargs: Any) -> Any:
            nonlocal fail
            if fail:
                fail = False
                raise httpx.ConnectError("history attachment connection lost")
            return content

        monkeypatch.setattr(session_history, "_resolve_forwarded_message_content", resolve_history)
        with pytest.raises(httpx.ConnectError, match="history attachment"):
            await _forward(client, "retryable")
        assert not harness.posted_bodies

        assert (await _forward(client, "retryable")).status_code == 200
        await _settle(app)
        assert len(harness.posted_bodies) == 1
        users = [item for item in _session_histories_ref[SESSION_ID] if item.get("role") == "user"]
        assert [item["content"] for item in users] == [
            _user(item_id)["content"] for item_id in ("previous", "retryable")
        ]


@pytest.mark.asyncio
async def test_attachment_failure_does_not_strand_already_recovered_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server()
    app, _, harness = _build_sdk_app(server)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json=_session_init_payload(suppress_recovery_turn=True))
        monkeypatch.setitem(_session_histories_ref, SESSION_ID, [])
        server.items = [_user("ready"), _user("attachment")]
        fail = True

        async def resolve_content(content: Any, **kwargs: Any) -> Any:
            if fail and content == _user("attachment")["content"]:
                raise httpx.ConnectError("attachment connection lost")
            return content

        monkeypatch.setattr(runner_app, "_resolve_forwarded_message_content", resolve_content)
        await app.state.catch_up_scan()
        await _settle(app)
        assert len(harness.posted_bodies) == 1
        assert not app.state.session_message_buffers.get(SESSION_ID)

        fail = False
        await app.state.catch_up_scan()
        await _settle(app)
        assert len(harness.posted_bodies) == 2
        users = [item for item in _session_histories_ref[SESSION_ID] if item.get("role") == "user"]
        assert [item["content"] for item in users] == [
            _user(item_id)["content"] for item_id in ("ready", "attachment")
        ]


@pytest.mark.asyncio
async def test_reconnect_recovers_a_missed_prompt_and_skill_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server()
    app, _, harness = _build_sdk_app(server)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json=_session_init_payload(suppress_recovery_turn=True))
        monkeypatch.setitem(_session_histories_ref, SESSION_ID, [])
        server.items = [_user("missed"), {**_user("skill"), "is_meta": True}]
        await app.state.catch_up_scan()
        await _settle(app)
        assert len(harness.posted_bodies) == 1
        users = [item for item in _session_histories_ref[SESSION_ID] if item.get("role") == "user"]
        assert [item["content"] for item in users] == [
            _user(item_id)["content"] for item_id in ("missed", "skill")
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed_ack", [False, True])
async def test_reconnect_after_stop_does_not_execute_the_interruption_marker(
    monkeypatch: pytest.MonkeyPatch, delayed_ack: bool
) -> None:
    started, persisted, release_ack = asyncio.Event(), asyncio.Event(), asyncio.Event()
    if not delayed_ack:
        release_ack.set()

    class CancellationServer(_Server):
        async def post(self, url: str, **kwargs: Any) -> _HistoryServerClient._Resp:
            event = kwargs.get("json", {})
            if event.get("type") != "external_conversation_item":
                return self._Resp({})
            data = event["data"]
            item = {
                "id": "interruption-marker",
                "type": data["item_type"],
                **data["item_data"],
                "response_id": data["response_id"],
            }
            self.items.append(item)
            persisted.set()
            await release_ack.wait()
            return self._Resp({"queued": False, "item_id": item["id"]})

    first = True
    original = _ScriptedHarnessClient._StreamHandle.aiter_text

    async def pause_first_turn(handle: Any) -> Any:
        nonlocal first
        if first:
            first = False
            started.set()
            await asyncio.Event().wait()
        async for frame in original(handle):
            yield frame

    monkeypatch.setattr(_ScriptedHarnessClient._StreamHandle, "aiter_text", pause_first_turn)
    server = CancellationServer()
    app, _, harness = _build_sdk_app(server)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json=_session_init_payload(suppress_recovery_turn=True))
        monkeypatch.setitem(_session_histories_ref, SESSION_ID, [])
        server.items.append(_user("abandoned"))
        assert (await _forward(client, "abandoned", stream=False)).status_code == 202
        await asyncio.wait_for(started.wait(), 2)
        try:
            response = await client.post(
                f"/v1/sessions/{SESSION_ID}/events", json={"type": "stop_session"}
            )
            assert response.status_code == 204
            await _settle(app)
            await asyncio.wait_for(persisted.wait(), 2)
            marker = server.items[-1]
            assert marker["role"] == "user"
            assert marker["content"][0]["text"].startswith("[System: interrupted]")

            server.items.append(_user("next-prompt"))
            assert (await _forward(client, "next-prompt")).status_code == 200
            await _settle(app)
            server.items.append(
                {"id": "reply", "type": "message", "role": "assistant", "content": []}
            )
            history = copy.deepcopy(_session_histories_ref[SESSION_ID])
            queue = app.state.session_event_queues[SESSION_ID]
            while not queue.empty():
                queue.get_nowait()

            await app.state.catch_up_scan()
            await _settle(app)
            assert len(harness.posted_bodies) == 2, "reconnect executed an internal Stop marker"
            assert _session_histories_ref[SESSION_ID] == history
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            assert not [e for e in events if e["type"] == "session.input.consumed"]
        finally:
            release_ack.set()

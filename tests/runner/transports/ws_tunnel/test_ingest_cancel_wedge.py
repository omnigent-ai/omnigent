"""E2E: a cancelled queued claude-sdk /compact must not wedge the conversation.

The runner serializes per-conversation message intake through an ingest gate.
If a queued request's dispatch task is cancelled (a ``request.cancel`` tunnel
frame) while it waits for its turn, the gate must drop the cancelled waiter;
a gate that leaks the cancelled request's place blocks every later message for
that conversation forever.

This drives the real runner ASGI app through the real WS-tunnel serve loop:
``_handle_tunnel_frame`` dispatches each ``RequestFrame`` as an ASGI task and
cancels it on a ``RequestCancelFrame`` -- the exact production cancellation
path. The only injected fault is a slow attachment fetch on the first message,
which holds the ingest slot. Everything below the injection is unmodified
product code.

Journey:

1. Message A arrives carrying an unresolved ``file_id`` attachment. Its handler
   enters the ingest gate, starts serving, and parks awaiting the (gated)
   attachment fetch -- holding the conversation slot.
2. A ``/compact`` request arrives and parks in the ingest wait behind message A.
3. A ``request.cancel`` frame for the compact cancels its dispatch task while it
   is parked in the wait.
4. The attachment fetch is released; message A finishes and releases the gate.
5. Message B arrives and must still be processed.

On a buggy build the cancelled compact's place in the gate is never served or
released, so message B never leaves the ingest wait and the assertion that B
completes times out. On a fixed build message B is processed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.transports.ws_tunnel.frames import (
    RequestCancelFrame,
    RequestFrame,
    ResponseHeadFrame,
    decode_frame,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.serve import _handle_tunnel_frame
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient

_SESSION_ID = "aa11bb22cc33dd44ee55ff6677889900"
_AGENT_ID = "0099887766554433221100ffeeddccbb"


class _EmptyResponse:
    """Stub 200 response with an empty JSON body."""

    status_code = 200
    headers: dict[str, str] = {}
    content = b""

    def json(self) -> dict[str, Any]:
        return {}

    def raise_for_status(self) -> None:
        return None


class _GatedAttachmentServerClient:
    """Server client that blocks the first attachment fetch until released.

    Every non-file call returns an empty 200 (session init, history, etc.).
    A ``/resources/files/`` GET signals that message A has entered attachment
    resolution -- i.e. it now holds the ingest slot -- and then blocks until
    the test releases it, at which point it fails so resolution falls through
    and message A proceeds to advance the ingest sequence.
    """

    def __init__(self) -> None:
        self.file_fetch_started = asyncio.Event()
        self.release_file_fetch = asyncio.Event()

    async def get(self, url: str, **kwargs: Any) -> _EmptyResponse:
        del kwargs
        if "/resources/files/" in url:
            self.file_fetch_started.set()
            await self.release_file_fetch.wait()
            raise httpx.ConnectError("attachment fetch aborted")
        return _EmptyResponse()

    async def post(self, url: str, **kwargs: Any) -> _EmptyResponse:
        del url, kwargs
        return _EmptyResponse()

    async def patch(self, url: str, **kwargs: Any) -> _EmptyResponse:
        del url, kwargs
        return _EmptyResponse()


def _request_frame(req_id: str, body: dict[str, Any]) -> str:
    return encode_frame(
        RequestFrame(
            id=req_id,
            method="POST",
            path=f"/v1/sessions/{_SESSION_ID}/events",
            headers=[["content-type", "application/json"]],
            body=json.dumps(body),
        )
    )


async def _drain_scheduler(iterations: int = 50) -> None:
    """Run every currently ready callback forward *iterations* times.

    The queued compact's only blocking ``await`` is the ingest-gate wait.
    Repeatedly yielding with ``sleep(0)`` advances it (and the ASGI hops before
    it) to that park without a wall-clock guess, since nothing on the path
    sleeps on a timer.
    """
    for _ in range(iterations):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancelled_queued_compact_does_not_wedge_later_messages() -> None:
    spec = AgentSpec(
        spec_version=1,
        name="compact-wedge-agent",
        executor=ExecutorSpec(config={"harness": "claude-sdk"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    server_client = _GatedAttachmentServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        init = await client.post(
            "/v1/sessions",
            json={"session_id": _SESSION_ID, "agent_id": _AGENT_ID},
        )
        assert init.status_code == 201, init.text

    responses: dict[str, list[Any]] = {}

    async def send_text(text: str) -> None:
        frame = decode_frame(text)
        responses.setdefault(frame.id, []).append(frame)

    dispatch_tasks: dict[str, asyncio.Task[None]] = {}
    ws_channels: dict[str, Any] = {}

    async def feed(raw: str) -> None:
        await _handle_tunnel_frame(app, raw, send_text, dispatch_tasks, ws_channels)

    try:
        # 1. Message A holds the slot on a slow attachment fetch.
        await feed(
            _request_frame(
                "req-msg-a",
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "here is a file"},
                        {"type": "input_image", "file_id": "file_slow_attachment"},
                    ],
                },
            )
        )
        await asyncio.wait_for(server_client.file_fetch_started.wait(), timeout=5.0)

        # 2. A /compact queues behind message A and parks in the ingest wait.
        await feed(_request_frame("req-compact", {"type": "compact"}))
        await _drain_scheduler()
        assert not dispatch_tasks["req-compact"].done(), (
            "compact should be parked in the ingest wait behind message A"
        )
        assert "req-compact" not in responses, (
            "compact must not have produced a response while parked"
        )

        # 3. request.cancel cancels the compact while it is parked in the wait.
        await feed(encode_frame(RequestCancelFrame(id="req-compact")))
        with pytest.raises(asyncio.CancelledError):
            await dispatch_tasks["req-compact"]

        # 4. Message A finishes; now_serving advances to the leaked number.
        server_client.release_file_fetch.set()
        await asyncio.wait_for(dispatch_tasks["req-msg-a"], timeout=5.0)

        # 5. A subsequent message must still be processed.
        await feed(
            _request_frame(
                "req-msg-b",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "are you there?"}],
                },
            )
        )
        try:
            await asyncio.wait_for(dispatch_tasks["req-msg-b"], timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail(
                "subsequent message wedged: the cancelled queued /compact kept its "
                "place in the ingest gate, so message B never left the ingest wait"
            )

        heads = [f for f in responses.get("req-msg-b", []) if isinstance(f, ResponseHeadFrame)]
        assert heads and heads[0].status < 500, (
            f"message B should have been accepted; got frames {responses.get('req-msg-b')}"
        )
    finally:
        server_client.release_file_fetch.set()
        pending = list(dispatch_tasks.values())
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(BaseException):
                await task

"""A cancelled queued runner request must not wedge the conversation.

The runner serializes per-conversation intake through a FIFO ingest gate. While
one message holds the gate, later requests queue behind it. If a queued request
is cancelled by a ``request.cancel`` tunnel frame while it waits, the gate must
drop it cleanly so that, once the holder finishes, following messages still pass
through. This test parks a request at the gate, cancels it mid-wait, and asserts
a later message is still accepted.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

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
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient, _sse

_SESSION = "ea532aed7642ec833ab31a5649c3495b"
_FILE_ID = "c531a3c97ad5fca15709d73d1f734a0c"


class _GatedFileServerClient:
    """Server client whose gated metadata GET parks the message that carries a
    ``file_id`` block, so that message holds the ingest gate until released."""

    def __init__(self) -> None:
        self.meta_fetch_started = asyncio.Event()
        self.release = asyncio.Event()

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if url.endswith("/content"):
            return _GatedFileServerClient._Resp(body=b"png-bytes")
        self.meta_fetch_started.set()
        await self.release.wait()
        return _GatedFileServerClient._Resp(
            payload={"id": _FILE_ID, "filename": "a.png", "content_type": "image/png"}
        )

    class _Resp:
        def __init__(self, *, body: bytes = b"", payload: dict[str, Any] | None = None) -> None:
            self.content = body
            self._payload = payload or {}
            self.headers = {"content-type": self._payload.get("content_type", "image/png")}
            self.status_code = 200

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            return None


def _message_frame(req_id: str, content: list[dict[str, Any]]) -> RequestFrame:
    return RequestFrame(
        id=req_id,
        method="POST",
        path=f"/v1/sessions/{_SESSION}/events",
        headers=[["content-type", "application/json"]],
        body=json.dumps({"type": "message", "role": "user", "content": content}),
    )


@pytest.mark.asyncio
async def test_cancelled_queued_request_does_not_wedge_later_messages() -> None:
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )
    server = _GatedFileServerClient()
    app = create_runner_app(
        process_manager=_FakeProcessManager(hc),  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
    )

    heads: dict[str, int] = {}
    dispatch_tasks: dict[str, asyncio.Task[None]] = {}
    held: list[asyncio.Task[None]] = []

    def _queued_behind_holder() -> bool:
        # Once the queued request parks, it waits on the still-held gate lock.
        lock = getattr(app.state, "ingest_locks", {}).get(_SESSION)
        waiters = getattr(lock, "_waiters", None)
        return bool(lock and lock.locked() and waiters)

    async def send_text(data: str) -> None:
        frame = decode_frame(data)
        if isinstance(frame, ResponseHeadFrame):
            heads[frame.id] = frame.status

    async def feed(frame: Any) -> asyncio.Task[None] | None:
        await _handle_tunnel_frame(app, encode_frame(frame), send_text, dispatch_tasks, {})
        # A cancelled task is popped from dispatch_tasks by its done callback, so
        # grab the handle now while it is still registered.
        task = dispatch_tasks.get(getattr(frame, "id", ""))
        if task is not None:
            held.append(task)
        return task

    async with app.router.lifespan_context(app):
        try:
            # Message A carries a gated file_id: it takes the ingest gate and
            # parks inside content resolution, holding the gate open.
            task_a = await feed(
                _message_frame(
                    "reqA",
                    [
                        {"type": "input_image", "file_id": _FILE_ID, "filename": "a.png"},
                        {"type": "input_text", "text": "hold-the-slot"},
                    ],
                )
            )
            assert task_a is not None
            await asyncio.wait_for(server.meta_fetch_started.wait(), timeout=5.0)

            # A second request queues behind A, waiting to enter the ingest gate.
            task_queued = await feed(
                _message_frame("reqQueued", [{"type": "input_text", "text": "queued"}])
            )
            assert task_queued is not None
            # Wait until it parks behind A so the cancel hits the queued-at-gate
            # path, not a cancel before the request reaches the gate. The lock is
            # observable only on the fixed runner, so the assert below is guarded.
            for _ in range(100):
                if _queued_behind_holder():
                    break
                await asyncio.sleep(0.02)
            if hasattr(app.state, "ingest_locks"):
                assert _queued_behind_holder(), (
                    "queued request never parked behind the ingest gate"
                )
            assert not task_queued.done()
            assert "reqQueued" not in heads

            # The server cancels the parked request mid-wait.
            await feed(RequestCancelFrame(id="reqQueued"))
            for _ in range(100):
                if task_queued.cancelled():
                    break
                await asyncio.sleep(0.02)
            assert task_queued.cancelled()

            # Release A so it finishes and frees the gate; later messages must still pass.
            server.release.set()
            await asyncio.wait_for(asyncio.shield(task_a), timeout=5.0)
            assert heads.get("reqA") == 202

            # A subsequent message must still be accepted.
            await feed(_message_frame("reqLater", [{"type": "input_text", "text": "later"}]))
            for _ in range(50):
                if "reqLater" in heads:
                    break
                await asyncio.sleep(0.1)

            assert heads.get("reqLater") == 202, (
                "subsequent message wedged: the cancelled queued request left the "
                "ingest gate stuck, so later messages never pass through"
            )
        finally:
            for task in held:
                task.cancel()
            await asyncio.gather(*held, return_exceptions=True)

"""Bounded delivery of native-forwarder events on the runner's existing tunnel."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import httpx
from websockets.exceptions import ConnectionClosed

from omnigent.runner.transports.ws_tunnel.frames import (
    EventAckFrame,
    EventBatchFrame,
    encode_frame,
)

_logger = logging.getLogger(__name__)
_EVENTS_PATH = re.compile(r"/v1/sessions/([^/]+)/events$")
_MAX_EVENTS = 32
_MAX_BATCH_BYTES = 256 * 1024


class TunnelIngestUnsupported(Exception):
    """The connected server does not support event ingestion on this tunnel."""


class RunnerEventDispatcher:
    """Queue source-replayable events; retain them until a server acknowledgement.

    Each forwarder awaits delivery before advancing its on-disk source cursor.
    A disconnected socket cancels only the current attempt: the worker waits
    for the next ready tunnel generation and retransmits source-keyed items.
    """

    def __init__(self) -> None:
        self._state = "disconnected"
        self._state_changed = asyncio.Event()
        self._ever_ready = False
        self._send: Callable[[str], Awaitable[None]] | None = None
        self._pending: dict[str, asyncio.Future[EventAckFrame]] = {}
        self._queue: asyncio.Queue[
            tuple[str, list[dict[str, Any]], asyncio.Future[EventAckFrame]]
        ] = asyncio.Queue(maxsize=32)
        self._locks: dict[str, asyncio.Lock] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._outstanding = 0

    @property
    def has_pending(self) -> bool:
        """Keep the runner alive while durable source events await an ACK."""
        return self._outstanding > 0

    def _set_state(self, state: str) -> None:
        self._state = state
        changed = self._state_changed
        self._state_changed = asyncio.Event()
        changed.set()

    def connected(self, send: Callable[[str], Awaitable[None]]) -> None:
        """Start capability negotiation for this connection generation."""
        self._send = send
        self._set_state("negotiating")

    async def _mode(self, *, initial_fallback: bool, preview: bool = False) -> bool:
        """Return tunnel support for this connection, waiting through outages."""
        while True:
            state = self._state
            changed = self._state_changed
            if state == "ready":
                return True
            if state == "unsupported":
                return False
            if state == "negotiating":
                try:
                    await asyncio.wait_for(changed.wait(), timeout=0.5)
                except TimeoutError:
                    if self._state == "negotiating":
                        self._set_state("unsupported")
                continue
            if initial_fallback and not self._ever_ready:
                try:
                    await asyncio.wait_for(changed.wait(), timeout=0.5)
                except TimeoutError:
                    return False
            elif preview:
                await asyncio.wait_for(changed.wait(), timeout=1.0)
            else:
                await changed.wait()

    async def wait_for_first_ready(self) -> bool:
        """Use HTTP on an old server, but retain work through a real outage."""
        if self._ever_ready and self._state == "disconnected":
            return True
        return await self._mode(initial_fallback=True)

    def ready(self, send: Callable[[str], Awaitable[None]]) -> None:
        """Enable delivery only after the current tunnel receives event.ready."""
        self._send = send
        self._ever_ready = True
        self._set_state("ready")

    def disconnected(self) -> None:
        """Wake pending attempts; source events remain queued for replay."""
        self._send = None
        self._set_state("disconnected")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("runner tunnel disconnected"))
        self._pending.clear()

    def acknowledge(self, ack: EventAckFrame) -> None:
        """Resolve only an acknowledgement from the current tunnel generation."""
        future = self._pending.pop(ack.id, None)
        if future is not None and not future.done():
            future.set_result(ack)

    async def submit(self, session_id: str, events: list[dict[str, Any]]) -> EventAckFrame:
        """Backpressure the producer until its ordered batch is acknowledged."""
        if not self._workers:
            self._workers = [
                asyncio.create_task(self._worker(), name=f"runner-event-worker-{i}")
                for i in range(2)
            ]
        result: asyncio.Future[EventAckFrame] = asyncio.get_running_loop().create_future()
        self._outstanding += 1
        try:
            await self._queue.put((session_id, events, result))
            return await result
        finally:
            self._outstanding -= 1
            if not result.done():
                result.cancel()

    async def _worker(self) -> None:
        while True:
            session_id, events, result = await self._queue.get()
            try:
                if result.cancelled():
                    continue
                async with self._locks.setdefault(session_id, asyncio.Lock()):
                    delivery = asyncio.create_task(self._deliver(session_id, events))

                    def stop_abandoned(
                        future: asyncio.Future[EventAckFrame],
                        delivery_task: asyncio.Task[EventAckFrame] = delivery,
                    ) -> None:
                        if future.cancelled():
                            delivery_task.cancel()

                    result.add_done_callback(stop_abandoned)
                    try:
                        ack = await delivery
                    finally:
                        result.remove_done_callback(stop_abandoned)
                if not result.done():
                    result.set_result(ack)
            except asyncio.CancelledError:
                if not result.cancelled():
                    raise
            except Exception as exc:  # noqa: BLE001 - propagate to the waiting producer.
                if not result.done():
                    result.set_exception(exc)
            finally:
                self._queue.task_done()

    async def _deliver(self, session_id: str, events: list[dict[str, Any]]) -> EventAckFrame:
        remaining = events
        preview = all(event["type"] == "external_output_text_delta" for event in events)
        while True:
            if not await self._mode(initial_fallback=False, preview=preview):
                raise TunnelIngestUnsupported
            send = self._send
            if send is None:
                continue
            batch_id = uuid.uuid4().hex
            future: asyncio.Future[EventAckFrame] = asyncio.get_running_loop().create_future()
            self._pending[batch_id] = future
            try:
                await send(encode_frame(EventBatchFrame(batch_id, session_id, remaining)))
                ack = await asyncio.wait_for(future, timeout=30.0)
            except (ConnectionError, ConnectionClosed, OSError, TimeoutError):
                # An ACK can disappear after the server committed the item.
                # All durable events admitted here carry a stable source_id.
                if preview:
                    raise
                await asyncio.sleep(0.25)
                continue
            finally:
                self._pending.pop(batch_id, None)
            if ack.applied < 0 or ack.applied > len(remaining):
                raise ValueError("invalid event acknowledgement")
            remaining = remaining[ack.applied :]
            if not remaining:
                return EventAckFrame(ack.id, len(events))
            if not ack.retryable:
                return EventAckFrame(ack.id, len(events) - len(remaining), ack.error)
            if preview:
                raise ConnectionError("preview backpressure")
            await asyncio.sleep(0.25)


class TunnelEventClient(httpx.AsyncClient):
    """Use the tunnel for supported native events; keep every other HTTP RPC."""

    def __init__(self, *, event_dispatcher: RunnerEventDispatcher, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._event_dispatcher = event_dispatcher

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        url = args[0] if args else kwargs.get("url")
        if not isinstance(url, (str, httpx.URL)):
            return await super().post(*args, **kwargs)
        match = _EVENTS_PATH.search(urlsplit(str(url)).path)
        if match is None:
            return await super().post(*args, **kwargs)
        value: object = kwargs.get("json")
        if value is None and isinstance(kwargs.get("content"), bytes):
            try:
                value = json.loads(kwargs["content"])
            except ValueError:
                return await super().post(*args, **kwargs)
        if isinstance(value, dict):
            events = [value]
        elif isinstance(value, list) and value and all(isinstance(e, dict) for e in value):
            events = cast("list[dict[str, Any]]", value)
        else:
            return await super().post(*args, **kwargs)
        if not all(
            event.get("type") == "external_output_text_delta"
            or (
                event.get("type") == "external_conversation_item"
                and isinstance(event.get("data"), dict)
                and isinstance(event["data"].get("source_id"), str)
                and bool(event["data"]["source_id"])
            )
            for event in events
        ):
            return await super().post(*args, **kwargs)
        # Child-history callers inspect each item_id in the HTTP batch
        # acknowledgement; even a one-entry array needs a list response.
        if (
            (
                isinstance(value, list)
                and any(event.get("type") == "external_conversation_item" for event in events)
            )
            or len(events) > _MAX_EVENTS
            or len(encode_frame(EventBatchFrame("", "", events)).encode("utf-8"))
            > _MAX_BATCH_BYTES
        ):
            return await super().post(*args, **kwargs)
        if not await self._event_dispatcher.wait_for_first_ready():
            return await super().post(*args, **kwargs)
        request = self.build_request("POST", url)
        try:
            submit = self._event_dispatcher.submit(unquote(match.group(1)), events)
            if all(event["type"] == "external_output_text_delta" for event in events):
                ack = await asyncio.wait_for(submit, timeout=2.0)
            else:
                ack = await submit
        except TunnelIngestUnsupported:
            return await super().post(*args, **kwargs)
        except (ConnectionError, TimeoutError):
            # Preview is best-effort. Final transcript items retain their
            # source cursor and will be retried after the tunnel returns.
            _logger.debug("Native event preview could not reach the server")
            return await self._synthetic_response(503, request=request)
        if ack.applied == len(events):
            return await self._synthetic_response(202, request=request, json={"queued": False})
        return await self._synthetic_response(
            503 if ack.retryable else 422,
            json={"detail": ack.error or "event batch was not accepted"},
            request=request,
        )

    async def _synthetic_response(
        self, status: int, *, request: httpx.Request, json: object | None = None
    ) -> httpx.Response:
        """Run HTTP-compatible response hooks for successful tunnel progress."""
        response = httpx.Response(status, request=request, json=json)
        for hook in self.event_hooks.get("response", []):
            await hook(response)
        return response

"""Server-side ``WSTunnelTransport`` — httpx transport that tunnels
HTTP through a runner's WebSocket (Phase 4).

Per ``designs/RUNNER.md`` §3 "Sketch of the adapters", this is an
``httpx.AsyncBaseTransport`` subclass. Every existing call site
that uses ``httpx.AsyncClient`` keeps working unchanged — only the
transport object handed to the client differs.

Wire flow per request:
1. Allocate a fresh ``req_id`` (uuid4 hex).
2. Open reassembly state in the registry.
3. Send a :class:`RequestFrame` over the runner's WebSocket.
4. Await the :class:`ResponseHeadFrame` for status + headers.
5. Stream :class:`ResponseBodyFrame` chunks until
   :class:`ResponseEndFrame` or session abort.
6. Close the request in the registry.

If the runner is offline (no session in registry) → raise
``httpx.ConnectError``. If the tunnel closes mid-request → the
abort propagates as a ``ConnectionError`` from the body iterator.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable
from typing import cast

import httpx
from httpx._content import AsyncIteratorByteStream

from omnigent.runner.transports.ws_tunnel.binary_requests import (
    BINARY_REQUEST_BODY_CAPABILITY,
    encode_binary_request,
)
from omnigent.runner.transports.ws_tunnel.frames import (
    RequestCancelFrame,
    RequestFrame,
    ResponseBodyFrame,
    decode_body,
    encode_body,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.registry import RequestState, TunnelRegistry
from omnigent.server.server_config import runner_tunnel_binary_request_body
from omnigent.util.tunnel_limits import RUNNER_TUNNEL_MAX_MESSAGE_BYTES


async def _aclose_if_available(stream: object) -> None:
    aclose = getattr(stream, "aclose", None)
    if callable(aclose):
        await cast(Awaitable[None], aclose())


async def _aclose_request_stream(stream: httpx.AsyncByteStream) -> None:
    try:
        if type(stream).aclose is AsyncIteratorByteStream.aclose:
            # HTTPX's wrapper does not close the async iterator it wraps.
            await _aclose_if_available(cast(AsyncIteratorByteStream, stream)._stream)
    finally:
        await _aclose_if_available(stream)


class _TunneledByteStream(httpx.AsyncByteStream):
    """Adapts the registry's body queue into an ``httpx.AsyncByteStream``."""

    def __init__(
        self,
        registry: TunnelRegistry,
        runner_id: str,
        req_id: str,
        state: RequestState,
    ) -> None:
        self._registry = registry
        self._runner_id = runner_id
        self._req_id = req_id
        self._state = state

    async def __aiter__(self) -> AsyncIterator[bytes]:
        state = self._state
        try:
            while True:
                item = await state.body_queue.get()
                if state.aborted_with is not None:
                    raise state.aborted_with
                if item is None:
                    # Sentinel: end-event signalled, no more chunks.
                    break
                # Mypy/runtime: item must be a ResponseBodyFrame here.
                if isinstance(item, ResponseBodyFrame):
                    yield decode_body(item.body, item.encoding)
        finally:
            self._registry.close_request(
                self._runner_id,
                self._req_id,
                session=state.session,
            )

    async def aclose(self) -> None:
        # Close the request from the caller side — typically called
        # when the consumer's ``async with`` exits early (e.g. SSE
        # client disconnect). The transport translates this into a
        # request.cancel frame so the runner aborts.
        state = self._state
        try:
            if self._registry.request_is_open(state.session, self._req_id):
                with contextlib.suppress(Exception):
                    await self._registry.send_text(
                        state.session,
                        encode_frame(
                            RequestCancelFrame(id=self._req_id, reason="client_disconnected")
                        ),
                    )
        finally:
            self._registry.close_request(
                self._runner_id,
                self._req_id,
                session=state.session,
            )


class WSTunnelTransport(httpx.AsyncBaseTransport):
    """httpx transport that tunnels each request through a runner WebSocket.

    Construct one transport per (registry, runner_id) pair (or share
    one via a thin lookup that resolves runner_id per request — that's
    a higher-level routing concern, not this transport's).

    :param registry: The :class:`TunnelRegistry` that owns the runner's
        live WebSocket and reassembly state.
    :param runner_id: Which runner this transport routes to.

    Set ``extensions={"runner_tunnel_binary_request_body": True}`` on an
    httpx request to opt in. Capable runners receive raw bytes when the server
    kill switch is enabled; otherwise the existing JSON encoding is used.
    At most two binary envelopes are admitted per connection; JSON fallbacks
    do not consume slots. Excess binary callers get ``httpx.PoolTimeout`` and
    may retry after an earlier runner response ends. Admission lasts until the runner's terminal
    response or session retirement, even if the caller cancels or closes early.
    Caller cancellation/timeouts before headers also send ``request.cancel``
    for enqueued opted-in requests. Writing cancel does not release admission.
    ``BINARY_REQUEST_DISPATCH_TIMEOUT_S`` bounds binary runner work to 150s.
    This exceeds the upload relay's longest deadline (finalize: 120s), allowing
    the relay to return 504 first. An ASGI handler that suppresses
    ``CancelledError`` keeps its slot until tunnel teardown. The existing ping
    keepalive retires an unresponsive runner; healthy pongs alone do not detect
    a stuck handler.
    An over-limit binary envelope raises ``httpx.LocalProtocolError`` with the
    request attached; upload relays can map this error to HTTP 413.
    """

    def __init__(self, registry: TunnelRegistry, runner_id: str) -> None:
        self._registry = registry
        self._runner_id = runner_id

    async def wait_for_runner(self, timeout_s: float) -> bool:
        """
        Wait until this transport's runner has a live tunnel or the timeout expires.

        Lets a caller that just saw the runner go offline park until it
        re-registers, instead of retrying requests on a fixed interval.

        :param timeout_s: Maximum seconds to wait, e.g. ``90.0``.
        :returns: ``True`` when the runner is registered when the wait ends.
        """
        return (
            await self._registry.wait_for_runner(self._runner_id, timeout_s=timeout_s) is not None
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        session = self._registry.get(self._runner_id)
        if session is None:
            # The runner is offline. Raising ConnectError matches
            # what httpx would emit for a TCP connect failure, so
            # the call site's exception handling for "runner went
            # away" is identical to "TCP connect refused."
            raise httpx.ConnectError(f"runner {self._runner_id!r} is offline")

        req_id = uuid.uuid4().hex
        opted_in = request.extensions.get("runner_tunnel_binary_request_body") is True
        use_binary = (
            opted_in
            and BINARY_REQUEST_BODY_CAPABILITY in session.hello.capabilities
            and runner_tunnel_binary_request_body()
        )
        generation = request.extensions.get("runner_tunnel_generation")
        try:
            state = self._registry.open_request(
                self._runner_id,
                req_id,
                generation=session.generation if use_binary and generation is None else generation,
                binary_body=use_binary,
            )
        except KeyError as exc:
            raise httpx.ConnectError(f"runner {self._runner_id!r} is offline") from exc
        except ConnectionError as exc:
            if use_binary:
                raise httpx.ConnectError(str(exc), request=request) from exc
            raise
        try:
            frame = RequestFrame(
                id=req_id,
                method=request.method,
                path=request.url.path,
                query_string=request.url.query.decode("utf-8"),
                headers=[[k, v] for k, v in request.headers.items()],
                stream=True,
            )
            if use_binary:
                stream = request.stream
                assert isinstance(stream, httpx.AsyncByteStream)
                try:
                    iterator = stream.__aiter__()
                    wire_buffer = bytearray(encode_binary_request(frame, b""))
                    body_budget = RUNNER_TUNNEL_MAX_MESSAGE_BYTES - len(wire_buffer)
                    body_size = 0
                    async for chunk in iterator:
                        if body_size + len(chunk) > body_budget:
                            raise httpx.LocalProtocolError(
                                "binary request exceeds tunnel message limit",
                                request=request,
                            )
                        wire_buffer.extend(chunk)
                        body_size += len(chunk)
                    wire = bytes(wire_buffer)
                    del wire_buffer
                except BaseException as error:
                    try:
                        await _aclose_request_stream(stream)
                    except BaseException as cleanup_error:
                        raise error from cleanup_error
                    raise
                else:
                    await _aclose_request_stream(stream)
            else:
                body = await request.aread()
                content_type = request.headers.get("content-type", "application/json")
                frame.body, frame.encoding = (
                    encode_body(body, content_type) if body else (None, "utf-8")
                )
                wire = encode_frame(frame)
            await self._registry.send_request(state.session, wire, request_id=req_id)
            # Block until the response head arrives (or the tunnel
            # aborts the request).
            head = await state.head_future
        except BaseException as exc:
            try:
                if (
                    opted_in
                    and isinstance(
                        exc, (asyncio.CancelledError, TimeoutError, httpx.TimeoutException)
                    )
                    and state.enqueued
                ):
                    with contextlib.suppress(Exception):
                        await self._registry.send_text(
                            state.session,
                            encode_frame(RequestCancelFrame(id=req_id, reason="client_cancelled")),
                        )
            finally:
                self._registry.close_request(self._runner_id, req_id, session=state.session)
            if use_binary and isinstance(exc, ConnectionError):
                raise httpx.ConnectError(str(exc), request=request) from exc
            raise

        # Wrap the body queue as an httpx AsyncByteStream. The stream
        # owns close_request() — cleanup happens when the response
        # iterator finishes or the consumer's `async with` exits.
        stream = _TunneledByteStream(self._registry, self._runner_id, req_id, state)
        return httpx.Response(
            status_code=head.status,
            headers=[(k, v) for k, v in head.headers],
            stream=stream,
            request=request,
        )

    async def aclose(self) -> None:
        # Nothing to close — the transport doesn't own connections;
        # the registry does. Implementing this lets httpx call it
        # without exploding.
        pass

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

import uuid
from collections.abc import AsyncIterator

import httpx

from omnigent.runner.transports.ws_tunnel.frames import (
    RequestCancelFrame,
    RequestFrame,
    ResponseBodyFrame,
    decode_body,
    encode_body,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.registry import RequestState, TunnelRegistry


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
        if self._registry.request_is_open(state.session, self._req_id):
            try:  # noqa: SIM105 — contextlib.suppress doesn't work with await
                await self._registry.send_text(
                    state.session,
                    encode_frame(
                        RequestCancelFrame(
                            id=self._req_id,
                            reason="client_disconnected",
                        )
                    ),
                )
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
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
    """

    def __init__(self, registry: TunnelRegistry, runner_id: str) -> None:
        self._registry = registry
        self._runner_id = runner_id

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        session = self._registry.get(self._runner_id)
        if session is None:
            # The runner is offline. Raising ConnectError matches
            # what httpx would emit for a TCP connect failure, so
            # the call site's exception handling for "runner went
            # away" is identical to "TCP connect refused."
            raise httpx.ConnectError(f"runner {self._runner_id!r} is offline")

        req_id = uuid.uuid4().hex
        # Read the request body up front. Streaming request bodies
        # would need a multi-frame send; v1 sends the whole body in
        # the request frame because all our request bodies are
        # tiny JSON.
        body = await request.aread() if request.content else b""
        content_type = request.headers.get("content-type", "application/json")
        body_str, encoding = (
            encode_body(body, content_type, request.headers.get("content-encoding", "identity"))
            if body
            else (None, "utf-8")
        )

        try:
            state = self._registry.open_request(self._runner_id, req_id)
        except KeyError as exc:
            raise httpx.ConnectError(f"runner {self._runner_id!r} is offline") from exc
        try:
            await self._registry.send_text(
                state.session,
                encode_frame(
                    RequestFrame(
                        id=req_id,
                        method=request.method,
                        path=request.url.path,
                        query_string=request.url.query.decode("utf-8"),
                        headers=[[k, v] for k, v in request.headers.items()],
                        body=body_str,
                        encoding=encoding,
                        # Best-effort hint for streaming responses;
                        # not load-bearing on the runner side.
                        stream=True,
                    )
                ),
            )
            # Block until the response head arrives (or the tunnel
            # aborts the request).
            head = await state.head_future
        except BaseException:
            # If we failed before getting head, clean up the slot so
            # we don't leak in_flight state.
            self._registry.close_request(self._runner_id, req_id, session=state.session)
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

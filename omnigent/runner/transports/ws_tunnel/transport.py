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
If the request carries an httpx read timeout and the response head
or body stalls past it while the tunnel stays up → raise
``httpx.ReadTimeout`` (the runner stays registered).
"""

from __future__ import annotations

import asyncio
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

# Bound on the best-effort request.cancel send: the send awaits an ack
# from the session's owner loop, and a wedged loop never acks — the
# timeout that triggered the cancel must still surface to the caller.
_CANCEL_SEND_TIMEOUT_S = 1.0


async def _send_cancel_frame(
    registry: TunnelRegistry,
    state: RequestState,
    req_id: str,
    reason: str,
) -> None:
    """Best-effort request.cancel so the runner stops its dispatch task.

    Without it the runner's stream generator leaks and keeps consuming
    from the session's shared event queue, starving a restarted stream.
    Must run while the request is still open; send failures and a send
    that outlasts :data:`_CANCEL_SEND_TIMEOUT_S` are ignored (the
    tunnel may already be dying or its owner loop wedged).
    """
    if not registry.request_is_open(state.session, req_id):
        return
    try:  # noqa: SIM105 — contextlib.suppress doesn't work with await
        await asyncio.wait_for(
            registry.send_text(
                state.session,
                encode_frame(RequestCancelFrame(id=req_id, reason=reason)),
            ),
            _CANCEL_SEND_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass


def _request_read_timeout(request: httpx.Request) -> float | None:
    """Return the request's httpx read timeout, or ``None`` for no limit."""
    timeouts = request.extensions.get("timeout")
    if isinstance(timeouts, dict):
        read = timeouts.get("read")
        if isinstance(read, (int, float)):
            return float(read)
    return None


class _TunneledByteStream(httpx.AsyncByteStream):
    """Adapts the registry's body queue into an ``httpx.AsyncByteStream``."""

    def __init__(
        self,
        registry: TunnelRegistry,
        runner_id: str,
        req_id: str,
        state: RequestState,
        read_timeout: float | None = None,
        request: httpx.Request | None = None,
    ) -> None:
        self._registry = registry
        self._runner_id = runner_id
        self._req_id = req_id
        self._state = state
        self._read_timeout = read_timeout
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        state = self._state
        try:
            while True:
                get = state.body_queue.get()
                if self._read_timeout is None:
                    item = await get
                else:
                    try:
                        item = await asyncio.wait_for(get, self._read_timeout)
                    except TimeoutError:
                        await _send_cancel_frame(
                            self._registry, state, self._req_id, "read_timeout"
                        )
                        # Replacement racing the stall: this request's
                        # generation is gone even though a runner is
                        # registered, so it's a tunnel transition, not a
                        # live-stream loss.
                        if self._registry.get(self._runner_id) is not state.session:
                            raise ConnectionError(
                                f"runner {self._runner_id!r} tunnel was replaced "
                                "during a stalled read"
                            ) from None
                        # Silent stream on a live tunnel: surface a read
                        # timeout; the tunnel itself stays registered.
                        raise httpx.ReadTimeout(
                            f"tunneled response from runner {self._runner_id!r} "
                            f"stalled beyond {self._read_timeout}s read timeout",
                            request=self._request,
                        ) from None
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
        await _send_cancel_frame(self._registry, state, self._req_id, "client_disconnected")
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

    def runner_registered(self, runner_id: str | None = None) -> bool:
        """Return True when the runner has a live tunnel registered.

        Public liveness probe for disconnect attribution: a stream error
        while this is True is a live-tunnel stream loss, not a runner
        disconnect. Stale-generation stalls never reach it — the timeout
        paths classify those as tunnel transitions themselves.

        :param runner_id: Runner to check, e.g. ``"runner_abc123"``.
            Defaults to this transport's bound runner.
        """
        rid = self._runner_id if runner_id is None else runner_id
        return self._registry.get(rid) is not None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        session = self._registry.get(self._runner_id)
        if session is None:
            # The runner is offline. Raising ConnectError matches
            # what httpx would emit for a TCP connect failure, so
            # the call site's exception handling for "runner went
            # away" is identical to "TCP connect refused."
            raise httpx.ConnectError(f"runner {self._runner_id!r} is offline")

        req_id = uuid.uuid4().hex
        read_timeout = _request_read_timeout(request)
        # Read the request body up front. Streaming request bodies
        # would need a multi-frame send; v1 sends the whole body in
        # the request frame because all our request bodies are
        # tiny JSON.
        body = await request.aread() if request.content else b""
        content_type = request.headers.get("content-type", "application/json")
        body_str, encoding = encode_body(body, content_type) if body else (None, "utf-8")

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
            if read_timeout is None:
                head = await state.head_future
            else:
                try:
                    head = await asyncio.wait_for(state.head_future, read_timeout)
                except TimeoutError:
                    await _send_cancel_frame(self._registry, state, req_id, "read_timeout")
                    # Replacement racing the stall: stale generation is a
                    # tunnel transition, not a live-stream loss.
                    if self._registry.get(self._runner_id) is not state.session:
                        raise ConnectionError(
                            f"runner {self._runner_id!r} tunnel was replaced during a stalled read"
                        ) from None
                    raise httpx.ReadTimeout(
                        f"response head from runner {self._runner_id!r} "
                        f"stalled beyond {read_timeout}s read timeout",
                        request=request,
                    ) from None
        except BaseException:
            # If we failed before getting head, clean up the slot so
            # we don't leak in_flight state.
            self._registry.close_request(self._runner_id, req_id, session=state.session)
            raise

        # Wrap the body queue as an httpx AsyncByteStream. The stream
        # owns close_request() — cleanup happens when the response
        # iterator finishes or the consumer's `async with` exits.
        stream = _TunneledByteStream(
            self._registry,
            self._runner_id,
            req_id,
            state,
            read_timeout=read_timeout,
            request=request,
        )
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

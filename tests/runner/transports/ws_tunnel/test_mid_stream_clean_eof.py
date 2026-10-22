"""Regression test: ws_tunnel mid-stream failures must not read as clean EOF.

ws_tunnel: a runner-side session-stream generator that raises mid-stream
causes ``dispatch_via_asgi`` to send a plain ``ResponseEndFrame`` instead of
an error signal.  The server-side ``_TunneledByteStream`` therefore exits its
``async for`` loop normally — no exception, no ``httpx.RemoteProtocolError``,
no ``aborted_with`` — and the consumer sees a clean EOF after the partial body.
The session simply stops streaming; it is never stamped ``session_stream_lost``
or ``runner_disconnected``.

This test encodes the two observable failure points:

1. ``dispatch_via_asgi`` emits ``['ResponseHeadFrame', 'ResponseBodyFrame',
   'ResponseEndFrame']`` for a mid-stream generator raise (the plain
   ``ResponseEndFrame`` carries no error signal).
2. The registry routes an error-flagged ``ResponseEndFrame`` as an abort so
   the body iterator raises rather than yielding a clean EOF.

Both assertions fail (i.e. the test FAILS) on the unfixed build — that is the
expected behaviour for a regression guard.  After the fix both pass.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    RequestFrame,
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    decode_frame,
)
from omnigent.runner.transports.ws_tunnel.registry import RunnerSession, TunnelRegistry
from omnigent.runner.transports.ws_tunnel.serve import dispatch_via_asgi

# ---------------------------------------------------------------------------
# Helpers shared by both tests
# ---------------------------------------------------------------------------


async def _async_append(lst: list[str], item: str) -> None:
    lst.append(item)


def _make_streaming_app_that_raises() -> FastAPI:
    """Return a FastAPI app whose /stream endpoint raises after 1 chunk."""
    app = FastAPI()

    @app.get("/stream")
    async def _stream() -> StreamingResponse:
        async def _chunks() -> AsyncIterator[bytes]:
            yield b"partial-chunk\n"
            raise RuntimeError("generator blew up mid-stream")

        return StreamingResponse(_chunks(), media_type="text/event-stream")

    return app


# ---------------------------------------------------------------------------
# Test 1: dispatch_via_asgi must emit an error signal on mid-stream failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_via_asgi_mid_stream_generator_raise_emits_error_signal() -> None:
    """dispatch_via_asgi sends an error-flagged end frame when a streaming
    generator raises after head + body were already sent.

    Before the fix: the ``except`` block sends a plain ``ResponseEndFrame``
    that carries no error indicator.  ``route_response_frame`` routes it as a
    normal end-of-stream, so the consumer's body iterator stops cleanly with
    no exception.

    After the fix: ``dispatch_via_asgi`` signals the error on the end frame
    (e.g. ``ResponseEndFrame(error=...)``), and ``route_response_frame``
    converts it into an ``_abort_request_state`` call so the consumer receives
    an ``httpx.RemoteProtocolError``.
    """
    app = _make_streaming_app_that_raises()
    sent: list[str] = []
    frame = RequestFrame(id="req-mid-stream", method="GET", path="/stream")

    with contextlib.suppress(Exception):
        await dispatch_via_asgi(app, frame, lambda t: _async_append(sent, t))

    frames = [decode_frame(s) for s in sent]
    frame_types = [type(f).__name__ for f in frames]

    # The fix must ensure the last ResponseEndFrame signals an error.
    assert len(frames) >= 1, f"No frames sent: {frame_types}"
    end_frames = [f for f in frames if isinstance(f, ResponseEndFrame)]
    assert end_frames, f"No ResponseEndFrame in {frame_types}"
    last_end = end_frames[-1]

    # The key assertion: the end frame must carry an error indicator.
    # On the unfixed build this fails because ResponseEndFrame has no error
    # field and the frame is indistinguishable from a clean end.
    assert getattr(last_end, "error", None) is not None, (
        f"ResponseEndFrame carries no error signal after mid-stream generator raise. "
        f"RUNNER_SENT_FRAMES {frame_types} — the plain ResponseEndFrame is the bug."
    )


# ---------------------------------------------------------------------------
# Test 2: registry routes error-flagged end frame as an abort
# ---------------------------------------------------------------------------


class _FakeWS:
    """Minimal in-memory WebSocket stand-in for registry tests."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_ws_tunnel_mid_stream_failure_raises_at_consumer() -> None:
    """An error-flagged ResponseEndFrame must abort the body iterator so the
    consumer raises rather than seeing a clean EOF after partial body.

    This tests the server-side registry path (``route_response_frame``) which
    is the second half of the fix: the runner now sends
    ``ResponseEndFrame(error='runner_stream_error')``, and the registry must
    call ``_abort_request_state`` instead of ``_end_response_body`` so the
    ``WSTunnelTransport``'s body iterator raises ``httpx.RemoteProtocolError``.

    Before the fix: ``route_response_frame`` always calls ``_end_response_body``
    for any ``ResponseEndFrame``, so ``aborted_with`` is never set and the
    body iterator yields a clean ``StopAsyncIteration`` — consumer_exception
    is ``None``.

    After the fix: ``route_response_frame`` calls ``_abort_request_state``
    when ``ResponseEndFrame.error`` is set, so the body iterator raises.
    """
    fake_ws = _FakeWS()
    registry = TunnelRegistry()
    hello = HelloFrame(
        runner_version="0.1.0-test",
        frame_protocol_version=1,
        harnesses=[],
        envs=[],
    )
    runner_id = "runner-mid-stream"
    session: RunnerSession = registry.register(runner_id, fake_ws, hello)

    req_id = "test-req-mid-stream"
    state = registry.open_request(runner_id, req_id)

    # Simulate the runner sending: head → body → error-flagged end.
    head_frame = ResponseHeadFrame(id=req_id, status=200, headers=[])
    body_frame = ResponseBodyFrame(id=req_id, body="cGFydGlhbC1jaHVuaw==", encoding="base64")
    end_frame = ResponseEndFrame(id=req_id, error="runner_stream_error")

    # Route frames on the same event loop (simulating server-side receive).
    registry.route_response_frame(runner_id, head_frame, session=session)
    registry.route_response_frame(runner_id, body_frame, session=session)
    registry.route_response_frame(runner_id, end_frame, session=session)

    # Give the loop a tick to process the callbacks.
    await asyncio.sleep(0)

    # The abort should have been set on the existing state; verify it now.
    assert state.aborted_with is not None, (
        f"registry did not set aborted_with after error-flagged ResponseEndFrame. "
        f"aborted_with={state.aborted_with!r} — the registry abort path is the bug."
    )
    assert isinstance(state.aborted_with, httpx.RemoteProtocolError), (
        f"aborted_with is {type(state.aborted_with).__name__!r}, "
        f"expected httpx.RemoteProtocolError"
    )


# ---------------------------------------------------------------------------
# Test 3: a raise after a clean end must not emit a second (error) end frame
# ---------------------------------------------------------------------------


def _make_app_that_raises_after_clean_end() -> FastAPI:
    """Return a FastAPI app whose endpoint raises after a clean end.

    The ASGI middleware sends the response (head + body + ``more_body=False``)
    and then raises, modelling any post-completion failure in the app stack.
    """
    app = FastAPI()

    @app.get("/done-then-boom")
    async def _ok() -> dict[str, bool]:
        return {"ok": True}

    class _RaiseAfterResponse:
        def __init__(self, inner: ASGIApp) -> None:
            self._inner = inner

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await self._inner(scope, receive, send)
            raise RuntimeError("blew up after clean end")

    app.add_middleware(_RaiseAfterResponse)
    return app


@pytest.mark.asyncio
async def test_dispatch_via_asgi_raise_after_clean_end_sends_single_end_frame() -> None:
    """A raise after ``more_body=False`` must not send a second end frame.

    The response is already complete on the wire; a trailing error-flagged
    ``ResponseEndFrame`` could race the consumer's drain and spuriously abort
    a fully-delivered response. ``dispatch_via_asgi`` must send exactly one
    end frame, and it must be clean.
    """
    app = _make_app_that_raises_after_clean_end()
    sent: list[str] = []
    frame = RequestFrame(id="req-after-clean-end", method="GET", path="/done-then-boom")

    with contextlib.suppress(Exception):
        await dispatch_via_asgi(app, frame, lambda t: _async_append(sent, t))

    frames = [decode_frame(s) for s in sent]
    end_frames = [f for f in frames if isinstance(f, ResponseEndFrame)]
    assert len(end_frames) == 1, (
        f"Expected exactly one ResponseEndFrame, got {len(end_frames)}: "
        f"{[type(f).__name__ for f in frames]}"
    )
    assert end_frames[0].error is None, (
        f"End frame after a clean completion must not carry an error, got {end_frames[0].error!r}"
    )

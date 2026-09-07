"""Redirects on stream opens: followed transparently, or failed loud.

``OmnigentClient`` builds its ``httpx.AsyncClient`` with
``follow_redirects=True``, so a proxy or gateway 3xx on a stream request is
chased to the final endpoint and the SSE stream flows from there. The
stream-open guards cover the remaining hole: a redirect that *cannot* be
followed (no ``Location`` header, or a caller-supplied client with redirects
disabled) must raise ``OmnigentError`` instead of handing the SSE parser a
non-SSE body that completes as a silent, error-free, zero-event stream.

``_stream_session_events`` documents ``:raises OmnigentError:`` for a non-2xx
stream open, including an unfollowed redirect; these tests pin both halves of
that contract so the empty-stream failure mode cannot come back.
"""

from __future__ import annotations

import httpx
import pytest
from omnigent_client import OmnigentClient
from omnigent_client._errors import OmnigentError
from omnigent_client._responses import ResponsesNamespace
from omnigent_client._sessions import _stream_session_events

_STREAM_PATH = "/v1/sessions/conv_1/stream"
_RELOCATED_PREFIX = "/relocated"

_SSE_BODY = (
    "event: response.output_text.delta\n"
    'data: {"type": "response.output_text.delta", "delta": "hi"}\n'
    "\n"
    "event: done\n"
    "data: [DONE]\n"
    "\n"
)


def _redirect_handler(request: httpx.Request) -> httpx.Response:
    # A gateway bouncing the stream elsewhere. No body — a real redirect
    # carries none, which is precisely why the SSE parser stays silent.
    return httpx.Response(
        302,
        headers={"location": "https://elsewhere.invalid/v1/sessions/conv_1/stream"},
    )


def _redirect_then_stream_handler(request: httpx.Request) -> httpx.Response:
    # A gateway hop: the original path 307s to the relocated one, which
    # serves a well-formed SSE stream.
    if not request.url.path.startswith(_RELOCATED_PREFIX):
        return httpx.Response(
            307,
            headers={"location": f"https://api.invalid{_RELOCATED_PREFIX}{request.url.path}"},
        )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_SSE_BODY.encode(),
    )


@pytest.mark.asyncio
async def test_client_follows_redirects() -> None:
    """``OmnigentClient``'s http client is built with redirect following on."""
    async with OmnigentClient(base_url="http://127.0.0.1:9") as client:
        assert client._http.follow_redirects is True


@pytest.mark.asyncio
async def test_stream_open_follows_redirect_and_yields_events() -> None:
    """A 307 on the stream GET is chased to the relocated endpoint.

    The http client mirrors ``OmnigentClient``'s ``follow_redirects=True``, so
    the redirect is transparent: the SSE stream from the relocated endpoint
    yields its events and the guard never fires.
    """
    async with httpx.AsyncClient(
        follow_redirects=True,
        transport=httpx.MockTransport(_redirect_then_stream_handler),
    ) as http:
        events = [
            event async for event in _stream_session_events(http, "https://api.invalid", "conv_1")
        ]

    assert [event.delta for event in events] == ["hi"]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_stream_open_on_unfollowed_redirect_raises_instead_of_yielding_nothing() -> None:
    """A 302 the client does not follow raises ``OmnigentError``, never empty.

    With redirects disabled on the caller's client, the 302 reaches the guard.
    Without it, ``_parse_sse_lines`` sees a body with no ``data:`` frames and
    the async generator finishes with zero events and no error — the exact
    silent failure a caller cannot distinguish from "the session produced
    nothing".
    """
    seen = 0

    async with httpx.AsyncClient(transport=httpx.MockTransport(_redirect_handler)) as http:
        with pytest.raises(OmnigentError) as excinfo:
            async for _event in _stream_session_events(http, "https://api.invalid", "conv_1"):
                seen += 1

    # The redirect status is carried on the raised error (not swallowed), and
    # not a single event leaked out before it was raised.
    assert excinfo.value.status_code == 302
    assert seen == 0


@pytest.mark.asyncio
async def test_responses_stream_on_unfollowed_redirect_raises_not_empty() -> None:
    """A 302 on the responses stream POST raises ``OmnigentError`` too.

    Without the guard, the redirect yields no SSE events, the tool loop sees
    no pending calls and breaks, and ``stream()`` finishes with zero events
    and no error — the same silent empty stream on the second stream-open
    path.
    """
    seen = 0

    async with httpx.AsyncClient(transport=httpx.MockTransport(_redirect_handler)) as http:
        responses = ResponsesNamespace(http, "https://api.invalid")
        with pytest.warns(DeprecationWarning):
            with pytest.raises(OmnigentError) as excinfo:
                async for _event in responses.stream(model="agent", input="hi"):
                    seen += 1

    assert excinfo.value.status_code == 302
    assert seen == 0

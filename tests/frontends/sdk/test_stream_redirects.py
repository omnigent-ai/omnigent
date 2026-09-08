"""Redirects on stream opens: followed on-origin, refused off it, never silent.

``OmnigentClient`` builds its ``httpx.AsyncClient`` with
``follow_redirects=True`` plus a response hook that restricts following to
the request's own origin (same scheme/host/port, or an http→https upgrade on
the same host). An on-origin proxy or gateway 3xx on a stream request is
chased to the final endpoint and the SSE stream flows from there; a
cross-origin or https→http hop raises ``OmnigentError`` before any header or
body is forwarded. The stream-open guards cover the remaining hole: a 3xx
that *cannot* be followed (no ``Location`` header, a ``304``, or a
caller-supplied client with redirects disabled) must raise ``OmnigentError``
instead of handing the SSE parser a non-SSE body that completes as a silent,
error-free, zero-event stream.

``_stream_session_events`` documents ``:raises OmnigentError:`` for a non-2xx
stream open, including an unfollowed redirect; these tests pin both halves of
that contract so the empty-stream failure mode cannot come back.
"""

from __future__ import annotations

import httpx
import pytest
from omnigent_client import OmnigentClient
from omnigent_client._client import _redirect_stays_on_origin
from omnigent_client._errors import OmnigentError
from omnigent_client._events import TextDelta
from omnigent_client._responses import ResponsesNamespace
from omnigent_client._sessions import _stream_session_events

# Loopback base so OmnigentClient sets trust_env=False: no env proxy mounts,
# every request routes through the swapped-in MockTransport.
_BASE = "http://127.0.0.1:9"
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
            headers={"location": f"{_BASE}{_RELOCATED_PREFIX}{request.url.path}"},
        )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_SSE_BODY.encode(),
    )


@pytest.mark.asyncio
async def test_client_follows_redirects() -> None:
    """``OmnigentClient``'s http client is built with redirect following on."""
    async with OmnigentClient(base_url=_BASE) as client:
        assert client._http.follow_redirects is True


@pytest.mark.asyncio
async def test_session_stream_follows_redirect_and_yields_events() -> None:
    """A real ``OmnigentClient`` chases a 307 on the session stream GET.

    The redirect is transparent: the SSE stream from the relocated endpoint
    yields its events and the unfollowed-3xx guard never fires.
    """
    async with OmnigentClient(base_url=_BASE) as client:
        client._http._transport = httpx.MockTransport(_redirect_then_stream_handler)
        events = [event async for event in client.sessions.stream("conv_1")]

    assert [event.delta for event in events] == ["hi"]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_responses_stream_follows_307_replaying_the_post_body() -> None:
    """A real ``OmnigentClient`` chases a 307 on the responses stream POST.

    A 307 must replay the method and body, so the relocated endpoint sees the
    original POST payload and streams from there.
    """
    relocated: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.startswith(_RELOCATED_PREFIX):
            return httpx.Response(
                307,
                headers={"location": f"{_BASE}{_RELOCATED_PREFIX}{request.url.path}"},
            )
        relocated.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_SSE_BODY.encode(),
        )

    async with OmnigentClient(base_url=_BASE) as client:
        client._http._transport = httpx.MockTransport(handler)
        with pytest.warns(DeprecationWarning):
            events = [event async for event in client.responses.stream(model="agent", input="hi")]

    assert [event.delta for event in events if isinstance(event, TextDelta)] == ["hi"]
    assert relocated[0].method == "POST"
    assert b'"model"' in relocated[0].content and b"agent" in relocated[0].content


@pytest.mark.asyncio
async def test_cross_origin_redirect_is_refused_and_nothing_is_forwarded() -> None:
    """A redirect off the configured origin raises; no second request is sent.

    A redirecting gateway must not be able to point caller headers or bodies
    at a foreign host: the response hook refuses the hop before the client
    issues any request to the redirect target.
    """
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        return httpx.Response(
            302,
            headers={"location": "https://elsewhere.invalid/v1/sessions/conv_1/stream"},
        )

    async with OmnigentClient(base_url=_BASE) as client:
        client._http._transport = httpx.MockTransport(handler)
        with pytest.raises(OmnigentError) as excinfo:
            async for _event in client.sessions.stream("conv_1"):
                pass

    assert excinfo.value.status_code == 302
    assert seen_hosts == ["127.0.0.1"]


@pytest.mark.asyncio
async def test_session_stream_follows_relative_location_redirect() -> None:
    """A relative ``Location`` resolves onto the origin and is followed.

    Gateways often send path-only redirects (e.g. trailing-slash fixes);
    those stay on-origin by construction and must stream normally.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.startswith(_RELOCATED_PREFIX):
            return httpx.Response(
                307,
                headers={"location": f"{_RELOCATED_PREFIX}{request.url.path}"},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_SSE_BODY.encode(),
        )

    async with OmnigentClient(base_url=_BASE) as client:
        client._http._transport = httpx.MockTransport(handler)
        events = [event async for event in client.sessions.stream("conv_1")]

    assert [event.delta for event in events] == ["hi"]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_responses_stream_cross_origin_redirect_forwards_nothing() -> None:
    """The POST path refuses a cross-origin hop before replaying the body.

    A 307/308 replays the request body, so the no-forward guarantee matters
    most here: the transport must never see a request to the foreign host.
    """
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        return httpx.Response(
            307,
            headers={"location": "https://elsewhere.invalid/v1/responses"},
        )

    async with OmnigentClient(base_url=_BASE) as client:
        client._http._transport = httpx.MockTransport(handler)
        with pytest.warns(DeprecationWarning):
            with pytest.raises(OmnigentError) as excinfo:
                async for _event in client.responses.stream(model="agent", input="hi"):
                    pass

    assert excinfo.value.status_code == 307
    assert seen_hosts == ["127.0.0.1"]


@pytest.mark.parametrize(
    ("base", "location", "allowed"),
    [
        # Default-port http→https upgrade — the one cross-scheme hop allowed.
        ("http://h/v1/x", "https://h/v1/x", True),
        ("http://h:80/v1/x", "https://h:443/v1/x", True),
        # Any other port combination is a different service — refused.
        ("http://h:8080/v1/x", "https://h:444/v1/x", False),
        ("http://h/v1/x", "https://h:8443/v1/x", False),
        ("http://h:8080/v1/x", "https://h:443/v1/x", False),
        # Same origin, default ports normalized.
        ("http://h:8080/v1/x", "http://h:8080/v2/y", True),
        ("http://h/v1/x", "http://h:80/v2/y", True),
        # Downgrade and cross-host — refused.
        ("https://h/v1/x", "http://h/v1/x", False),
        ("http://h/v1/x", "http://other/v1/x", False),
        ("http://h/v1/x", "//other/v1/x", False),
    ],
)
def test_redirect_stays_on_origin_port_matrix(base: str, location: str, allowed: bool) -> None:
    """The origin predicate matches httpx's rule, port dimension included."""
    assert _redirect_stays_on_origin(httpx.URL(base), location) is allowed


@pytest.mark.asyncio
async def test_https_downgrade_redirect_is_refused() -> None:
    """An https→http redirect on the same host is a downgrade and is refused."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            301,
            headers={"location": "http://127.0.0.1:9/v1/sessions/conv_1/stream"},
        )

    async with OmnigentClient(base_url="https://127.0.0.1:9") as client:
        client._http._transport = httpx.MockTransport(handler)
        with pytest.raises(OmnigentError) as excinfo:
            async for _event in client.sessions.stream("conv_1"):
                pass

    assert excinfo.value.status_code == 301


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

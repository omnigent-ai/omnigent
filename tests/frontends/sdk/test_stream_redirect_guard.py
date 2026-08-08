"""Regression: opening a stream on a redirect must fail loud, not empty.

The SDK's ``httpx.AsyncClient`` is built with httpx's default
``follow_redirects=False`` (see :class:`omnigent_client._client.OmnigentClient`),
and the stream-open guard only rejected ``status >= 400``. A proxy or gateway
answering a 3xx (e.g. a ``302``) on the stream request therefore fell through to
the SSE parser, which found no ``data:`` frames in the redirect body and yielded
nothing — handing the caller a silent, error-free, zero-event stream instead of
surfacing the connection that never reached the server.

``_stream_session_events`` already documents ``:raises OmnigentError: If the
server returns a non-2xx status``; this pins the redirect half of that contract
so the empty-stream failure mode cannot come back.
"""

from __future__ import annotations

import httpx
import pytest
from omnigent_client._errors import OmnigentError
from omnigent_client._sessions import _stream_session_events


@pytest.mark.asyncio
async def test_stream_open_on_redirect_raises_instead_of_yielding_nothing() -> None:
    """A 302 on the stream GET raises ``OmnigentError``; it never completes empty.

    Fails without the fix: the old ``status >= 400`` guard lets the 302 through,
    ``_parse_sse_lines`` sees a body with no ``data:`` frames, and the async
    generator finishes with zero events and no error — the exact silent failure
    a caller cannot distinguish from "the session produced nothing".
    """
    seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        # A gateway bouncing the stream elsewhere. No body — a real redirect
        # carries none, which is precisely why the SSE parser stays silent.
        return httpx.Response(
            302,
            headers={"location": "https://elsewhere.invalid/v1/sessions/conv_1/stream"},
        )

    # follow_redirects defaults to False here exactly as in OmnigentClient, so
    # the 302 is returned to the caller rather than transparently chased.
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OmnigentError) as excinfo:
            async for _event in _stream_session_events(http, "https://api.invalid", "conv_1"):
                seen += 1

    # The redirect status is carried on the raised error (not swallowed), and
    # not a single event leaked out before it was raised.
    assert excinfo.value.status_code == 302
    assert seen == 0

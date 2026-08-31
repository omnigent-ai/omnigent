"""E2E: the event stream works end-to-end over its WebSocket transport.

Production defaults the session event stream to
``WS /v1/sessions/{id}/stream/ws`` so many open tabs stop exhausting the
browser's ~6-per-origin HTTP/1.1 connection pool (held-open SSE GETs used
to fill every slot and stall unrelated navigation). The rest of this suite
builds the SPA with ``VITE_EVENT_STREAM_TRANSPORT=sse`` because much of it
drives the SSE transport directly, so these tests opt back in to the
WebSocket via the ``omnigent.eventStream.transport`` localStorage override
and prove the default transport renders a real turn.

A failure here means one of:

- The WS route regressed (auth/access gate, snapshot-on-connect, or the
  live tail in ``routes_events.stream_session_ws``).
- The client transport regressed (``web/src/lib/sessionEventSocket.ts``)
  or ``startStreamPump`` stopped selecting it.
- The shared pump core (``pumpParsedEvents``) stopped reducing events into
  blocks for a non-SSE source.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
# Flip the transport before any app code runs, so the very first stream
# bind uses the WebSocket (an init script runs pre-navigation).
_FORCE_WS = "window.localStorage.setItem('omnigent.eventStream.transport', 'ws')"


def test_turn_streams_over_event_websocket(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A full turn renders with the event stream carried over a WebSocket.

    Also asserts the SSE endpoint is never opened, so a silent fallback to
    the old transport can't make this test pass green.

    :param page: Playwright page.
    :param seeded_session: ``(base_url, session_id)`` from the fixture.
    """
    base_url, session_id = seeded_session
    page.add_init_script(_FORCE_WS)

    ws_urls: list[str] = []
    page.on("websocket", lambda ws: ws_urls.append(ws.url))
    sse_opens: list[str] = []
    page.on(
        "request",
        lambda r: (
            sse_opens.append(r.url)
            if r.url.endswith(f"/v1/sessions/{session_id}/stream")
            or f"/v1/sessions/{session_id}/stream?" in r.url
            else None
        ),
    )

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill("Say hello")
    page.get_by_role("button", name="Send").click()

    # The reply can only arrive through the live event stream — the pump has
    # no other source for a streamed turn — so a rendered assistant bubble
    # proves the WS transport carried it.
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_ASSISTANT).first).not_to_have_text("")

    assert any(f"/v1/sessions/{session_id}/stream/ws" in url for url in ws_urls), (
        f"event WebSocket was never opened (sockets seen: {ws_urls})"
    )
    assert not sse_opens, f"SSE stream was opened despite the WS transport: {sse_opens}"

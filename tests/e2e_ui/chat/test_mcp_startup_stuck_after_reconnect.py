"""Regression: the MCP startup band must clear on reconnect.

A native (codex) session boots its harness MCP servers when its thread
starts; the forwarder mirrors that as ``external_mcp_startup`` posts, and
the web chat renders a ``Starting MCP servers (N/total): …`` band while the
round is in flight. The round is *settled* (band clears) by a later
``external_mcp_startup`` post whose map has nothing left ``starting`` — the
forwarder sends it once the servers finish (or the settle window elapses).

The session stream is snapshot + live-tail with **no replay** (see
``_stream_live_events``). If that settle post fires while the client is
between streams — an iOS tab backgrounded, a network path change, an
ingress recycle — the clearing ``session.mcp_startup`` event lands in a dead
socket and is missed. On reconnect the client recovers ``sessionStatus``,
usage counters, and the transcript window from a fresh snapshot
(``reconnectStatusPatch`` / ``reconcileOnReconnect``) — but it does **not**
recover ``mcpStartup`` from that snapshot. So the band stays frozen on the
last-rendered ``Starting MCP servers (0/N)`` forever, and the response never
appears to progress; only a full page reload (a cold ``bindStream``, which
*does* apply the snapshot's ``mcp_startup``) clears it.

This test drives the real ``session.mcp_startup`` SSE path (the same events
route the codex-native forwarder posts to — driving a live codex TUI's MCP
round timing would be flaky), forces a reconnect while the settle post fires
into the gap, and asserts the band clears. It fails today (band stuck) and
passes once reconnect recovers ``mcpStartup`` from the snapshot.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, Route, expect

_BAND = '[data-testid="mcp-startup-indicator"]'


def _publish_mcp_startup(
    base_url: str,
    session_id: str,
    servers: dict[str, dict[str, str | None]],
) -> None:
    """Publish a per-server MCP startup map through the events route.

    This is the exact path the codex-native forwarder posts to; the server
    republishes it as a ``session.mcp_startup`` SSE and updates the snapshot
    cache (a map with nothing left non-``ready`` evicts the cache entry).

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param servers: Full startup map, e.g.
        ``{"safe": {"status": "starting", "error": None}}``.
    :returns: None.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_mcp_startup", "data": {"servers": servers}},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_mcp_startup_band_clears_after_reconnect(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A missed settle event must not strand the startup band on reconnect.

    Journey:

    1. A session's MCP servers are starting → the chat shows
       ``Starting MCP servers (0/3): glean, jira, safe``.
    2. The live stream drops (the client is between streams — an iOS tab
       backgrounded, a proxy recycle).
    3. While the socket is down the servers finish and the round settles on
       the host — the clearing ``session.mcp_startup`` event fires into the
       dead socket and is missed.
    4. The client reconnects.

    Expected (this assertion): the reconnect recovers the settled startup
    state from the fresh snapshot, so the band clears.

    Bug: the band stays stuck on ``Starting MCP servers (0/3)``
    because the reconnect path never re-derives ``mcpStartup`` from the
    snapshot — so this assertion times out on the unfixed build.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session
    stream_path = f"/v1/sessions/{session_id}/stream"

    # 1. The startup round is in flight before the page opens: the snapshot
    #    cache seeds the band on the cold bind.
    _publish_mcp_startup(
        base_url,
        session_id,
        {
            "glean": {"status": "starting", "error": None},
            "jira": {"status": "starting", "error": None},
            "safe": {"status": "starting", "error": None},
        },
    )

    # Drive the connect → drop → reconnect cycle from the stream route.
    #
    # Until ``settle`` is flipped, every stream open is answered with a valid
    # but immediately-closed SSE response (200 + text/event-stream, one
    # comment, no ``[DONE]``): the open counts as a successful connect, then
    # ends like a transport drop, so the *next* open is a reconnect — the
    # path that must recover ``mcpStartup``. The band, lit by the cold bind's
    # snapshot (fetched before any settle), stays visible throughout because
    # no settle has happened yet, on both the fixed and unfixed builds.
    #
    # Once the test flips ``settle`` (after confirming the band is up), the
    # next reconnect open first publishes the settled (all-``ready``) map —
    # which evicts the server's startup snapshot cache, mirroring the
    # forwarder's settle post landing while the client is offline — and then
    # lets the real, settled stream through. The clearing SSE has fired into
    # the gap, so recovery must come from the reconnect's fresh snapshot.
    state = {"settle": False}

    def _handle_stream(route: Route) -> None:
        if urlparse(route.request.url).path != stream_path:
            route.continue_()
            return
        if not state["settle"]:
            route.fulfill(
                status=200,
                content_type="text/event-stream",
                headers={"cache-control": "no-cache"},
                body=": connected\n\n",
            )
            return
        _publish_mcp_startup(
            base_url,
            session_id,
            {
                "glean": {"status": "ready", "error": None},
                "jira": {"status": "ready", "error": None},
                "safe": {"status": "ready", "error": None},
            },
        )
        route.continue_()

    page.route(f"**/v1/sessions/{session_id}/stream*", _handle_stream)
    page.goto(f"{base_url}/c/{session_id}")

    band = page.locator(_BAND)
    # 2/3. The band is up (seeded from the snapshot) while the client cycles
    #      through connect → drop → reconnect with the round still pending.
    expect(band).to_contain_text("Starting MCP servers (0/3): glean, jira, safe", timeout=15_000)

    # The round settles on the host while the client is between streams; the
    # clearing event fires into the dead socket and is missed.
    state["settle"] = True

    # 4. The next reconnect must recover the cleared startup state from the
    #    fresh snapshot and drop the band. On the unfixed build the band is
    #    never re-derived on reconnect, so it stays stuck and this times out.
    expect(band).to_have_count(0, timeout=20_000)

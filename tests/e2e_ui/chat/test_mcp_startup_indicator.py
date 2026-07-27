"""MCP startup band lifecycle on the session page.

A codex-native session boots its harness MCP servers when its thread
starts; the forwarder mirrors that round as ``external_mcp_startup``
posts and the web chat must show it — an otherwise-idle session used to
look hung for the whole boot (and forever, when servers failed). These
tests drive the real per-server maps through the Sessions events route
(the same path the codex-native forwarder posts to), so they are
deterministic — no live codex TUI, whose MCP round timing would make the
assertions flaky. The forwarder-side synthesis/settle bookkeeping is
covered by the codex_native_forwarder unit tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
from playwright.sync_api import Page, expect

_BAND = '[data-testid="mcp-startup-indicator"]'


def _publish_mcp_startup(
    base_url: str,
    session_id: str,
    servers: dict[str, dict[str, str | None]],
) -> None:
    """Publish a per-server MCP startup map through the events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param servers: Full startup map, e.g.
        ``{"safe": {"status": "starting", "error": None}}``. An empty map
        settles the round (band clears, snapshot cache evicts).
    :returns: None.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_mcp_startup", "data": {"servers": servers}},
        timeout=10.0,
    )
    resp.raise_for_status()


def _publish_until(
    base_url: str,
    session_id: str,
    servers: dict[str, dict[str, str | None]],
    expectation: Callable[[], None],
) -> None:
    """Publish a live-only startup map until the band reflects it.

    The session stream is snapshot-plus-live-tail with no buffer or
    replay (see ``_stream_live_events``): a map published in the window
    between the page's snapshot load and its live SSE subscription is
    dropped, leaving the band stuck on the last-rendered state. The
    startup map is full-state and idempotent, so the fix is to keep
    re-publishing it until the assertion passes — a real live-handler
    regression still never satisfies *expectation*, so this closes the
    connect race without weakening the check.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param servers: Full startup map to publish each attempt.
    :param expectation: Playwright ``expect`` assertion for the state the
        published map should drive; polled between re-publishes.
    :returns: None.
    """
    deadline = time.monotonic() + 30.0
    while True:
        _publish_mcp_startup(base_url, session_id, servers)
        try:
            expectation()
            return
        except AssertionError:
            if time.monotonic() >= deadline:
                raise


def test_mcp_startup_band_lifecycle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Band tracks starting → progress → settled-with-failure → cleared.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    band = page.locator(_BAND)

    # 1. Startup begins BEFORE the page is opened: the snapshot cache must
    #    seed the band on load — a mid-startup page load (or reload) that
    #    showed nothing was exactly the "session looks hung" bug.
    _publish_mcp_startup(
        base_url,
        session_id,
        {
            "glean": {"status": "starting", "error": None},
            "jira": {"status": "starting", "error": None},
            "safe": {"status": "starting", "error": None},
        },
    )
    page.goto(f"{base_url}/c/{session_id}")
    expect(band).to_contain_text("Starting MCP servers (0/3): glean, jira, safe", timeout=15_000)

    # 2. Live progress: one server settles, the count advances and the
    #    settled name drops out of the pending list. This is the first
    #    live-tail-dependent step, so re-publish the idempotent map until
    #    the browser's SSE subscription is up and receives it.
    _publish_until(
        base_url,
        session_id,
        {
            "glean": {"status": "ready", "error": None},
            "jira": {"status": "starting", "error": None},
            "safe": {"status": "starting", "error": None},
        },
        lambda: expect(band).to_contain_text(
            "Starting MCP servers (1/3): jira, safe", timeout=3_000
        ),
    )

    # 3. The round settles with a failure: the spinner flips to the
    #    warning naming the server that never came up.
    _publish_until(
        base_url,
        session_id,
        {
            "glean": {"status": "ready", "error": None},
            "jira": {"status": "ready", "error": None},
            "safe": {"status": "failed", "error": "handshaking with MCP server failed"},
        },
        lambda: expect(band).to_contain_text(
            "MCP startup incomplete (failed: safe)", timeout=3_000
        ),
    )

    # 4. A settled-empty map clears the band entirely (and evicts the
    #    snapshot cache): the session reads as a normal idle chat again.
    _publish_until(
        base_url,
        session_id,
        {},
        lambda: expect(band).to_have_count(0, timeout=3_000),
    )


def test_mcp_startup_band_shows_cancelled_after_stop(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A Stop-cancelled round renders the cancelled warning, not a spinner.

    The runner's Stop path flips still-``starting`` servers to
    ``cancelled`` and publishes the flipped map (codex's own cancelled
    edges are owner-only and never reach the web); this pins the rendering
    of that published map so a user who stopped a slow MCP boot sees what
    happened instead of a stuck "Starting…" spinner.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    band = page.locator(_BAND)

    _publish_mcp_startup(
        base_url,
        session_id,
        {"storage-console": {"status": "starting", "error": None}},
    )
    page.goto(f"{base_url}/c/{session_id}")
    expect(band).to_contain_text("Starting MCP server: storage-console", timeout=15_000)

    # What the runner's Stop handler publishes after cancel_pending_mcp_startup.
    # First live-tail-dependent step — re-publish until the SSE subscription
    # is up (the seed above rode the snapshot on load; this one does not).
    _publish_until(
        base_url,
        session_id,
        {"storage-console": {"status": "cancelled", "error": None}},
        lambda: expect(band).to_contain_text(
            "MCP startup incomplete (cancelled: storage-console)", timeout=3_000
        ),
    )

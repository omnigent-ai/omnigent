"""MCP startup failure notices must stay out of the chat viewport.

The web chat renders MCP startup diagnostics through the startup band in
``web/src/pages/ChatIndicators.tsx``. The in-flight "Starting MCP servers"
spinner is expected UX, but once a startup round settles with failures the
band flips to an inline, assistant-style ``MCP startup incomplete
(failed: ...)`` notice inside the conversation viewport — setup/diagnostic
noise that should not add an item to the conversation.

This test drives the real per-server startup maps through the Sessions
events route — the same path the codex-native forwarder posts to, mirroring
``test_mcp_startup_indicator.py`` so the assertions are deterministic — and
asserts the settled-failure notice never renders in the chat viewport. It
fails while the notice is rendered (the bug) and passes once the notice is
hidden, while still pinning that the in-flight spinner (which is wanted)
keeps working.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
from playwright.sync_api import Page, expect

_BAND = '[data-testid="mcp-startup-indicator"]'
_FAILURE_NOTICE = "MCP startup incomplete"


def _publish_mcp_startup(
    base_url: str,
    session_id: str,
    servers: dict[str, dict[str, str | None]],
) -> None:
    """Publish a per-server MCP startup map through the events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param servers: Full startup map, e.g.
        ``{"test-check": {"status": "failed", "error": "..."}}``.
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

    The session stream is snapshot-plus-live-tail with no replay, so a map
    published between the page's snapshot load and its live SSE
    subscription is dropped. The startup map is full-state and idempotent,
    so re-publish it until the assertion passes (see
    ``test_mcp_startup_indicator.py`` for the full rationale).

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


def test_mcp_failure_notice_hidden_from_chat_viewport(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A settled-with-failure MCP round must not add a notice to the chat.

    Journey: open a session whose MCP startup round is in flight, watch the
    round settle with a failed server, and assert the chat viewport gains
    no ``MCP startup incomplete (...)`` item. The in-flight spinner is
    asserted first both as expected UX and as proof the startup pipeline is
    live in this run (so the final absence check cannot false-pass on a
    dead pipeline).

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    band = page.locator(_BAND)

    # 1. Startup begins before the page opens: the snapshot seeds the
    #    in-flight spinner on load. The spinner is wanted UX and proves the
    #    startup pipeline reaches this page.
    _publish_mcp_startup(
        base_url,
        session_id,
        {"test-check": {"status": "starting", "error": None}},
    )
    page.goto(f"{base_url}/c/{session_id}")
    expect(band).to_contain_text("Starting MCP server: test-check", timeout=15_000)

    # 2. The round settles with the server failed — what the codex-native
    #    forwarder publishes when an MCP server never comes up. Re-publish
    #    until the spinner is gone (receipt confirmed): with the bug the
    #    band flips to the failure notice (spinner text gone); with the
    #    fix the band unmounts entirely. Both read as zero Starting-bands.
    _publish_until(
        base_url,
        session_id,
        {"test-check": {"status": "failed", "error": "handshaking with MCP server failed"}},
        lambda: expect(band.filter(has_text="Starting")).to_have_count(0, timeout=3_000),
    )

    # 3. Give a delayed settled-state render a window to appear, then
    #    assert the failure notice is not in the conversation viewport.
    page.wait_for_timeout(2_000)
    notices = page.get_by_text(_FAILURE_NOTICE)
    if notices.count() > 0:
        rendered = band.inner_text() if band.count() > 0 else notices.first.inner_text()
        raise AssertionError(
            "MCP startup failure notice rendered in the chat viewport: "
            f"{rendered!r} — setup diagnostics must not add conversation items"
        )

"""MCP startup failures must be visible on the agent-info diagnostics surface.

A session whose MCP server fails to start used to show the user nothing:
the failure went to a server log only, so the session looked stuck or
quietly answered without the tool. Failure notices are deliberately kept
out of the conversation viewport (they are setup diagnostics, not
conversation content), so the web UI surfaces them on the agent-info
header popover instead: the header trigger flips to a warning state and
its Tools section names each failed server with its error. When the
server recovers (e.g. a refreshed token), the warning clears instead of
leaving a stale error.

This test drives the real per-server startup maps through the Sessions
events route — the same publish pipeline both the native forwarder and
the runner ``tools/list`` path feed (mirroring
``test_mcp_failure_notice_hidden_from_chat.py`` so the assertions are
deterministic) — and asserts the failure appears on the diagnostics
surface, stays out of the chat viewport, and clears on recovery. It
fails while failures are invisible (the bug) and passes once the
diagnostics surface lights up.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
from playwright.sync_api import Page, expect

_TRIGGER = '[data-testid="agent-info-trigger"]'
_FAILURE_ICON = '[data-testid="agent-info-mcp-failure-icon"]'
_FAILURE_BLOCK = '[data-testid="mcp-startup-failures"]'
_FAILURE_NOTICE = "MCP startup incomplete"
_SERVER_NAME = "pipeshub"
_SERVER_ERROR = "ConnectError: All connection attempts failed"


def _publish_mcp_startup(
    base_url: str,
    session_id: str,
    servers: dict[str, dict[str, str | None]],
) -> None:
    """Publish a per-server MCP startup map through the events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param servers: Full startup map, e.g.
        ``{"pipeshub": {"status": "failed", "error": "..."}}``.
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
    """Publish a startup map until the UI reflects it.

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


def test_mcp_startup_failure_shown_in_agent_diagnostics(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A failed MCP server is named on the agent-info surface, not the chat.

    Journey: a session's MCP startup round settles with a failed server
    before the page opens (the snapshot cache retains the failure), the
    user opens the session, notices the header agent-info trigger in its
    warning state, opens the popover, and reads which server failed and
    why. The server then recovers and the warning clears.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local
        server fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session

    # 1. The startup round settles with a failure before the page opens —
    #    what the runner tools/list path publishes for an SDK session with
    #    a bad token / unreachable server. The snapshot cache retains it.
    _publish_mcp_startup(
        base_url,
        session_id,
        {_SERVER_NAME: {"status": "failed", "error": _SERVER_ERROR}},
    )
    page.goto(f"{base_url}/c/{session_id}")

    # 2. The header trigger flips to its warning state, seeded from the
    #    session snapshot. This is the discoverability affordance: the
    #    user sees something is wrong without opening anything.
    expect(page.locator(_FAILURE_ICON)).to_be_visible(timeout=15_000)

    # 3. The failure never renders as conversation content — the
    #    diagnostics surface replaces the old inline viewport notice.
    expect(page.get_by_text(_FAILURE_NOTICE)).to_have_count(0)

    # 4. Opening the agent-info popover names the failed server and its
    #    error in the Tools section.
    page.locator(_TRIGGER).click()
    failure_block = page.locator(_FAILURE_BLOCK)
    expect(failure_block).to_be_visible(timeout=5_000)
    expect(failure_block).to_contain_text(_SERVER_NAME)
    expect(failure_block).to_contain_text("failed to start")
    expect(failure_block).to_contain_text(_SERVER_ERROR)

    # 5. The server recovers (what the runner republishes after a token
    #    refresh): the warning block and the header warning state clear
    #    instead of leaving a stale error. Re-publish until the live
    #    stream delivers it (snapshot-plus-live-tail has no replay).
    _publish_until(
        base_url,
        session_id,
        {_SERVER_NAME: {"status": "ready", "error": None}},
        lambda: expect(page.locator(_FAILURE_BLOCK)).to_have_count(0, timeout=3_000),
    )
    expect(page.locator(_FAILURE_ICON)).to_have_count(0)

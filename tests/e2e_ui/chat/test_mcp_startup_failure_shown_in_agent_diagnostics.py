"""Browser coverage for MCP failure diagnostics and recovery."""

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
    """Republish full state across the snapshot-to-live stream gap."""
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
    base_url, session_id = seeded_session

    _publish_mcp_startup(
        base_url,
        session_id,
        {_SERVER_NAME: {"status": "failed", "error": _SERVER_ERROR}},
    )
    page.goto(f"{base_url}/c/{session_id}")

    expect(page.locator(_FAILURE_ICON)).to_be_visible(timeout=15_000)

    expect(page.get_by_text(_FAILURE_NOTICE)).to_have_count(0)

    page.locator(_TRIGGER).click()
    failure_block = page.locator(_FAILURE_BLOCK)
    expect(failure_block).to_be_visible(timeout=5_000)
    expect(failure_block).to_contain_text(_SERVER_NAME)
    expect(failure_block).to_contain_text("failed to start")
    expect(failure_block).to_contain_text(_SERVER_ERROR)

    _publish_until(
        base_url,
        session_id,
        {_SERVER_NAME: {"status": "ready", "error": None}},
        lambda: expect(page.locator(_FAILURE_BLOCK)).to_have_count(0, timeout=3_000),
    )
    expect(page.locator(_FAILURE_ICON)).to_have_count(0)

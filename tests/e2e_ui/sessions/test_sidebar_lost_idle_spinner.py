"""Sidebar spinner for a finished native session whose idle status edge was lost."""

from __future__ import annotations

import uuid

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import set_fallback_mock_llm

_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_TERMINAL_VIEW = '[data-testid="terminal-view"]'
# Longer than one 60 s sidebar list refetch.
_SETTLE_TIMEOUT_MS = 90_000


def _row(page: Page, session_id: str) -> Locator:
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _running_badge(page: Page, session_id: str) -> Locator:
    return _row(page, session_id).locator(
        '[data-testid="session-state-badge"][data-state="running"]'
    )


def _listed_status(base_url: str, session_id: str) -> str | None:
    response = httpx.get(
        f"{base_url}/v1/sessions", params={"limit": 100, "visibility": "all"}, timeout=10.0
    )
    response.raise_for_status()
    row = next(item for item in response.json()["data"] if item["id"] == session_id)
    return row.get("status")


def _finish_composer_turn(page: Page, mock_llm_server_url: str) -> None:
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=120_000)
    page.get_by_test_id("view-mode-terminal").click()
    expect(page.locator(_TERMINAL_VIEW).last).to_have_attribute(
        "data-state", "connected", timeout=120_000
    )
    page.get_by_test_id("view-mode-chat").click()
    token = f"DONE-{uuid.uuid4().hex[:8]}"
    set_fallback_mock_llm(mock_llm_server_url, "default", token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, token)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(f"Reply with exactly this token and nothing else: {token}")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(timeout=90_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=90_000)


@pytest.mark.timeout(480)
def test_finished_session_stops_spinning_after_a_lost_idle_edge(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_claude_mock_session
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _finish_composer_turn(page, mock_llm_server_url)
    assert _listed_status(base_url, session_id) == "idle"

    # A native forwarder's running edge whose matching idle never arrives.
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": {"status": "running"}},
        timeout=10.0,
    ).raise_for_status()

    page.goto(f"{base_url}/")
    expect(_row(page, session_id)).to_be_visible(timeout=30_000)
    expect(_running_badge(page, session_id)).to_have_count(0, timeout=_SETTLE_TIMEOUT_MS)
    assert _listed_status(base_url, session_id) == "idle"

    page.reload()
    expect(_row(page, session_id)).to_be_visible(timeout=30_000)
    expect(_running_badge(page, session_id)).to_have_count(0, timeout=10_000)

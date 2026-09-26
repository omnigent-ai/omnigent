"""E2E: web ``/login``/``/logout`` on a Claude Code session are a notice, not a failed turn.

The intercepted command must complete the turn, keep the session out of ``failed``,
show the omni-setup guidance on a neutral notice, and still show it after a reload.
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Send a message…"
_USER = '[data-testid="message-bubble"][data-role="user"]'
_WORKING = '[data-testid="working-indicator"]'
_TERMINAL_VIEW = '[data-testid="terminal-view"]'
_FAILED_TURN_PILL = '[data-testid="error-pill"][data-level="error"]'
_NOTICE_PILL = '[data-testid="error-pill"][data-level="info"]'
_GUIDANCE = "omni setup on the host"

_TERMINAL_READY_TIMEOUT_MS = 180_000
_TURN_SETTLE_TIMEOUT_S = 90.0


def _select_view_mode(page: Page, option: str) -> None:
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    segment = page.get_by_test_id(f"view-mode-{option}")
    expect(segment).to_be_enabled(timeout=30_000)
    segment.click()


def _wait_for_claude_terminal(page: Page) -> None:
    _select_view_mode(page, "terminal")
    expect(page.locator(_TERMINAL_VIEW).last).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    _select_view_mode(page, "chat")


def _send(page: Page, text: str) -> None:
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _session_status(base_url: str, session_id: str) -> str:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return str(response.json()["status"])


def _wait_for_turn_outcome(page: Page, base_url: str, session_id: str) -> str:
    """Poll until the turn failed, or settled idle with the guidance shown."""
    pill = page.locator(_FAILED_TURN_PILL).first
    deadline = time.monotonic() + _TURN_SETTLE_TIMEOUT_S
    status = _session_status(base_url, session_id)
    while time.monotonic() < deadline:
        status = _session_status(base_url, session_id)
        if status == "failed" or pill.is_visible():
            return status
        if (
            status == "idle"
            and page.locator(_WORKING).count() == 0
            and page.get_by_text(_GUIDANCE).count() > 0
        ):
            return status
        page.wait_for_timeout(1_000)
    return status


def _expect_notice(page: Page) -> None:
    """The guidance is readable on a neutral notice pill; no failed-turn pill exists."""
    notice = page.locator(_NOTICE_PILL).first
    expect(notice).to_be_visible(timeout=30_000)
    expect(notice.get_by_text(_GUIDANCE).first).to_be_visible()
    expect(page.locator(_FAILED_TURN_PILL)).to_have_count(0)


@pytest.mark.nightly
@pytest.mark.timeout(600)
@pytest.mark.parametrize("command", ["/login", "/logout"], ids=["login", "logout"])
def test_auth_slash_command_is_not_a_failed_turn(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    command: str,
) -> None:
    base_url, session_id = native_claude_mock_session

    page.goto(f"{base_url}/c/{session_id}")
    _wait_for_claude_terminal(page)

    _send(page, command)
    expect(page.locator(_USER, has_text=command).first).to_be_visible(timeout=60_000)

    status = _wait_for_turn_outcome(page, base_url, session_id)

    pill = page.locator(_FAILED_TURN_PILL).first
    if pill.is_visible():
        headline = pill.get_by_test_id("error-headline")
        headline_text = headline.inner_text()
        headline.click()
        detail = pill.get_by_test_id("error-message-content").inner_text()
        # Keep the expanded guidance on screen long enough for the recording.
        page.wait_for_timeout(2_000)
        pytest.fail(
            f"{command} surfaced as a failed turn (session status {status!r}): "
            f"destructive error pill {headline_text!r} with the guidance hidden "
            f"behind its disclosure: {detail!r}"
        )
    assert status != "failed", f"{command} flipped the session to status {status!r}"
    _expect_notice(page)

    # The notice is persisted, so a reload still shows the answer to the command.
    page.reload()
    expect(page.locator(_USER, has_text=command).first).to_be_visible(timeout=60_000)
    _expect_notice(page)
    assert _session_status(base_url, session_id) != "failed"

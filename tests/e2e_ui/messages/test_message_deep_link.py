"""E2E: persistent message links open Chat and locate virtualized history."""

from __future__ import annotations

import re
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Browser, Page, Route, expect

from tests.e2e_ui.chat.test_transcript_scroll_persistence import _seed_turns

_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'


def test_pending_message_link_persists_and_opens_from_terminal(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Pending text stays copyable; its saved link opens Chat and flashes the target."""
    base_url, session_id = seeded_session
    marker = f"deep-link-{uuid.uuid4().hex[:8]}"
    held_requests: list[Route] = []
    with browser.new_context(permissions=["clipboard-read", "clipboard-write"]) as ctx:
        page = ctx.new_page()
        page.goto(f"{base_url}/c/{session_id}")
        page.route(
            f"**/v1/sessions/{session_id}/events", lambda route: held_requests.append(route)
        )
        try:
            composer = page.get_by_placeholder("Send a message…")
            expect(composer).to_be_visible()
            composer.fill(marker)
            page.get_by_role("button", name="Send", exact=True).click()

            bubble = page.locator(_USER_BUBBLE).filter(has_text=marker)
            copy_link = bubble.get_by_test_id("copy-message-link")
            expect(copy_link).to_be_disabled()
            expect(bubble).to_have_attribute("data-message-id", re.compile(r"^pend_"))
            bubble.hover()
            copy_text = bubble.get_by_role("button", name="Copy", exact=True)
            copy_text.click()
            expect(copy_text.locator("svg.lucide-check")).to_have_count(1)
            assert page.evaluate("navigator.clipboard.readText()") == marker

            assert len(held_requests) == 1
            held_requests.pop().continue_()
            expect(copy_link).to_be_enabled(timeout=15_000)
            message_id = bubble.get_attribute("data-message-id")
            assert message_id and not message_id.startswith("pend_")
            copy_link.click()
            expect(copy_link.locator("svg.lucide-check")).to_have_count(1)
            clipboard = page.evaluate("navigator.clipboard.readText()")
            url = urlparse(clipboard)
            assert url.path == f"/c/{session_id}"
            assert parse_qs(url.query)["message"] == [message_id]
        finally:
            for request in held_requests:
                request.abort()

    # A fresh browser must resolve the copied URL without the sender's state.
    with browser.new_context() as fresh:
        page = fresh.new_page()
        response = page.request.patch(
            f"{base_url}/v1/sessions/{session_id}",
            data={"labels": {"omnigent.ui": "terminal"}},
        )
        assert response.ok, response.text()
        page.goto(f"{base_url}/c/{session_id}")
        page.get_by_test_id("view-mode-terminal").click(timeout=15_000)
        expect(page.get_by_test_id("main-terminal-view")).to_be_visible(timeout=15_000)
        expect(page.locator(_USER_BUBBLE)).to_have_count(0)
        storage_key = f"omnigent.web.panel-key:{session_id}"
        assert page.evaluate("key => sessionStorage.getItem(key)", storage_key) is not None

        page.goto(clipboard)
        target = page.locator(f'[data-message-id="{message_id}"]')
        # Assert the short-lived flash first, before waiting for other UI state.
        expect(target).to_have_class(re.compile(r"\banimate-message-highlight\b"), timeout=20_000)
        expect(target).to_contain_text(marker)
        expect(target).to_be_in_viewport(timeout=5_000)
        expect(page.get_by_test_id("main-terminal-view")).not_to_be_visible()
        assert page.evaluate("key => sessionStorage.getItem(key)", storage_key) == "__chat__"


@pytest.mark.parametrize("target_role", ["user", "assistant"])
def test_message_link_reaches_virtualized_history(
    page: Page,
    seeded_session: tuple[str, str],
    target_role: str,
) -> None:
    """Find both a paged-out user message and a loaded, windowed-out response."""
    base_url, session_id = seeded_session
    _seed_turns(session_id, "deep")
    if target_role == "user":
        response = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 20, "order": "asc"},
        )
        response.raise_for_status()
        message_id = next(
            item["id"]
            for item in response.json()["data"]
            if item["type"] == "message" and item.get("role") == "user"
        )
        expected_text = "deep prompt 0"
    else:
        message_id = "resp_deep_060"
        expected_text = "deep reply 60"

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(f"{base_url}/c/{session_id}?message={message_id}")
    target = page.locator(f'[data-message-id="{message_id}"]')
    expect(target).to_have_class(re.compile(r"\banimate-message-highlight\b"), timeout=20_000)
    expect(target).to_contain_text(expected_text)
    expect(target).to_be_in_viewport(timeout=5_000)

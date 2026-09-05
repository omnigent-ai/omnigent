"""Client-only protection for ambiguous background message delivery."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, Request, Route, expect

_COMPOSER_LABEL = "Message the agent"


def _wait_for(page: Page, predicate: Callable[[], bool], *, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(50)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _session_is_idle(base_url: str, session_id: str) -> bool:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=5.0)
    response.raise_for_status()
    return response.json()["status"] == "idle"


def _gate_is_pending(mock_url: str) -> bool:
    response = httpx.get(f"{mock_url}/gate/pending", timeout=5.0)
    response.raise_for_status()
    return bool(response.json()["pending"])


def _release_gate(mock_url: str) -> None:
    response = httpx.post(f"{mock_url}/gate/release", timeout=5.0)
    response.raise_for_status()
    assert response.json()["released"] is True


def _matching_user_items(base_url: str, session_id: str, text: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=15.0,
    )
    response.raise_for_status()
    matches: list[dict[str, Any]] = []
    for item in response.json().get("data", []):
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        item_text = " ".join(
            str(block["text"])
            for block in item.get("content", [])
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
        if item_text == text:
            matches.append(item)
    return matches


def _message_text(request: Request, session_id: str) -> str | None:
    if request.method != "POST":
        return None
    if urlparse(request.url).path != f"/v1/sessions/{session_id}/events":
        return None
    body = request.post_data_json
    if not isinstance(body, dict) or body.get("type") != "message":
        return None
    for block in body.get("data", {}).get("content", []):
        if isinstance(block, dict) and block.get("type") == "input_text":
            return str(block.get("text", ""))
    return None


def test_accepted_background_message_is_not_retried_after_response_loss(
    page: Page,
    paused_mid_turn_session: tuple[str, str, str],
) -> None:
    """An ambiguous accepted POST stays visible but does not POST again."""
    base_url, session_id, mock_url = paused_mid_turn_session
    setup_text = "client-only-response-loss-hold"
    sentinel = "client-only-response-loss-message"
    accepted_statuses: list[int] = []
    request_failures: list[str | None] = []

    def hide_first_accepted_response(route: Route, request: Request) -> None:
        if _message_text(request, session_id) != sentinel:
            route.continue_()
            return

        upstream = route.fetch()
        accepted_statuses.append(upstream.status)
        if len(accepted_statuses) == 1:
            route.abort("failed")
        else:
            route.fulfill(response=upstream)

    def record_request_failure(request: Request) -> None:
        if _message_text(request, session_id) == sentinel:
            request_failures.append(request.failure)

    page.route(f"**/v1/sessions/{session_id}/events", hide_first_accepted_response)
    page.on("requestfailed", record_request_failure)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill(setup_text)
    composer.press("Enter")
    _wait_for(page, lambda: _gate_is_pending(mock_url), timeout_s=30.0)

    composer.fill(sentinel)
    composer.press("Enter")
    expect(page.get_by_test_id("composer-queued-strip")).to_contain_text(sentinel)

    fake_target = "ffffffffffffffffffffffffffffffff"
    page.evaluate(
        """target => {
            window.history.pushState({}, "", `/c/${target}`);
            window.dispatchEvent(new PopStateEvent("popstate"));
        }""",
        fake_target,
    )
    expect(page).to_have_url(f"{base_url}/c/{fake_target}", timeout=30_000)

    _release_gate(mock_url)
    _wait_for(page, lambda: len(accepted_statuses) == 1, timeout_s=60.0)
    _wait_for(page, lambda: len(request_failures) == 1, timeout_s=15.0)
    _wait_for(page, lambda: _session_is_idle(base_url, session_id), timeout_s=60.0)

    # Wait past the old retry cooldown, then return to force the foreground
    # queue flush that used to send the accepted message a second time.
    page.wait_for_timeout(5_500)
    source_link = page.locator(f'a[href="/c/{session_id}"]')
    expect(source_link).to_be_visible(timeout=30_000)
    source_link.click()
    expect(page).to_have_url(f"{base_url}/c/{session_id}", timeout=30_000)

    queued_strip = page.get_by_test_id("composer-queued-strip")
    expect(queued_strip).to_contain_text(sentinel)
    expect(queued_strip).to_contain_text("Delivery uncertain")
    expect(queued_strip.get_by_role("button", name="Retry uncertain message")).to_have_count(1)

    page.wait_for_timeout(2_000)
    stored_items = _matching_user_items(base_url, session_id, sentinel)
    assert accepted_statuses == [202]
    assert len(request_failures) == 1
    assert len(stored_items) == 1

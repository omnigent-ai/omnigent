"""E2E: Settings → General "Always steer" dispatches busy follow-ups now."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Locator, Page, Request, expect

_STORAGE_KEY = "omnigent:always-steer"
_COMPOSER_LABEL = "Message the agent"
_INITIAL_DIRECT = "always-steer initial direct turn"
_DIRECT_FOLLOWUP = "always-steer direct busy follow-up"
_INITIAL_ORDERING = "always-steer initial ordering turn"
_EARLIER_QUEUED = "always-steer earlier queued follow-up"
_LATER_QUEUED = "always-steer later queued follow-up"


def _wait_for(page: Page, predicate: Callable[[], bool], *, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _open_general_settings(page: Page, base_url: str, *, from_chat: bool = False) -> Locator:
    if from_chat:
        page.get_by_test_id("settings-button").click()
    else:
        page.goto(f"{base_url}/settings/appearance")
    general_link = page.get_by_test_id("settings-nav-general")
    expect(general_link).to_be_visible(timeout=30_000)
    general_link.click()
    expect(page).to_have_url(re.compile(r"/settings/general$"))
    expect(page.get_by_role("heading", name="Composer", exact=True)).to_be_visible()
    toggle = page.get_by_test_id("always-steer-toggle")
    expect(toggle).to_be_visible()
    return toggle


def _send(page: Page, text: str) -> None:
    page.get_by_label(_COMPOSER_LABEL).fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wait_for_gate(page: Page, mock_url: str) -> None:
    def pending() -> bool:
        return bool(httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json()["pending"])

    _wait_for(page, pending)


def _release_gate(mock_url: str) -> None:
    response = httpx.post(f"{mock_url}/gate/release", timeout=5.0)
    response.raise_for_status()
    assert response.json()["released"] is True


def _record_message_posts(page: Page, session_id: str) -> list[str]:
    posts: list[str] = []

    def record(request: Request) -> None:
        if request.method != "POST":
            return
        if urlparse(request.url).path != f"/v1/sessions/{session_id}/events":
            return
        body = request.post_data_json
        if not isinstance(body, dict) or body.get("type") != "message":
            return
        content = body.get("data", {}).get("content", [])
        for block in content:
            if isinstance(block, dict) and block.get("type") == "input_text":
                posts.append(str(block.get("text", "")))

    page.on("request", record)
    return posts


def _items_contain(base_url: str, session_id: str, *texts: str) -> bool:
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000},
        timeout=10.0,
    )
    response.raise_for_status()
    payload = json.dumps(response.json())
    return all(text in payload for text in texts)


def test_always_steer_persists_and_posts_a_busy_followup(
    page: Page,
    paused_mid_turn_session: tuple[str, str, str],
) -> None:
    """A reloaded opt-in sends through the real server while the turn is gated."""
    base_url, session_id, mock_url = paused_mid_turn_session

    toggle = _open_general_settings(page, base_url)
    expect(toggle).to_have_attribute("aria-checked", "false")
    assert page.evaluate(f"localStorage.getItem('{_STORAGE_KEY}')") is None

    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    assert page.evaluate(f"localStorage.getItem('{_STORAGE_KEY}')") == "true"

    page.reload()
    toggle = page.get_by_test_id("always-steer-toggle")
    expect(toggle).to_have_attribute("aria-checked", "true", timeout=30_000)

    posts = _record_message_posts(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
    _send(page, _INITIAL_DIRECT)
    _wait_for_gate(page, mock_url)
    _wait_for(page, lambda: posts == [_INITIAL_DIRECT])

    _send(page, _DIRECT_FOLLOWUP)
    _wait_for(page, lambda: posts == [_INITIAL_DIRECT, _DIRECT_FOLLOWUP])
    expect(page.get_by_test_id("composer-queued-strip")).to_have_count(0)

    _release_gate(mock_url)
    _wait_for(
        page,
        lambda: _items_contain(base_url, session_id, _INITIAL_DIRECT, _DIRECT_FOLLOWUP),
        timeout_s=60.0,
    )

    page.reload()
    expect(page.get_by_text(_DIRECT_FOLLOWUP, exact=True)).to_be_visible(timeout=30_000)
    toggle = _open_general_settings(page, base_url, from_chat=True)
    expect(toggle).to_have_attribute("aria-checked", "true")


def test_existing_queue_still_drains_in_order_after_enabling_always_steer(
    page: Page,
    paused_mid_turn_session: tuple[str, str, str],
) -> None:
    """A queued message keeps later sends behind it even after the opt-in."""
    base_url, session_id, mock_url = paused_mid_turn_session
    posts = _record_message_posts(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
    _send(page, _INITIAL_ORDERING)
    _wait_for_gate(page, mock_url)
    _wait_for(page, lambda: posts == [_INITIAL_ORDERING])

    _send(page, _EARLIER_QUEUED)
    queue = page.get_by_test_id("composer-queued-strip")
    expect(queue).to_contain_text(_EARLIER_QUEUED)
    assert posts == [_INITIAL_ORDERING]

    toggle = _open_general_settings(page, base_url, from_chat=True)
    expect(toggle).to_have_attribute("aria-checked", "false")
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    page.get_by_role("link", name="Back", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(session_id)}$"))
    expect(queue).to_contain_text(_EARLIER_QUEUED)

    _send(page, _LATER_QUEUED)
    expect(queue).to_contain_text(_LATER_QUEUED)
    page.wait_for_timeout(300)
    assert posts == [_INITIAL_ORDERING]

    _release_gate(mock_url)
    _wait_for(
        page,
        lambda: posts == [_INITIAL_ORDERING, _EARLIER_QUEUED, _LATER_QUEUED],
        timeout_s=90.0,
    )
    _wait_for(
        page,
        lambda: _items_contain(base_url, session_id, _EARLIER_QUEUED, _LATER_QUEUED),
        timeout_s=60.0,
    )
    expect(queue).to_have_count(0)

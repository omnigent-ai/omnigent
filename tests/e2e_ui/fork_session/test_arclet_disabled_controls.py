"""Unsupported Arclet actions stay discoverable without reaching write APIs.

The local server and transcript are real; only the managed source metadata is
patched because the isolated test environment has no Arclet provider.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_FORK_REASON = "Forking Arclet sessions is not supported yet."
_SWITCH_REASON = "Switching hosts is not supported for Arclet sessions yet."


def _patch_arclet(page: Page, session_id: str) -> None:
    def snapshot(route: Route) -> None:
        response = fetch_with_retry(route)
        body = response.json()
        body["host_id"] = "host_arclet"
        body["host_resumable"] = True
        body["workspace"] = "/workspace/project"
        body["labels"] = {**body.get("labels", {}), "omnigent.host_type": "managed"}
        route.fulfill(response=response, body=json.dumps(body))

    def sessions(route: Route) -> None:
        response = fetch_with_retry(route)
        body = response.json()
        for row in body.get("data", []):
            if row.get("id") == session_id:
                row["host_id"] = "host_arclet"
                row["workspace"] = "/workspace/project"
                # Sidebar rows need not carry the snapshot's synthetic label.
        route.fulfill(response=response, body=json.dumps(body))

    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), snapshot)
    page.route(re.compile(r"/v1/sessions(\?|$)"), sessions)
    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(
            json={
                "hosts": [
                    {
                        "host_id": "host_arclet",
                        "name": "Arclet",
                        "owner": "local",
                        "status": "online",
                        "sandbox_provider": "arclet",
                    }
                ]
            }
        ),
    )
    page.route_web_socket("**/v1/sessions/updates*", lambda _ws: None)


def _disabled_menu_action(page: Page, item: Locator, reason: str) -> None:
    expect(item).to_be_disabled()
    item.hover()
    expect(page.get_by_role("tooltip")).to_have_text(reason)
    # ARIA-disabled menu items remain in the arrow-key focus order. Enter and
    # Space must neither select them nor dismiss the menu.
    page.mouse.move(0, 0)
    expect(page.get_by_role("tooltip")).to_have_count(0)
    menu_items = page.get_by_role("menu").locator('[role="menuitem"]:visible:not([data-disabled])')
    item_index = menu_items.all_text_contents().index(item.inner_text())
    page.keyboard.press("Home")
    expect(menu_items.first).to_be_focused()
    for index in range(item_index):
        page.keyboard.press("ArrowDown")
        expect(menu_items.nth(index + 1)).to_be_focused()
    expect(item).to_be_focused()
    expect(page.get_by_role("tooltip")).to_have_text(reason)
    page.keyboard.press("Enter")
    page.keyboard.press("Space")
    expect(item).to_be_visible()
    expect(page.get_by_test_id("fork-session-dialog")).to_have_count(0)
    expect(page.get_by_test_id("switch-host-dialog")).to_have_count(0)
    expect(page.get_by_role("tooltip")).to_have_count(0)
    item.hover()
    expect(page.get_by_role("tooltip")).to_have_text(reason)


def _close_menu(page: Page) -> None:
    page.keyboard.press("Escape")
    # The tooltip's dismissable layer stays mounted during its exit animation.
    expect(page.get_by_role("tooltip")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.get_by_role("menu")).to_have_count(0)


@pytest.mark.parametrize("viewport_width", [1280, 390], ids=["desktop", "mobile"])
def test_arclet_fork_and_switch_host_disabled(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
    viewport_width: int,
) -> None:
    """Hover and keyboard explanations work across the real menu surfaces."""
    del mock_llm_server_url
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": viewport_width, "height": 844})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    composer.fill("Reply with just OK.")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
    expect(assistant).to_have_count(1, timeout=60_000)

    _patch_arclet(page, session_id)
    writes: list[str] = []
    page.on(
        "request",
        lambda request: (
            writes.append(request.url)
            if request.method in {"POST", "PATCH"}
            and re.search(r"/v1/(sessions|hosts)/", request.url)
            else None
        ),
    )
    page.reload()

    page.get_by_test_id("header-conversation-actions").click()
    header_fork = page.get_by_test_id("header-fork-conversation")
    _disabled_menu_action(page, header_fork, _FORK_REASON)
    page.screenshot(path=str(tmp_path / "arclet-fork-tooltip.png"))
    _close_menu(page)

    if viewport_width < 768:
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    row = page.locator(f'li[data-sidebar-session-id="{session_id}"]')
    row.hover()
    if viewport_width >= 768:
        row.get_by_test_id("conversation-actions").click()
        _disabled_menu_action(page, page.get_by_test_id("fork-conversation"), _FORK_REASON)
        _close_menu(page)
    row.locator(f'a[href="/c/{session_id}"]').click(button="right")
    _disabled_menu_action(page, page.get_by_test_id("fork-conversation"), _FORK_REASON)
    _close_menu(page)
    if viewport_width < 768:
        row.locator(f'a[href="/c/{session_id}"]').click()

    assistant.hover()
    message_fork = page.get_by_test_id("fork-from-response")
    expect(message_fork).to_be_disabled()
    message_fork.locator("..").hover()
    expect(page.get_by_role("tooltip")).to_have_text(_FORK_REASON)
    message_fork.locator("..").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_test_id("fork-session-dialog")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.get_by_role("tooltip")).to_have_count(0)

    page.get_by_test_id("composer-host-select").click()
    switch_host = page.get_by_role("menuitem", name="Switch host…")
    _disabled_menu_action(page, switch_host, _SWITCH_REASON)
    page.screenshot(path=str(tmp_path / "arclet-switch-host-tooltip.png"))
    assert writes == []

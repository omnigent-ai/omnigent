"""Unsupported sandbox actions stay discoverable without reaching write APIs.

An Arclet source instead forks onto a new Arclet when the server offers one.
The local server and transcript are real; only the managed source metadata is
patched because the isolated test environment has no Databricks sandbox providers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_FORK_REASON = "Forking this sandbox session is not supported yet."
_SWITCH_REASON = "Switching hosts is not supported for this sandbox session yet."
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'


def _patch_sandbox(page: Page, session_id: str, provider: str) -> None:
    def snapshot(route: Route) -> None:
        response = fetch_with_retry(route)
        body = response.json()
        body["host_id"] = "host_sandbox"
        body["host_resumable"] = True
        body["workspace"] = "/workspace/project"
        # Exercise the provider fallback without the managed snapshot marker.
        body["labels"] = dict(body.get("labels") or {})
        body["labels"].pop("omnigent.host_type", None)
        route.fulfill(response=response, body=json.dumps(body))

    def sessions(route: Route) -> None:
        response = fetch_with_retry(route)
        body = response.json()
        for row in body.get("data", []):
            if row.get("id") == session_id:
                row["host_id"] = "host_sandbox"
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
                        "host_id": "host_sandbox",
                        "name": "Databricks Sandbox" if provider == "lakebox" else "Arclet",
                        "owner": "local",
                        "status": "online",
                        "sandbox_provider": provider,
                    }
                ]
            }
        ),
    )
    page.route_web_socket("**/v1/sessions/updates*", lambda _ws: None)


def _reason_tooltip(page: Page, reason: str) -> Locator:
    # Another tooltip (a hovered sidebar row's) can stay mounted while it
    # animates out, so match the explanation itself.
    return page.get_by_role("tooltip").filter(has_text=reason)


def _disabled_menu_action(page: Page, item: Locator, reason: str) -> None:
    tooltip = _reason_tooltip(page, reason)
    expect(item).to_be_disabled()
    item.hover()
    expect(tooltip).to_have_text(reason)
    # ARIA-disabled menu items remain in the arrow-key focus order. Enter and
    # Space must neither select them nor dismiss the menu.
    page.mouse.move(0, 0)
    expect(tooltip).to_have_count(0)
    menu_items = page.get_by_role("menu").locator('[role="menuitem"]:visible:not([data-disabled])')
    item_index = menu_items.all_inner_texts().index(item.inner_text())
    page.keyboard.press("Home")
    expect(menu_items.first).to_be_focused()
    for index in range(item_index):
        page.keyboard.press("ArrowDown")
        expect(menu_items.nth(index + 1)).to_be_focused()
    expect(item).to_be_focused()
    expect(tooltip).to_have_text(reason)
    page.keyboard.press("Enter")
    page.keyboard.press("Space")
    expect(item).to_be_visible()
    expect(page.get_by_test_id("fork-session-dialog")).to_have_count(0)
    expect(page.get_by_test_id("switch-host-dialog")).to_have_count(0)
    expect(tooltip).to_have_count(0)
    item.hover()
    expect(tooltip).to_have_text(reason)


def _close_menu(page: Page) -> None:
    page.keyboard.press("Escape")
    # The tooltip's dismissable layer stays mounted during its exit animation.
    expect(page.get_by_role("tooltip")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.get_by_role("menu")).to_have_count(0)


# This server offers no Arclet sandbox, so an Arclet source has nothing to fork onto.
@pytest.mark.parametrize(
    "sandbox_provider", ["arclet", "lakebox"], ids=["arclet", "databricks-sandbox"]
)
@pytest.mark.parametrize("viewport_width", [1280, 390], ids=["desktop", "mobile"])
def test_sandbox_fork_and_switch_host_disabled(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
    viewport_width: int,
    sandbox_provider: str,
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

    _patch_sandbox(page, session_id, sandbox_provider)
    writes: list[str] = []
    page.on(
        "request",
        lambda request: (
            writes.append(request.url)
            if request.method in {"POST", "PATCH"}
            and re.search(r"/v1/(sessions|hosts)(/|\?|$)", request.url)
            else None
        ),
    )
    page.reload()

    page.get_by_test_id("header-conversation-actions").click()
    header_fork = page.get_by_test_id("header-fork-conversation")
    _disabled_menu_action(page, header_fork, _FORK_REASON)
    page.screenshot(path=str(tmp_path / f"{sandbox_provider}-fork-tooltip.png"))
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
    message_fork_target = page.get_by_role("group", name="Fork from here", exact=True)
    expect(message_fork).to_be_disabled()
    message_fork_target.hover()
    fork_tooltip = _reason_tooltip(page, _FORK_REASON)
    expect(fork_tooltip).to_have_text(_FORK_REASON)
    page.keyboard.press("Escape")
    expect(fork_tooltip).to_have_count(0)
    page.mouse.move(0, 0)
    message_fork_target.focus()
    expect(message_fork_target).to_be_focused()
    expect(fork_tooltip).to_have_text(_FORK_REASON)
    page.keyboard.press("Enter")
    expect(page.get_by_test_id("fork-session-dialog")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(fork_tooltip).to_have_count(0)

    page.get_by_test_id("composer-host-select").click()
    switch_host = page.get_by_role("menuitem", name="Switch host…")
    _disabled_menu_action(page, switch_host, _SWITCH_REASON)
    page.screenshot(path=str(tmp_path / f"{sandbox_provider}-switch-host-tooltip.png"))
    assert writes == []


def _offer_arclet_sandboxes(page: Page) -> None:
    def info(route: Route) -> None:
        response = fetch_with_retry(route)
        body = response.json()
        body["managed_sandboxes_enabled"] = True
        body["sandbox_provider"] = "arclet"
        body["sandbox_providers"] = ["arclet"]
        route.fulfill(response=response, body=json.dumps(body))

    page.route("**/v1/info", info)


def test_arclet_session_forks_only_onto_a_new_arclet(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Fork entry points are enabled, and the header Fork offers only a fresh Arclet."""
    del mock_llm_server_url
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_placeholder("Send a message…").fill("Reply with just OK.")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator(_ASSISTANT)
    expect(assistant).to_have_count(1, timeout=60_000)

    _patch_sandbox(page, session_id, "arclet")
    _offer_arclet_sandboxes(page)
    fork_bodies: list[dict[str, Any]] = []

    def handle_fork(route: Route) -> None:
        # No sandbox provider runs here, so forward an ordinary fork after recording it.
        body = json.loads(route.request.post_data or "{}")
        fork_bodies.append(body)
        forwarded = {
            key: value
            for key, value in body.items()
            if key not in {"host_type", "sandbox_provider", "workspace", "workspaces", "host_id"}
        }
        route.fulfill(response=route.fetch(post_data=json.dumps(forwarded)))

    page.route(re.compile(r"/v1/sessions/[0-9a-f]{32}/fork$"), handle_fork)
    page.reload()

    row = page.locator(f'li[data-sidebar-session-id="{session_id}"]')
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    expect(page.get_by_test_id("fork-conversation")).to_be_enabled()
    _close_menu(page)
    assistant.hover()
    expect(page.get_by_test_id("fork-from-response")).to_be_enabled()
    page.get_by_test_id("composer-host-select").click()
    _disabled_menu_action(page, page.get_by_role("menuitem", name="Switch host…"), _SWITCH_REASON)
    _close_menu(page)

    page.get_by_test_id("header-conversation-actions").click()
    page.get_by_test_id("header-fork-conversation").click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    expect(page.get_by_test_id("fork-session-sandbox-only-hint")).to_have_text(
        "This session can only be cloned onto a new Arclet Sandbox."
    )
    host_select = page.get_by_test_id("fork-session-host-select")
    expect(host_select).to_contain_text("Arclet Sandbox")
    host_select.click()
    expect(page.get_by_role("option")).to_have_count(1)
    sandbox_row = page.get_by_test_id("fork-session-sandbox-option")
    expect(sandbox_row).to_have_text("Arclet Sandbox")
    page.screenshot(path=str(tmp_path / "arclet-fork-target.png"))
    sandbox_row.click()
    page.get_by_test_id("fork-session-submit").click()

    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"), timeout=30_000
    )
    expect(dialog).not_to_be_visible()
    assert len(fork_bodies) == 1, fork_bodies
    assert fork_bodies[0]["host_type"] == "managed", fork_bodies[0]
    assert fork_bodies[0]["sandbox_provider"] == "arclet", fork_bodies[0]
    assert fork_bodies[0]["workspaces"] == [], fork_bodies[0]

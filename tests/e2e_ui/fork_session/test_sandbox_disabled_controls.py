"""Sandbox sessions fork to any destination, and switching their host stays disabled.

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

    def hosts(route: Route) -> None:
        # The isolated server registers no host, so add one connected machine
        # that the fork dialog offers beside the sandbox.
        response = fetch_with_retry(route)
        body = response.json()
        body["hosts"] = [
            *body.get("hosts", []),
            {
                "host_id": "host_sandbox",
                "name": "Databricks Sandbox" if provider == "lakebox" else "Arclet",
                "owner": "local",
                "status": "online",
                "sandbox_provider": provider,
            },
            {"host_id": "host_laptop", "name": "laptop", "owner": "local", "status": "online"},
        ]
        route.fulfill(response=response, body=json.dumps(body))

    page.route("**/v1/hosts", hosts)
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


@pytest.mark.parametrize(
    "sandbox_provider", ["arclet", "lakebox"], ids=["arclet", "databricks-sandbox"]
)
@pytest.mark.parametrize("viewport_width", [1280, 390], ids=["desktop", "mobile"])
def test_sandbox_fork_enabled_and_switch_host_disabled(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
    viewport_width: int,
    sandbox_provider: str,
) -> None:
    """Fork is enabled on every menu surface, and switching host explains why it is unavailable."""
    del mock_llm_server_url
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": viewport_width, "height": 844})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    composer.fill("Reply with just OK.")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator(_ASSISTANT)
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
    expect(header_fork).to_be_enabled()
    header_fork.click()
    expect(page.get_by_test_id("fork-session-dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_test_id("fork-session-dialog")).to_have_count(0)

    if viewport_width < 768:
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    row = page.locator(f'li[data-sidebar-session-id="{session_id}"]')
    row.hover()
    if viewport_width >= 768:
        row.get_by_test_id("conversation-actions").click()
        expect(page.get_by_test_id("fork-conversation")).to_be_enabled()
        _close_menu(page)
    row.locator(f'a[href="/c/{session_id}"]').click(button="right")
    expect(page.get_by_test_id("fork-conversation")).to_be_enabled()
    _close_menu(page)
    if viewport_width < 768:
        row.locator(f'a[href="/c/{session_id}"]').click()

    assistant.hover()
    expect(page.get_by_test_id("fork-from-response")).to_be_enabled()

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


def test_arclet_session_forks_onto_connected_hosts_and_a_new_arclet(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Fork entry points are enabled, and the dialog offers connected hosts beside a new Arclet."""
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
    expect(page.get_by_test_id("fork-session-unavailable")).to_have_count(0)
    host_select = page.get_by_test_id("fork-session-host-select")
    host_select.click()
    # The sandbox row comes first; any further option is a connected host.
    expect(page.get_by_role("option").nth(1)).to_be_visible()
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

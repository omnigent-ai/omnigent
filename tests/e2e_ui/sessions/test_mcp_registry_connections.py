"""Managed MCP settings routing, sign-in callbacks, and representative launch paths."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright
from playwright.async_api import expect as async_expect
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _managed_info_body,
    _register_common_routes,
    _run_in_fresh_loop,
)


def test_managed_mcp_account_controls(page: Page, live_server: str) -> None:
    def info(route: Route) -> None:
        response = route.fetch()
        body = response.json()
        body["enabled_connections"] = ["mcp"]
        route.fulfill(response=response, json=body)

    def catalog(route: Route) -> None:
        route.fulfill(
            json={
                "data": [
                    {
                        "id": "tracker",
                        "title": "Demo work tracker",
                        "description": "A fictional account",
                        "auth": "oauth",
                        "connected": False,
                        "tools": ["read_ticket"],
                        "connect_provider": "mcp-tracker",
                    }
                ]
            }
        )

    callback_url = f"{live_server}/settings/mcp?mcp-tracker=connected"

    def connect(route: Route) -> None:
        assert parse_qs(urlsplit(route.request.url).query)["return_to"] == ["/settings/mcp"]
        route.fulfill(status=302, headers={"Location": callback_url})

    page.route("**/v1/info", info)
    page.route("**/v1/mcp-registry/services*", catalog)
    page.route("**/v1/connections/mcp-tracker/connect?*", connect)
    page.goto(f"{live_server}/settings/general")
    expect(page.get_by_test_id("settings-nav-integrations")).to_have_count(0)
    page.get_by_test_id("settings-nav-mcp").click()
    expect(page).to_have_url(f"{live_server}/settings/mcp")
    expect(page.get_by_role("heading", name="Managed MCP services")).to_be_visible()
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page).to_have_url(callback_url)
    expect(page.get_by_role("heading", name="Managed MCP services")).to_be_visible()
    if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
        page.screenshot(path=Path(directory) / "mcp-settings.png", animations="disabled")


@pytest.mark.parametrize(
    ("width", "managed"),
    [(1280, True), (390, False)],
    ids=["desktop-sandbox", "mobile-local-host"],
)
def test_managed_mcp_selection_is_sent_with_launch(
    seeded_session: tuple[str, str], width: int, managed: bool
) -> None:
    _run_in_fresh_loop(_drive_launch(*seeded_session, width, managed))


def test_databricks_connects_from_launch_picker(seeded_session: tuple[str, str]) -> None:
    _run_in_fresh_loop(_drive_launch(*seeded_session, 1280, False, databricks=True))


async def _drive_launch(
    base_url: str, session_id: str, width: int, managed: bool, databricks: bool = False
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": width, "height": 480 if managed else 900},
            color_scheme="dark" if managed else "light",
        )
        page = await context.new_page()
        creates: list[dict] = []
        connected = not databricks
        try:
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=creates
            )
            info = json.loads(_managed_info_body())
            info.update(
                enabled_connections=["mcp"],
                managed_sandboxes_enabled=managed,
                sandbox_provider="agent_sandbox" if managed else None,
                databricks_features=False,
                sandbox_provider_capabilities={"agent_sandbox": {"inference_models": True}},
            )
            if not managed:
                await page.add_init_script(
                    "localStorage.setItem('omnigent:recent-workspaces', "
                    f"JSON.stringify({json.dumps({_HOST_ID: ['/work/repo']})}));"
                )
                await page.route(
                    "**/v1/hosts/*/harnesses/*/model-options",
                    lambda route: route.fulfill(
                        json={"models": [{"id": "demo-model", "displayName": "Demo model"}]}
                    ),
                )
            await page.route("**/v1/info", lambda route: route.fulfill(json=info))
            await page.route(
                "**/v1/sandbox-providers/*/harnesses/*/model-options*",
                lambda route: route.fulfill(
                    json={
                        "configured": True,
                        "status": "ready",
                        "models": [
                            {"id": "demo-model", "displayName": "Demo model", "isDefault": True}
                        ],
                        "configuration_revision": "demo-revision",
                        "provider_label": "Demo provider",
                        "default_model": "demo-model",
                    }
                ),
            )
            await page.route(
                "**/v1/mcp-registry/services",
                lambda route: route.fulfill(
                    json={
                        "data": [
                            {
                                "id": "tracker",
                                "title": "Demo work tracker",
                                "auth": "databricks" if databricks else "oauth",
                                "connected": connected,
                                "tools": ["read_ticket"],
                                "connect_provider": "databricks" if databricks else "mcp-tracker",
                            },
                            {
                                "id": "github-demo",
                                "title": "GitHub",
                                "auth": "oauth",
                                "connected": True,
                                "tools": ["list_repositories"],
                                "connect_provider": "mcp-github-demo",
                            },
                            {
                                "id": "search-demo",
                                "title": "Knowledge search",
                                "auth": "none",
                                "connected": True,
                                "tools": ["search"],
                                "connect_provider": "",
                            },
                            *[
                                {
                                    "id": f"extra-{number}",
                                    "title": f"Extra service {number}",
                                    "auth": "none",
                                    "connected": True,
                                    "tools": [],
                                }
                                for number in range(24 if managed else 0)
                            ],
                        ]
                    }
                ),
            )

            async def connect_workspace(route):
                nonlocal connected
                params = parse_qs(urlsplit(route.request.url).query)
                assert params["workspace"] == ["https://workspace.example.test"]
                connected = True
                await route.fulfill(
                    status=302,
                    headers={"Location": base_url + "/settings/mcp?databricks=connected"},
                )

            if databricks:
                await page.context.route(
                    "**/v1/connections/databricks/connect?*", connect_workspace
                )
            await page.goto(base_url)
            await page.get_by_test_id("new-chat-landing-input").fill("Read the demo ticket")
            await async_expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
            chip = page.get_by_test_id("new-chat-landing-workspace-controls").get_by_test_id(
                "new-chat-landing-mcp-registry-chip"
            )
            await async_expect(chip).to_be_visible()
            chip_bounds = await chip.bounding_box()
            composer_bounds = await page.get_by_test_id("new-chat-landing-composer").bounding_box()
            assert chip_bounds and composer_bounds
            assert chip_bounds["y"] + chip_bounds["height"] <= composer_bounds["y"] + 1
            await chip.click()
            toggle = page.get_by_role("switch", name="Demo work tracker")
            await toggle.click()
            if databricks:
                await async_expect(toggle).not_to_be_checked()
                await page.get_by_label("Databricks workspace URL").fill(
                    "https://workspace.example.test"
                )
                if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
                    await page.screenshot(
                        path=Path(directory) / "mcp-databricks-workspace.png",
                        animations="disabled",
                    )
                await page.get_by_role("button", name="Connect workspace").click()
            await async_expect(toggle).to_be_checked()
            if managed:
                last_toggle = page.get_by_role("switch", name="Extra service 23", exact=True)
                await last_toggle.click()
                await async_expect(last_toggle).to_be_checked()
                await async_expect(last_toggle).to_be_in_viewport(ratio=1)
                await async_expect(toggle).not_to_be_in_viewport()
                await async_expect(
                    page.get_by_role("heading", name="Tools for this session")
                ).to_be_in_viewport(ratio=1)
                await async_expect(
                    page.get_by_role("link", name="Manage MCP accounts")
                ).to_be_in_viewport(ratio=1)
                if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
                    await page.screenshot(
                        path=Path(directory) / "mcp-picker-overflow.png", animations="disabled"
                    )
                await last_toggle.click()
                await async_expect(last_toggle).not_to_be_checked()
                await toggle.scroll_into_view_if_needed()
            if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
                await page.screenshot(
                    path=Path(directory)
                    / f"mcp-launch-{'sandbox' if managed else 'local'}-{width}.png",
                    animations="disabled",
                )
            await page.keyboard.press("Escape")
            if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
                await page.screenshot(
                    path=Path(directory) / f"mcp-topbar-{width}.png",
                    animations="disabled",
                )
            if not databricks:
                await page.reload()
                await async_expect(chip).to_have_attribute(
                    "aria-label", "MCP services: 1 selected"
                )
                await chip.click()
                await async_expect(toggle).to_be_checked()
                await toggle.click()
                await async_expect(toggle).not_to_be_checked()
                await page.reload()
                await chip.click()
                await async_expect(toggle).not_to_be_checked()
                await toggle.click()
                await async_expect(toggle).to_be_checked()
                await page.keyboard.press("Escape")
                await page.get_by_test_id("new-chat-landing-input").fill("Read the demo ticket")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url(f"{base_url}/c/{session_id}")
            assert len(creates) == 1
            assert creates[0]["mcp_registry_services"] == ["tracker"]
            if not databricks:
                await _check_session_management(page, session_id, width, info)
        finally:
            await page.unroute_all(behavior="wait")
            await context.close()
            await browser.close()


async def _check_session_management(page, session_id: str, width: int, info: dict) -> None:
    servers = [{"name": "builtin-tools", "transport": "http"}]
    agent = {
        "id": "builtin-mcp-browser-fixture",
        "object": "agent",
        "name": "builtin-mcp-browser-fixture",
        "mcp_registry_editable": True,
        "mcp_servers_editable": False,
        "mcp_servers": servers,
    }
    await page.route(f"**/v1/sessions/{session_id}/agent", lambda route: route.fulfill(json=agent))

    async def change_selection(route):
        if route.request.method == "POST":
            reference = route.request.post_data_json
            assert reference == {"name": "tracker", "transport": "registry"}
            servers.append(reference)
            await route.fulfill(json=reference)
        else:
            assert route.request.method == "DELETE"
            assert route.request.url.endswith("/tracker")
            servers.remove({"name": "tracker", "transport": "registry"})
            await route.fulfill(status=204)

    await page.route(f"**/v1/sessions/{session_id}/agent/mcp-servers**", change_selection)
    for enabled in (True, False):
        info["enabled_connections"] = ["mcp"] if enabled else []
        await page.reload()
        if width < 768:
            await page.get_by_role("button", name="Conversation actions", exact=True).click()
            await page.get_by_role("menuitem", name="Agent info", exact=True).click()
        else:
            await page.get_by_role("button", name="Agent tools and policies").press("Enter")
        await async_expect(page.get_by_text("builtin-tools", exact=True)).to_be_visible()
        await async_expect(
            page.get_by_role("button", name="builtin-tools", exact=True)
        ).to_have_count(0)
        await page.get_by_role("button", name="Manage MCP servers").click()
        dialog = page.get_by_role("dialog").filter(
            has=page.get_by_role(
                "heading", name="MCP services" if enabled else "Manage MCP Servers", exact=True
            )
        )
        await async_expect(
            dialog.get_by_role("button", name="Advanced: custom MCP servers")
        ).to_have_count(0)
        await async_expect(dialog.get_by_role("textbox", name="URL", exact=True)).to_have_count(0)
        if enabled:
            toggle = dialog.get_by_role("switch", name="Demo work tracker")
            await toggle.click()
            await async_expect(toggle).to_be_checked()
        else:
            await async_expect(dialog.get_by_text("No longer available")).to_be_visible()
            await async_expect(
                dialog.get_by_role("button", name="Edit builtin-tools")
            ).to_be_disabled()
            await async_expect(
                dialog.get_by_role("button", name="Delete builtin-tools")
            ).to_be_disabled()
        if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
            await page.screenshot(
                path=Path(directory)
                / f"mcp-manager-{'enabled' if enabled else 'unavailable'}-{width}.png",
                animations="disabled",
            )
        if not enabled:
            await dialog.get_by_role("button", name="Delete tracker").click()
            await async_expect(page.get_by_text("tracker", exact=True)).to_have_count(0)
            await async_expect(
                page.get_by_text("Restart to apply changes", exact=True)
            ).to_be_visible()
            if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
                await page.screenshot(
                    path=Path(directory) / f"mcp-manager-removed-{width}.png",
                    animations="disabled",
                )

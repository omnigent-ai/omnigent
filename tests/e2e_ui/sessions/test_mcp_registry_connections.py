"""Browser account controls, including connection errors and disconnect."""

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
    connected = False
    fail_test = False

    def info(route: Route) -> None:
        response = route.fetch()
        body = response.json()
        body["enabled_connections"] = ["mcp"]
        route.fulfill(response=response, json=body)

    def catalog(route: Route) -> None:
        if "after=" not in route.request.url:
            route.fulfill(
                json={
                    "data": [
                        {
                            "id": "announcement",
                            "title": "Announcements",
                            "description": "",
                            "auth": "none",
                            "connected": True,
                            "tools": ["read"],
                            "connect_provider": "none",
                        }
                    ],
                    "next_cursor": "announcement",
                }
            )
            return
        assert parse_qs(urlsplit(route.request.url).query)["after"] == ["announcement"]

        route.fulfill(
            json={
                "data": [
                    {
                        "id": "tracker",
                        "title": "Demo work tracker",
                        "description": "A fictional account",
                        "auth": "bearer",
                        "connected": connected,
                        "tools": ["read_ticket"],
                        "connect_provider": "bearer",
                    }
                ]
            }
        )

    def connection(route: Route) -> None:
        nonlocal connected
        if route.request.method == "PUT":
            assert route.request.post_data_json == {"token": "demo-token"}
            connected = True
        elif route.request.method == "DELETE":
            connected = False
        route.fulfill(json={"connected": connected})

    def test_connection(route: Route) -> None:
        if fail_test:
            route.fulfill(status=502, json={"detail": "MCP connection failed"})
        else:
            route.fulfill(json={"tools": ["read_ticket"]})

    page.route("**/v1/info", info)
    page.route("**/v1/mcp-registry/services*", catalog)
    page.route("**/v1/mcp-registry/services/tracker/connection", connection)
    page.route("**/v1/mcp-registry/services/tracker/test", test_connection)
    page.goto(f"{live_server}/settings/general")
    expect(page.get_by_test_id("settings-nav-integrations")).to_have_count(0)
    page.get_by_test_id("settings-nav-mcp").click()
    expect(page).to_have_url(f"{live_server}/settings/mcp")
    expect(page.get_by_text("Not connected", exact=True)).to_be_visible()
    page.get_by_role("button", name="Connect", exact=True).click()
    token = page.get_by_label("Token for Demo work tracker")
    token.fill("demo-token")
    page.get_by_role("button", name="Save token").click()
    expect(page.get_by_text("Connected", exact=True)).to_have_count(2)
    expect(token).to_have_count(0)
    page.get_by_role("button", name="Test connection").last.click()
    expect(page.get_by_role("status")).to_contain_text(
        "Tool discovery succeeded. Tools: read_ticket"
    )
    expect(page.get_by_role("status")).to_contain_text(
        "Individual calls may require additional permissions"
    )
    if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
        page.screenshot(path=Path(directory) / "mcp-settings.png", animations="disabled")
    fail_test = True
    page.get_by_role("button", name="Test connection").last.click()
    expect(page.get_by_role("alert")).to_contain_text("MCP connection failed")
    page.get_by_role("button", name="Disconnect", exact=True).click()
    expect(page.get_by_text("Not connected", exact=True)).to_be_visible()


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
@pytest.mark.parametrize("managed", [True, False], ids=["sandbox", "local-host"])
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
        page = await browser.new_page(viewport={"width": width, "height": 900})
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
                            }
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
            chip = page.get_by_test_id("new-chat-landing-mcp-registry-chip")
            await chip.click()
            checkbox = page.get_by_role("checkbox", name="Demo work tracker")
            await checkbox.click()
            if databricks:
                await async_expect(checkbox).not_to_be_checked()
                await page.get_by_label("Databricks workspace URL").fill(
                    "https://workspace.example.test"
                )
                if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
                    await page.screenshot(
                        path=Path(directory) / "mcp-databricks-workspace.png",
                        animations="disabled",
                    )
                await page.get_by_role("button", name="Connect workspace").click()
            await async_expect(checkbox).to_be_checked()
            if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
                await page.screenshot(
                    path=Path(directory)
                    / f"mcp-launch-{'sandbox' if managed else 'local'}-{width}.png",
                    animations="disabled",
                )
            await page.keyboard.press("Escape")
            await async_expect(chip).to_have_accessible_name("MCP services: 1 selected")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url(f"{base_url}/c/{session_id}")
            assert len(creates) == 1
            assert creates[0]["mcp_registry_services"] == ["tracker"]
            if managed:
                assert creates[0]["host_type"] == "managed"
            else:
                assert creates[0]["host_id"] == _HOST_ID
                assert creates[0]["workspace"] == "/work/repo"
                assert creates[0].get("host_type") != "managed"
        finally:
            await page.unroute_all(behavior="wait")
            await browser.close()

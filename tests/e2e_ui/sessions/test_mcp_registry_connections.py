"""Browser account controls, including connection errors and disconnect."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright
from playwright.async_api import expect as async_expect
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.start_session.test_start_session import (
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
    page.route("**/v1/mcp-registry/services", catalog)
    page.route("**/v1/mcp-registry/services/tracker/connection", connection)
    page.route("**/v1/mcp-registry/services/tracker/test", test_connection)
    page.goto(f"{live_server}/settings/integrations")
    expect(page.get_by_text("Not connected", exact=True)).to_be_visible()
    page.get_by_role("button", name="Connect", exact=True).click()
    token = page.get_by_label("Token for Demo work tracker")
    token.fill("demo-token")
    page.get_by_role("button", name="Save token").click()
    expect(page.get_by_text("Connected", exact=True)).to_be_visible()
    expect(token).to_have_count(0)
    page.get_by_role("button", name="Test connection").click()
    expect(page.get_by_role("status")).to_contain_text(
        "Tool discovery succeeded. Tools: read_ticket"
    )
    expect(page.get_by_role("status")).to_contain_text(
        "Individual calls may require additional permissions"
    )
    fail_test = True
    page.get_by_role("button", name="Test connection").click()
    expect(page.get_by_role("alert")).to_contain_text("MCP connection failed")
    page.get_by_role("button", name="Disconnect", exact=True).click()
    expect(page.get_by_text("Not connected", exact=True)).to_be_visible()


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_managed_mcp_selection_is_sent_with_launch(
    seeded_session: tuple[str, str], width: int
) -> None:
    _run_in_fresh_loop(_drive_launch(*seeded_session, width))


async def _drive_launch(base_url: str, session_id: str, width: int) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": width, "height": 900})
        creates: list[dict] = []
        try:
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=creates
            )
            info = json.loads(_managed_info_body())
            info.update(
                enabled_connections=["mcp"],
                sandbox_provider="agent_sandbox",
                databricks_features=False,
                sandbox_provider_capabilities={"agent_sandbox": {"inference_models": True}},
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
                                "auth": "oauth",
                                "connected": True,
                                "tools": ["read_ticket"],
                                "connect_provider": "mcp-tracker",
                            }
                        ]
                    }
                ),
            )
            await page.goto(base_url)
            await page.get_by_test_id("new-chat-landing-input").fill("Read the demo ticket")
            await async_expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
            chip = page.get_by_test_id("new-chat-landing-mcp-registry-chip")
            await chip.click()
            await page.get_by_role("checkbox", name="Demo work tracker").check()
            if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
                await page.screenshot(
                    path=Path(directory) / f"mcp-launch-{width}.png", animations="disabled"
                )
            await page.keyboard.press("Escape")
            await async_expect(chip).to_have_accessible_name("MCP services: 1 selected")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url(f"{base_url}/c/{session_id}")
            assert len(creates) == 1
            assert creates[0]["mcp_registry_services"] == ["tracker"]
            assert creates[0]["host_type"] == "managed"
        finally:
            await page.unroute_all(behavior="wait")
            await browser.close()

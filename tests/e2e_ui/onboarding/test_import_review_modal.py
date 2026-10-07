"""E2E: the import modal opens once for a newly connected host.

``/v1/hosts``, host-scoped ``/v1/skills``, and the host MCP inventory are
stubbed so the test controls exactly what the host's harnesses report while
the rest of the real UI runs against the live e2e server.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page, Route, async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop as _run_in_fresh_loop
from tests.e2e_ui.start_session.helpers import stub_empty_host_picker_data

_HOST_ID = "host_import_e2e"


_HOSTS = {
    "hosts": [
        {
            "host_id": _HOST_ID,
            "name": "import-e2e-host",
            "owner": "e2e",
            "status": "online",
            "configured_harnesses": {"claude-native": True, "codex-native": "needs-auth"},
            "gateway_inference": {"claude-native": True},
        }
    ]
}

_SKILLS = {
    "claude-native": ["review", "toolkit:lint", "toolkit:ship"],
    "codex-native": ["fix-ci"],
}

_MCP_SERVERS = {
    "mcp_servers": [
        {"name": "github", "harness": "claude", "transport": "stdio", "scope": "user"},
        {
            "name": "linear",
            "harness": "claude",
            "transport": "http",
            "scope": "user",
            "url_host": "mcp.linear.app",
        },
        {"name": "docs", "harness": "codex", "transport": "stdio", "scope": "user"},
    ]
}


async def _register_routes(page: Page) -> None:
    async def handle_hosts(route: Route) -> None:
        await route.fulfill(json=_HOSTS)

    async def handle_skills(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        if query.get("host_id") != [_HOST_ID]:
            await route.fallback()
            return
        names = _SKILLS.get(query.get("harness", [""])[0], [])
        await route.fulfill(json={"skills": [{"name": n, "description": ""} for n in names]})

    async def handle_mcp_servers(route: Route) -> None:
        await route.fulfill(json=_MCP_SERVERS)

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/skills?*", handle_skills)
    await page.route(f"**/v1/hosts/{_HOST_ID}/mcp-servers", handle_mcp_servers)
    await stub_empty_host_picker_data(page, _HOST_ID)


def test_import_modal_opens_once_and_reopens_from_settings(live_server: str) -> None:
    """A new host's imports show once, stay dismissed on reload, and reopen in Settings."""
    _run_in_fresh_loop(_drive(live_server))


async def _drive(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(page)
            await page.goto(f"{base_url}/")

            dialog = page.get_by_role("dialog", name="Your setup is ready")
            await expect(dialog).to_be_visible(timeout=30_000)
            await expect(dialog).to_contain_text("These carry over automatically.")
            await expect(dialog).to_contain_text("Databricks Unity Gateway")
            # Review only: nothing to select.
            await expect(dialog.get_by_role("checkbox")).to_have_count(0)

            assets = dialog.get_by_role("tablist", name="Asset type")
            await expect(assets.get_by_role("tab")).to_have_text(
                ["MCPs 2", "Skills 1", "Plugins 1"]
            )
            await expect(dialog.get_by_role("list", name="MCPs")).to_contain_text("mcp.linear.app")
            await assets.get_by_role("tab", name="Plugins").click()
            await expect(dialog.get_by_role("list", name="Plugins")).to_contain_text("toolkit")
            await expect(dialog.get_by_role("list", name="Plugins")).to_contain_text("2 skills")

            await dialog.get_by_role("tab", name="Codex").click()
            await expect(assets.get_by_role("tab")).to_have_text(["MCPs 1", "Skills 1"])
            await assets.get_by_role("tab", name="Skills").click()
            await expect(dialog.get_by_role("list", name="Skills")).to_contain_text("fix-ci")
            await expect(dialog).not_to_contain_text("Databricks Unity Gateway")

            await dialog.get_by_role("button", name="Confirm").click()
            await expect(dialog).to_be_hidden()
            reviewed = await page.evaluate(
                f"window.localStorage.getItem('omnigent:imports-reviewed:{_HOST_ID}')"
            )
            assert reviewed is not None

            await page.reload()
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await expect(dialog).to_be_hidden()

            await page.goto(f"{base_url}/settings/import")
            await page.get_by_role("button", name="Review imports on import-e2e-host").click()
            await expect(dialog).to_be_visible()
            await expect(dialog.get_by_role("list", name="MCPs")).to_contain_text("github")
            await dialog.get_by_role("button", name="Close").click()
            await expect(dialog).to_be_hidden()
        finally:
            await browser.close()


def test_import_modal_stays_closed_without_assets(live_server: str) -> None:
    """A host whose harnesses bring nothing never opens the modal on its own."""
    _run_in_fresh_loop(_drive_empty(live_server))


async def _drive_empty(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await page.route("**/v1/hosts", lambda route: route.fulfill(json=_HOSTS))
            await page.route("**/v1/skills?*", lambda route: route.fulfill(json={"skills": []}))
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/mcp-servers",
                lambda route: route.fulfill(json={"mcp_servers": []}),
            )
            await stub_empty_host_picker_data(page, _HOST_ID)
            async with page.expect_response(lambda r: r.url.endswith("/mcp-servers")):
                await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # Give the gate a moment to act on the settled inventory.
            await page.wait_for_timeout(1_000)
            await expect(page.get_by_role("dialog", name="Your setup is ready")).to_be_hidden()
            reviewed = await page.evaluate(
                f"window.localStorage.getItem('omnigent:imports-reviewed:{_HOST_ID}')"
            )
            assert reviewed is None
        finally:
            await browser.close()

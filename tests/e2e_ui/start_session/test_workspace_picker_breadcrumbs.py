"""Filesystem breadcrumbs render one root slash without changing navigation."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.helpers import (
    open_landing_workspace_picker,
    stub_empty_host_picker_data,
)
from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _agents_body,
    _hosts_body,
    _run_in_fresh_loop,
)

_HOME = "/Users/alice"
_DIRECTORIES = {
    "/": ["/Users", "/var"],
    "/Users": [_HOME],
    _HOME: [f"{_HOME}/projects"],
    f"{_HOME}/projects": [f"{_HOME}/projects/app"],
    f"{_HOME}/projects/app": [],
    "/var": ["/var/log"],
    "/var/log": [],
}


def test_workspace_picker_breadcrumbs(live_server: str, browser_name: str) -> None:
    """Root, absolute and home-relative paths retain their navigation targets."""
    _run_in_fresh_loop(_drive(live_server, browser_name))


async def _drive(base_url: str, browser_name: str) -> None:
    async with async_playwright() as playwright:
        browser = await getattr(playwright, browser_name).launch()
        page = await browser.new_page()
        try:
            await page.route(
                re.compile(r"/v1/agents(?:\?.*)?$"),
                lambda route: route.fulfill(content_type="application/json", body=_agents_body()),
            )
            await page.route(
                re.compile(r"/v1/hosts(?:\?.*)?$"),
                lambda route: route.fulfill(content_type="application/json", body=_hosts_body()),
            )
            await stub_empty_host_picker_data(page, _HOST_ID)

            async def filesystem(route: Route) -> None:
                path = unquote(urlsplit(route.request.url).path).split("/filesystem", 1)[1]
                path = path or _HOME
                await route.fulfill(
                    json={
                        "object": "list",
                        "data": [
                            {
                                "name": child.rsplit("/", 1)[1],
                                "path": child,
                                "type": "directory",
                                "bytes": None,
                                "modified_at": 0,
                            }
                            for child in _DIRECTORIES[path]
                        ],
                        "has_more": False,
                    }
                )

            await page.route(f"**/v1/hosts/{_HOST_ID}/filesystem**", filesystem)
            await page.goto(f"{base_url}/")
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_have_text(
                "alice", timeout=30_000
            )
            await open_landing_workspace_picker(page)
            breadcrumbs = page.get_by_test_id("workspace-picker-breadcrumbs")
            path_input = page.get_by_test_id("workspace-picker-path-input")
            up = page.get_by_test_id("workspace-picker-up")
            await expect(breadcrumbs).to_have_text("alice")

            await up.click()
            await expect(breadcrumbs).to_have_text("/Users")
            await expect(path_input).to_have_value("/Users")
            await page.get_by_test_id("workspace-picker-home").click()
            await expect(breadcrumbs).to_have_text("/")
            await expect(path_input).to_have_value("/")
            await expect(up).to_be_disabled()

            for folder, label, path in (
                ("Users", "/Users", "/Users"),
                ("alice", "alice", _HOME),
                ("projects", "alice/projects", f"{_HOME}/projects"),
                ("app", "alice/projects/app", f"{_HOME}/projects/app"),
            ):
                await page.get_by_test_id(f"workspace-picker-entry-{folder}").click()
                await expect(breadcrumbs).to_have_text(label)
                await expect(path_input).to_have_value(path)

            await breadcrumbs.get_by_role("button", name="projects", exact=True).click()
            await expect(breadcrumbs).to_have_text("alice/projects")
            await expect(path_input).to_have_value(f"{_HOME}/projects")

            await page.get_by_test_id("workspace-picker-home").click()
            await expect(breadcrumbs).to_have_text("alice")
            await expect(path_input).to_have_value(_HOME)
            await breadcrumbs.get_by_role("button", name="alice", exact=True).click()
            await path_input.fill("/var/log")
            await path_input.press("Enter")
            await expect(breadcrumbs).to_have_text("/var/log")
            await expect(path_input).to_have_value("/var/log")
            await up.click()
            await expect(breadcrumbs).to_have_text("/var")
            await expect(path_input).to_have_value("/var")
        finally:
            await browser.close()

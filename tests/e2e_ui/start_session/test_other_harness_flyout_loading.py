"""E2E: editing a harness under "Other..." keeps the flyout open while its models load.

The landing picker fetches a host model catalog only for the selected harness.
Clicking Edit on Pi (listed under the harness "Other..." flyout) selects it and
starts that fetch; the menu must stay open and show the models once they
arrive, without a second click.
"""

from __future__ import annotations

import asyncio
import json
import re

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _agents_body,
    _pi_native_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
)

_PI_MODEL_ID = "example/model-a"


def test_other_harness_flyout_stays_open_while_models_load(
    seeded_session: tuple[str, str],
) -> None:
    """Edit on an "Other..." harness lands on its model list after the catalog loads.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_other_flyout_edit(base_url, session_id))


async def _drive_other_flyout_edit(base_url: str, session_id: str) -> None:
    agents = json.loads(_agents_body())["data"] + json.loads(_pi_native_agents_body())["data"]
    pi_requested = asyncio.Event()
    release_pi = asyncio.Event()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        try:
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=[],
                agents_body=json.dumps({"data": agents}),
            )
            # Only the stubbed agents: leftover sessions on the shared server
            # would otherwise be scanned in as extra picker entries.
            await page.route(
                "**/v1/sessions?*visibility=mine*",
                lambda route: route.fulfill(json={"data": [], "has_more": False}),
            )

            async def handle_pi_models(route: Route) -> None:
                pi_requested.set()
                await release_pi.wait()
                await route.fulfill(
                    json={
                        "models": [
                            {"id": _PI_MODEL_ID, "displayName": "Model A", "isDefault": True}
                        ]
                    }
                )

            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
                lambda route: route.fulfill(
                    json={"models": [{"id": "sonnet", "displayName": "Sonnet", "isDefault": True}]}
                ),
            )
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/pi-native/model-options", handle_pi_models
            )
            # A real host workspace, so host model catalogs are fetched.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            picker = page.get_by_test_id("new-chat-landing-agent-select")
            await expect(picker).to_be_enabled(timeout=30_000)
            await expect(picker).to_have_attribute("aria-label", re.compile(r"^Claude Code"))
            await picker.click()

            other = page.get_by_test_id("new-chat-landing-harness-more")
            await other.click()
            await expect(other).to_have_attribute("data-state", "open")
            await (
                page.get_by_test_id("new-chat-landing-agent-config-ag_pi_e2e")
                .get_by_text("Edit", exact=True)
                .click()
            )
            await asyncio.wait_for(pi_requested.wait(), timeout=15)

            # While Pi's catalog loads, the flyout and its model submenu stay open.
            models = page.get_by_test_id("new-chat-landing-agent-models")
            await expect(other).to_have_attribute("data-state", "open")
            await expect(models).to_contain_text("Loading models…")

            release_pi.set()
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-model-{_PI_MODEL_ID}")
            ).to_be_visible()
            await expect(models).not_to_contain_text("Loading models…")
            await expect(other).to_have_attribute("data-state", "open")
        finally:
            release_pi.set()
            await context.close()
            await browser.close()

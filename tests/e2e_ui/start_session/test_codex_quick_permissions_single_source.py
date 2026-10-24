"""E2E: the Codex new-chat composer has a single permissions control.

Selecting the Codex agent must leave the hand menu as the only permissions
control: the quick dropdown offers all four stances (including bypass) and the
agent picker's integrated config submenu exposes no "Advanced settings" row
that could diverge from it.
"""

from __future__ import annotations

import re

from playwright.async_api import async_playwright, expect

from tests.e2e_ui.start_session.helpers import select_landing_agent
from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _codex_native_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
)


def test_codex_permissions_single_source(seeded_session: tuple[str, str]) -> None:
    _run_in_fresh_loop(_drive(*seeded_session))


async def _drive(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=[],
                agents_body=_codex_native_agents_body(),
            )
            await page.route(
                re.compile(r"/v1/sessions\?.*kind=any"),
                lambda route: route.fulfill(json={"data": []}),
            )
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            chip = page.get_by_test_id("new-chat-landing-permission-chip")
            await expect(chip).to_be_visible()
            await chip.click()
            menu = page.get_by_test_id("new-chat-landing-permission-menu")
            await expect(menu).to_be_visible()
            labels = await menu.get_by_role("menuitemradio").all_inner_texts()
            assert labels == [
                "Default",
                "Full access",
                "Read only",
                "Bypass approvals & sandbox",
            ], labels
            await page.keyboard.press("Escape")
            await expect(menu).to_be_hidden()

            await select_landing_agent(page, "ag_codex_e2e")
            trigger = page.get_by_test_id("new-chat-landing-agent-select")
            await trigger.click()
            await expect(trigger).to_have_attribute("aria-expanded", "true")
            advanced = page.get_by_text("Advanced settings")
            assert await advanced.count() == 0, "Advanced settings still offered for Codex"
            permissions_row = page.get_by_role("menu").get_by_text("Permissions", exact=False)
            assert await permissions_row.count() == 0, "Agent menu still offers a Permissions row"
        finally:
            await page.context.close()
            await browser.close()

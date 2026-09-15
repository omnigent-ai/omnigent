from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _codex_native_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
)


def test_quick_bypass_is_sent_on_create(seeded_session: tuple[str, str], tmp_path: Path) -> None:
    _run_in_fresh_loop(_drive(*seeded_session, tmp_path))


async def _drive(base_url: str, session_id: str, output: Path) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            bodies: list[dict] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=bodies,
                agents_body=_codex_native_agents_body(),
            )
            await page.route(
                re.compile(r"/v1/sessions\?.*kind=any"),
                lambda route: route.fulfill(json={"data": []}),
            )
            await page.add_init_script(
                "localStorage.setItem('omnigent:recent-workspaces', "
                f"JSON.stringify({{{_HOST_ID}: ['/work/repo']}}))"
            )
            await page.goto(base_url)
            chip = page.get_by_test_id("new-chat-landing-permission-chip")
            await expect(chip).to_be_visible(timeout=30_000)
            await chip.click()
            await page.get_by_test_id("new-chat-landing-permission-menu").screenshot(
                path=output / "quick-bypass-option.png", animations="disabled"
            )
            await page.get_by_role(
                "menuitem", name="Bypass approvals & sandbox", exact=True
            ).click()
            await expect(chip).to_contain_text("Bypass approvals & sandbox")
            await page.locator("[data-composer-card]").screenshot(
                path=output / "quick-bypass-selected.png", animations="disabled"
            )
            await page.get_by_test_id("new-chat-landing-input").fill("Check quick approval")
            async with page.expect_request(
                lambda request: request.method == "POST" and request.url.endswith("/v1/sessions")
            ) as created:
                await page.get_by_test_id("new-chat-landing-submit").click()
            body = (await created.value).post_data_json
            assert body["labels"]["omnigent.codex_native.bypass_sandbox"] == "1"
        finally:
            await browser.close()

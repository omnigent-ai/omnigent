"""Codex launch permissions survive reloads and visits to other harnesses."""

from __future__ import annotations

import json
import re

import pytest
from playwright.async_api import async_playwright, expect

from tests.e2e_ui.start_session.helpers import select_landing_agent
from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _agents_body,
    _codex_native_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
)


@pytest.mark.parametrize(
    ("mode", "label", "args"),
    [
        ("default", "Default", None),
        (
            "full-access",
            "Full access",
            ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
        ),
        (
            "read-only",
            "Read only",
            ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
        ),
        ("bypass", "Bypass approvals & sandbox", None),
    ],
)
def test_codex_permissions_persist(
    seeded_session: tuple[str, str], mode: str, label: str, args: list[str] | None
) -> None:
    _run_in_fresh_loop(_drive(*seeded_session, mode, label, args))


async def _drive(
    base_url: str, session_id: str, mode: str, label: str, args: list[str] | None
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            agents = (
                json.loads(_agents_body())["data"]
                + json.loads(_codex_native_agents_body())["data"]
            )
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=[],
                agents_body=json.dumps({"data": agents}),
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
            await select_landing_agent(page, "ag_codex_e2e")
            chip = page.get_by_test_id("new-chat-landing-permission-chip")
            # Start from another choice so Default must overwrite a saved value too.
            await chip.click()
            await page.get_by_test_id("new-chat-landing-permission-option-full-access").click()
            await expect(page.get_by_test_id("new-chat-landing-permission-menu")).to_be_hidden()
            await chip.click()
            await page.get_by_test_id(f"new-chat-landing-permission-option-{mode}").click()
            await expect(chip).to_have_accessible_name(f"Permission mode: {label}")

            await page.reload()
            await expect(chip).to_have_accessible_name(f"Permission mode: {label}")
            await select_landing_agent(page, "ag_claude_e2e")
            await chip.click()
            await page.get_by_test_id("new-chat-landing-permission-option-plan").click()
            await select_landing_agent(page, "ag_codex_e2e")
            await expect(chip).to_have_accessible_name(f"Permission mode: {label}")
            await page.reload()
            await expect(chip).to_have_accessible_name(f"Permission mode: {label}")

            await page.get_by_test_id("new-chat-landing-input").fill("Check saved permissions")
            async with page.expect_request(
                lambda request: request.method == "POST" and request.url.endswith("/v1/sessions")
            ) as created:
                await page.get_by_test_id("new-chat-landing-submit").click()
            body = (await created.value).post_data_json
            assert body.get("terminal_launch_args") == args
            assert body["labels"].get("omnigent.codex_native.bypass_sandbox") == (
                "1" if mode == "bypass" else None
            )
        finally:
            await page.context.close()
            await browser.close()

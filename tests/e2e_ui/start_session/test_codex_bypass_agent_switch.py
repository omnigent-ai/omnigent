"""E2E: per-harness permission picks survive switching agents on the landing composer.

Pick Codex and "Bypass approvals & sandbox", switch to Claude Code and pick
"Plan", then return to Codex. The composer remembers each harness's last mode
pick (localStorage via ``rememberPickerOptions``), so returning to Codex must
restore bypass — and a second switch back to Claude must restore Plan.
"""

from __future__ import annotations

import json
import re

from playwright.async_api import async_playwright, expect

from tests.e2e_ui.start_session.helpers import select_landing_agent
from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _register_common_routes,
    _run_in_fresh_loop,
)


def _codex_and_claude_agents_body() -> str:
    """Stub ``GET /v1/agents``: both native agents, so the picker can switch."""
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_claude_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": None,
                    "skills": [],
                },
                {
                    "id": "ag_codex_e2e",
                    "name": "codex-native-ui",
                    "display_name": "Codex",
                    "description": "OpenAI's coding agent",
                    "harness": "codex-native",
                    "skills": [],
                },
            ]
        }
    )


def test_codex_bypass_survives_agent_switch(seeded_session: tuple[str, str]) -> None:
    _run_in_fresh_loop(_drive(*seeded_session))


async def _drive(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_and_claude_agents_body(),
            )
            # Neutralize agent discovery so only the two stubbed agents feed
            # the picker.
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

            await select_landing_agent(page, "ag_codex_e2e")
            await expect(chip).to_contain_text("Default")
            await chip.click()
            await page.get_by_test_id("new-chat-landing-permission-option-bypass").click()
            await expect(chip).to_contain_text("Bypass approvals & sandbox")

            await select_landing_agent(page, "ag_claude_e2e")
            await expect(chip).to_be_visible()
            await chip.click()
            await page.get_by_test_id("new-chat-landing-permission-option-plan").click()
            await expect(chip).to_contain_text("Plan")

            await select_landing_agent(page, "ag_codex_e2e")
            await expect(chip).to_contain_text("Bypass approvals & sandbox")

            await select_landing_agent(page, "ag_claude_e2e")
            await expect(chip).to_contain_text("Plan")
        finally:
            # Close the context before the browser so a recording context
            # flushes its video even when an assertion above failed.
            await page.context.close()
            await browser.close()

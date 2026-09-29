"""Diagonal pointer navigation into the landing picker's long harness submenu."""

from __future__ import annotations

import json
import re

from playwright.async_api import async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _agents_body,
    _bundle_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
)


def test_harness_submenu_keeps_diagonal_path_open(seeded_session: tuple[str, str]) -> None:
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_diagonal_path(base_url, session_id))


async def _drive_diagonal_path(base_url: str, session_id: str) -> None:
    agents = json.loads(_agents_body())["data"] + json.loads(_bundle_agents_body())["data"]
    for name in ("opencode", "pi", "antigravity", "kiro", "qwen", "goose", "kimi", "hermes"):
        agents.append(
            {
                "id": f"ag_{name}_grace",
                "name": f"{name}-native-ui",
                "display_name": name,
                "harness": f"{name}-native",
                "description": "",
                "skills": [],
            }
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1600, "height": 1200})
        page = await context.new_page()
        try:
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=[],
                agents_body=json.dumps({"data": agents}),
            )
            await page.route(
                re.compile(r"/v1/sessions\?"),
                lambda route: route.fulfill(json={"data": [], "has_more": False}),
            )
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            other = page.get_by_test_id("new-chat-landing-harness-more")
            await other.hover()
            await expect(other).to_have_attribute("data-state", "open")
            trigger_id = await other.get_attribute("id")
            submenu = page.locator(f'[role="menu"][aria-labelledby="{trigger_id}"]')
            await expect(submenu).to_be_visible()
            await submenu.evaluate(
                "element => Promise.all("
                "element.getAnimations().map(animation => animation.finished))"
            )
            await expect(submenu).to_have_attribute("data-side", "right")
            last = submenu.get_by_role("menuitem").last
            other_box = await other.bounding_box()
            last_box = await last.bounding_box()
            polly = page.get_by_test_id("new-chat-landing-agent-ag_polly_e2e")
            polly_box = await polly.bounding_box()
            assert other_box is not None and last_box is not None and polly_box is not None
            start_x = other_box["x"] + other_box["width"] / 2
            start_y = other_box["y"] + other_box["height"] / 2
            end_x = last_box["x"] + 12
            end_y = last_box["y"] + last_box["height"] / 2
            # Ensure the path crosses a selectable sibling, not just empty menu padding.
            polly_y = polly_box["y"] + polly_box["height"] / 2
            crossing_x = start_x + (end_x - start_x) * (polly_y - start_y) / (end_y - start_y)
            assert start_y < polly_y < end_y
            assert polly_box["x"] < crossing_x < polly_box["x"] + polly_box["width"]
            await page.mouse.move(start_x, start_y)
            await page.mouse.move(end_x, end_y, steps=12)
            await expect(other).to_have_attribute("data-state", "open")
            await expect(last).to_be_focused()
            await page.mouse.click(end_x, end_y)
            await expect(page.get_by_role("menu")).to_have_count(0)
        finally:
            await context.close()
            await browser.close()

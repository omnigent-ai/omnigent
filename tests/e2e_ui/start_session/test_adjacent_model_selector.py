"""Pointer reachability for the desktop composer's adjacent model selector."""

from __future__ import annotations

import re

import pytest
from playwright.async_api import async_playwright, expect

from tests.e2e_ui.start_session.test_model_flows_prelaunch import _CLAUDE_HOST_ROWS
from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _register_common_routes,
    _run_in_fresh_loop,
)


@pytest.mark.parametrize("width", [929, 1600])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_model_selector_stays_adjacent_and_reachable(
    seeded_session: tuple[str, str], width: int, theme: str
) -> None:
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_adjacent_selector(base_url, session_id, width, theme))


async def _drive_adjacent_selector(base_url: str, session_id: str, width: int, theme: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": width, "height": 1000})
        page = await context.new_page()
        try:
            await _register_common_routes(page, created_session_id=session_id, create_bodies=[])
            await page.route(
                re.compile(r"/v1/sessions\?"),
                lambda route: route.fulfill(json={"data": [], "has_more": False}),
            )
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
                lambda route: route.fulfill(json={"models": _CLAUDE_HOST_ROWS}),
            )
            await page.add_init_script(
                f'localStorage.setItem("web-theme", "{theme}");'
                'localStorage.setItem("omnigent:ui-font-size", "16");'
            )
            await page.goto(f"{base_url}/")
            draft = page.get_by_test_id("new-chat-landing-input")
            await expect(draft).to_be_visible(timeout=30_000)
            await expect(draft).to_have_css("font-size", "16px")
            await expect(draft).to_have_css("line-height", "25.6px")
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            harness = page.get_by_test_id("new-chat-landing-agent-ag_claude_e2e")
            await harness.click()
            await expect(page.get_by_role("menu")).to_have_count(2)
            models = page.get_by_test_id("new-chat-landing-agent-models")
            await expect(models).to_be_visible()
            parent = page.get_by_role("menu").first
            child = models.locator('xpath=ancestor::*[@role="menu"]')
            for menu in (parent, child):
                await menu.evaluate(
                    "element => Promise.all("
                    "element.getAnimations().map(animation => animation.finished))"
                )
            parent_box = await parent.bounding_box()
            child_box = await child.bounding_box()
            assert parent_box is not None and child_box is not None
            assert (
                child_box["x"] >= parent_box["x"] + parent_box["width"]
                or child_box["x"] + child_box["width"] <= parent_box["x"]
            ), (parent_box, child_box)
            await expect(child).to_have_attribute("data-side", "left" if width == 929 else "right")

            edit = page.get_by_test_id("new-chat-landing-agent-config-ag_claude_e2e")
            await edit.hover()
            target = models.get_by_role("menuitemcheckbox", name="Sonnet 5", exact=True)
            target_box = await target.bounding_box()
            assert target_box is not None
            await page.mouse.move(
                target_box["x"] + target_box["width"] / 2,
                target_box["y"] + target_box["height"] / 2,
                steps=20,
            )
            await expect(target).to_be_visible()
            await page.mouse.click(
                target_box["x"] + target_box["width"] / 2,
                target_box["y"] + target_box["height"] / 2,
            )
            await expect(page.get_by_test_id("new-chat-landing-agent-model-value")).to_have_text(
                "Sonnet 5"
            )
            await expect(page.get_by_role("menu")).to_have_count(2)
            await page.keyboard.press("Escape")
            await expect(page.get_by_role("menu")).to_have_count(0)
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_be_focused()
        finally:
            await context.close()
            await browser.close()

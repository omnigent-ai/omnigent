"""Catalog labels survive landing selection, session binding, and reload."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from playwright.async_api import Page, Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _close_entry_models,
    _codex_native_agents_body,
    _open_entry_models,
    _register_common_routes,
    _run_in_fresh_loop,
)

_MODEL_ID = "astra-catalog-entry"
_WIRE_MODEL = "system.ai.gpt-6-astra"
_DISPLAY_NAME = "GPT-6 Astra"
_MODEL_OPTIONS = [
    {"id": "gpt-default", "model": "gpt-default", "displayName": "GPT Default", "isDefault": True},
    {"id": _MODEL_ID, "model": _WIRE_MODEL, "displayName": _DISPLAY_NAME},
]


@pytest.mark.parametrize("viewport_width", [1280, 390], ids=["desktop", "mobile"])
def test_catalog_display_name_survives_launch_and_reload(
    seeded_session: tuple[str, str], viewport_width: int
) -> None:
    """Both pickers display the catalog name while create still sends its id."""
    _run_in_fresh_loop(_drive(*seeded_session, viewport_width))


async def _drive(base_url: str, session_id: str, viewport_width: int) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": viewport_width, "height": 900})
        try:
            create_bodies: list[dict] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
            )
            await page.route(
                re.compile(r"/v1/sessions\?.*kind=any"),
                lambda route: route.fulfill(json={"data": []}),
            )
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/codex-native/model-options",
                lambda route: route.fulfill(json={"models": _MODEL_OPTIONS}),
            )

            async def snapshot(route: Route) -> None:
                if route.request.method != "GET":
                    await route.fallback()
                    return
                response = await route.fetch()
                body = await response.json()
                body.update(
                    harness="codex-native",
                    llm_model=_WIRE_MODEL,
                    model_override=_MODEL_ID,
                    model_options=_MODEL_OPTIONS,
                )
                body["labels"] = {**body.get("labels", {}), "omnigent.wrapper": "codex-native-ui"}
                await route.fulfill(response=response, json=body)

            await page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), snapshot)
            await page.add_init_script(
                "localStorage.setItem('omnigent:recent-workspaces', "
                f"JSON.stringify({{{_HOST_ID}: ['/work/repo']}}))"
            )
            await page.goto(base_url)
            await _open_entry_models(page, "ag_codex_e2e")
            await page.get_by_role("menuitemcheckbox", name=_DISPLAY_NAME, exact=True).click()
            await expect(page.get_by_test_id("new-chat-landing-agent-model-value")).to_have_text(
                _DISPLAY_NAME
            )
            await _capture_demo(page, f"catalog-landing-{viewport_width}")
            await _close_entry_models(page)

            await page.get_by_test_id("new-chat-landing-input").fill("Check catalog display names")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url(f"{base_url}/c/{session_id}")
            assert len(create_bodies) == 1
            assert create_bodies[0]["model_override"] == _MODEL_ID

            label = page.get_by_test_id("composer-agent-model-value")
            await expect(label).to_have_text(_DISPLAY_NAME)
            await page.reload()
            await expect(label).to_have_text(_DISPLAY_NAME)
            await page.get_by_test_id("composer-config-gear").click()
            await page.get_by_test_id("composer-agent-edit").click()
            row = page.locator(f'[role="menuitemcheckbox"][data-model-id="{_MODEL_ID}"]')
            await expect(row).to_have_text(_DISPLAY_NAME)
            await expect(row).to_have_attribute("aria-checked", "true")
            await _capture_demo(page, f"catalog-composer-{viewport_width}")
        finally:
            await page.unroute_all(behavior="wait")
            await browser.close()


async def _capture_demo(page: Page, name: str) -> None:
    """Save optional local demo captures outside the tracked source tree."""
    if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
        await page.screenshot(path=Path(directory) / f"{name}.png", animations="disabled")

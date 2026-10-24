"""Edit on a harness under the landing picker's "Other..." flyout keeps the open menu
while its host catalog loads: "Loading models…" shows in place, then the models."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Literal

import pytest
from playwright.async_api import Page, Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _agents_body,
    _pi_native_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
)

_CLAUDE_AGENT_ID = "ag_claude_e2e"
_PI_AGENT_ID = "ag_pi_e2e"
_PI_MODEL_ID = "example/model-a"

# Counts commits that showed the picker's loading placeholder; reset once the
# picker is ready so only the Edit interaction is measured.
_PLACEHOLDER_SAMPLER_JS = """window.placeholderSightings = 0;
new MutationObserver(() => {
  if (document.querySelector('[data-testid="new-chat-landing-picker-loading"]')) {
    window.placeholderSightings += 1;
  }
}).observe(document, {childList: true, subtree: true});
"""

Activation = Literal["pointer", "keyboard"]


@pytest.mark.parametrize("activation", ["pointer", "keyboard"])
def test_other_harness_flyout_stays_open_while_models_load(
    live_server: str, activation: Activation
) -> None:
    """Edit on an "Other..." harness lands on its model list; ``keyboard`` hovers
    "Other..." then uses the arrow keys, so the cursor rests on that row when Pi's
    model submenu opens over it."""
    _run_in_fresh_loop(_drive_other_flyout_edit(live_server, activation))


class _HeldCatalog:
    """Hold one harness's host model catalog until the test releases it."""

    def __init__(self, models: list[dict[str, object]]) -> None:
        self.models = models
        self.requested = asyncio.Event()
        self.released = asyncio.Event()

    async def handle(self, route: Route) -> None:
        self.requested.set()
        await self.released.wait()
        await route.fulfill(json={"models": self.models})


async def _prepare_page(page: Page) -> None:
    agents = json.loads(_agents_body())["data"] + json.loads(_pi_native_agents_body())["data"]
    # The journey never creates a session, so the stubbed create needs no real id.
    await _register_common_routes(
        page,
        created_session_id="unused",
        create_bodies=[],
        agents_body=json.dumps({"data": agents}),
    )
    # Only the stubbed agents: leftover sessions on the shared server would
    # otherwise be scanned in as extra picker entries.
    await page.route(
        "**/v1/sessions?*visibility=mine*",
        lambda route: route.fulfill(json={"data": [], "has_more": False}),
    )
    # A remembered host workspace, so host model catalogs are fetched.
    await page.add_init_script(
        f"""window.localStorage.setItem("omnigent:last-agent-id", {json.dumps(_CLAUDE_AGENT_ID)});
        window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {json.dumps(_HOST_ID)}: ["/work/repo"] }})
        );"""
    )
    await page.add_init_script(_PLACEHOLDER_SAMPLER_JS)


async def _open_ready_picker(page: Page, selected_label: str) -> None:
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    await expect(picker).to_be_enabled(timeout=30_000)
    await expect(picker).to_have_attribute("aria-label", re.compile(rf"^{selected_label}"))
    await expect(picker).not_to_have_attribute("aria-busy", "true")
    await page.evaluate("window.placeholderSightings = 0")
    await picker.click()
    await expect(picker).to_have_attribute("aria-expanded", "true")


async def _expect_menu_kept_until_models_arrive(
    page: Page, catalog: _HeldCatalog, model_id: str
) -> None:
    """The open menu shows the loading row while held, then the released models."""
    await asyncio.wait_for(catalog.requested.wait(), timeout=15)
    await expect(page.get_by_test_id("new-chat-landing-picker-loading")).to_have_count(0)
    models = page.get_by_test_id("new-chat-landing-agent-models")
    await expect(models).to_contain_text("Loading models…")
    catalog.released.set()
    await expect(page.get_by_test_id(f"new-chat-landing-agent-model-{model_id}")).to_be_visible()
    await expect(models).not_to_contain_text("Loading models…")
    assert await page.evaluate("window.placeholderSightings") == 0, (
        "the open menu was replaced by the loading placeholder"
    )


async def _drive_other_flyout_edit(base_url: str, activation: Activation) -> None:
    catalog = _HeldCatalog([{"id": _PI_MODEL_ID, "displayName": "Model A", "isDefault": True}])
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        try:
            await _prepare_page(page)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
                lambda route: route.fulfill(
                    json={"models": [{"id": "sonnet", "displayName": "Sonnet", "isDefault": True}]}
                ),
            )
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/pi-native/model-options", catalog.handle
            )
            await page.goto(f"{base_url}/")
            await _open_ready_picker(page, "Claude Code")

            other = page.get_by_test_id("new-chat-landing-harness-more")
            if activation == "pointer":
                await other.click()
                await expect(other).to_have_attribute("data-state", "open")
                await (
                    page.get_by_test_id(f"new-chat-landing-agent-config-{_PI_AGENT_ID}")
                    .get_by_text("Edit", exact=True)
                    .click()
                )
            else:
                await other.hover()
                await expect(other).to_have_attribute("data-state", "open")
                # Right Arrow enters the flyout on its first row (Pi); Right Arrow
                # on that row opens Edit.
                await page.keyboard.press("ArrowRight")
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
                ).to_be_focused()
                await page.keyboard.press("ArrowRight")

            # While Pi's catalog loads, the flyout and its model submenu stay open.
            await _expect_menu_kept_until_models_arrive(page, catalog, _PI_MODEL_ID)
            await expect(other).to_have_attribute("data-state", "open")
        finally:
            catalog.released.set()
            await context.close()
            await browser.close()

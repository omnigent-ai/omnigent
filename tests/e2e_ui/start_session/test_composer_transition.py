from __future__ import annotations

import asyncio
import re
from pathlib import Path

from playwright.async_api import async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _close_entry_models,
    _open_entry_models,
    _register_common_routes,
    _run_in_fresh_loop,
)


def test_selected_model_survives_delayed_create(
    seeded_session_pair: tuple[str, str, str], tmp_path: Path
) -> None:
    _run_in_fresh_loop(_drive(*seeded_session_pair, tmp_path))


async def _drive(base_url: str, session_id: str, previous_id: str, output: Path) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1000})
        release = asyncio.Event()
        selected = "claude-opus-4-8[1m]"
        previous = "claude-sonnet-5"
        rows = [{"id": selected, "model": selected, "displayName": "Opus"}]
        try:
            await _register_common_routes(page, created_session_id=session_id, create_bodies=[])
            await page.route(
                re.compile(r"/v1/sessions\?.*kind=any"),
                lambda route: route.fulfill(json={"data": []}),
            )
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
                lambda route: route.fulfill(json={"models": rows}),
            )

            async def snapshot(route):
                response = await route.fetch()
                body = await response.json()
                body.update(
                    llm_model=previous if previous_id in route.request.url else selected,
                    model_options=rows,
                    harness="claude",
                    reasoning_effort="high",
                )
                body["labels"] = {
                    **body.get("labels", {}),
                    "omnigent.wrapper": "claude-code-native-ui",
                }
                await route.fulfill(response=response, json=body)

            for identity in (session_id, previous_id):
                await page.route(re.compile(rf"/v1/sessions/{identity}(?:\?.*)?$"), snapshot)

            async def create(route):
                if route.request.method != "POST":
                    await route.fallback()
                    return
                await release.wait()
                await route.fulfill(json={"id": session_id})

            await page.route(re.compile(r"/v1/sessions(?:\?.*)?$"), create)
            await page.goto(f"{base_url}/c/{previous_id}")
            await expect(page.get_by_test_id("composer-agent-config-value")).to_contain_text(
                previous
            )
            await page.evaluate(
                "localStorage.setItem('omnigent:recent-workspaces', "
                f"JSON.stringify({{{_HOST_ID}: ['/work/repo']}}))"
            )
            await page.get_by_test_id("new-chat-button").click()
            await _open_entry_models(page, "ag_claude_e2e")
            await page.get_by_role("menuitemcheckbox", name=selected, exact=True).click()
            await page.get_by_role("menuitemcheckbox", name="High", exact=True).click()
            await _close_entry_models(page)
            await page.evaluate("""() => {
              window.composerSamples = [];
              const capture = () => {
                const selector = '[data-testid="composer-agent-config-value"]';
                const label = document.querySelector(selector);
                if (label) window.composerSamples.push({
                  path: location.pathname, text: label.textContent});
              };
              new MutationObserver(capture).observe(document.body,
                {subtree:true, childList:true, characterData:true});
            }""")
            await page.get_by_test_id("new-chat-landing-input").fill("Check model transition")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url(re.compile(r"/c/temp"))
            label = page.get_by_test_id("composer-agent-config-value")
            await expect(label).to_be_visible()
            temporary_label = await label.inner_text()
            await expect(label).to_contain_text("High")
            await page.locator("[data-composer-card]").screenshot(
                path=output / "temporary-model.png", animations="disabled"
            )
            release.set()
            await page.wait_for_url(f"{base_url}/c/{session_id}")
            await expect(label).to_contain_text(selected)
            await page.locator("[data-composer-card]").screenshot(
                path=output / "bound-model.png", animations="disabled"
            )
            assert selected in temporary_label, temporary_label
            samples = await page.evaluate("window.composerSamples")
            assert any("/c/temp" in sample["path"] for sample in samples), samples
            assert all(previous not in sample["text"] for sample in samples), samples
        finally:
            release.set()
            await page.unroute_all(behavior="wait")
            await browser.close()

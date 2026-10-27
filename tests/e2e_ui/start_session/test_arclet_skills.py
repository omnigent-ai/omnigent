"""The first Arclet prompt discovers Arca skills without launching on the Arca."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _agents_body,
    _codex_native_agents_body,
    _hosts_body,
    _managed_info_body,
    _register_common_routes,
    _run_in_fresh_loop,
)


@pytest.mark.parametrize("harness,prefix", [("claude-native", "/"), ("codex-native", "$")])
def test_arclet_first_prompt_uses_connected_arca_skills(
    seeded_session: tuple[str, str], output_path: str, harness: str, prefix: str
) -> None:
    """Verify browser discovery, completion, and the managed initial-input payload."""
    _run_in_fresh_loop(
        _drive(*seeded_session, evidence=Path(output_path), harness=harness, prefix=prefix)
    )


async def _drive(
    base_url: str, session_id: str, *, evidence: Path, harness: str, prefix: str
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 960})
        creates: list[dict] = []
        discoveries: list[dict[str, list[str]]] = []
        try:
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=creates,
                agents_body=_codex_native_agents_body()
                if harness == "codex-native"
                else _agents_body(),
            )
            info = json.loads(_managed_info_body())
            info.update(sandbox_provider="arclet", durable_initial_input=True)
            await page.route("**/v1/info", lambda route: route.fulfill(json=info))
            hosts = json.loads(_hosts_body())
            hosts["hosts"][0].update(host_id="connected-arca", name="e2e's arca")
            await page.route("**/v1/hosts", lambda route: route.fulfill(json=hosts))
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"),
                lambda route: route.fulfill(json={"data": []}),
            )

            async def discover(route: Route) -> None:
                discoveries.append(parse_qs(urlparse(route.request.url).query))
                await route.fulfill(
                    json={
                        "skills": [
                            {
                                "name": "dev:review",
                                "description": "Review a change with your installed skill",
                            }
                        ]
                    }
                )

            await page.route("**/v1/skills?*", discover)
            await page.goto(base_url)
            composer = page.get_by_test_id("new-chat-landing-input")
            await expect(composer).to_be_visible(timeout=30_000)
            await composer.fill(f"{prefix}review")
            row = page.get_by_test_id("slash-menu-item-dev:review")
            await expect(row).to_be_visible(timeout=30_000)
            assert discoveries
            assert discoveries[-1] == {
                "host_id": ["connected-arca"],
                "harness": [harness],
                "path": ["~"],
                "agent_id": ["ag_codex_e2e" if harness == "codex-native" else "ag_claude_e2e"],
            }
            assert creates == []
            evidence.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(evidence / f"{harness}-arclet-skills.png"))
            await row.click()
            await expect(composer).to_have_value(f"{prefix}dev:review ")
            prompt = f"{prefix}dev:review check these changes"
            await composer.fill(prompt)
            await page.get_by_test_id("new-chat-landing-submit").click()
            await expect(page).to_have_url(re.compile(rf"/c/{session_id}$"))
            assert len(creates) == 1
            assert creates[0]["host_type"] == "managed"
            assert creates[0]["sandbox_provider"] == "arclet"
            assert "host_id" not in creates[0]
            assert creates[0]["initial_input"] == {
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            }
        finally:
            await browser.close()

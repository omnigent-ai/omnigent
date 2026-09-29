"""E2E (hermetic): a new Codex session must offer Plan mode before the first prompt.
Codex's landing add menu left Plan disabled until the session existed; the landing must
arm Plan for Codex and seed the ``omnigent.codex_native.collaboration_mode`` label."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.helpers import select_landing_agent
from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _codex_native_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
    _wait_until,
)

_CODEX_AGENT_ID = "ag_codex_e2e"
_COLLABORATION_MODE_LABEL_KEY = "omnigent.codex_native.collaboration_mode"


def test_new_codex_session_offers_plan_mode_before_first_prompt(
    seeded_session: tuple[str, str],
) -> None:
    """Plan mode can be engaged on the landing screen and rides the create call."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive(base_url, session_id))


async def _stamp_codex_native_session(page: Any, session_id: str) -> None:
    """Make the browser see the created session as the codex-native wrapper."""

    async def handle(route: Route) -> None:
        if route.request.method != "GET":
            await route.continue_()
            return
        response = await route.fetch()
        payload = await response.json()
        payload["labels"] = {**payload.get("labels", {}), "omnigent.wrapper": "codex-native-ui"}
        await route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    await page.route(
        lambda url: urlparse(url).path == f"/v1/sessions/{session_id}",
        handle,
    )


async def _drive(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
            )
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"),
                lambda route: route.fulfill(json={"data": []}),
            )
            await _stamp_codex_native_session(page, session_id)
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
            await select_landing_agent(page, _CODEX_AGENT_ID)

            await page.get_by_test_id("new-chat-landing-attach").click()
            await expect(page.get_by_test_id("new-chat-landing-add-menu")).to_be_visible()
            landing_plan = page.get_by_test_id("composer-plan-action")
            await expect(landing_plan).to_be_visible()
            landing_plan_enabled = await landing_plan.is_enabled()
            if landing_plan_enabled:
                await landing_plan.click()
                # The armed pick is visible before anything is typed.
                await expect(page.get_by_test_id("new-chat-landing-plan-mode")).to_be_visible()
            else:
                await page.keyboard.press("Escape")
            await expect(page.get_by_test_id("new-chat-landing-add-menu")).to_be_hidden()

            await page.get_by_test_id("new-chat-landing-input").fill(
                "Plan how to add a hello.txt file with a greeting"
            )
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_until(lambda: len(create_bodies) == 1)
            await page.wait_for_url(f"**/c/{session_id}**", timeout=30_000)

            # The in-session composer keeps offering Plan mode after launch.
            await page.get_by_test_id("composer-attach").click()
            await expect(page.get_by_test_id("composer-plan-action")).to_be_enabled(timeout=15_000)
            await page.keyboard.press("Escape")

            assert landing_plan_enabled, (
                "the new-session composer's Plan entry is disabled for Codex; "
                "Plan mode is only selectable after the first prompt is submitted"
            )
            body = create_bodies[0]
            assert body["agent_id"] == _CODEX_AGENT_ID, body
            labels = body.get("labels") or {}
            assert labels.get(_COLLABORATION_MODE_LABEL_KEY) == "plan", body
        finally:
            await context.close()
            await browser.close()

"""E2E (hermetic): Hermes pre-launch model picker pins the session model.

The new-session composer must offer Hermes a Model picker before the session
starts (it previously rendered no model controls at all for hermes-native),
and the picked row must ride the create POST as ``model_override`` — the field
the hermes-native runner passes to the TUI as ``--model``.

Mirrors ``test_opencode_model_prelaunch``: the driving surface is the real SPA
in a browser; only the server edges the landing screen consults (hosts, agents,
model-options, create) are faked. The stubbed Hermes catalog carries one
qualified default row, and the failure mode is either "no Model control
renders" or "the control renders but the create body carries no
model_override".
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _close_entry_models,
    _open_entry_models,
    _register_common_routes,
    _run_in_fresh_loop,
    _wait_until,
)

_HERMES_MODEL_ID = "zai/glm-5.3-flash"


def _hermes_native_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the native Hermes agent.

    ``hermes-native-ui`` + ``harness: "hermes-native"`` maps (via
    ``nativeCodingAgents``) to the ``modelPicker`` capability. Sole agent, so
    it auto-selects and no explicit pick is needed.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_hermes_e2e",
                    "name": "hermes-native-ui",
                    "display_name": "Hermes",
                    "description": "Hermes Agent TUI",
                    "harness": "hermes-native",
                    "skills": [],
                }
            ]
        }
    )


def test_new_hermes_session_offers_model_picker(
    seeded_session: tuple[str, str],
) -> None:
    """The landing composer lists the host's Hermes catalog and pins the pick."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_hermes_model_prelaunch(base_url, session_id))


async def _drive_hermes_model_prelaunch(base_url: str, session_id: str) -> None:
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
                agents_body=_hermes_native_agents_body(),
            )

            # Neutralize agent discovery so ONLY the stubbed Hermes agent
            # feeds the picker — a native agent another test left behind on
            # the shared server would rank ahead and auto-select.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            async def handle_model_options(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "models": [
                                {
                                    "id": _HERMES_MODEL_ID,
                                    "model": _HERMES_MODEL_ID,
                                    "displayName": "glm-5.3-flash",
                                    "default": True,
                                }
                            ]
                        }
                    ),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/hermes-native/model-options",
                handle_model_options,
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
            await _open_entry_models(page, "ag_hermes_e2e")

            models_menu = page.get_by_role("menu").filter(
                has=page.get_by_test_id("new-chat-landing-agent-models")
            )
            await expect(models_menu).to_be_visible()
            # The menu row shows the displayName; the qualified id is what the
            # create body must carry (asserted below).
            await expect(models_menu).to_contain_text("glm-5.3-flash")
            await models_menu.get_by_text("glm-5.3-flash", exact=True).click()
            await _close_entry_models(page)

            # The pick must take effect: it rides the create call as
            # ``model_override``, exactly like the OpenCode lane asserts.
            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_hermes_e2e", body
            assert body.get("model_override") == _HERMES_MODEL_ID, body
        finally:
            await context.close()
            await browser.close()

"""E2E (hermetic): OpenCode pre-launch model picker pins the session model.

The new-session composer must offer OpenCode a Model picker before the session
starts (it previously rendered no model controls at all for opencode-native),
and the picked row must ride the create POST as ``model_override`` — the field
the opencode-native runner already reads per prompt.

Mirrors ``test_codex_effort_prelaunch``: the driving surface is the real SPA in
a browser; only the server edges the landing screen consults (hosts, agents,
model-options, create) are faked. The stubbed OpenCode catalog carries one
zai provider row, and the failure mode is either "no Model control renders"
or "the control renders but the create body carries no model_override".
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

_OPENCODE_MODEL_ID = "zai/glm-5.3"


def _opencode_native_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the native OpenCode agent.

    ``opencode-native-ui`` + ``harness: "opencode-native"`` maps (via
    ``nativeCodingAgents``) to the ``modelPicker`` capability. Sole agent, so
    it auto-selects and no explicit pick is needed.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_opencode_e2e",
                    "name": "opencode-native-ui",
                    "display_name": "OpenCode",
                    "description": "Open source coding agent",
                    "harness": "opencode-native",
                    "skills": [],
                }
            ]
        }
    )


def test_new_opencode_session_offers_model_picker(
    seeded_session: tuple[str, str],
) -> None:
    """The landing composer lists the host's OpenCode catalog and pins the pick."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_opencode_model_prelaunch(base_url, session_id))


async def _drive_opencode_model_prelaunch(base_url: str, session_id: str) -> None:
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
                agents_body=_opencode_native_agents_body(),
            )

            # Neutralize agent discovery so ONLY the stubbed OpenCode agent
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
                                    "id": _OPENCODE_MODEL_ID,
                                    "model": "glm-5.3",
                                    "displayName": _OPENCODE_MODEL_ID,
                                }
                            ]
                        }
                    ),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/opencode-native/model-options",
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
            await _open_entry_models(page, "ag_opencode_e2e")

            models_menu = page.get_by_role("menu").filter(
                has=page.get_by_test_id("new-chat-landing-agent-models")
            )
            await expect(models_menu).to_be_visible()
            await expect(models_menu).to_contain_text(_OPENCODE_MODEL_ID)
            await models_menu.get_by_text(_OPENCODE_MODEL_ID, exact=True).click()
            await _close_entry_models(page)

            # The pick must take effect: it rides the create call as
            # ``model_override``, exactly like the Codex effort row asserts
            # its ``reasoning_effort``.
            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_opencode_e2e", body
            assert body.get("model_override") == _OPENCODE_MODEL_ID, body
        finally:
            await context.close()
            await browser.close()

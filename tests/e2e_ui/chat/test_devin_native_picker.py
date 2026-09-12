"""E2E: the New Chat picker offers Devin with its own model + effort lists.

Opening Devin's config submenu in the New Chat picker must surface:

* Devin's model **families** (from the host's ``devin-native`` catalog probe) —
  not Claude's or Pi's list; and
* an Effort ladder, because Devin has no ``--effort`` flag and Omnigent composes
  the (model, effort) pair into one variant id at launch
  (``resolve_devin_launch_model``). Without the ladder rendered there is no way
  to express effort when starting a chat.

Regression target: Devin declares only the ``devinMode`` capability, so the
model + effort sections hang off that flag alone. Both the config-content gate
(``selectedAgentHasKnobs``) and the models-section gate must honour it, or the
config submenu (and with it every model/effort control) never renders — the
``agent-config-*`` Edit entry ``_open_entry_models`` clicks would not even exist.

Drives the picker through the shared ``_open_entry_models`` helper so it opens
the config submenu the same way the passing Pi/Codex picker tests do, rather
than re-deriving the menu navigation here.
"""

from __future__ import annotations

import json
import re

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _open_entry_models,
    _register_common_routes,
    _run_in_fresh_loop,
)

_DEVIN_AGENT_ID = "ag_devin_e2e"

# Devin model *families* (claude-opus-5, swe-2, …), the shape
# ``list_devin_cli_model_options`` returns. Effort is a separate axis, so no
# variant suffixes appear here.
_DEVIN_MODELS = [
    {"id": "claude-opus-5", "displayName": "Claude Opus 5", "isDefault": False},
    {"id": "swe-2", "displayName": "SWE-2", "isDefault": True},
]


def _devin_native_agents_body() -> str:
    """Stub ``GET /v1/agents``: the native Devin agent as the sole built-in.

    ``name: "devin-native-ui"`` + ``harness: "devin-native"`` is what the
    frontend maps (via ``nativeCodingAgents``) to the ``devinMode`` capability
    that gates Devin's model + effort rows. Sole agent, so it auto-selects and
    no explicit pick is needed before opening its config.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": _DEVIN_AGENT_ID,
                    "name": "devin-native-ui",
                    "display_name": "Devin",
                    "description": "Cognition's coding agent",
                    "harness": "devin-native",
                    "skills": [],
                }
            ]
        }
    )


def test_devin_picker_offers_its_own_models_and_effort(
    seeded_session: tuple[str, str],
) -> None:
    """Devin's config submenu exposes its own families plus an Effort ladder.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive(base_url, session_id))


async def _drive(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=[],
                agents_body=_devin_native_agents_body(),
            )

            async def handle_devin_models(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"models": _DEVIN_MODELS}),
                )

            async def handle_agent_scan(route: Route) -> None:
                # Only the stubbed built-in Devin should feed the picker; leftover
                # sessions on the shared e2e_ui server must not leak in.
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/devin-native/model-options",
                handle_devin_models,
            )
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # A real (non-sandbox) host workspace so the devin-native catalog is
            # probed (`useHostModelOptions(hostId, "devin-native", !sandbox)`).
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

            # Open Devin's config submenu (the `agent-config-*` Edit entry only
            # exists when `selectedAgentHasKnobs` honours `devinMode`).
            await _open_entry_models(page, _DEVIN_AGENT_ID)

            # Devin's own families render, from the devin-native catalog probe.
            models = page.get_by_test_id("new-chat-landing-agent-models")
            await expect(models).to_be_visible(timeout=30_000)
            for model in _DEVIN_MODELS:
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-model-{model['id']}")
                ).to_be_visible()

            # The Effort ladder renders. Devin has no --effort flag, so this is
            # the only way to express effort when starting a chat; the runner
            # composes it onto the model id at launch.
            await expect(page.get_by_test_id("new-chat-landing-agent-efforts")).to_be_visible()
            for rung in ("low", "medium", "high", "xhigh", "max"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-effort-{rung}")
                ).to_be_visible()

            # A model + effort pick sticks, which is what the create call sends as
            # model_override + reasoning_effort.
            await page.get_by_test_id("new-chat-landing-agent-model-claude-opus-5").click()
            await expect(
                page.get_by_test_id("new-chat-landing-agent-model-claude-opus-5")
            ).to_have_attribute("data-state", "checked")
            await page.get_by_test_id("new-chat-landing-agent-effort-xhigh").click()
            await expect(
                page.get_by_test_id("new-chat-landing-agent-effort-xhigh")
            ).to_have_attribute("data-state", "checked")
        finally:
            await browser.close()

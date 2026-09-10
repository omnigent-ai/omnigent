"""E2E (hermetic): the new-session gear must offer Codex reasoning effort.

When creating a new Codex session in the Web UI, the composer gear's config
modal used to show Model + Approval rows but no reasoning-effort selector,
while the gear of an existing Codex session (the ``composer-config-effort``
row, driven by the same per-model ``supportedReasoningEfforts`` metadata)
does let effort be picked. The new-session composer must expose the Codex
effort selector before the session starts, consistent with the
existing-session composer.

The driving surface is the real SPA in a browser; only the server edges the
landing screen consults (hosts, agents, model-options) are faked, exactly like
the sibling tests in ``test_start_session.py``. The stubbed Codex catalog
advertises per-model effort ladders — the metadata the in-session gear builds
its Effort row from — so the levels are available to the landing screen; the
failure is that the new-session modal never renders a control for them.
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _codex_native_agents_body,
    _open_entry_config,
    _register_common_routes,
    _run_in_fresh_loop,
    _save_config,
    _wait_until,
)

# Host catalog rows for codex-native, shaped like Codex's own ``model/list``
# payload: each model advertises its effort ladder, exactly as the in-session
# snapshot's ``model_options`` do (see tests/e2e_ui/chat/
# test_codex_model_metadata.py).
_CODEX_HOST_ROWS = [
    {
        "id": "gpt-live-default",
        "displayName": "GPT Live Default",
        "isDefault": True,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Fastest"},
            {"reasoningEffort": "medium", "description": "Balanced"},
            {"reasoningEffort": "high", "description": "Most thorough"},
        ],
    },
    {
        "id": "gpt-live-fast",
        "displayName": "GPT Live Fast",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Fastest"},
            {"reasoningEffort": "medium", "description": "Balanced"},
        ],
    },
]


def test_new_codex_session_gear_offers_reasoning_effort(
    seeded_session: tuple[str, str],
) -> None:
    """The new-session config modal lets a Codex reasoning effort be picked.

    With a Codex agent selected on the new-chat landing screen, the gear
    modal must render a reasoning-effort selector (as it already does for
    Claude Code and Pi, and as the in-session Codex gear does), the
    catalog-advertised levels must be selectable, and the picked level must
    ride the create call as ``reasoning_effort`` — the field the
    codex-native launch path already consumes
    (``config.extra["reasoning_effort"]``), and the same field the Claude
    landing row commits.

    Red while the bug lives: the Codex branch of ``HarnessConfigModal``
    renders only Model + Approval rows, so no effort control ever appears.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_codex_effort_prelaunch(base_url, session_id))


async def _drive_codex_effort_prelaunch(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Explicit context so the `finally` can close IT before the browser —
        # closing only the browser can drop an in-flight video recording
        # (OMNIGENT_E2E_RECORD_DIR) on the floor as a 0-byte file.
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

            # Neutralize agent discovery so ONLY the stubbed Codex agent feeds
            # the picker — a native agent another test left behind on the
            # shared server would rank ahead, auto-select, and the gear would
            # open the wrong agent's config modal.
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
                    body=json.dumps({"models": _CODEX_HOST_ROWS}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/codex-native/model-options",
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
            await _open_entry_config(page, "ag_codex_e2e")

            modal = page.get_by_test_id("new-chat-landing-config-modal")
            await expect(modal).to_be_visible()
            # Sanity: the Codex branch resolved the host catalog — the Model
            # row names the catalog default — so the effort metadata riding
            # the same response was available to the modal too.
            await expect(page.get_by_test_id("new-chat-landing-config-model")).to_contain_text(
                "Default (GPT Live Default)"
            )

            # THE BUG: no reasoning-effort control exists in the Codex
            # new-session modal. Locate it the way a user finds it — an
            # effort/thinking select inside the modal — plus the sibling
            # rows' testid, so any reasonable fix (reusing the Claude row's
            # testid or adding a codex-specific one) turns this green.
            effort = (
                modal.get_by_role(
                    "combobox",
                    name=re.compile(r"(reasoning\s+)?effort|thinking level", re.IGNORECASE),
                )
                .or_(modal.get_by_test_id("new-chat-landing-config-effort"))
                .first
            )
            await expect(effort).to_be_visible()
            await expect(effort).to_be_enabled()

            # The catalog's ladder must actually be selectable: pick "high"
            # (advertised by the default model's supportedReasoningEfforts).
            await effort.click()
            option = page.get_by_role("option", name=re.compile(r"^high$", re.IGNORECASE))
            await expect(option).to_be_visible()
            await option.click()
            await expect(effort).to_contain_text(re.compile(r"high", re.IGNORECASE))
            await _save_config(page)

            # The pick must take effect: it rides the create call as
            # ``reasoning_effort``, exactly like the Claude Code landing row
            # (test_start_session_select_effort asserts the same field).
            await page.get_by_test_id("new-chat-landing-input").fill("set up the project")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_codex_e2e", body
            assert body.get("reasoning_effort") == "high", body
        finally:
            await context.close()
            await browser.close()

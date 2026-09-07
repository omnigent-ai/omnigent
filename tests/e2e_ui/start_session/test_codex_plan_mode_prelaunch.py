"""E2E: Codex Plan mode must be selectable BEFORE launch.

Plan-first Codex users set plan mode before typing anything (the TUI habit is
shift+tab before the first message). In the web UI the plan-mode toggle
(``codex-plan-mode-toggle``) only renders inside an existing session's
composer, so on the new-session landing screen — the only surface a user sees
before submitting the first prompt — there is no way to engage Plan mode at
all. This test encodes the expected behavior and is red until a pre-launch
plan-mode control lands.

Journey (the reporter's): open the web UI new-session screen, select the
Codex agent, look for a Plan-mode control (on the landing composer or inside
its gear-icon run-config modal) before typing — and, once it exists, engage
it and submit the first prompt so the created session starts in Plan mode.

The driving surface is the real SPA in a browser; only the server edges the
landing screen consults (hosts, agents, model-options) are faked, exactly
like the sibling tests in ``test_start_session.py``. The create
``POST /v1/sessions`` is captured so the plan selection's server handoff can
be asserted regardless of its wire encoding (a ``collaboration_mode`` create
field, a label, a launch arg, or an immediate post-create PATCH).
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _codex_native_agents_body,
    _open_entry_config,
    _register_common_routes,
    _run_in_fresh_loop,
    _wait_until,
)

# Any reasonable rendering of a pre-launch Codex Plan-mode affordance: the
# in-session toggle's testid reused on the landing surface, a dedicated
# landing/config testid, or a control carrying the toggle's aria-labels.
_PLAN_CONTROL = (
    '[data-testid*="plan-mode"], [aria-label="Enter Plan mode"], [aria-label="Exit Plan mode"]'
)

# PATCH to a bare session id (``/v1/sessions/{id}``, no subresource) — the
# alternate handoff shape where the SPA creates the session first and then
# immediately PATCHes ``collaboration_mode`` onto it.
_SESSION_PATCH_RE = re.compile(r"/v1/sessions/[^/?]+$")


def _carries_plan(body: object) -> bool:
    """Return whether a captured request body encodes the Plan-mode pick.

    Accepts every plausible wire encoding so the assertion survives the fix's
    implementation choice: the session-update ``collaboration_mode`` field,
    a collaboration-mode label value, or a launch arg.

    :param body: A captured JSON request body (create POST or session PATCH).
    :returns: True when the body carries Plan mode.
    """
    if not isinstance(body, dict):
        return False
    if body.get("collaboration_mode") == "plan":
        return True
    labels = body.get("labels")
    if isinstance(labels, dict) and "plan" in labels.values():
        return True
    launch_args = body.get("terminal_launch_args")
    if isinstance(launch_args, list) and "plan" in launch_args:
        return True
    return False


def test_codex_plan_mode_selectable_before_first_prompt(
    seeded_session: tuple[str, str],
) -> None:
    """A Plan-mode control exists pre-launch and its pick reaches the server.

    Red on the unfixed tree: no Plan-mode affordance renders anywhere on the
    new-session screen — neither on the landing composer nor in the Codex
    run-config modal — so a plan-first user cannot engage Plan mode before
    submitting the first prompt.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_prelaunch_plan_mode(base_url, session_id))


async def _drive_prelaunch_plan_mode(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            patch_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
            )

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
                                    "id": "gpt-live-default",
                                    "displayName": "GPT Live Default",
                                    "isDefault": True,
                                }
                            ]
                        }
                    ),
                )

            async def handle_session_patch(route: Route) -> None:
                # Capture a post-create PATCH (an alternate plan handoff
                # shape) without disturbing it.
                if route.request.method == "PATCH":
                    body = route.request.post_data_json
                    if isinstance(body, dict):
                        patch_bodies.append(body)
                await route.continue_()

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/codex-native/model-options",
                handle_model_options,
            )
            await page.route(_SESSION_PATCH_RE, handle_session_patch)
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

            # The Codex agent is selected and its run-config modal opened —
            # the two places a pre-launch Plan-mode control could live.
            await _open_entry_config(page, "ag_codex_e2e")
            plan_in_modal = await page.locator(_PLAN_CONTROL).count()
            if plan_in_modal == 0:
                # Not in the modal — close it and check the landing composer
                # surface itself (where the in-session toggle would mirror).
                await page.keyboard.press("Escape")
                await page.get_by_test_id("new-chat-landing-input").wait_for(
                    state="visible", timeout=10_000
                )
            plan_control = page.locator(_PLAN_CONTROL).first
            assert await page.locator(_PLAN_CONTROL).count() > 0, (
                "no Codex Plan-mode control exists on the "
                "new-session screen (checked the landing composer and the "
                "Codex run-config modal). Plan mode is only selectable after "
                "submitting the first prompt, so a plan-first user cannot "
                "start a Codex session in Plan mode from the web UI."
            )

            # The control exists (post-fix path): engage it, then launch.
            await plan_control.click()
            engaged = False
            for handle in await page.locator(_PLAN_CONTROL).all():
                if (
                    await handle.get_attribute("aria-pressed") == "true"
                    or await handle.get_attribute("aria-checked") == "true"
                    or await handle.get_attribute("data-active") == "true"
                ):
                    engaged = True
                    break
            assert engaged, (
                "The pre-launch Plan-mode control did not report an engaged "
                "state (aria-pressed / aria-checked / data-active) after a "
                "click."
            )

            # If the control lives in the config modal, commit the draft.
            save = page.get_by_test_id("new-chat-landing-config-save")
            if await save.count() > 0 and await save.is_visible():
                await save.click()

            await page.get_by_test_id("new-chat-landing-input").fill("plan the auth refactor")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_until(lambda: len(create_bodies) == 1)

            def _plan_reached_server() -> bool:
                return any(_carries_plan(body) for body in [*create_bodies, *patch_bodies])

            # The plan handoff may ride the create POST itself or an
            # immediate post-create PATCH; give the latter a short window.
            await _wait_until(_plan_reached_server, timeout_s=10.0)
        finally:
            # Close the page's context first so a journey recording
            # (record_video_dir) is flushed to disk before the browser dies.
            await page.context.close()
            await browser.close()

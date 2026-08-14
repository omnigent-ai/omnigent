"""E2E: a copilot custom agent and its model/effort config modal.

Covers the copilot chat flow in the landing composer (``NewChatLandingScreen``
in ``web/src/shell/NewChatDialog.tsx``) end to end. Copilot deliberately has
no top-level picker presence of its own (it is not a native terminal
harness): it is reached through agents on the copilot harness, so the test
drives a user-created custom agent. The agent lists behind the picker's
"Custom agents" flyout and never inline, its gear modal offers the
host-resolved model catalog with per-model reasoning efforts under the Agent
Harness row, and the picked model + effort reach the create call as
``model_override`` / ``reasoning_effort``.

Stubbing mirrors ``test_start_session.py``: the e2e harness's runner
registers no host and the CI machine has no Copilot seat, so ``/v1/hosts``,
``/v1/agents``, and the copilot ``model-options`` probe are faked, and the
create ``POST /v1/sessions`` is captured instead of really launching. The
catalog stub mirrors the live probe's shape: the ``auto`` router (default,
no efforts), a reasoning model with a ladder including ``max``, and an
effort-less model, so the test exercises the per-model effort rules the
backend's real catalog dictates.
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _SESSIONS_RE,
    _pick_config_select,
    _run_in_fresh_loop,
    _save_config,
    _wait_until,
)

# The copilot model-options probe the gear modal fires once the copilot
# harness is the effective pick. Bare endpoint, no query expected.
_MODEL_OPTIONS_RE = re.compile(r"/v1/hosts/[^/]+/harnesses/copilot/model-options")


def _copilot_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: one user-created copilot agent.

    ``harness: "copilot"`` is what keys the gear modal's Model/Effort rows to
    the copilot catalog; the name is an ordinary custom-agent slug, so the
    row files under the picker's "Custom agents" flyout. Sole agent, so it
    auto-selects and the composer gear is directly reachable.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_copilot_e2e",
                    "name": "my-copilot",
                    "display_name": "My Copilot",
                    "description": "GitHub Copilot",
                    "harness": "copilot",
                    "skills": [],
                }
            ]
        }
    )


def _copilot_ready_hosts_body() -> str:
    """Stub body for ``GET /v1/hosts``: one online host with copilot ready.

    The readiness map keeps the "needs setup" badge off the agent's harness
    row, so the test asserts the config surface rather than badge styling.
    """
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": "e2e-host",
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": {"copilot": True},
                }
            ]
        }
    )


def _model_options_body() -> str:
    """Stub body for the copilot model-options probe.

    Mirrors the host's live answer (``copilot_model_options()``): the
    ``auto`` router is the default and takes no effort, a reasoning model
    carries its own ladder including ``max``, and an effort-less model has
    no ``supportedReasoningEfforts`` at all.
    """
    return json.dumps(
        {
            "models": [
                {"id": "auto", "model": "auto", "displayName": "Auto", "isDefault": True},
                {
                    "id": "claude-sonnet-5",
                    "model": "claude-sonnet-5",
                    "displayName": "Claude Sonnet 5",
                    "isDefault": False,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low"},
                        {"reasoningEffort": "medium"},
                        {"reasoningEffort": "high"},
                        {"reasoningEffort": "xhigh"},
                        {"reasoningEffort": "max"},
                    ],
                },
                {
                    "id": "claude-haiku-4.5",
                    "model": "claude-haiku-4.5",
                    "displayName": "Claude Haiku 4.5",
                    "isDefault": False,
                },
            ]
        }
    )


def test_copilot_agent_model_and_effort_reach_the_create(
    seeded_session: tuple[str, str],
) -> None:
    """A copilot custom agent's model/effort pick reaches the create.

    Asserts the behaviors this feature adds to the new chat flow: the agent
    has no inline row of its own (it files under "Custom agents", keeping
    copilot out of the top-level picker), the gear modal renders the
    host-resolved catalog with per-model efforts (no effort row for
    Default/auto or an effort-less model, the full ladder including Max for
    a reasoning model), and the committed pick reaches ``POST /v1/sessions``
    as ``model_override`` + ``reasoning_effort`` with no harness override
    (the agent already runs on copilot).
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_copilot_agent(base_url, session_id))


async def _drive_copilot_agent(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_copilot_ready_hosts_body(),
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_copilot_agents_body(),
                )

            async def handle_model_options(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_model_options_body(),
                )

            async def handle_events(route: Route) -> None:
                # Swallow the auto-sent initial prompt so no real turn runs.
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_sessions(route: Route) -> None:
                # Capture only the composer's create POST; everything else
                # (conversation list, agent-discovery scan) stays real.
                if route.request.method == "POST":
                    create_bodies.append(route.request.post_data_json)
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route(_MODEL_OPTIONS_RE, handle_model_options)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)

            # Neutralize agent discovery so only the stubbed copilot agent
            # feeds the picker (see test_start_session for the rationale).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # Seed a recent working directory so Send can enable without the
            # (host-less) file browser.
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

            # Copilot has no top-level picker presence: the custom agent has
            # no inline row and files under the "Custom agents" flyout.
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            row = page.get_by_test_id("new-chat-landing-agent-ag_copilot_e2e")
            await expect(row).to_have_count(0)
            await page.get_by_test_id("new-chat-landing-custom-agents").hover()
            await expect(row).to_be_visible()
            await page.keyboard.press("Escape")

            # Sole agent, so it auto-selected; the composer gear opens its
            # config directly.
            await page.get_by_test_id("new-chat-landing-config-gear").click()

            # A copilot agent is a bundle agent, so the Agent Harness row
            # renders, and the Model select rides under it with the
            # host-resolved catalog. Default rides the backend's auto pick,
            # which takes no effort, so no Effort row is rendered yet.
            await expect(page.get_by_test_id("new-chat-landing-config-harness")).to_be_visible()
            model = page.get_by_test_id("new-chat-landing-config-model")
            await expect(model).to_be_visible()
            await expect(page.get_by_test_id("new-chat-landing-config-effort")).to_have_count(0)
            await model.click()
            for label in ("Auto", "Claude Sonnet 5", "Claude Haiku 4.5"):
                await expect(page.get_by_role("option", name=label, exact=True)).to_be_visible()
            await page.get_by_role("option", name="Claude Sonnet 5", exact=True).click()

            # A reasoning model brings its own ladder, including Max (which
            # the static claude-native list never offered per model).
            effort = page.get_by_test_id("new-chat-landing-config-effort")
            await expect(effort).to_be_visible()
            await _pick_config_select(page, "new-chat-landing-config-effort", "Max")
            await expect(effort).to_contain_text("Max")

            # An effort-less model hides the row entirely and drops the
            # drafted effort; picking the reasoning model again re-offers it.
            await _pick_config_select(page, "new-chat-landing-config-model", "Claude Haiku 4.5")
            await expect(page.get_by_test_id("new-chat-landing-config-effort")).to_have_count(0)
            await _pick_config_select(page, "new-chat-landing-config-model", "Claude Sonnet 5")
            await _pick_config_select(page, "new-chat-landing-config-effort", "Max")
            await _save_config(page)

            await page.get_by_test_id("new-chat-landing-input").fill("write a haiku about CI")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_copilot_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body.get("model_override") == "claude-sonnet-5", body
            assert body.get("reasoning_effort") == "max", body
            # The agent already runs on copilot; no override was picked.
            assert body.get("harness_override") is None, body
        finally:
            await browser.close()

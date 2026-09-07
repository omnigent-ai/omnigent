"""E2E: Smart Routing on the new-chat landing screen.

Two independent ways to hand a new session to the router, both reachable from
the landing composer (``NewChatLandingScreen`` in
``web/src/shell/NewChatDialog.tsx``):

1. **Harness-level** — a "Smart Routing" row above the Harnesses group in the
   agent/harness picker. The router picks the native harness AND the model at
   create time, so the create call sends ``harness_override: "auto"`` plus the
   typed message (``smart_routing_message``) for the router to score, and none
   of the placeholder wrapper's own knobs.
2. **Model-level** — "Smart Routing" as an option in the gear-modal's Model
   row on a routable native harness. The harness stays pinned and the router
   picks the model per turn, so the create call sends
   ``cost_control_mode_override: "on"`` and no ``model_override``.

Both surfaces are gated: the server must report routing on with a configured
source (``GET /v1/info``), both native wrapper agents must be registered, and
their CLIs must be ready on the selected host. The e2e harness's tunneled
runner registers no host and the real server has routing off, so ``/v1/info``,
``/v1/hosts`` and ``/v1/agents`` are stubbed — the same shape (and for the same
reasons) as ``test_start_session.py``, whose helpers this file reuses.
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _SESSIONS_RE,
    _open_entry_config,
    _run_in_fresh_loop,
    _save_config,
    _wait_until,
)

# The two native wrapper agents Smart Routing routes between. Both must be in
# the picker's Harnesses group or the row is withheld ("wrappers-missing").
_ROUTING_AGENTS_BODY = json.dumps(
    {
        "data": [
            {
                "id": "ag_claude_e2e",
                "name": "claude-native-ui",
                "display_name": "Claude Code",
                "description": "Anthropic's coding agent",
                "harness": "claude-native",
                "skills": [],
            },
            {
                "id": "ag_codex_e2e",
                "name": "codex-native-ui",
                "display_name": "Codex",
                "description": "OpenAI's coding agent",
                "harness": "codex-native",
                "skills": [],
            },
        ]
    }
)

# One online host with no readiness or gateway-inference map: both read as
# "unknown", which the landing treats as ready/backed (only an explicit false
# withholds the row). A host that reported otherwise is the unavailable-notice
# path, covered by the unit tests in web/src/shell/NewChatDialog.test.tsx.
_ROUTING_HOSTS_BODY = json.dumps(
    {
        "hosts": [
            {
                "host_id": _HOST_ID,
                "name": "e2e-host",
                "owner": "e2e",
                "status": "online",
            }
        ]
    }
)


def _info_body(*, routing: bool) -> str:
    """Stub body for ``GET /v1/info``.

    :param routing: Whether the server advertises Smart Routing with the
        external (AI-Gateway) router configured.
    :returns: JSON body.
    """
    return json.dumps(
        {
            "accounts_enabled": False,
            "single_user": True,
            "needs_setup": False,
            "smart_routing_enabled": routing,
            "smart_routing_sources": {"external": routing, "oss": False},
        }
    )


async def _register_routing_routes(
    page,
    *,
    created_session_id: str,
    create_bodies: list[dict[str, Any]],
    routing: bool = True,
) -> None:
    """Install the host / agent / info / create stubs both tests share.

    :param page: The Playwright page to install routes on.
    :param created_session_id: Real pre-seeded session id the faked create
        returns, so the post-send navigation lands somewhere real.
    :param create_bodies: Sink the create ``POST /v1/sessions`` body lands in.
    :param routing: Whether ``/v1/info`` advertises Smart Routing.
    """

    async def handle_info(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=_info_body(routing=routing)
        )

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_ROUTING_HOSTS_BODY)

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_ROUTING_AGENTS_BODY)

    async def handle_events(route: Route) -> None:
        # Swallow the auto-sent initial prompt so no real LLM turn runs.
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
        )

    async def handle_sessions(route: Route) -> None:
        if route.request.method == "POST":
            create_bodies.append(route.request.post_data_json)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": created_session_id}),
            )
        else:
            await route.continue_()

    async def handle_agent_scan(route: Route) -> None:
        # Neutralize agent discovery so only the two stubbed wrappers feed the
        # picker; an agent another test left behind could otherwise rank first
        # and auto-select, opening the wrong agent's config modal.
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/info", handle_info)
    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route("**/v1/sessions/*/events", handle_events)
    await page.route(_SESSIONS_RE, handle_sessions)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
    # The landing needs a working directory before Send enables, and the
    # stubbed host has no browsable filesystem.
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


def test_start_session_smart_routing_harness_row(seeded_session: tuple[str, str]) -> None:
    """The picker offers Smart Routing, and picking it routes the create.

    The row is the harness-level entry: the router owns harness AND model, so
    the create must send the ``auto`` sentinel plus the message to score, and
    must NOT send the placeholder wrapper's model / launch args / labels.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_smart_routing_harness(base_url, session_id))


async def _drive_smart_routing_harness(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_routing_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await page.get_by_test_id("new-chat-landing-agent-select").click()
            smart_row = page.get_by_test_id("new-chat-landing-harness-smart-routing")
            await expect(smart_row).to_be_visible()
            await expect(smart_row).to_contain_text("Smart Routing")
            await smart_row.click()

            # The trigger now names the router, not one of the harnesses it
            # picks between.
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "Smart Routing"
            )

            await page.get_by_test_id("new-chat-landing-input").fill("refactor the auth module")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["harness_override"] == "auto", body
            # The router scores the typed message at create time.
            assert body["smart_routing_message"] == "refactor the auth module", body
            # None of the placeholder wrapper's own knobs ride along — the
            # router may pick the other harness entirely.
            assert body.get("model_override") is None, body
            assert body.get("terminal_launch_args") is None, body
            assert body.get("labels") is None, body
        finally:
            await browser.close()


def test_start_session_smart_routing_model_option(seeded_session: tuple[str, str]) -> None:
    """Smart Routing is also a Model choice on a routable native harness.

    Picking it in the gear modal leaves the harness pinned and hands the model
    to the router per turn: the create sends ``cost_control_mode_override:
    "on"`` and no ``model_override``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_smart_routing_model_option(base_url, session_id))


async def _drive_smart_routing_model_option(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_routing_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Pin Claude Code, then open its run-config modal.
            await _open_entry_config(page, "ag_claude_e2e")
            model = page.get_by_test_id("new-chat-landing-config-model")
            await expect(model).to_be_visible()
            await model.click()
            await page.get_by_role("option", name="Smart Routing", exact=True).click()
            await expect(model).to_contain_text("Smart Routing")
            # The router picks the effort with the model, so the row is frozen.
            await expect(page.get_by_test_id("new-chat-landing-config-effort")).to_be_disabled()
            await _save_config(page)

            await page.get_by_test_id("new-chat-landing-input").fill("fix the flaky test")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_claude_e2e", body
            assert body["cost_control_mode_override"] == "on", body
            # Per-turn routing owns the model, so nothing is pinned; the
            # harness itself stays the one the user picked.
            assert body.get("model_override") is None, body
            assert body.get("harness_override") != "auto", body
        finally:
            await browser.close()


def test_start_session_hides_smart_routing_when_server_disables_it(
    seeded_session: tuple[str, str],
) -> None:
    """Routing off on the server withholds both Smart Routing surfaces.

    The negative half of the gate: the same stubs with
    ``smart_routing_enabled: false`` must leave the picker row absent and the
    Model row without the router option, so a server that can't route never
    offers a pick it would have to drop.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_smart_routing_disabled(base_url, session_id))


async def _drive_smart_routing_disabled(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_routing_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                routing=False,
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await page.get_by_test_id("new-chat-landing-agent-select").click()
            # The Harnesses group renders, so the picker is populated — only the
            # routing row is missing.
            await expect(
                page.get_by_test_id("new-chat-landing-agent-ag_claude_e2e")
            ).to_be_visible()
            await expect(
                page.get_by_test_id("new-chat-landing-harness-smart-routing")
            ).to_have_count(0)

            await page.get_by_test_id("new-chat-landing-agent-ag_claude_e2e").click()
            await page.get_by_test_id("new-chat-landing-config-gear").click()
            await page.get_by_test_id("new-chat-landing-config-model").click()
            await expect(
                page.get_by_role("option", name="Smart Routing", exact=True)
            ).to_have_count(0)
        finally:
            await browser.close()

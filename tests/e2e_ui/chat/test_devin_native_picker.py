"""E2E: the New Chat picker offers Devin with its own model + effort lists.

Devin is not ``fullySupported``, so it lives in the picker's Other group with the
rest of the natives. Selecting it must surface:

* Devin's model **families** (from the host's ``devin-native`` catalog probe) —
  not Claude's or Pi's list; and
* an Effort ladder, because Devin has no ``--effort`` flag and Omnigent composes
  the (model, effort) pair into one variant id at launch
  (``resolve_devin_launch_model``). Without the ladder rendered there is no way
  to express effort when starting a chat.

Regression targets: a Devin row missing from the picker entirely, a picker that
reuses another harness's catalog, and an Effort section that fails to render for
devin-native — its rungs come from the shared Anthropic ladder rather than a
Devin-specific list, so a missing branch in ``pickerEffortOptions`` silently
removes the only way to choose effort when starting a chat.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

# Stubbed host the composer auto-selects (the tunneled runner registers no host).
_HOST_ID = "host_e2e"
_HOST_NAME = "e2e-host"

_DEVIN_AGENT_ID = "ag_devin_e2e"
_CLAUDE_AGENT_ID = "ag_claude_e2e"
_PI_AGENT_ID = "ag_pi_e2e"

# Devin model *families*, the shape ``list_devin_cli_model_options`` returns.
# Effort is a separate axis, so no variant suffixes appear here.
_DEVIN_MODELS = [
    {"id": "claude-opus-5", "displayName": "Claude Opus 5", "isDefault": False},
    {"id": "swe-2", "displayName": "SWE-2", "isDefault": True},
]


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own event loop.

    The e2e_ui suite runs pytest-playwright **sync** tests in the same session;
    once one has run, pytest-asyncio can't start a loop on the main thread.
    Mirrors ``test_harness_support_level_split``.

    :param coro: The coroutine to run to completion.
    :raises Exception: Whatever the coroutine raised, re-raised here.
    """
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


def _hosts_body() -> str:
    """Stub ``GET /v1/hosts``: one online host with every stubbed harness ready."""
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": _HOST_NAME,
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": {
                        "devin-native": True,
                        "claude-native": True,
                        "pi-native": True,
                    },
                }
            ]
        }
    )


def _agents_body() -> str:
    """Stub ``GET /v1/agents``: Devin, a primary harness (Claude), and Pi."""
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
                },
                {
                    "id": _CLAUDE_AGENT_ID,
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": "claude-native",
                    "skills": [],
                },
                {
                    "id": _PI_AGENT_ID,
                    "name": "pi-native-ui",
                    "display_name": "Pi",
                    "description": "Pi coding agent",
                    "harness": "pi-native",
                    "skills": [],
                },
            ]
        }
    )


async def _register_routes(page) -> None:
    """Stub hosts, agents, the Devin model catalog, and agent discovery.

    :param page: The Playwright page to install routes on.
    """

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_devin_models(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"models": _DEVIN_MODELS}),
        )

    async def handle_other_models(route: Route) -> None:
        # Any other harness's catalog is empty, so a Devin row rendered from
        # someone else's list would show nothing rather than passing by luck.
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"models": []})
        )

    async def handle_agent_scan(route: Route) -> None:
        # Neutralize agent discovery so only the stubbed agents feed the picker.
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(
        re.compile(r"/v1/hosts/[^/]+/harnesses/devin-native/model-options"), handle_devin_models
    )
    await page.route(
        re.compile(r"/v1/hosts/[^/]+/harnesses/(?!devin-native)[^/]+/model-options"),
        handle_other_models,
    )
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)


async def _open_picker(page) -> None:
    """Open the landing agent/harness picker dropdown."""
    await page.get_by_test_id("new-chat-landing-agent-select").click()


def test_devin_picker_offers_its_own_models_and_effort(
    seeded_session: tuple[str, str],
) -> None:
    """Devin is offered in Other and exposes its families plus an Effort ladder."""
    base_url, session_id = seeded_session
    del session_id  # this flow only reads the picker; it creates no session
    _run_in_fresh_loop(_drive(base_url))


async def _drive(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(page)
            # Seed a recent working directory so the composer auto-fills and
            # never touches the host-less file browser.
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

            # 1. Devin is offered, in the Other group alongside the other
            #    non-primary natives.
            await _open_picker(page)
            devin = page.get_by_test_id(f"new-chat-landing-agent-{_DEVIN_AGENT_ID}")
            await expect(devin).to_have_count(0)
            await page.get_by_test_id("new-chat-landing-harness-more").click()
            await expect(devin).to_be_visible(timeout=30_000)

            # 2. Selecting Devin shows DEVIN's families, from the devin-native
            #    catalog probe (every other harness's stub is empty).
            await devin.click()
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_have_attribute(
                "aria-label", re.compile("Devin")
            )
            await page.keyboard.press("Escape")
            await expect(page.get_by_role("menu")).to_have_count(0)
            await _open_picker(page)
            await expect(page.get_by_test_id("new-chat-landing-agent-models")).to_be_visible(
                timeout=30_000
            )
            for model in _DEVIN_MODELS:
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-model-{model['id']}")
                ).to_be_visible()

            # 3. The Effort ladder renders. Devin has no --effort flag, so this
            #    is the only way to express effort when starting a chat; the
            #    runner composes it onto the model id at launch.
            await expect(page.get_by_test_id("new-chat-landing-agent-efforts")).to_be_visible()
            for rung in ("low", "medium", "high", "xhigh", "max"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-effort-{rung}")
                ).to_be_visible()

            # 4. A model + effort pick sticks, which is what the create call
            #    sends as model_override + reasoning_effort.
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

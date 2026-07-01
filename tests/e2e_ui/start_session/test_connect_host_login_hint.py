"""E2E: the "Connect a host" modal on the new-session landing page.

Covers the optional ``omni login`` set-as-default hint that renders
alongside the ``omni host`` command in the connect-host instructions.

Uses the same route-stubbing approach as ``test_create_custom_agent.py``:
``/v1/hosts`` and ``/v1/agents`` are faked so the landing renders without
a real host.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own event loop."""
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


def _agents_body() -> str:
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_claude_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": None,
                    "skills": [],
                }
            ]
        }
    )


async def _register_routes(page) -> None:
    """Stub hosts (empty) and agents so the landing renders deterministically."""

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"hosts": []})
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)


def test_connect_host_modal_shows_login_hint(seeded_session: tuple[str, str]) -> None:
    """The connect-host modal offers the optional `omni login` command."""
    base_url, _ = seeded_session
    _run_in_fresh_loop(_drive_login_hint(base_url))


async def _drive_login_hint(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the host dropdown → "Connect a host".
            await page.get_by_test_id("new-chat-landing-host-chip").click()
            await page.get_by_test_id("new-chat-landing-connect-host").click()

            # The modal shows the host command plus the optional login hint.
            await expect(page.get_by_test_id("connect-host-dialog")).to_be_visible(timeout=5_000)
            await expect(page.get_by_test_id("connect-host-command")).to_be_visible()
            await expect(page.get_by_test_id("connect-host-login-hint")).to_be_visible()
            await expect(page.get_by_test_id("connect-host-login-command")).to_contain_text(
                "omni login"
            )
        finally:
            await browser.close()

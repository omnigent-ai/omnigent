"""Picker coverage for unavailable native agents on Windows hosts."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

_HOST_ID = "host_e2e_windows"
_WINDOWS_NATIVE_HARNESS_AVAILABLE: bool = False
_TESTED_NATIVE_HARNESS = "claude-native"


def _windows_hosts_body() -> str:
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": "windows-e2e-host",
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": {
                        _TESTED_NATIVE_HARNESS: _WINDOWS_NATIVE_HARNESS_AVAILABLE,
                        "claude-sdk": True,
                    },
                }
            ]
        }
    )


def _claude_native_agents_body() -> str:
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_claude_native_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent (native terminal)",
                    "harness": "claude-native",
                    "skills": [],
                },
            ]
        }
    )


def _info_body() -> str:
    return json.dumps({"version": "0.0.0", "features": [], "installable_harnesses": []})


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
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


_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")


async def _register_common_routes(page: Any, created_session_id: str) -> None:
    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=_windows_hosts_body(),
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=_claude_native_agents_body(),
        )

    async def handle_info(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=_info_body(),
        )

    async def handle_create_session(route: Route) -> None:
        if route.request.method != "POST":
            await route.continue_()
            return
        await route.fulfill(
            status=201,
            content_type="application/json",
            body=json.dumps({"session_id": created_session_id}),
        )

    async def handle_events(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="text/event-stream",
            body="",
        )

    await page.route(re.compile(r"/v1/hosts$"), handle_hosts)
    await page.route(re.compile(r"/v1/agents"), handle_agents)
    await page.route(re.compile(r"/v1/info$"), handle_info)
    await page.route(_SESSIONS_RE, handle_create_session)
    await page.route(re.compile(r"/v1/events/"), handle_events)


def test_native_agents_show_warning_badge_on_windows_host(
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_windows_host_picker_warning(base_url, session_id))


async def _drive_windows_host_picker_warning(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_common_routes(page, session_id)

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ "{_HOST_ID}": ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await page.get_by_test_id("new-chat-landing-agent-select").click()

            native_row = page.get_by_test_id("new-chat-landing-agent-ag_claude_native_e2e")
            await expect(native_row).to_be_visible()

            native_badge = page.get_by_test_id(
                "new-chat-landing-agent-warning-ag_claude_native_e2e"
            )
            await expect(native_badge).to_be_visible(timeout=5_000)
        finally:
            await browser.close()

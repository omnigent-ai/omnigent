"""E2E: version-too-low warning in the New Chat landing screen.

The composer warns when a native harness is installed but its CLI version is
below the supported minimum (``"version-too-low"``). This test verifies the
NewChatDialog renders the updated copy: an under-composer message mentioning
"outdated CLI".
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

_HOST_ID = "host_version_e2e"
_HOST_NAME = "version-e2e-host"


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


def _codex_native_agents_body() -> str:
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_codex_version_e2e",
                    "name": "codex-native-ui",
                    "display_name": "Codex",
                    "description": "OpenAI's coding agent",
                    "harness": "codex-native",
                    "skills": [],
                }
            ]
        }
    )


async def _register_routes(page, *, configured_harnesses: dict[str, Any]) -> None:
    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _HOST_ID,
                            "name": _HOST_NAME,
                            "owner": "e2e",
                            "status": "online",
                            "configured_harnesses": configured_harnesses,
                        }
                    ]
                }
            ),
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=_codex_native_agents_body(),
        )

    async def handle_agent_scan(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": []}),
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)


def test_version_too_low_warns_with_outdated_cli_copy(
    seeded_session: tuple[str, str],
) -> None:
    """A version-too-low Codex host renders the outdated CLI warning."""
    base_url, session_id = seeded_session
    del session_id
    _run_in_fresh_loop(_drive_version_too_low(base_url))


async def _drive_version_too_low(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(
                page,
                configured_harnesses={"codex-native": "version-too-low"},
            )
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID!r}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            warning = page.get_by_test_id("new-chat-landing-harness-warning")
            await expect(warning).to_be_visible(timeout=30_000)
            await expect(warning).to_contain_text("has an outdated CLI")
            await expect(warning).to_contain_text(_HOST_NAME)
            await expect(warning).to_contain_text("omni setup")
        finally:
            await browser.close()

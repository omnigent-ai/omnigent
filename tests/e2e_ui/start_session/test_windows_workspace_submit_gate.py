"""A Windows workspace picked through the host tunnel must enable Start session.

The host is simulated; agent discovery and session creation are stubbed."""

from __future__ import annotations

import json
import re

from playwright.async_api import Request, Route, async_playwright, expect

from tests.e2e_ui.start_session.helpers import commit_landing_workspace_picker
from tests.e2e_ui.start_session.test_windows_workspace_picker import (
    _open_picker_at_windows_home,
    _run_in_fresh_loop,
    _video_kwargs,
    _windows_host,
)

_PICKED_DIR = "C:\\Users\\alice\\work"
_SESSIONS_URL = re.compile(r"/v1/sessions(\?.*)?$")


def test_windows_workspace_enables_start_session(live_server: str) -> None:
    """Picking a drive-letter directory must enable Start session."""
    _run_in_fresh_loop(_drive_windows_submit(live_server))


async def _drive_windows_submit(base_url: str) -> None:
    async with _windows_host(base_url) as host_id, async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Explicit context so a recorded video is finalized on context.close()
        # even when the drive fails mid-way.
        context = await browser.new_context(**_video_kwargs())
        page = await context.new_page()
        try:

            async def handle_sessions(route: Route) -> None:
                # Stub the composer's create POST; the fake host has no
                # runner, so a real create could not dispatch anyway.
                if route.request.method == "POST":
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": "conv_win_submit_e2e"}),
                    )
                else:
                    await route.continue_()

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
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
                    ),
                )

            def is_session_create(request: Request) -> bool:
                return request.method == "POST" and _SESSIONS_URL.search(request.url) is not None

            await page.route(_SESSIONS_URL, handle_sessions)
            # A single agent auto-selects; hide agents left by other tests so
            # no explicit pick is needed.
            await page.route("**/v1/agents", handle_agents)
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"),
                lambda route: route.fulfill(json={"data": []}),
            )

            await _open_picker_at_windows_home(page, base_url, host_id)

            await page.get_by_test_id("workspace-picker-entry-work").dispatch_event("click")
            await expect(page.get_by_test_id("workspace-picker-entry-omnigent-app")).to_be_visible(
                timeout=10_000
            )
            await commit_landing_workspace_picker(page)

            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_have_attribute(
                "aria-label",
                re.compile(r"Working directory: C:[\\/]Users[\\/]alice[\\/]work$"),
            )

            await page.get_by_test_id("new-chat-landing-input").fill("ping")

            submit = page.get_by_test_id("new-chat-landing-submit")
            # Surface the submit tooltip; a disabled button swallows pointer
            # events, so hover its tooltip wrapper instead.
            await page.locator('span[data-slot="tooltip-trigger"]', has=submit).hover()
            await expect(submit).to_be_enabled(timeout=10_000)

            async with page.expect_request(is_session_create) as create_request:
                await submit.click()
            body = (await create_request.value).post_data_json
            assert body["host_id"] == host_id, body
            assert body["workspace"] == _PICKED_DIR, body
        finally:
            await context.close()
            await browser.close()

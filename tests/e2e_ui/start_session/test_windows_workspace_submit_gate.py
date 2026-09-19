"""E2E: the landing submit gate must accept a Windows drive-letter workspace.

Regression guard: on a native Windows host, picking a working directory
such as ``C:\\Users\\alice\\work`` filled the folder chip, but the
composer's Start session control stayed disabled with the tooltip
"Please choose a host and working directory". The landing submit gate
(``isValidWorkspace`` in ``NewChatDialog.tsx``) accepted only paths
starting with ``/``, so ``workspaceValid`` stayed false even though the
picker, the browse API, and session create all accept the same path.

Test shape: same fake-Windows-host-over-the-real-tunnel geometry as
``test_windows_workspace_picker`` — the real server routes and the real
SPA run the exact production path a Windows machine exercises. The test
picks ``C:\\Users\\alice\\work`` through the picker, confirms the chip
shows that path, types a message, and asserts the Start session control
enables and dispatches a create carrying the drive-letter workspace.
Only ``POST /v1/sessions`` is stubbed (captured), so the fake host never
needs a runner.
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.helpers import commit_landing_workspace_picker
from tests.e2e_ui.start_session.test_start_session import _wait_until
from tests.e2e_ui.start_session.test_windows_workspace_picker import (
    _open_picker_at_windows_home,
    _run_in_fresh_loop,
    _video_kwargs,
    _windows_host,
)

_PICKED_DIR = "C:\\Users\\alice\\work"


def test_windows_workspace_enables_start_session(live_server: str) -> None:
    """Picking ``C:\\Users\\alice\\work`` must enable Start session.

    The failure mode this catches: the workspace chip shows the picked
    drive-letter directory, the host is online, an agent is selected and
    the composer has text, yet the submit stays disabled because the
    submit gate rejects any workspace not starting with ``/``.
    """
    _run_in_fresh_loop(_drive_windows_submit(live_server))


async def _drive_windows_submit(base_url: str) -> None:
    async with _windows_host(base_url) as host_id, async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Explicit context so a recorded video is finalized on context.close()
        # even when the drive fails mid-way.
        context = await browser.new_context(**_video_kwargs())
        page = await context.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_sessions(route: Route) -> None:
                # Capture only the composer's create POST; the fake host has
                # no runner, so a real create could not dispatch anyway.
                if route.request.method == "POST":
                    create_bodies.append(route.request.post_data_json)
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

            await page.route(re.compile(r"/v1/sessions(\?.*)?$"), handle_sessions)
            # A single agent auto-selects; hide agents left by other tests so
            # no explicit pick is needed.
            await page.route("**/v1/agents", handle_agents)
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"),
                lambda route: route.fulfill(json={"data": []}),
            )

            await _open_picker_at_windows_home(page, base_url, host_id)

            # Browse into the picked directory and commit it back to the chip.
            await page.get_by_test_id("workspace-picker-entry-work").dispatch_event("click")
            await expect(page.get_by_test_id("workspace-picker-entry-omnigent-app")).to_be_visible(
                timeout=10_000
            )
            await commit_landing_workspace_picker(page)

            # The picker part works: the chip shows the drive-letter path,
            # not "Not selected". A failure here is a picker regression, not
            # the submit gate under test.
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

            await submit.click()
            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == host_id, body
            assert body["workspace"] == _PICKED_DIR, body
        finally:
            await context.close()
            await browser.close()

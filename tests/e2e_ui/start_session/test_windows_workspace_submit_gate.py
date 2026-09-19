"""E2E: the landing submit gate accepts a Windows drive-letter workspace.

Regression guard for the native-Windows report: picking a working
directory such as ``C:\\Users\\alice\\work`` filled the folder chip, the
host was online, an agent was selected and the composer had text, yet
**Start session** stayed disabled with the tooltip "Please choose a host
and working directory". The landing submit gate (``isValidWorkspace`` in
``NewChatDialog.tsx``) accepted only paths starting with ``/``, so
``workspaceValid`` never became true for a drive-letter path even though
the picker, the browse API and ``POST /v1/sessions`` all accept it.

Same fake-Windows-host-over-the-real-tunnel geometry as
``test_windows_workspace_picker``: the real server routes and the real
SPA run, only the host's filesystem answers are simulated. The drive
picks ``C:\\Users\\alice\\work`` through the picker, confirms the chip
shows that path, types a message, and asserts the submit control enables
and dispatches a session create carrying the drive-letter workspace.
Only ``POST /v1/sessions`` is captured, so the fake host never needs a
runner.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.helpers import commit_landing_workspace_picker
from tests.e2e_ui.start_session.test_windows_workspace_picker import (
    _open_picker_at_windows_home,
    _run_in_fresh_loop,
    _video_kwargs,
    _windows_host,
)

_PICKED_DIR = "C:\\Users\\alice\\work"


def test_windows_drive_letter_workspace_enables_start_session(live_server: str) -> None:
    """Picking ``C:\\Users\\alice\\work`` must enable and dispatch Start session.

    Buggy build: the chip shows the picked directory but the submit
    control stays ``disabled`` and no ``POST /v1/sessions`` is ever sent.
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
                # Capture the composer's create POST only; the fake host has
                # no runner, so a real create could not dispatch anyway.
                if route.request.method == "POST":
                    body = route.request.post_data_json
                    assert body is not None, "session create POST had no JSON body"
                    create_bodies.append(body)
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": "conv_win_submit_e2e"}),
                    )
                else:
                    await route.continue_()

            await page.route("**/v1/sessions", handle_sessions)
            await page.route("**/v1/sessions?*", handle_sessions)

            await _open_picker_at_windows_home(page, base_url, host_id)

            # The listing re-renders continuously (host polling), so fire the
            # click event on the row directly rather than a position-checked
            # click (same reason as the picker tests).
            await page.get_by_test_id("workspace-picker-entry-work").dispatch_event("click")
            await expect(page.get_by_test_id("workspace-picker-entry-omnigent-app")).to_be_visible(
                timeout=15_000
            )
            await commit_landing_workspace_picker(page)

            chip = page.get_by_test_id("new-chat-landing-workspace-chip")
            await expect(chip).to_contain_text("work")

            await page.get_by_test_id("new-chat-landing-input").fill("ping")

            # The claim under test: a drive-letter workspace satisfies the
            # submit gate. On the buggy build this stays disabled forever.
            submit = page.get_by_test_id("new-chat-landing-submit")
            await expect(submit).to_be_enabled(timeout=10_000)
            await submit.click()

            deadline = asyncio.get_running_loop().time() + 15.0
            while not create_bodies and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            assert len(create_bodies) == 1, create_bodies
            body = create_bodies[0]
            assert body["host_id"] == host_id, body
            assert body["workspace"] == _PICKED_DIR, body
        finally:
            await context.close()
            await browser.close()

"""E2E: a Windows working directory must not disable Start (send).

The landing composer gates Send on ``isValidWorkspace`` in
``web/src/shell/NewChatDialog.tsx``, which accepted only POSIX ``/…`` paths.
A session targeting a Windows host therefore ends up with a workspace like
``C:\\general_agent_temp`` that the workspace picker itself accepts
(``isHostAbsolutePath`` handles drive-letter and UNC paths) while the Send
button stays permanently disabled with the tooltip "Please choose a host and
working directory", and Enter dispatches nothing.

Journey: home composer with an online (stubbed) host whose recent working
directory is ``C:\\general_agent_temp`` — the state a Windows user is in
after previously picking that folder. The chip auto-fills with it; typing a
message must leave Send enabled, and submitting must POST ``/v1/sessions``
with that workspace.
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _register_common_routes,
    _run_in_fresh_loop,
    _wait_until,
)

_WINDOWS_WORKSPACE = "C:\\general_agent_temp"


def test_windows_workspace_enables_send(seeded_session: tuple[str, str]) -> None:
    """A Windows drive-letter workspace must leave Send usable.

    With an online host selected and ``C:\\general_agent_temp`` as the
    working directory, typing a message must enable Send, and clicking it
    must dispatch the create with exactly that workspace.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_windows_workspace_send(base_url, session_id))


async def _drive_windows_workspace_send(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            # Hide agents left by other tests so the stubbed one auto-selects.
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"),
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                ),
            )

            # The state a Windows user lands in after picking this folder on
            # their host: the working-directory chip auto-fills from recents.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: [{json.dumps(_WINDOWS_WORKSPACE)}] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(
                "general_agent_temp"
            )

            await page.get_by_test_id("new-chat-landing-input").fill(
                "set up the project on this Windows machine"
            )

            # Hover Send so its tooltip (the disabled reason, when gated) is
            # visible in recordings. force: a tooltip-trigger span wraps the
            # button and intercepts pointer events.
            submit = page.get_by_test_id("new-chat-landing-submit")
            await submit.hover(force=True)
            await expect(submit).to_be_enabled()

            await submit.click()
            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == _WINDOWS_WORKSPACE, body
        finally:
            # Close the context before the browser so a video, when recording
            # is enabled, is finalized even on failure.
            await page.context.close()
            await browser.close()

"""E2E: an empty Pi host catalog is explicit in the pre-launch model picker.

Only the host/agent/model-options edges are stubbed; the real SPA must show
that models are unavailable rather than inventing selectable catalog entries.
The server-side unmanaged-provider behavior is covered separately in
``tests/e2e/test_pi_native_unmanaged_model.py``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _expect_model_menu_without_advanced_settings,
    _open_entry_models,
    _pi_native_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
)


def test_pi_native_prelaunch_picker_empty_without_managed_provider(
    seeded_session: tuple[str, str],
) -> None:
    """An empty host catalog exposes no selectable model rows before launch.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_empty_pi_picker(base_url, session_id))


async def _drive_empty_pi_picker(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # An explicit context (not browser.new_page()) so that closing it
        # finalizes the recorded video reliably when OMNIGENT_E2E_RECORD_DIR is
        # set — the e2e_ui conftest injects `record_video_dir` into new_context.
        context = await browser.new_context()
        page = await context.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_pi_native_agents_body(),
            )

            # Neutralize agent discovery so only the stubbed built-in Pi shows
            # (sibling pi-native driver does the same): the landing picker
            # merges `/v1/agents` with agents found by scanning the caller's
            # sessions, and leftover sessions on the shared e2e_ui server would
            # otherwise leak in and auto-select ahead of Pi.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            # The bug injection: the unmanaged host returns an EMPTY pi-native
            # catalog. This is the payload the real host daemon produces when
            # `resolve_pi_native_provider()` is None (see the server-side guard
            # in tests/e2e/test_pi_native_unmanaged_model.py).
            async def handle_pi_model_options(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"models": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/pi-native/model-options",
                handle_pi_model_options,
            )

            # Seed a recent working directory so a real (non-sandbox) host
            # workspace is selected — pi model options are only fetched for a
            # real host (`useHostModelOptions(hostId, "pi-native", !sandbox)`).
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

            await _open_entry_models(page, "ag_pi_e2e")
            await _expect_model_menu_without_advanced_settings(page)
            models = page.get_by_test_id("new-chat-landing-agent-models")
            await expect(models).to_be_visible()
            await expect(models).to_contain_text("Models unavailable")
            await expect(models.get_by_role("menuitemcheckbox")).to_have_count(0)
            await expect(
                page.get_by_test_id("new-chat-landing-agent-model-search")
            ).to_be_visible()
            await expect(page.get_by_test_id("new-chat-landing-agent-efforts")).to_contain_text(
                "Thinking level"
            )
            await expect(page.get_by_test_id("new-chat-landing-agent-effort-high")).to_be_visible()
            await expect(page.get_by_test_id("new-chat-landing-config-modal")).to_have_count(0)
        finally:
            # Close the context first so the recorded video is flushed to disk,
            # then tear the browser down.
            await context.close()
            await browser.close()

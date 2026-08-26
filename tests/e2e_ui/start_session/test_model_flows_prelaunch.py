"""E2E (hermetic): pre-launch model picker rows per model-flows-design.md §10.1.

Row 6's hermetic half: with a host catalog whose rows carry ``isDefault``, the
new-chat model select must read "Default (X)" for BOTH harnesses — X being the
default row's display name. Codex already renders this; the claude branch of
the landing screen historically discarded ``isDefault``, so its select read a
bare "Default" no matter what the host said. This test encodes the design's
target behavior and is red until landing-order step 7.

The driving surface is the real SPA in a browser; only the server edges the
landing screen consults (hosts, agents, model-options) are faked, exactly like
the sibling tests in ``test_start_session.py``.
"""

from __future__ import annotations

import json
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _open_entry_config,
    _register_common_routes,
    _run_in_fresh_loop,
)

_CLAUDE_HOST_ROWS = [
    {
        "id": "sonnet",
        "model": "claude-sonnet-5",
        "displayName": "Sonnet 5",
        "isDefault": False,
    },
    {
        "id": "opus[1m]",
        "model": "claude-opus-4-8[1m]",
        "displayName": "Opus 4.8 (1M context)",
        "isDefault": True,
    },
    {
        "id": "haiku",
        "model": "claude-haiku-4-5-20251001",
        "displayName": "Haiku 4.5",
        "isDefault": False,
    },
]


def test_claude_default_entry_names_the_true_default(
    seeded_session: tuple[str, str],
) -> None:
    """Row 6: the claude model select reads "Default (Opus 4.8 (1M context))".

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_claude_default_label(base_url, session_id))


async def _drive_claude_default_label(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            async def handle_model_options(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"models": _CLAUDE_HOST_ROWS}),
                )

            import re as _re

            await page.route(_re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
                handle_model_options,
            )
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
            await _open_entry_config(page, "ag_claude_e2e")
            model = page.get_by_test_id("new-chat-landing-config-model")
            # The design's row 6: the untouched select names the model a
            # Default launch truly runs, for claude exactly as for codex.
            await expect(model).to_contain_text("Default (Opus 4.8 (1M context))")
        finally:
            await browser.close()

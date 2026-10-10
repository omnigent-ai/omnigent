"""E2E: a user-hidden picker entry disappears from the real picker.

Settings' per-entry visibility list stores agent NAMES in localStorage; the SPA
must drop those rows from the new-chat picker. The readiness filter next door
cannot do this — it only hides rows that fail to launch, and both entries here
are launchable on the stubbed host.

Only the host/agent edges are stubbed. Store-level coverage lives in
``web/src/lib/pickerEntryVisibility.test.ts``, component-level in
``web/src/shell/AgentHarnessPicker.visibility.test.tsx``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _register_common_routes,
    _run_in_fresh_loop,
)

_CLAUDE_AGENT_ID = "ag_claude_e2e"
_PI_AGENT_ID = "ag_pi_e2e"


def _two_harness_agents_body() -> str:
    """Stub ``GET /v1/agents``: two harnesses, both ready on the stub host."""
    return json.dumps(
        {
            "data": [
                {
                    "id": _CLAUDE_AGENT_ID,
                    "name": "claude-native-ui",
                    "display_name": "claude-native-ui",
                    "description": "Claude Code",
                    "harness": "claude-native",
                    "skills": [],
                },
                {
                    "id": _PI_AGENT_ID,
                    "name": "pi-native-ui",
                    "display_name": "pi-native-ui",
                    "description": "Pi coding agent",
                    "harness": "pi-native",
                    "skills": [],
                },
            ]
        }
    )


def test_hidden_entry_is_absent_from_the_picker(
    seeded_session: tuple[str, str],
) -> None:
    """A name in the hidden set drops its row; the other stays.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_hidden_entry(base_url, session_id))


async def _drive_hidden_entry(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # An explicit context (not browser.new_page()) so closing it finalizes
        # the recorded video when OMNIGENT_E2E_RECORD_DIR is set.
        context = await browser.new_context()
        page = await context.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_two_harness_agents_body(),
            )

            # Neutralize agent discovery so only the stubbed pair shows:
            # leftover sessions on the shared e2e_ui server would otherwise add
            # rows and could auto-select ahead of the intended entry.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
            )

            # Hide Pi, and pin the selection to Claude: the picker never hides
            # the selected entry, so selecting the hidden one would mask the
            # very behavior under test.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:hidden-picker-agents", JSON.stringify(["pi-native-ui"])
                );
                window.localStorage.setItem(
                    "omnigent:last-agent-id", "{_CLAUDE_AGENT_ID}"
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await page.get_by_test_id("new-chat-landing-agent-select").click()

            # Claude is still offered...
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_CLAUDE_AGENT_ID}")
            ).to_be_visible()
            # ...and Pi is gone entirely, not merely demoted into "Other...".
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
            ).to_have_count(0)
            await expect(page.get_by_test_id("new-chat-landing-harness-more")).to_have_count(0)
        finally:
            # Close the context first so the recorded video flushes, then the
            # browser.
            await context.close()
            await browser.close()

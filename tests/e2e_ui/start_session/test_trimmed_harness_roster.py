"""E2E: a trimmed harness roster still fills the picker's inline list.

The picker keeps a few preferred harnesses inline and folds the rest into an
"Other..." submenu. The preferred set is a fixed ``claude / cursor / codex``.
The selected entry is always promoted inline, so a trimmed roster only breaks
when the selection is an *agent* rather than a harness: nothing promotes the
remaining harnesses, none of them is preferred, and the "Harnesses" group
renders EMPTY with every choice buried one level down.

This is reachable on a deployment that trims its built-in roster
(``OMNIGENT_SEEDED_AGENTS=pi-native-ui,polly``) and whose user has the bundle
agent selected.

Only the host/agent edges are stubbed; the real SPA must decide where the row
renders. Component-level coverage lives in
``web/src/shell/AgentHarnessPicker.test.tsx``.
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

# Trimmed roster: one non-preferred harness (Pi) plus one bundle agent. No
# claude / cursor / codex row exists, so nothing lands in the preferred set.
_PI_AGENT_ID = "ag_pi_e2e"
_POLLY_AGENT_ID = "ag_polly_e2e"


def _trimmed_agents_body() -> str:
    """Stub ``GET /v1/agents``: the Pi harness and the polly bundle agent."""
    return json.dumps(
        {
            "data": [
                {
                    "id": _PI_AGENT_ID,
                    "name": "pi-native-ui",
                    "display_name": "pi-native-ui",
                    "description": "Pi coding agent",
                    "harness": "pi-native",
                    "skills": [],
                },
                {
                    "id": _POLLY_AGENT_ID,
                    "name": "polly",
                    "display_name": "polly",
                    "description": "Multi-agent coding",
                    "harness": "claude-sdk",
                    "skills": [],
                },
            ]
        }
    )


def test_non_preferred_harness_stays_inline_when_an_agent_is_selected(
    seeded_session: tuple[str, str],
) -> None:
    """Pi is reachable without opening "Other..." while polly is selected.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_trimmed_roster(base_url, session_id))


async def _drive_trimmed_roster(base_url: str, session_id: str) -> None:
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
                agents_body=_trimmed_agents_body(),
            )

            # Neutralize agent discovery so only the stubbed Pi shows: the
            # landing picker merges /v1/agents with agents scanned from the
            # caller's sessions, and leftover sessions on the shared e2e_ui
            # server would otherwise add rows this assertion counts.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
            )

            # Select the bundle agent, not the harness. The picker promotes
            # whatever is selected into the inline list, so with Pi selected
            # there would be nothing to fix; polly selected is what leaves the
            # harness group with no inline candidate.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:last-agent-id", "{_POLLY_AGENT_ID}"
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await page.get_by_test_id("new-chat-landing-agent-select").click()

            # polly must actually be the pick, or the harness promotion below
            # would be the selected-entry path rather than the backfill.
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_POLLY_AGENT_ID}")
            ).to_be_visible()

            # The assertion: Pi renders inline. Pre-fix nothing promoted it, so
            # the row lived only inside the "Other..." flyout.
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
            ).to_be_visible()
            # ...and with nothing left to demote there is no submenu at all.
            await expect(page.get_by_test_id("new-chat-landing-harness-more")).to_have_count(0)
        finally:
            # Close the context first so the recorded video flushes, then the
            # browser.
            await context.close()
            await browser.close()

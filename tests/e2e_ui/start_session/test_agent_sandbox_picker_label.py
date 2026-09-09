"""E2E: the ``agent_sandbox`` managed-sandbox picker label is humanized.

A server configured with ``sandbox: {provider: agent_sandbox}`` (the
kubernetes-sigs agent-sandbox launcher) advertises the provider on
``GET /v1/info`` as ``sandbox_provider: "agent_sandbox"`` /
``sandbox_providers: ["agent_sandbox"]``. The new-session picker labels the
managed sandbox option from that id (``sandboxOptionLabel`` in
``web/src/lib/capabilities.ts``); providers without a display-name mapping
fall back to capitalizing the raw id, so the underscore leaks through and the
user sees **"Agent_sandbox Sandbox"** where they should see **"Agent
Sandbox"**.

The journey under test (mirrors the multi-provider picker tests in
``test_start_session.py``):

1. open the web UI against a server whose ``/v1/info`` reports the
   ``agent_sandbox`` managed sandbox provider (stubbed here with exactly the
   payload a really-configured server was observed to return),
2. open the new-session host picker,
3. read the managed sandbox row (and the host chip that defaults to it).

Both must read "Agent Sandbox" — never the raw "Agent_sandbox" id, and not a
doubled "Agent Sandbox Sandbox".
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


def _agent_sandbox_info_body() -> str:
    """Stub body for ``GET /v1/info``: a server offering the agent_sandbox provider.

    Field-for-field what a real ``omnigent server`` started with
    ``sandbox: {provider: agent_sandbox, server_url: ...}`` reports (verified
    live against such a server): the provider is launch-capable, so it is both
    the scalar ``sandbox_provider`` and the sole ``sandbox_providers`` row.
    Every other field the SPA's boot probe reads is supplied so the capability
    set resolves fully rather than to the fail-closed sentinel.
    """
    return json.dumps(
        {
            "accounts_enabled": False,
            "login_url": None,
            "needs_setup": False,
            "databricks_features": False,
            "managed_sandboxes_enabled": True,
            "sandbox_provider": "agent_sandbox",
            "sandbox_providers": ["agent_sandbox"],
            "server_version": "0.0.0-e2e",
            "smart_routing_enabled": False,
        }
    )


def test_agent_sandbox_picker_label_is_humanized(seeded_session: tuple[str, str]) -> None:
    """The agent_sandbox sandbox row and host chip read "Agent Sandbox".

    With the raw-id fallback in place the row renders "Agent_sandbox
    Sandbox", so the not-contains assertion on the raw id is what fails while
    the bug is live; once the provider label is humanized the row (and the
    chip defaulting to it) must read "Agent Sandbox" — with no leaked
    underscore and no doubled "Sandbox".
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_agent_sandbox_label(base_url, session_id))


async def _drive_agent_sandbox_label(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            # Managed capability probe naming the agent_sandbox provider —
            # the deployment shape behind the complaint.
            async def handle_info(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_agent_sandbox_info_body(),
                )

            await page.route("**/v1/info", handle_info)

            # No connected hosts, so the managed sandbox is unambiguously the
            # picker default and the chip is labeled from the provider id.
            async def handle_no_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"hosts": []})
                )

            await page.route("**/v1/hosts", handle_no_hosts)

            # Neutralize agent discovery so a leaked native agent from another
            # test can't switch the picker mid-flow (see _drive_permission_mode).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the new-session host picker and find the managed sandbox row.
            chip = page.get_by_test_id("new-chat-landing-host-chip")
            await chip.click()
            row = page.get_by_test_id("new-chat-landing-sandbox-option")
            await expect(row).to_be_visible()

            # The row must carry the humanized provider name: no raw
            # underscored id, no doubled "Sandbox", the readable name present.
            await expect(row).not_to_contain_text("Agent_sandbox")
            await expect(row).to_contain_text("Agent Sandbox")
            await expect(row).not_to_contain_text("Sandbox Sandbox")

            # Picking the row labels the chip the same humanized way.
            await row.click()
            await expect(chip).not_to_contain_text("Agent_sandbox")
            await expect(chip).to_contain_text("Agent Sandbox")
            await expect(chip).not_to_contain_text("Sandbox Sandbox")
        finally:
            # Close the context before the browser so a recorded video
            # flushes even when an assertion above fails mid-journey.
            await page.context.close()
            await browser.close()

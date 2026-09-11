"""E2E: the landing harness picker must not be hostage to a slow sessions scan.

The new-chat landing composer's agent/harness picker is fed by
``useAvailableAgents`` (``web/src/hooks/useAvailableAgents.ts``), which awaits
``Promise.all([GET /v1/agents, GET /v1/sessions?limit=100&kind=any&…]])`` before
returning ANY rows. The harness rows (Claude Code, Codex, …) come entirely from
the ``/v1/agents`` catalog, yet they cannot render until the sessions discovery
scan also resolves — so on a deployment where that scan is slow (managed
servers with large session tables), the picker sits disabled reading
"No agents" for the whole scan latency even though the catalog answered
immediately. Users see "harnesses slow to show up" in the new-session composer.

This test reproduces that hostage-taking deterministically: the catalog and
hosts endpoints are stubbed to answer instantly while the discovery scan is
delayed by ``_SCAN_DELAY_S`` (standing in for managed session-list latency),
and the test asserts the picker offers the harnesses within
``_PICKER_BUDGET_S`` — well under the injected delay. While the picker is
gated on the scan, the harnesses only appear after ``_SCAN_DELAY_S`` and the
assertion fails; once the picker renders catalog rows without waiting for the
discovery extension, it passes.

The stubbing shape (hosts/agents faked, ``kind=any`` scan intercepted) and the
async-in-a-fresh-thread drive mirror ``test_start_session.py`` /
``test_harness_install.py`` — see those modules for why the e2e harness needs
the stub host and the fresh event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _run_in_fresh_loop,
)

# Only the agent-discovery scan uses ``kind=any`` — the sidebar conversation
# list does not — so this pattern delays exactly the scan and nothing else.
_SCAN_RE = re.compile(r"/v1/sessions\?.*kind=any")
# Bare catalog list (with optional query), NOT ``/v1/agents/{id}`` subpaths.
_AGENTS_RE = re.compile(r"/v1/agents(\?.*)?$")

# Injected discovery-scan latency, standing in for a managed deployment's slow
# session-table scan. Far above the budget so a pass can't be a lucky race.
_SCAN_DELAY_S = 8.0
# How quickly the harness rows must be offered once the composer is on screen.
# The catalog answers instantly here, so anything slower means the picker is
# blocked on the scan.
_PICKER_BUDGET_S = 3.0


def _agents_body() -> str:
    """Instant ``GET /v1/agents``: the two seeded native-harness builtins."""
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_claude_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": "claude-native",
                    "skills": [],
                    "builtin": True,
                },
                {
                    "id": "ag_codex_e2e",
                    "name": "codex-native-ui",
                    "display_name": "Codex",
                    "description": "OpenAI's coding agent",
                    "harness": "codex-native",
                    "skills": [],
                    "builtin": True,
                },
            ]
        }
    )


def _hosts_body() -> str:
    """One online host with both stubbed harnesses ready."""
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": "e2e-host",
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": {
                        "claude-native": True,
                        "codex-native": True,
                    },
                }
            ]
        }
    )


async def _register_routes(page) -> None:
    """Instant hosts/agents; discovery scan held for ``_SCAN_DELAY_S``."""

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_scan(route: Route) -> None:
        # The scan itself succeeds — it is merely SLOW. (A failing scan is
        # already degraded to catalog-only by fetchAvailableAgents; slowness
        # is the uncovered path.) Empty data also keeps the seeded DB
        # sessions from leaking extra picker rows.
        await asyncio.sleep(_SCAN_DELAY_S)
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route(_AGENTS_RE, handle_agents)
    await page.route(_SCAN_RE, handle_scan)


async def _seed_workspace(page) -> None:
    """Seed a recent workspace so the composer settles on the stub host."""
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


def test_harness_picker_not_blocked_by_slow_session_scan(live_server: str) -> None:
    """Harness rows must appear promptly even when the discovery scan is slow.

    The catalog request resolves immediately; only the ``kind=any`` sessions
    scan lags. The picker must offer the catalog harnesses within
    ``_PICKER_BUDGET_S`` instead of sitting disabled ("No agents") until the
    scan returns.
    """
    _run_in_fresh_loop(_drive(live_server))


async def _drive(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
        context = await browser.new_context(
            **({"record_video_dir": record_dir} if record_dir else {})
        )
        page = await context.new_page()
        try:
            await _register_routes(page)
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            composer = page.get_by_test_id("new-chat-landing-input")
            await composer.wait_for(state="visible", timeout=30_000)

            picker = page.get_by_test_id("new-chat-landing-agent-select")
            await picker.wait_for(state="attached", timeout=10_000)

            # Clock starts when the composer is interactive: from here the
            # user is looking at the picker waiting for harnesses to show up.
            start = time.monotonic()
            deadline = start + _SCAN_DELAY_S + 20.0
            enabled_after_s: float | None = None
            while time.monotonic() < deadline:
                if await picker.is_enabled():
                    enabled_after_s = time.monotonic() - start
                    break
                await asyncio.sleep(0.1)

            assert enabled_after_s is not None, (
                "harness picker never offered any agents: it stayed disabled "
                f"('No agents') for {_SCAN_DELAY_S + 20.0:.0f}s even though "
                "GET /v1/agents returned the harness catalog immediately"
            )

            # Journey sanity: once enabled, the picker really offers the
            # catalog harnesses (this also closes the filmed journey).
            await picker.click()
            claude_row = page.get_by_test_id("new-chat-landing-agent-ag_claude_e2e")
            await expect(claude_row).to_be_visible(timeout=10_000)
            codex_row = page.get_by_test_id("new-chat-landing-agent-ag_codex_e2e")
            await expect(codex_row).to_be_visible(timeout=10_000)
            # Brief settle so a recorded run doesn't cut on the click frame.
            await asyncio.sleep(0.5)

            assert enabled_after_s <= _PICKER_BUDGET_S, (
                f"harnesses took {enabled_after_s:.1f}s to show up in the "
                "new-session composer's picker (disabled, 'No agents') even "
                "though GET /v1/agents answered instantly — the picker is "
                "hostage to the sessions discovery scan (delayed "
                f"{_SCAN_DELAY_S:.0f}s here); catalog rows should render "
                f"within {_PICKER_BUDGET_S:.0f}s without waiting for the scan"
            )
        finally:
            # Close the context even on failure so a recorded run still
            # finalizes its video.
            await context.close()
            await browser.close()

"""E2E: the prototype picker leads with Claude Code, Cursor, and Codex.

Other harnesses stay in Other; its label identifies the selected harness.
All stubbed harnesses are ready, so grouping is independent of availability.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

# Stubbed host the composer auto-selects (the tunneled runner registers no
# host). Keyed identically in the recent-workspaces localStorage seed.
_HOST_ID = "host_e2e"
_HOST_NAME = "e2e-host"

# Claude Code, Cursor and Codex are primary; Pi starts in Other.
_CLAUDE_AGENT_ID = "ag_claude_e2e"
_CODEX_AGENT_ID = "ag_codex_e2e"
_CURSOR_AGENT_ID = "ag_cursor_e2e"
_PI_AGENT_ID = "ag_pi_e2e"


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop.

    The e2e_ui suite runs many pytest-playwright **sync** tests in the same
    session; once one has run, pytest-asyncio can't start a loop on the main
    thread. Running the coroutine from a fresh thread via :func:`asyncio.run`
    sidesteps that. Any exception (including assertion failures) is captured and
    re-raised on the calling thread so the test fails normally.

    :param coro: The coroutine to run to completion.
    :raises Exception: Whatever the coroutine raised, re-raised here.
    """
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


def _hosts_body() -> str:
    """Stub body for ``GET /v1/hosts``: one online host, every harness ready.

    All four harnesses are configured so availability cannot affect grouping.
    """
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": _HOST_NAME,
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": {
                        "claude-native": True,
                        "codex-native": True,
                        "cursor-native": True,
                        "pi-native": True,
                    },
                }
            ]
        }
    )


def _agents_body() -> str:
    """Stub body for ``GET /v1/agents``: two supported + two unsupported natives."""
    return json.dumps(
        {
            "data": [
                {
                    "id": _CLAUDE_AGENT_ID,
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": "claude-native",
                    "skills": [],
                },
                {
                    "id": _CODEX_AGENT_ID,
                    "name": "codex-native-ui",
                    "display_name": "Codex",
                    "description": "OpenAI's coding agent",
                    "harness": "codex-native",
                    "skills": [],
                },
                {
                    "id": _CURSOR_AGENT_ID,
                    "name": "cursor-native-ui",
                    "display_name": "Cursor",
                    "description": "Cursor's coding agent",
                    "harness": "cursor-native",
                    "skills": [],
                },
                {
                    "id": _PI_AGENT_ID,
                    "name": "pi-native-ui",
                    "display_name": "Pi",
                    "description": "Pi coding agent",
                    "harness": "pi-native",
                    "skills": [],
                },
            ]
        }
    )


async def _register_routes(page) -> None:
    """Register the host/agent stubs and neutralize agent discovery.

    :param page: The Playwright page to install routes on.
    """

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_agent_scan(route: Route) -> None:
        # Neutralize agent discovery so only the stubbed agents feed the picker;
        # sessions other tests left behind would otherwise leak in and swap the
        # selection out from under the assertions.
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)


async def _open_picker(page) -> None:
    """Open the landing agent/harness picker dropdown."""
    await page.get_by_test_id("new-chat-landing-agent-select").click()


def test_recent_harness_remains_in_other_group(
    seeded_session: tuple[str, str],
) -> None:
    """Recent launches do not change the prototype's primary harness order."""
    base_url, session_id = seeded_session
    del session_id  # this flow never creates a session — only reads the picker
    _run_in_fresh_loop(_drive_recent(base_url))


async def _drive_recent(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(page)
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );
                window.localStorage.setItem(
                    "omnigent:recent-harnesses",
                    JSON.stringify(["pi-native"])
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _open_picker(page)

            # Recent Pi remains in Other, keeping the primary list stable.
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
            ).to_have_count(0)
            # Cursor is always primary, independently of launch history.
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_CURSOR_AGENT_ID}")
            ).to_be_visible()
        finally:
            await browser.close()


def test_picker_leads_with_primary_harnesses(
    seeded_session: tuple[str, str],
) -> None:
    """Primary harnesses remain inline; Other identifies a selected Pi harness."""
    base_url, session_id = seeded_session
    del session_id  # this flow never creates a session — only reads the picker
    _run_in_fresh_loop(_drive(base_url))


async def _drive(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(page)
            # Seed a recent working directory so the composer auto-fills (it
            # never has to touch the host-less file browser).
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

            # 1. The fully supported harnesses lead the list inline.
            await _open_picker(page)
            claude = page.get_by_test_id(f"new-chat-landing-agent-{_CLAUDE_AGENT_ID}")
            codex = page.get_by_test_id(f"new-chat-landing-agent-{_CODEX_AGENT_ID}")
            await expect(claude).to_be_visible(timeout=30_000)
            await expect(codex).to_be_visible(timeout=30_000)

            # Cursor is primary; Pi appears only after opening Other.
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_CURSOR_AGENT_ID}")
            ).to_be_visible()
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
            ).to_have_count(0)

            await page.get_by_test_id("new-chat-landing-harness-more").click()
            cursor_row = page.get_by_test_id(f"new-chat-landing-agent-{_CURSOR_AGENT_ID}")
            pi_row = page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
            await expect(cursor_row).to_be_visible(timeout=30_000)
            await expect(pi_row).to_be_visible()
            # Primary rows precede the Other submenu.
            assert await _renders_before(page, _CURSOR_AGENT_ID, _PI_AGENT_ID), (
                "expected primary Cursor row to precede Pi in Other"
            )

            # The trigger and Other label both identify the selected harness.
            await pi_row.click()
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_have_attribute(
                "aria-label", re.compile("Pi")
            )
            await page.keyboard.press("Escape")
            await expect(page.get_by_role("menu")).to_have_count(0)
            await _open_picker(page)
            other = page.get_by_test_id("new-chat-landing-harness-more")
            await expect(other).to_contain_text("Pi")
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
            ).to_have_count(0)
            await other.click()
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
            ).to_have_attribute("data-active", "true")
        finally:
            await browser.close()


async def _renders_before(page, first_agent_id: str, second_agent_id: str) -> bool:
    """Whether *first_agent_id*'s row precedes *second_agent_id*'s in the DOM.

    Document order is what the user reads top-to-bottom, so comparing positions
    checks the rendered ordering rather than merely that both rows exist.

    :param page: The Playwright page (both rows are mounted).
    :param first_agent_id: Stubbed agent id expected to render first.
    :param second_agent_id: Stubbed agent id expected to render second.
    :returns: True when the first row precedes the second in document order.
    """
    return await page.evaluate(
        """([firstId, secondId]) => {
            const sel = (id) => document.querySelector(
                `[data-testid="new-chat-landing-agent-${id}"]`
            );
            const a = sel(firstId);
            const b = sel(secondId);
            if (!a || !b) return false;
            return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
        }""",
        [first_agent_id, second_agent_id],
    )

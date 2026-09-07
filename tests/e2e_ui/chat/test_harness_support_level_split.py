"""E2E: the New Chat picker leads with fully supported harnesses.

The landing composer (``NewChatLandingScreen`` in
``web/src/shell/NewChatDialog.tsx``) splits its **Harnesses** group by *support
level*, not host readiness: only harnesses flagged ``fullySupported`` in
``web/src/lib/nativeCodingAgents.ts`` (Claude Code and Codex) list inline, and
every other harness folds into the "More" submenu.

Why the stub marks **every** harness configured: readiness could otherwise
populate "More" on its own, and the test would pass without proving anything
about the support-level split. With all four harnesses ready on the host, the
only thing that can push Cursor and Pi behind "More" is the flag — so this
covers the actual behavior rather than a confound.

The two rules that interact with the split get their own assertions: the
currently-selected harness is pinned inline even when it isn't fully supported
(so the active pick is never buried), and the ordering inside "More" follows the
same ``sortRank`` as the primary list.

Why the ``page.route`` stubbing and the async-in-a-fresh-thread shape: both are
inherited from ``chat/test_hide_unconfigured_harnesses.py`` — the e2e harness's
runner tunnels into the server and registers no *host*, so faking ``/v1/hosts``
(with ``configured_harnesses``) and ``/v1/agents`` is the established way to
drive the landing picker, and once a pytest-playwright *sync* test has run in
the session, pytest-asyncio can't start a loop on the main thread, so each
async body runs in its own thread via :func:`asyncio.run`.
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

# Two fully supported harnesses (lead inline) and two that are not (fold into
# "More"). Cursor (sortRank 30) precedes Pi (40), which the "More" ordering
# assertion relies on.
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

    Marking all four harnesses configured is the point of the fixture — it
    removes readiness as a possible cause for the "More" grouping, leaving the
    ``fullySupported`` flag as the only explanation.
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


def test_previously_launched_harness_is_promoted_inline(
    seeded_session: tuple[str, str],
) -> None:
    """A harness in ``omnigent:recent-harnesses`` leads instead of hiding in "More".

    Same fixture as the split test — every harness configured — so the promotion
    can only come from the stored recents list. Seeding localStorage rather than
    driving a real launch keeps this a picker test: the create path's write is
    covered by the ``NewChatDialog.flow`` unit test.
    """
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

            # Pi was launched before → inline alongside the supported harnesses.
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
            ).to_be_visible(timeout=30_000)
            # Cursor was not → still behind "More", so the promotion is specific
            # to the stored entry rather than a blanket un-grouping.
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_CURSOR_AGENT_ID}")
            ).to_have_count(0)
        finally:
            await browser.close()


def test_picker_leads_with_fully_supported_harnesses(
    seeded_session: tuple[str, str],
) -> None:
    """Claude Code and Codex lead; other harnesses fold into "More".

    1. **primary list** — Claude Code and Codex render inline, in rank order.
    2. **"More"** — Cursor and Pi are absent inline and appear (in rank order)
       only after opening the submenu, even though the host reports both ready.
    3. **selected pick pinned** — choosing Pi promotes it inline, so the active
       harness is never buried behind a hover.
    """
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

            # 2. Cursor and Pi are configured on this host yet still not inline:
            #    support level, not readiness, put them behind "More".
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_CURSOR_AGENT_ID}")
            ).to_have_count(0)
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
            ).to_have_count(0)

            await page.get_by_test_id("new-chat-landing-harness-more").click()
            cursor_row = page.get_by_test_id(f"new-chat-landing-agent-{_CURSOR_AGENT_ID}")
            pi_row = page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
            await expect(cursor_row).to_be_visible(timeout=30_000)
            await expect(pi_row).to_be_visible()
            # "More" keeps the primary list's rank ordering: Cursor (30) → Pi (40).
            assert await _renders_before(page, _CURSOR_AGENT_ID, _PI_AGENT_ID), (
                "expected Cursor to render before Pi inside the More submenu"
            )

            # 3. Committing Pi pins it inline — the active pick is never buried.
            await pi_row.click()
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "Pi"
            )
            # Committing closes the menu; wait for it to unmount before
            # reopening, else the click lands mid-close and never reopens.
            await page.get_by_test_id("new-chat-landing-harness-more").wait_for(
                state="detached", timeout=10_000
            )
            await _open_picker(page)
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
            ).to_be_visible(timeout=30_000)
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

"""E2E: the "Hide unconfigured harnesses" setting filters the New Chat picker.

The landing composer (``NewChatLandingScreen`` in
``web/src/shell/NewChatDialog.tsx``) lists every harness and badges the ones
that aren't set up on the selected host. The Settings → Appearance toggle
``HideUnconfiguredHarnessesControl`` (``pages/SettingsPage.tsx``) writes
``localStorage["omnigent:hide-unconfigured-harnesses"]``; when on, the picker's
**Harnesses** group drops rows the selected host reports as unconfigured
(``harnessUnconfiguredOnHost`` against the host's ``configured_harnesses`` map).

This drives the whole flow end to end against the rendered UI: the toggle
starts off (every harness shown), flipping the real Switch persists the
preference, and the picker then hides the harness the stubbed host reports as
unconfigured while keeping the configured one.

Why the ``page.route`` stubbing and the async-in-a-fresh-thread shape: both are
inherited from ``chat/test_codex_auth_availability.py`` — the e2e harness's
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

_TOGGLE_KEY = "omnigent:hide-unconfigured-harnesses"

# Two native harness agents: Claude Code is configured on the stubbed host,
# Goose is not. Both are native coding agents, so both render under the
# picker's "Harnesses" group — the surface the filter acts on.
_CLAUDE_AGENT_ID = "ag_claude_e2e"
_GOOSE_AGENT_ID = "ag_goose_e2e"


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
    """Stub body for ``GET /v1/hosts``: one online host the composer picks.

    Its ``configured_harnesses`` marks ``claude-native`` available and
    ``goose-native`` unconfigured — the wire shape the ``host.hello`` readiness
    map produces, so the stub exercises the real availability → picker path.
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
                        "goose-native": False,
                    },
                }
            ]
        }
    )


def _agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the Claude and Goose native agents."""
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
                    "id": _GOOSE_AGENT_ID,
                    "name": "goose-native-ui",
                    "display_name": "Goose",
                    "description": "Block's coding agent",
                    "harness": "goose-native",
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


def test_hide_unconfigured_harnesses_filters_the_picker(
    seeded_session: tuple[str, str],
) -> None:
    """Off shows every harness; flipping the setting hides host-unconfigured ones.

    1. **default (off)** — the picker lists both Claude Code and Goose, even
       though the host reports Goose unconfigured (it's badged, not hidden).
    2. **toggle on** — flipping the real Settings → Appearance Switch persists
       the preference; the picker now drops the Goose row while keeping Claude.
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

            # 1. Default (toggle off): Claude lists inline; Goose is unconfigured
            #    on the host, so it folds into the "More" submenu (badged "needs
            #    setup") but is still reachable/selectable.
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _open_picker(page)
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_CLAUDE_AGENT_ID}")
            ).to_be_visible(timeout=30_000)
            # Goose isn't inline — drill into "More" to reveal it.
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_GOOSE_AGENT_ID}")
            ).to_have_count(0)
            await page.get_by_test_id("new-chat-landing-harness-more").click()
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_GOOSE_AGENT_ID}")
            ).to_be_visible()

            # 2. Flip the real Settings → Appearance Switch on and confirm it
            #    persists to localStorage.
            await page.goto(f"{base_url}/settings/appearance")
            toggle = page.get_by_test_id("hide-unconfigured-harnesses-toggle")
            await expect(toggle).to_be_visible(timeout=30_000)
            await expect(toggle).to_have_attribute("aria-checked", "false")
            await toggle.click()
            await expect(toggle).to_have_attribute("aria-checked", "true")
            stored = await page.evaluate(f"() => window.localStorage.getItem('{_TOGGLE_KEY}')")
            assert stored == "true", f"toggle did not persist (got {stored!r})"

            # Back on the composer, the picker remounts and re-reads the
            # preference: Goose (unconfigured on the host) is gone; Claude stays.
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _open_picker(page)
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_CLAUDE_AGENT_ID}")
            ).to_be_visible(timeout=30_000)
            # count()==0 (not "not visible"): the row is conditionally rendered,
            # never just hidden.
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-{_GOOSE_AGENT_ID}")
            ).to_have_count(0)
        finally:
            await browser.close()

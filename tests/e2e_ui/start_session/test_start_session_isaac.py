"""E2E: starting a new Isaac native session from the home composer ("/").

Mirrors the Pi-native picker coverage in ``test_start_session.py`` for the
Isaac native terminal-mirror agent this PR adds. Isaac is a Claude Code
wrapper CLI (``isaac``) that runs as a pure terminal mirror, so the
user-facing surface is: the new-chat picker offers "Isaac", and selecting
it + sending POSTs ``/v1/sessions`` with the terminal-first wrapper labels
that make the runner launch the Isaac TUI and the web UI render the
Chat/Terminal view.

The async-in-a-fresh-thread shape and the host/agent/create/events stubbing
are inherited from ``test_start_session.py`` for the reasons documented
there (the e2e harness's tunneled runner registers no host, so the composer
needs ``/v1/hosts`` + ``/v1/agents`` faked, and the create POST is captured
rather than launched).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

# Stubbed host the composer auto-selects (the tunneled runner registers no host).
_HOST_ID = "host_e2e"
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own event loop.

    The e2e_ui suite runs many pytest-playwright **sync** tests in the same
    session; once one has run, pytest-asyncio can't start a loop on the main
    thread. Running the coroutine from a fresh thread via :func:`asyncio.run`
    sidesteps that, re-raising any failure on the calling thread.

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


async def _wait_until(predicate, *, timeout_s: float = 15.0) -> None:
    """Poll ``predicate`` on the event loop until true or timeout.

    :param predicate: Zero-arg callable returning truthy when satisfied.
    :param timeout_s: Max seconds to wait before failing the test.
    :raises AssertionError: If the predicate never becomes truthy.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _isaac_native_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the native Isaac agent.

    ``name: "isaac-native-ui"`` + ``harness: "isaac-native"`` is what the
    frontend maps (via ``nativeCodingAgents``) to the display label **"Isaac"**
    and the isaac-native wrapper labels. The wire ``display_name`` is set to the
    raw ``"isaac-native-ui"`` to prove the picker derives "Isaac" itself rather
    than echoing the server. Sole agent, so it auto-selects.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_isaac_e2e",
                    "name": "isaac-native-ui",
                    "display_name": "isaac-native-ui",
                    "description": "Isaac coding agent",
                    "harness": "isaac-native",
                    "skills": [],
                }
            ]
        }
    )


def _hosts_body() -> str:
    """Stub body for ``GET /v1/hosts``: one online host the composer picks."""
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": "e2e-host",
                    "owner": "e2e",
                    "status": "online",
                }
            ]
        }
    )


def test_start_session_isaac_native_picker_and_wrapper_labels(
    seeded_session: tuple[str, str],
) -> None:
    """Native Isaac: the picker shows "Isaac" and create carries terminal labels.

    Covers the user-facing Isaac native-agent flow this PR adds:

    1. **Picker label** — the agent chip renders the harness-derived display
       label **"Isaac"** (via ``nativeCodingAgents``), NOT the raw agent name
       ``"isaac-native-ui"`` the server sends.
    2. **Session-creation wrapper labels** — selecting Isaac and sending POSTs
       ``/v1/sessions`` with the terminal-first wrapper labels
       (``omnigent.ui: terminal`` + ``omnigent.wrapper: isaac-native-ui``) that
       make the runner launch the Isaac TUI and the web UI render the
       Chat/Terminal view.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_isaac_native_start(base_url, session_id))


async def _drive_isaac_native_start(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_isaac_native_agents_body(),
                )

            async def handle_events(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    create_bodies.append(route.request.post_data_json)
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            # Neutralize agent discovery so the picker shows ONLY the stubbed
            # built-in Isaac. The landing picker merges /v1/agents with agents
            # found by scanning the caller's sessions (/v1/sessions?kind=any);
            # on the shared e2e_ui server, a native fork another test left behind
            # would otherwise leak in and — ranking ahead of Isaac — auto-select.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)
            # Registered after the bare-sessions route so it wins for kind=any.
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # Seed a recent working directory so the working-directory chip
            # auto-fills and Send can enable without touching the file browser.
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

            # Isaac auto-selects (sole agent). The chip shows the derived label
            # "Isaac" — and crucially NOT the raw "...native..." agent name.
            agent_chip = page.get_by_test_id("new-chat-landing-agent-select")
            await expect(agent_chip).to_contain_text("Isaac")
            await expect(agent_chip).not_to_contain_text("native")

            await page.get_by_test_id("new-chat-landing-input").fill("explore the repo")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["agent_id"] == "ag_isaac_e2e", body
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == "/work/repo", body
            # The terminal-first wrapper labels are the contract that drives the
            # runner-owned Isaac TUI and the web UI's Chat/Terminal view.
            assert body.get("labels") == {
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "isaac-native-ui",
            }, body
        finally:
            await browser.close()

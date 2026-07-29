"""E2E: the built-in omp (Oh My Pi) agent renders in the New Chat picker.

Covers the UI change that adds the ACP-backed ``omp`` harness. omp is not a
native coding-agent tile (it renders in the web composer, not a terminal), so
its picker label comes from ``DISPLAY_NAMES`` in
``web/src/hooks/useAvailableAgents.ts`` (``displayNameForAgent`` maps the ``omp``
agent name to "Oh My Pi") and its glyph from the ``omp`` branch of
``AgentCard.iconForAgent``. This drives the rendered picker end to end: on a host
that reports ``omp`` configured, the "Oh My Pi" tile is listed and selectable.

The ``page.route`` stubbing and async-in-a-fresh-thread shape mirror
``chat/test_hide_unconfigured_harnesses.py``: the e2e harness's runner tunnels
into the server and registers no *host*, so faking ``/v1/hosts`` (with
``configured_harnesses``) and ``/v1/agents`` is the established way to drive the
landing picker, and once a pytest-playwright *sync* test has run in the session,
pytest-asyncio can't start a loop on the main thread, so each async body runs in
its own thread via :func:`asyncio.run`.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

_HOST_ID = "host_e2e"
_HOST_NAME = "e2e-host"
_OMP_AGENT_ID = "ag_omp_e2e"


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own event loop (see module docstring)."""
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001 — re-raised on the calling thread below
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


def _hosts_body() -> str:
    """Stub ``GET /v1/hosts``: one online host that reports ``omp`` configured."""
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": _HOST_NAME,
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": {"omp": True},
                }
            ]
        }
    )


def _agents_body() -> str:
    """Stub ``GET /v1/agents``: the built-in omp (Oh My Pi) agent.

    The server returns the raw ``name``/``harness``; the web recomputes the
    display label via ``displayNameForAgent`` (so ``display_name`` here is
    intentionally the un-prettified name, proving the "Oh My Pi" mapping is the
    frontend's doing, not the stub's).
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": _OMP_AGENT_ID,
                    "name": "omp",
                    "display_name": "omp",
                    "description": "Oh My Pi (omp) coding agent, driven over ACP.",
                    "harness": "omp",
                    "skills": [],
                }
            ]
        }
    )


async def _register_routes(page) -> None:
    """Stub the host/agent endpoints and neutralize session-scan discovery."""

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_agent_scan(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)


def test_omp_agent_tile_renders_in_picker(seeded_session: tuple[str, str]) -> None:
    """The omp built-in shows as an "Oh My Pi" tile in the New Chat picker."""
    base_url, session_id = seeded_session
    del session_id  # this flow only reads the picker; it never creates a session
    _run_in_fresh_loop(_drive(base_url))


async def _drive(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(page)
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
            await page.get_by_test_id("new-chat-landing-agent-select").click()

            tile = page.get_by_test_id(f"new-chat-landing-agent-{_OMP_AGENT_ID}")
            await expect(tile).to_be_visible(timeout=30_000)
            # The label is the frontend-computed "Oh My Pi" (DISPLAY_NAMES["omp"]),
            # not the raw agent name the stub returned.
            await expect(tile).to_contain_text("Oh My Pi")
        finally:
            await browser.close()

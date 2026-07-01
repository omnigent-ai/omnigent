"""E2E: the landing mascot (Otto) follows whichever the user last moved.

The ``NewChatLandingScreen`` hero renders ``OttoEyes`` — the starfish mascot
whose pupils track what the user is doing. Otto looks at whatever moved last:
the mouse pointer, or — while the composer textarea (``new-chat-landing-input``,
which sits *below* the mascot) is focused — its **text caret**. Moving the
mouse pulls his gaze to the pointer even while the field is focused; typing
pulls it back to the caret.

The pupils never re-render React — the effect writes a ``translate(...)``
transform straight onto the ``g.otto-pupil`` groups — so the test reads that
transform's vertical component to prove where Otto is looking:

* mouse parked at the top of the viewport (above the mascot) → pupils ride the
  rim **upward** (negative ``ty``);
* the composer (below the mascot) focused and typed into → pupils swing
  **downward** (positive ``ty``) toward the caret;
* mouse moved back to the top *while the composer stays focused* → pupils swing
  back **up**, proving the pointer still wins on a mouse move;
* one more keystroke → pupils swing **down** again, proving the caret wins on a
  caret move.

The heavy ``page.route`` stubbing mirrors the sibling ``test_start_session``
suite: the tunneled e2e runner registers no host and the landing's data calls
are async, so ``/v1/hosts``, ``/v1/agents``, the host filesystem, and the
session list are faked to give the hero a deterministic, fully-painted
composer. No session is created here, so the create ``POST`` is left to fall
through. The async-in-a-fresh-thread shape is inherited from
``test_start_session`` for the reason documented there (pytest-asyncio can't
start a loop on the main thread once a pytest-playwright sync test has run).
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
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")
_FILESYSTEM_RE = re.compile(r"/v1/hosts/[^/]+/filesystem")

_AGENTS_BODY = json.dumps(
    {
        "data": [
            {
                "id": "ag_claude_e2e",
                "name": "claude-native-ui",
                "display_name": "Claude Code",
                "description": "Anthropic's coding agent",
                "harness": None,
                "skills": [],
            }
        ]
    }
)
_HOSTS_BODY = json.dumps(
    {"hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]}
)
_EMPTY_LIST_BODY = json.dumps({"object": "list", "data": [], "has_more": False})

# Reads the first pupil's live translate() transform, or null before the
# rAF pipeline has written one. Only the vertical component (ty) is asserted:
# its sign is where Otto is looking on the vertical axis.
_READ_PUPIL_TY = """
() => {
  const g = document.querySelector('g.otto-pupil');
  const t = (g && g.style.transform) || '';
  const m = t.match(/translate\\((-?[\\d.]+)px, (-?[\\d.]+)px\\)/);
  return m ? parseFloat(m[2]) : null;
}
"""


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop."""
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


async def _register_landing_routes(page) -> None:
    """Stub the landing's data endpoints so the hero paints deterministically."""

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_HOSTS_BODY)

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_AGENTS_BODY)

    async def handle_filesystem(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_EMPTY_LIST_BODY)

    async def handle_sessions(route: Route) -> None:
        # GET listing / agent-discovery scan → empty; the create POST isn't
        # exercised here, so let anything non-GET fall through to the server.
        if route.request.method == "GET":
            await route.fulfill(status=200, content_type="application/json", body=_EMPTY_LIST_BODY)
        else:
            await route.continue_()

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(_FILESYSTEM_RE, handle_filesystem)
    await page.route(_SESSIONS_RE, handle_sessions)


def test_landing_mascot_follows_last_moved_source(live_server: str) -> None:
    """Otto follows the mouse on a move and the caret on a keystroke, each winning in turn."""
    _run_in_fresh_loop(_drive_mascot_eyes(live_server))


async def _drive_mascot_eyes(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            await _register_landing_routes(page)
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            composer = page.get_by_test_id("new-chat-landing-input")
            await composer.wait_for(state="visible", timeout=30_000)
            # The mascot renders as a labelled image; its pupils are the
            # g.otto-pupil groups the effect steers.
            await expect(page.get_by_role("img", name="Omnigent")).to_be_visible()

            look_up = (
                f"() => {{ const ty = ({_READ_PUPIL_TY})(); return ty !== null && ty < -0.5; }}"
            )
            look_down = (
                f"() => {{ const ty = ({_READ_PUPIL_TY})(); return ty !== null && ty > 0.5; }}"
            )

            # Move the mouse to the top of the viewport, above the mascot: the
            # pupils ride upward toward it.
            await page.mouse.move(640, 4)
            await page.wait_for_function(look_up, timeout=10_000)

            # Focus the composer (below the mascot) and type: the caret is now
            # the last-moved source, so Otto swings DOWN to watch it.
            await composer.click()
            await composer.type("hello there")
            await page.wait_for_function(look_down, timeout=10_000)

            # Move the mouse back up WITHOUT touching the caret, composer still
            # focused: the pointer wins again and the pupils swing back UP —
            # the fix for the mouse being ignored while the box is focused.
            await page.mouse.move(640, 4)
            await page.wait_for_function(look_up, timeout=10_000)

            # One more keystroke moves the caret: Otto swings DOWN again,
            # proving the caret reclaims his gaze on a caret move.
            await composer.press("!")
            await page.wait_for_function(look_down, timeout=10_000)
        finally:
            await browser.close()

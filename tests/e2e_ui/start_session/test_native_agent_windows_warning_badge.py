"""E2E: New-session picker shows warning badges for native-terminal
agents on a Windows host.

## Bug

On a Windows host the ``*-native-ui`` agents (Claude Code, Codex, Cursor, Pi,
OpenCode, Kimi, Kiro, Goose, Hermes, Qwen, Antigravity) were listed in the
new-session picker **without** any warning badge.  Selecting one caused the
session to fail immediately with ``native_terminal_start_failed`` because
tmux/PTY is not supported on Windows.

The root cause was that the host daemon's ``configured_harness_map()``
(:mod:`omnigent.onboarding.harness_readiness`) did not check ``IS_WINDOWS``
before probing binary presence.  On Windows, a native CLI binary (e.g.
``claude.exe``) may well be installed, so the probe returned ``True`` — but
the native terminal launch fails anyway because tmux is unavailable.  The
``configured_harnesses`` map the daemon sent to the server therefore contained
``"claude-native": true``, so the UI's
``harnessUnavailableReasonOnHost("claude-native", host)`` returned ``null`` and
**no warning badge was rendered**.

## Fix

``configured_harness_map()`` now returns ``False`` for every member of
``NATIVE_HARNESSES`` when ``IS_WINDOWS`` is ``True``, short-circuiting before
any auth/binary probe.  The picker maps ``False`` → a non-null unavailability
reason → renders a warning badge, so the user sees the agent is unavailable
before they try to start it.

## What this test asserts

This test stubs a host whose ``configured_harnesses["claude-native"]`` is
``False`` (what the fixed daemon sends on Windows) and verifies that the
web picker renders a warning badge on the ``claude-native-ui`` row.

The constant ``_WINDOWS_NATIVE_HARNESS_AVAILABLE = False`` reflects the
**post-fix** daemon behavior.  Setting it to ``True`` (the pre-fix, buggy
state) would cause the test to fail because the picker would see no
unavailability reason and render no badge — reproducing the original symptom.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_HOST_ID = "host_e2e_windows"

# What the **fixed** Windows daemon reports for a native harness: False.
# The IS_WINDOWS guard in configured_harness_map() short-circuits before the
# binary probe, so the daemon sends False even when claude.exe is on PATH.
# Setting this to True simulates the pre-fix (buggy) state: the picker would
# see True → no unavailability reason → no warning badge → user picks agent
# → session fails with native_terminal_start_failed.
_WINDOWS_NATIVE_HARNESS_AVAILABLE: bool = False

# Canonical native terminal harness that the picker must warn about on Windows.
_TESTED_NATIVE_HARNESS = "claude-native"

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _windows_hosts_body() -> str:
    """One online Windows host whose ``configured_harnesses`` reflects the
    **fixed** daemon: native harnesses report ``False`` because IS_WINDOWS is
    True and the Windows guard short-circuits before the binary probe.

    The picker maps ``False`` to a non-null unavailability reason, which causes
    it to render a warning badge on the native-agent row.
    """
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": "windows-e2e-host",
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": {
                        _TESTED_NATIVE_HARNESS: _WINDOWS_NATIVE_HARNESS_AVAILABLE,
                        "claude-sdk": True,  # SDK is always fine
                    },
                }
            ]
        }
    )


def _claude_native_agents_body() -> str:
    """One native-terminal agent (claude-native-ui) in the picker catalog.

    Its ``harness: "claude-native"`` is the key the picker uses to look up
    readiness in ``configured_harnesses``.  When the lookup returns ``False``,
    ``harnessUnavailableReasonOnHost`` returns a non-null reason → warning badge
    is rendered.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_claude_native_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent (native terminal)",
                    "harness": "claude-native",
                    "skills": [],
                },
            ]
        }
    )


def _info_body() -> str:
    """Minimal /v1/info stub (features off, no setup dialog path)."""
    return json.dumps({"version": "0.0.0", "features": [], "installable_harnesses": []})


# ---------------------------------------------------------------------------
# Thread helpers
# ---------------------------------------------------------------------------


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own event loop.

    Matches the pattern used throughout ``test_start_session.py``.
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


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------

_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")


async def _register_common_routes(page: Any, created_session_id: str) -> None:
    """Stub the minimal routes the landing composer needs."""

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=_windows_hosts_body(),
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=_claude_native_agents_body(),
        )

    async def handle_info(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=_info_body(),
        )

    async def handle_create_session(route: Route) -> None:
        if route.request.method != "POST":
            await route.continue_()
            return
        await route.fulfill(
            status=201,
            content_type="application/json",
            body=json.dumps({"session_id": created_session_id}),
        )

    async def handle_events(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="text/event-stream",
            body="",
        )

    await page.route(re.compile(r"/v1/hosts$"), handle_hosts)
    await page.route(re.compile(r"/v1/agents"), handle_agents)
    await page.route(re.compile(r"/v1/info$"), handle_info)
    await page.route(_SESSIONS_RE, handle_create_session)
    await page.route(re.compile(r"/v1/events/"), handle_events)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_native_agents_show_warning_badge_on_windows_host(
    seeded_session: tuple[str, str],
) -> None:
    """Native-terminal agents must carry a warning badge on a Windows host.

    The picker listed all ``*-native-ui`` agents without any warning when the
    selected host was Windows, even though every native terminal launch fails
    with ``native_terminal_start_failed`` on that platform.

    ## Steps

    1. Load the landing composer with a stubbed Windows host whose
       ``configured_harnesses`` reports ``"claude-native"`` as ``False``
       (what the fixed daemon sends: IS_WINDOWS guard short-circuits the binary
       probe).
    2. Open the agent-picker dropdown.
    3. Assert the warning badge IS visible on the ``claude-native-ui`` row.

    ## Why it works

    With ``_WINDOWS_NATIVE_HARNESS_AVAILABLE = False`` the host stub mirrors
    the fixed Windows daemon.  The UI calls
    ``harnessUnavailableReasonOnHost("claude-native", host)`` which returns a
    non-null reason for a ``False`` value → warning badge is rendered →
    ``expect(badge).to_be_visible()`` passes.

    Setting ``_WINDOWS_NATIVE_HARNESS_AVAILABLE = True`` (the pre-fix, buggy
    state) would cause the assertion to fail: the picker would see no
    unavailability reason and render no badge — reproducing the original
    symptom where the user selected an agent that immediately failed.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_windows_host_picker_warning(base_url, session_id))


async def _drive_windows_host_picker_warning(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_common_routes(page, session_id)

            # Suppress the agent-discovery scan so only our stubbed agent
            # feeds the picker (same pattern as test_start_session.py).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)

            # Seed a recent working directory so the Send button can be
            # enabled without touching the (unavailable) host filesystem API.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ "{_HOST_ID}": ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the agent-picker dropdown.
            await page.get_by_test_id("new-chat-landing-agent-select").click()

            # The claude-native-ui row should be visible in the picker.
            native_row = page.get_by_test_id("new-chat-landing-agent-ag_claude_native_e2e")
            await expect(native_row).to_be_visible()

            # Assert that the warning badge IS visible.
            # The fixed daemon sends False for native harnesses on Windows;
            # the picker maps False → a non-null unavailability reason → badge.
            native_badge = page.get_by_test_id(
                "new-chat-landing-agent-warning-ag_claude_native_e2e"
            )
            await expect(native_badge).to_be_visible(timeout=5_000)
        finally:
            await browser.close()

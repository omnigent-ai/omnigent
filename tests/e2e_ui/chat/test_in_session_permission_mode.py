"""E2E: in-session permission/approval mode selector in the AgentPicker.

The AgentPicker dropdown (bottom-right of the composer in an active
session) shows a Permission mode / Approval mode section for native
terminal sessions (Claude Code, Codex). Selecting a non-default mode
PATCHes ``terminal_launch_args`` onto the session.

These tests intercept the session snapshot GET so the SPA believes it's
inside a Claude-native (or Codex-native) session, and intercept the
PATCH to capture the ``terminal_launch_args`` payload the UI sends.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

_SESSION_ID_RE = re.compile(r"/v1/sessions/(?P<id>[^/]+)$")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
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
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _claude_native_session_body(session_id: str) -> str:
    """Stub session snapshot for a Claude-native terminal session."""
    return json.dumps(
        {
            "id": session_id,
            "agent_id": "ag_claude_e2e",
            "agent_name": "claude-native-ui",
            "status": "idle",
            "created_at": 0,
            "title": "test session",
            "labels": {
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "claude-code-native-ui",
            },
            "items": [],
            "reasoning_effort": "high",
            "llm_model": "anthropic/claude-sonnet-4-6",
            "terminal_launch_args": None,
        }
    )


def _codex_native_session_body(session_id: str) -> str:
    """Stub session snapshot for a Codex-native terminal session."""
    return json.dumps(
        {
            "id": session_id,
            "agent_id": "ag_codex_e2e",
            "agent_name": "codex-native-ui",
            "status": "idle",
            "created_at": 0,
            "title": "test session",
            "labels": {
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "codex-native-ui",
            },
            "items": [],
            "reasoning_effort": "high",
            "terminal_launch_args": None,
        }
    )


async def _register_session_routes(
    page,
    *,
    session_id: str,
    session_body: str,
    patch_bodies: list[dict[str, Any]],
) -> None:
    """Register route stubs for an in-session permission mode test.

    - GET /v1/sessions/{id} returns the stubbed snapshot.
    - PATCH /v1/sessions/{id} captures the request body and echoes the
      snapshot back (so the store reconciles).
    - Other routes pass through to the real server.
    """

    async def handle_session(route: Route) -> None:
        if route.request.method == "GET":
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=session_body,
            )
        elif route.request.method == "PATCH":
            patch_bodies.append(route.request.post_data_json)
            # Echo the session snapshot back with the patched args applied.
            patched = json.loads(session_body)
            req = route.request.post_data_json or {}
            if "terminal_launch_args" in req:
                patched["terminal_launch_args"] = req["terminal_launch_args"]
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(patched),
            )
        else:
            await route.continue_()

    # Match the specific session URL (not sub-paths like /events).
    await page.route(
        re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?.*)?$"),
        handle_session,
    )

    # Swallow events so no real LLM turn runs.
    async def handle_events(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
        )

    await page.route(f"**/v1/sessions/{session_id}/events", handle_events)


def test_in_session_permission_mode_claude(seeded_session: tuple[str, str]) -> None:
    """The AgentPicker shows permission modes for a Claude-native session.

    Opening the AgentPicker in a Claude-native session must show the
    Permission mode section. Selecting "Accept edits" must PATCH the
    session with ``terminal_launch_args: ["--permission-mode", "acceptEdits"]``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_claude_permission_mode(base_url, session_id))


async def _drive_claude_permission_mode(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            patch_bodies: list[dict[str, Any]] = []
            await _register_session_routes(
                page,
                session_id=session_id,
                session_body=_claude_native_session_body(session_id),
                patch_bodies=patch_bodies,
            )

            await page.goto(f"{base_url}/c/{session_id}")
            trigger = page.get_by_test_id("agent-picker-trigger")
            await expect(trigger).to_be_visible(timeout=30_000)
            await trigger.click()

            # The Permission mode section should be visible with all six options.
            for mode in ("default", "auto", "acceptEdits", "plan", "dontAsk", "bypassPermissions"):
                await expect(
                    page.locator(
                        f'[data-testid="permission-mode-picker-item"][data-mode-value="{mode}"]'
                    )
                ).to_be_visible()

            # Select "acceptEdits".
            await page.locator(
                '[data-testid="permission-mode-picker-item"][data-mode-value="acceptEdits"]'
            ).click()

            await _wait_until(lambda: len(patch_bodies) >= 1)
            # Find the PATCH that carries terminal_launch_args.
            mode_patch = next(
                (b for b in patch_bodies if "terminal_launch_args" in b),
                None,
            )
            assert mode_patch is not None, f"No PATCH with terminal_launch_args: {patch_bodies}"
            assert mode_patch["terminal_launch_args"] == [
                "--permission-mode",
                "acceptEdits",
            ], mode_patch
        finally:
            await browser.close()


def test_in_session_approval_mode_codex(seeded_session: tuple[str, str]) -> None:
    """The AgentPicker shows approval modes for a Codex-native session.

    Opening the AgentPicker in a Codex-native session must show the
    Approval mode section. Selecting "full-auto" must PATCH the session
    with ``terminal_launch_args: ["--approval-mode", "full-auto"]``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_codex_approval_mode(base_url, session_id))


async def _drive_codex_approval_mode(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            patch_bodies: list[dict[str, Any]] = []
            await _register_session_routes(
                page,
                session_id=session_id,
                session_body=_codex_native_session_body(session_id),
                patch_bodies=patch_bodies,
            )

            await page.goto(f"{base_url}/c/{session_id}")
            trigger = page.get_by_test_id("agent-picker-trigger")
            await expect(trigger).to_be_visible(timeout=30_000)
            await trigger.click()

            # The Approval mode section should be visible with all three options.
            for mode in ("suggest", "auto-edit", "full-auto"):
                await expect(
                    page.locator(
                        f'[data-testid="permission-mode-picker-item"][data-mode-value="{mode}"]'
                    )
                ).to_be_visible()

            # Select "full-auto".
            await page.locator(
                '[data-testid="permission-mode-picker-item"][data-mode-value="full-auto"]'
            ).click()

            await _wait_until(lambda: len(patch_bodies) >= 1)
            mode_patch = next(
                (b for b in patch_bodies if "terminal_launch_args" in b),
                None,
            )
            assert mode_patch is not None, f"No PATCH with terminal_launch_args: {patch_bodies}"
            assert mode_patch["terminal_launch_args"] == [
                "--approval-mode",
                "full-auto",
            ], mode_patch
        finally:
            await browser.close()

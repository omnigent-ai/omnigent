"""Regression tests: new-session prompt loss + workspace-picker hang.

Symptom 1 — the typed prompt must never be silently lost. On the
new-session page the user types a prompt and presses Enter; the create
POST succeeds, but every GET of the fresh session fails (e.g. a
rate-limit window answering 429). Today the SPA drops to "Conversation
not found" with the prompt nowhere on screen, and "Start a new chat"
lands on an EMPTY landing composer — the text is unrecoverable. The
plain create-*failure* path already restores the draft to the composer,
so this test requires the same recovery when the create succeeds but the
session's first load fails: returning to the landing page must restore
the typed prompt.

Symptom 2 — the workspace picker must give bounded feedback. With a host
that is online but wedged (its tunnel never answers ``host.list_dir``),
opening the landing workspace picker today shows a bare "Loading
folder…" spinner with no error and no entries while the server times
each request out at 5s and the client silently retries — first visible
feedback only appears after ~26s. This test requires the picker to
surface feedback (an error row or actual entries) within 15s of opening.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
import uuid
from collections.abc import AsyncIterator, Coroutine
from typing import Any

import httpx
import pytest
from playwright.async_api import Page, Route, async_playwright, expect

from omnigent.host.frames import (
    HostCreateDirFrame,
    HostCreateDirResultFrame,
    HostDetectCredentialsFrame,
    HostDetectCredentialsResultFrame,
    HostHelloFrame,
    HostListDirFrame,
    HostListWorktreesFrame,
    HostListWorktreesResultFrame,
    HostModelOptionsFrame,
    HostModelOptionsResultFrame,
    HostStatFrame,
    HostStatResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runner.transports.ws_tunnel.frames import (
    PingFrame,
    PongFrame,
    decode_frame,
    encode_frame,
)
from tests.e2e_ui.start_session.helpers import open_landing_workspace_picker

_HOST_ID = "host_e2e"
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")
_PROMPT = "prompt that must survive a failed first load"

# The server times a single list_dir out at 5s; feedback must not need
# multiple silent client-side retry rounds on top of that.
_PICKER_FEEDBACK_DEADLINE_S = 15.0


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run ``coro`` on a dedicated thread/event loop (pytest is sync here)."""
    captured: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the pytest thread
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


# ---------------------------------------------------------------------------
# Symptom 1 — prompt survives create-OK + first-load-failure
# ---------------------------------------------------------------------------


def _hosts_body() -> str:
    return json.dumps(
        {
            "hosts": [
                {"host_id": _HOST_ID, "name": "arca-like-host", "owner": "e2e", "status": "online"}
            ]
        }
    )


def _agents_body() -> str:
    return json.dumps(
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


async def _stub_landing(page: Page, on_create: Any) -> None:
    """Give the landing composer a ready host/agent/workspace to send with."""

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_scan(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_scan)
    await page.route(_SESSIONS_RE, on_create)
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


def test_prompt_survives_failed_first_load(seeded_session: tuple[str, str]) -> None:
    """A prompt sent from the landing page is recoverable when the fresh
    session's first load fails (create POST 200, every session GET 429)."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_prompt_loss(base_url, session_id))


async def _drive_prompt_loss(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:

            async def on_create(route: Route) -> None:
                if route.request.method == "POST":
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            # A rate-limit window: every request for the created session 429s.
            async def on_session_request(route: Route) -> None:
                await route.fulfill(
                    status=429,
                    content_type="application/json",
                    body=json.dumps({"detail": "Too many requests"}),
                )

            await page.route(re.compile(rf"/v1/sessions/{session_id}"), on_session_request)
            await _stub_landing(page, on_create)

            await page.goto(f"{base_url}/")
            landing = page.get_by_test_id("new-chat-landing-input")
            await landing.wait_for(state="visible", timeout=30_000)
            await landing.fill(_PROMPT)
            await landing.press("Enter")

            # The create succeeded, the load fails: the SPA lands on the
            # conversation-load error screen.
            await expect(
                page.get_by_role("heading", name="Conversation not found")
            ).to_be_visible(timeout=30_000)

            # Going back to the landing page must restore the typed prompt
            # to the composer (parity with the create-failure recovery).
            # Today the composer comes back EMPTY and the text is gone.
            await page.get_by_role("button", name="Start a new chat").click()
            await expect(landing).to_be_visible(timeout=15_000)
            await expect(landing).to_have_value(_PROMPT, timeout=10_000)
        finally:
            # Close the context before the browser so a recorded video (when
            # OMNIGENT_E2E_RECORD_DIR is set) is finalized even on failure.
            await page.context.close()
            await browser.close()


# ---------------------------------------------------------------------------
# Symptom 2 — picker gives bounded feedback against an unresponsive host
# ---------------------------------------------------------------------------

_WEDGED_HOST_NAME = "e2e-wedged-host"


async def _serve_wedged_host(ws: Any) -> None:
    """Answer everything EXCEPT host.list_dir, which is silently dropped."""
    async for raw in ws:
        if not isinstance(raw, str):
            continue
        try:
            frame = decode_host_frame(raw)
        except ValueError:
            try:
                runner_frame = decode_frame(raw)
            except ValueError:
                continue
            if isinstance(runner_frame, PingFrame):
                await ws.send(encode_frame(PongFrame(ts=runner_frame.ts)))
            continue
        reply: Any = None
        if isinstance(frame, HostListDirFrame):
            continue  # wedged: never answer the directory listing
        if isinstance(frame, HostCreateDirFrame):
            reply = HostCreateDirResultFrame(
                request_id=frame.request_id, status="ok", error="unsupported"
            )
        elif isinstance(frame, HostStatFrame):
            reply = HostStatResultFrame(
                request_id=frame.request_id,
                status="ok",
                exists=True,
                type="directory",
                canonical_path=frame.path,
            )
        elif isinstance(frame, HostListWorktreesFrame):
            reply = HostListWorktreesResultFrame(
                request_id=frame.request_id, status="failed", error="not a git repository"
            )
        elif isinstance(frame, HostModelOptionsFrame):
            reply = HostModelOptionsResultFrame(request_id=frame.request_id, status="ok")
        elif isinstance(frame, HostDetectCredentialsFrame):
            reply = HostDetectCredentialsResultFrame(request_id=frame.request_id)
        if reply is not None:
            await ws.send(encode_host_frame(reply))


@contextlib.asynccontextmanager
async def _wedged_host(base_url: str) -> AsyncIterator[str]:
    """Connect a real tunnel host that goes online but never lists dirs."""
    import websockets

    host_id = uuid.uuid4().hex
    ws_url = base_url.replace("http://", "ws://") + f"/v1/hosts/{host_id}/tunnel"
    async with websockets.connect(ws_url) as ws:
        await ws.send(
            encode_host_frame(
                HostHelloFrame(
                    version="0.0.0-e2e", frame_protocol_version=1, name=_WEDGED_HOST_NAME
                )
            )
        )
        serve_task = asyncio.create_task(_serve_wedged_host(ws))
        try:
            rest_host_id: str | None = None
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    resp = await client.get(f"{base_url}/v1/hosts")
                    hosts = resp.json().get("hosts", [])
                    match = next(
                        (
                            h
                            for h in hosts
                            if h["name"] == _WEDGED_HOST_NAME and h["status"] == "online"
                        ),
                        None,
                    )
                    if match is not None:
                        rest_host_id = match["host_id"]
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("fake wedged host never came online")
            assert rest_host_id is not None
            yield rest_host_id
        finally:
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await serve_task


def test_picker_feedback_when_host_unresponsive(live_server: str) -> None:
    """The workspace picker surfaces an error (or entries) within a bounded
    window when the selected host never answers ``host.list_dir`` — instead
    of an unbounded, silent "Loading folder…" spinner."""
    _run_in_fresh_loop(_drive_picker_wedged(live_server))


async def _drive_picker_wedged(base_url: str) -> None:
    async with _wedged_host(base_url) as host_id, async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await page.get_by_test_id("new-chat-landing-host-chip").click()
            await page.get_by_test_id(f"new-chat-landing-host-{host_id}").click(timeout=15_000)
            await page.locator('[data-slot="dropdown-menu-content"]').first.wait_for(
                state="detached", timeout=10_000
            )
            await open_landing_workspace_picker(page)

            loop = asyncio.get_running_loop()
            start = loop.time()
            last_state = "picker just opened"
            while loop.time() - start < _PICKER_FEEDBACK_DEADLINE_S:
                error = page.get_by_test_id("workspace-picker-error")
                if await error.count() and await error.first.is_visible():
                    return  # feedback surfaced — expected behavior
                entries = await page.locator('[data-testid^="workspace-picker-entry-"]').count()
                if entries:
                    return  # listing populated — expected behavior
                listing = page.get_by_test_id("workspace-picker-listing")
                busy = (
                    await listing.get_attribute("aria-busy") if await listing.count() else None
                )
                loading_rows = await page.get_by_text("Loading folder…").count()
                last_state = (
                    f"aria-busy={busy} loading_row={loading_rows} entries=0 error=absent"
                )
                await asyncio.sleep(0.5)
            pytest.fail(
                "workspace picker gave no feedback within "
                f"{_PICKER_FEEDBACK_DEADLINE_S:.0f}s of opening against an unresponsive host "
                f"(last observed: {last_state}) — bare 'Loading folder…' spinner with no "
                "error and no entries"
            )
        finally:
            # Close the context before the browser so a recorded video (when
            # OMNIGENT_E2E_RECORD_DIR is set) is finalized even on failure.
            await page.context.close()
            await browser.close()

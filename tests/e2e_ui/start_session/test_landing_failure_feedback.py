"""E2E: the new-session page keeps the user informed when a host or session fails.

Two journeys drive the real SPA against the live server with a scripted host
connected over the real host WebSocket tunnel. The host registers online and
answers ``host.stat`` / ``host.launch_runner`` like a healthy daemon, so a
landing-page create succeeds for real. The picker journey additionally never
answers ``host.list_dir``, which is what an online-but-wedged host looks like
to the server (its listing times out).

Browser journeys run on a fresh thread and loop (``tests._helpers.async_thread``)
because pytest-asyncio can't start a loop on the main thread once a sync
pytest-playwright test has run in the session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from playwright.async_api import Page, Response, Route, async_playwright, expect

from omnigent.host.frames import (
    HostCreateDirResultFrame,
    HostHelloFrame,
    HostLaunchRunnerResultFrame,
    HostListDirEntry,
    HostListDirResultFrame,
    HostListWorktreesResultFrame,
    HostMcpServersResultFrame,
    HostModelOptionsResultFrame,
    HostSkillsResultFrame,
    HostStatResultFrame,
    HostStopRunnerResultFrame,
    encode_host_frame,
)
from omnigent.runner.identity import token_bound_runner_id
from omnigent.runner.transports.ws_tunnel.frames import (
    PingFrame,
    PongFrame,
    decode_frame,
    encode_frame,
)
from omnigent.server.routes.host_tunnel import SUPPORTED_FRAME_PROTOCOL_MAJOR
from omnigent.version import VERSION
from tests._helpers.async_thread import run_in_fresh_loop
from tests.e2e_ui.start_session.helpers import open_landing_workspace_picker

HOST_NAME = "wedged-host-e2e"
WORKSPACE = "/work/repo"
AGENT_NAME = "polly"
AGENT_LABEL = "Polly"
PROMPT = "Prompt that must survive a failed first load of the new session"

# Any request scoped to a session id; the create POST and the list are untouched.
_SESSION_SCOPED_RE = re.compile(r"/v1/sessions/[0-9a-f]{32}(?:[/?]|$)")
# The server times a listing out after 5s; feedback later than this is a hang.
PICKER_FEEDBACK_WINDOW_S = 15.0
_VIEWPORT = {"width": 1440, "height": 900}
_CONTEXT_ARGS: dict[str, Any] = {"viewport": _VIEWPORT, "record_video_size": _VIEWPORT}


# ── Scripted host ────────────────────────────────────────────────────────────


async def _serve_host(ws: Any, *, answer_list_dir: bool, seen: list[str]) -> None:
    """Answer host frames like a healthy daemon, optionally withholding list_dir."""
    async for raw in ws:
        if not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        kind = payload.get("kind")
        request_id = str(payload.get("request_id", ""))
        if isinstance(kind, str):
            seen.append(kind)
        reply: Any = None
        if kind == "host.stat":
            path = str(payload.get("path", "~"))
            reply = HostStatResultFrame(
                request_id=request_id,
                status="ok",
                exists=True,
                type="directory",
                canonical_path="/home/e2e" if path == "~" else path,
            )
        elif kind == "host.list_dir":
            if not answer_list_dir:
                continue
            reply = HostListDirResultFrame(
                request_id=request_id,
                status="ok",
                entries=[
                    HostListDirEntry(
                        name="repo", path=WORKSPACE, type="directory", bytes=None, modified_at=0
                    )
                ],
            )
        elif kind == "host.list_worktrees":
            reply = HostListWorktreesResultFrame(
                request_id=request_id, status="failed", error="not a git repository"
            )
        elif kind == "host.create_dir":
            reply = HostCreateDirResultFrame(
                request_id=request_id, status="ok", path=payload.get("path")
            )
        elif kind == "host.model_options":
            reply = HostModelOptionsResultFrame(request_id=request_id, status="ok")
        elif kind == "host.skills":
            reply = HostSkillsResultFrame(request_id=request_id, status="ok")
        elif kind == "host.mcp_servers":
            reply = HostMcpServersResultFrame(request_id=request_id, status="ok")
        elif kind == "host.launch_runner":
            reply = HostLaunchRunnerResultFrame(
                request_id=request_id,
                status="launched",
                runner_id=token_bound_runner_id(str(payload["binding_token"])),
            )
        elif kind == "host.stop_runner":
            reply = HostStopRunnerResultFrame(request_id=request_id, status="ok")
        if reply is not None:
            await ws.send(encode_host_frame(reply))
            continue
        if isinstance(kind, str) and kind.startswith("host."):
            continue
        try:
            runner_frame = decode_frame(raw)
        except ValueError:
            continue
        if isinstance(runner_frame, PingFrame):
            await ws.send(encode_frame(PongFrame(ts=runner_frame.ts)))


@contextlib.asynccontextmanager
async def scripted_host(
    base_url: str, *, answer_list_dir: bool
) -> AsyncIterator[tuple[str, list[str]]]:
    """Connect a scripted host to the live server's tunnel and yield its REST id.

    :param base_url: The live server's base URL.
    :param answer_list_dir: ``False`` makes the host swallow every ``host.list_dir``
        so the server's listing times out — an online-but-unresponsive host.
    :returns: ``(host_id, seen)`` where ``seen`` accumulates received frame kinds.
    """
    import websockets

    ws_url = base_url.replace("http://", "ws://") + f"/v1/hosts/{uuid.uuid4().hex}/tunnel"
    seen: list[str] = []
    async with websockets.connect(ws_url) as ws:
        await ws.send(
            encode_host_frame(
                HostHelloFrame(
                    version=VERSION,
                    frame_protocol_version=SUPPORTED_FRAME_PROTOCOL_MAJOR,
                    name=HOST_NAME,
                    configured_harnesses={"claude-sdk": True, "claude-native": True},
                )
            )
        )
        serve_task = asyncio.create_task(
            _serve_host(ws, answer_list_dir=answer_list_dir, seen=seen)
        )
        try:
            yield await _wait_host_online(base_url), seen
        finally:
            # Suppress only the cancellation; a real error from _serve_host must
            # propagate so a crashed responder isn't hidden behind a later timeout.
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await serve_task


async def _wait_host_online(base_url: str) -> str:
    async with httpx.AsyncClient(trust_env=False) as client:
        for _ in range(100):
            resp = await client.get(f"{base_url}/v1/hosts")
            if resp.status_code != 200:
                # Server still warming up: retry rather than let a non-JSON body
                # mask the "host never came online" diagnosis.
                await asyncio.sleep(0.1)
                continue
            for host in resp.json().get("hosts", []):
                if host["name"] == HOST_NAME and host["status"] == "online":
                    return str(host["host_id"])
            await asyncio.sleep(0.1)
    raise AssertionError(
        f"scripted host never came online; last /v1/hosts response: "
        f"{resp.status_code} {resp.text[:500]}"
    )


async def agent_id_by_name(base_url: str, name: str) -> str:
    async with httpx.AsyncClient(trust_env=False) as client:
        resp = await client.get(f"{base_url}/v1/agents")
    assert resp.status_code == 200, f"/v1/agents returned {resp.status_code}: {resp.text[:500]}"
    agents = {agent["name"]: agent["id"] for agent in resp.json()["data"]}
    assert name in agents, f"agent {name!r} not registered; have {sorted(agents)}"
    return str(agents[name])


# ── Landing-page steps ───────────────────────────────────────────────────────


def seed_landing_preferences(host_id: str, agent_id: str) -> str:
    """Remembered picks: a recent workspace on the host and the last-used agent."""
    recents = json.dumps({host_id: [WORKSPACE]})
    return (
        f"window.localStorage.setItem('omnigent:recent-workspaces', JSON.stringify({recents}));"
        f"window.localStorage.setItem('omnigent:last-agent-id', {json.dumps(agent_id)});"
    )


async def open_landing(page: Page, base_url: str, host_id: str) -> None:
    """Open the new-session page and pick the scripted host through the host chip."""
    await page.goto(f"{base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    chip = page.get_by_test_id("new-chat-landing-host-chip")
    await chip.click()
    await page.get_by_test_id(f"new-chat-landing-host-{host_id}").click()
    # A loopback server with a single online host labels it as the local machine.
    await expect(chip).to_have_attribute(
        "aria-label", re.compile(rf"(?:{re.escape(HOST_NAME)}|This machine), Online")
    )
    # The closing host menu hands focus back to its chip a moment later, which
    # would dismiss any menu opened in between.
    await expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)
    await expect(chip).to_be_focused()
    await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_have_text("repo")
    await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
        AGENT_LABEL, timeout=30_000
    )


def _output_dir(request: pytest.FixtureRequest, name: str) -> Path:
    output = Path(str(request.config.getoption("--output"))) / name
    output.mkdir(parents=True, exist_ok=True)
    return output


# ── Journey 1: first prompt when the fresh session cannot load ───────────────


def test_first_prompt_survives_failed_session_load(
    live_server: str, browser_name: str, request: pytest.FixtureRequest
) -> None:
    """Enter on the landing composer must not lose the prompt when the session fails to load."""
    output = _output_dir(request, "prompt")
    run_in_fresh_loop(_drive_prompt_journey(live_server, browser_name, output))


async def _drive_prompt_journey(base_url: str, browser_name: str, output: Path) -> None:
    created: list[dict[str, Any]] = []

    async def record_create(response: Response) -> None:
        if response.request.method == "POST" and urlparse(response.url).path == "/v1/sessions":
            body = await response.json() if response.ok else {}
            created.append({"status": response.status, "id": body.get("id")})

    async def rate_limit(route: Route) -> None:
        await route.fulfill(
            status=429,
            content_type="application/json",
            body=json.dumps({"detail": "Too many requests; try again later."}),
        )

    async with scripted_host(base_url, answer_list_dir=True) as (host_id, _seen):
        agent_id = await agent_id_by_name(base_url, AGENT_NAME)
        async with async_playwright() as playwright:
            browser = await getattr(playwright, browser_name).launch()
            context = await browser.new_context(
                **_CONTEXT_ARGS,
                record_har_path=str(output / "network.har"),
                record_har_content="omit",
            )
            try:
                await context.add_init_script(seed_landing_preferences(host_id, agent_id))
                await context.route(_SESSION_SCOPED_RE, rate_limit)
                page = await context.new_page()
                page.on("response", record_create)
                await open_landing(page, base_url, host_id)

                composer = page.get_by_test_id("new-chat-landing-input")
                await composer.click()
                await composer.press_sequentially(PROMPT, delay=25)
                await expect(composer).to_have_value(PROMPT)
                # Enter is a no-op until the create guard opens; wait for the submit
                # control to enable so the host-backed workspace validation settled.
                await expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled(
                    timeout=15_000
                )
                await composer.press("Enter")

                await expect(
                    page.get_by_role("heading", name="Conversation not found")
                ).to_be_visible(timeout=30_000)
                await page.screenshot(path=str(output / "conversation-not-found.png"))
                assert created and 200 <= created[0]["status"] < 300, (
                    f"the create itself must succeed for this journey; saw {created}"
                )
                await expect(page.get_by_text(PROMPT)).to_have_count(0)
                # Wait for any late echo of the prompt, then re-assert none landed.
                await page.wait_for_timeout(3_000)
                await expect(page.get_by_text(PROMPT)).to_have_count(0)

                await page.get_by_role("button", name="Start a new chat").click()
                await expect(composer).to_be_visible(timeout=15_000)
                await page.screenshot(path=str(output / "landing-after-start-new-chat.png"))
                await expect(composer).to_have_value(PROMPT, timeout=5_000)
            finally:
                # Chain teardown so a context.close() failure still closes the
                # browser and runs session cleanup rather than leaking both.
                try:
                    try:
                        await context.close()
                    finally:
                        await browser.close()
                finally:
                    (output / "created-sessions.json").write_text(json.dumps(created))
                    async with httpx.AsyncClient(trust_env=False) as client:
                        for entry in created:
                            if entry["id"]:
                                await client.delete(f"{base_url}/v1/sessions/{entry['id']}")


# ── Journey 2: directory picker on an online-but-unresponsive host ───────────


def test_directory_picker_reports_unresponsive_host_promptly(
    live_server: str, browser_name: str, request: pytest.FixtureRequest
) -> None:
    """The picker must show entries or an error within the feedback window, not spin."""
    output = _output_dir(request, "picker")
    run_in_fresh_loop(_drive_picker_journey(live_server, browser_name, output))


async def _drive_picker_journey(base_url: str, browser_name: str, output: Path) -> None:
    async with scripted_host(base_url, answer_list_dir=False) as (host_id, seen):
        agent_id = await agent_id_by_name(base_url, AGENT_NAME)
        async with async_playwright() as playwright:
            browser = await getattr(playwright, browser_name).launch()
            context = await browser.new_context(**_CONTEXT_ARGS)
            try:
                await context.add_init_script(seed_landing_preferences(host_id, agent_id))
                page = await context.new_page()
                await open_landing(page, base_url, host_id)
                await open_landing_workspace_picker(page)
                opened = time.monotonic()
                listing = page.get_by_test_id("workspace-picker-listing")
                await expect(listing).to_contain_text("Loading folder")

                samples: list[tuple[float, str]] = []
                feedback_at: float | None = None
                while time.monotonic() - opened < PICKER_FEEDBACK_WINDOW_S:
                    text = " ".join((await listing.inner_text()).split())
                    if not samples or samples[-1][1] != text:
                        samples.append((round(time.monotonic() - opened, 1), text))
                    if "Loading folder" not in text:
                        feedback_at = time.monotonic() - opened
                        break
                    await page.wait_for_timeout(500)
                await page.screenshot(path=str(output / "picker-after-window.png"))
                assert feedback_at is not None, (
                    f"picker showed no entries and no error within {PICKER_FEEDBACK_WINDOW_S:g}s; "
                    f"host received {seen.count('host.list_dir')} list_dir request(s); "
                    f"listing samples (s, text): {samples}"
                )
                # The feedback must be the server's connectivity error, not an
                # empty-directory render: the host never answers list_dir, so a
                # blank-but-not-loading listing would be a regression.
                error = page.get_by_test_id("workspace-picker-error")
                await expect(error).to_be_visible(timeout=2_000)
                await expect(error).to_contain_text("did not respond to list_dir")
            finally:
                try:
                    await context.close()
                finally:
                    await browser.close()

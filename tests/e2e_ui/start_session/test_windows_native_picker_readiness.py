"""End-to-end Windows native-readiness coverage for the new-session picker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import Route, async_playwright, expect

import omnigent
from omnigent.host.frames import (
    HostCreateDirFrame,
    HostCreateDirResultFrame,
    HostDetectCredentialsFrame,
    HostDetectCredentialsResultFrame,
    HostHelloFrame,
    HostListDirFrame,
    HostListDirResultFrame,
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

_AGENT_ID = "ag_claude_native_e2e"
_HARNESS = "claude-native"
_WIN_WORKSPACE = "C:\\Users\\alice\\work"

# These values mean the local machine could not stage the test precondition.
_BROKEN_PRECONDITION_REASONS = frozenset({"binary-missing", "needs-auth", "version-too-low"})

# Import readiness in a subprocess so the Windows simulation cannot leak.
_READINESS_PROBE_SOURCE = """
import json, os
import platform as platform_mod
import omnigent._platform as omni_platform

if os.environ.get("SIMULATE_WINDOWS") == "1":
    omni_platform.IS_WINDOWS = True
    platform_mod.system = lambda: "Windows"
import omnigent.onboarding.harness_readiness as readiness
if os.environ.get("SIMULATE_WINDOWS") == "1" and hasattr(readiness, "IS_WINDOWS"):
    readiness.IS_WINDOWS = True
print(json.dumps(readiness.configured_harness_map()))
"""

_ANTHROPIC_CREDENTIAL_CONFIG = {
    "providers": {
        "e2e-claude": {
            "kind": "key",
            "default": ["anthropic"],
            "anthropic": {
                "base_url": "http://127.0.0.1:1/v1",
                "api_key": "e2e-key",
                "models": {"default": "claude-sonnet-4-20250514"},
            },
        }
    }
}

_SCRUBBED_ENV_TOKENS = ("ANTHROPIC", "OPENAI", "CLAUDE", "CODEX")


def _daemon_readiness_map(*, simulate_windows: bool, credentialed: bool) -> dict[str, Any]:
    """Compute an isolated readiness map using this checkout."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        config_home = Path(tmp) / "config"
        home.mkdir()
        config_home.mkdir()
        if credentialed:
            (config_home / "config.yaml").write_text(
                json.dumps(_ANTHROPIC_CREDENTIAL_CONFIG), encoding="utf-8"
            )
        script = Path(tmp) / "probe.py"
        script.write_text(_READINESS_PROBE_SOURCE, encoding="utf-8")
        # Keep ambient credentials out of the readiness result.
        env = {
            key: value
            for key, value in os.environ.items()
            if not any(token in key.upper() for token in _SCRUBBED_ENV_TOKENS)
            and not key.upper().endswith("_API_KEY")
        }
        env["HOME"] = str(home)
        env["OMNIGENT_CONFIG_HOME"] = str(config_home)
        # Force the subprocess to import this checkout rather than another editable install.
        repo_root = Path(omnigent.__file__).resolve().parent.parent
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{repo_root}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else str(repo_root)
        )
        if simulate_windows:
            env["SIMULATE_WINDOWS"] = "1"
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
            check=False,
        )
        assert result.returncode == 0, (
            f"readiness probe failed (exit {result.returncode}):\n{result.stderr[-2000:]}"
        )
        return json.loads(result.stdout.strip().splitlines()[-1])


async def _serve_host_frames(ws: Any) -> None:
    """Answer host frames on the tunnel like an idle host daemon."""
    async for raw in ws:
        if not isinstance(raw, str):
            continue
        try:
            frame = decode_host_frame(raw)
        except ValueError:
            # The server sends keepalives with the runner-tunnel encoding.
            try:
                runner_frame = decode_frame(raw)
            except ValueError:
                continue
            if isinstance(runner_frame, PingFrame):
                await ws.send(encode_frame(PongFrame(ts=runner_frame.ts)))
            continue
        reply: Any = None
        if isinstance(frame, HostListDirFrame):
            reply = HostListDirResultFrame(
                request_id=frame.request_id, status="ok", entries=[], has_more=False
            )
        elif isinstance(frame, HostCreateDirFrame):
            reply = HostCreateDirResultFrame(
                request_id=frame.request_id, status="ok", error="permission denied"
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
async def _fake_host(
    base_url: str, name: str, configured_harnesses: dict[str, Any]
) -> AsyncIterator[str]:
    """Connect a host with the computed readiness map to the real tunnel."""
    import websockets

    tunnel_host_id = uuid.uuid4().hex
    ws_url = base_url.replace("http://", "ws://") + f"/v1/hosts/{tunnel_host_id}/tunnel"
    async with websockets.connect(ws_url) as ws:
        await ws.send(
            encode_host_frame(
                HostHelloFrame(
                    version="0.0.0-e2e",
                    frame_protocol_version=1,
                    name=name,
                    configured_harnesses=configured_harnesses,
                )
            )
        )
        serve_task = asyncio.create_task(_serve_host_frames(ws))
        try:
            rest_host_id: str | None = None
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    resp = await client.get(f"{base_url}/v1/hosts")
                    hosts = resp.json().get("hosts", [])
                    match = next(
                        (h for h in hosts if h["name"] == name and h["status"] == "online"),
                        None,
                    )
                    if match is not None:
                        rest_host_id = match["host_id"]
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError(f"fake host {name} never came online")
            assert rest_host_id is not None
            yield rest_host_id
        finally:
            serve_task.cancel()
            await asyncio.gather(serve_task, return_exceptions=True)


def _agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the Claude Code native agent."""
    return json.dumps(
        {
            "data": [
                {
                    "id": _AGENT_ID,
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": _HARNESS,
                    "skills": [],
                }
            ]
        }
    )


async def _register_routes(page: Any) -> None:
    """Pin the agent catalog; hosts stay real (they come from the tunnel)."""

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_agent_scan(route: Route) -> None:
        # Keep agents from other tests out of this picker.
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/agents", handle_agents)
    await page.route(
        re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
    )


async def _open_picker_on_host(page: Any, base_url: str, host_id: str) -> None:
    """Open the landing agent picker with the fake host selected."""
    await page.add_init_script(
        "window.localStorage.setItem('omnigent:recent-workspaces', "
        + json.dumps(json.dumps({host_id: [_WIN_WORKSPACE]}))
        + ");"
    )
    await page.goto(f"{base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    await page.get_by_test_id("new-chat-landing-host-chip").click()
    await expect(page.get_by_test_id(f"new-chat-landing-host-{host_id}")).to_be_visible(
        timeout=15_000
    )
    await page.get_by_test_id(f"new-chat-landing-host-{host_id}").click()
    # Wait for the host menu to unmount so it cannot steal focus from the picker.
    await expect(page.locator('[data-slot="dropdown-menu-content"]')).to_have_count(0)
    await page.get_by_test_id("new-chat-landing-agent-select").click()


async def _reveal_claude_row(page: Any) -> Any:
    """Return the Claude Code picker row, expanding "More" when it is folded."""
    row = page.get_by_test_id(f"new-chat-landing-agent-{_AGENT_ID}")
    if await row.count() == 0:
        more = page.get_by_test_id("new-chat-landing-harness-more")
        if await more.count() > 0:
            await more.click()
    await expect(row).to_be_visible(timeout=15_000)
    return row


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run after sync Playwright has occupied the main thread's event loop."""
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


def test_windows_host_native_agent_carries_warning_badge(live_server: str) -> None:
    """A native agent on a simulated Windows host carries a warning badge."""
    real_map = _daemon_readiness_map(simulate_windows=False, credentialed=True)
    if real_map.get(_HARNESS) is not True:
        pytest.skip(
            "cannot stage an installed+credentialed claude CLI here "
            f"(readiness probe reports {real_map.get(_HARNESS)!r})"
        )
    windows_map = _daemon_readiness_map(simulate_windows=True, credentialed=True)
    if windows_map.get(_HARNESS) in _BROKEN_PRECONDITION_REASONS:
        pytest.skip(
            "Windows simulation broke the CLI probe itself "
            f"(readiness probe reports {windows_map.get(_HARNESS)!r})"
        )
    _run_in_fresh_loop(_drive_windows_badge(live_server, windows_map))


async def _drive_windows_badge(base_url: str, windows_map: dict[str, Any]) -> None:
    name = f"win11-e2e-{uuid.uuid4().hex[:8]}"
    async with _fake_host(base_url, name, windows_map) as host_id, async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await _register_routes(page)
            await _open_picker_on_host(page, base_url, host_id)
            row = await _reveal_claude_row(page)
            await row.hover()
            await page.wait_for_timeout(1_200)
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-warning-{_AGENT_ID}")
            ).to_be_visible(timeout=5_000)
        finally:
            await context.close()
            await browser.close()


def test_posix_host_ready_native_agent_stays_unbadged(live_server: str) -> None:
    """A ready native agent on a POSIX host remains unbadged."""
    real_map = _daemon_readiness_map(simulate_windows=False, credentialed=True)
    if real_map.get(_HARNESS) is not True:
        pytest.skip(
            "cannot stage an installed+credentialed claude CLI here "
            f"(readiness probe reports {real_map.get(_HARNESS)!r})"
        )
    _run_in_fresh_loop(_drive_posix_no_badge(live_server, real_map))


async def _drive_posix_no_badge(base_url: str, posix_map: dict[str, Any]) -> None:
    name = f"posix-e2e-{uuid.uuid4().hex[:8]}"
    async with _fake_host(base_url, name, posix_map) as host_id, async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await _register_routes(page)
            await _open_picker_on_host(page, base_url, host_id)
            row = await _reveal_claude_row(page)
            await row.hover()
            await page.wait_for_timeout(1_200)
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-warning-{_AGENT_ID}")
            ).to_have_count(0)
        finally:
            await context.close()
            await browser.close()


def test_unready_native_harness_is_badged_at_the_point_of_choice(live_server: str) -> None:
    """The picker warns when the selected host reports an unready harness."""
    bare_map = _daemon_readiness_map(simulate_windows=False, credentialed=False)
    if bare_map.get(_HARNESS) is True:
        pytest.skip(
            "ambient claude credentials leaked into the bare probe; "
            "cannot stage an unready claude CLI here"
        )
    _run_in_fresh_loop(_drive_unready_badge(live_server, bare_map))


async def _drive_unready_badge(base_url: str, bare_map: dict[str, Any]) -> None:
    name = f"bare-e2e-{uuid.uuid4().hex[:8]}"
    async with _fake_host(base_url, name, bare_map) as host_id, async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await _register_routes(page)
            await _open_picker_on_host(page, base_url, host_id)
            await _reveal_claude_row(page)
            await page.wait_for_timeout(1_200)
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-warning-{_AGENT_ID}")
            ).to_be_visible(timeout=10_000)
        finally:
            await context.close()
            await browser.close()

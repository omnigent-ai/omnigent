"""E2E: saving a harness credential to a host running omnigent v0.6.0.

A 0.6.0 host daemon predates ``host.store_secret``: it cannot decode the frame
and drops it without replying. The bug lived between the credential route and
the tunnel, so ``test_harness_credential.py``'s ``page.route`` stub cannot reach
it. Here a fake v0.6.0 host connects over the real host WebSocket tunnel, sends
the 0.6.0 hello, answers only frame kinds that release knew and drops the rest,
while the real SPA drives the real server. The shared ``live_server`` does not
enable ``harness_install``, so a dedicated server is spawned with it on.

Browser journeys run on a fresh thread and loop (``tests._helpers.async_thread``)
because pytest-asyncio can't start a loop on the main thread once a sync
pytest-playwright test has run in the session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from omnigent.host.frames import (
    HostCreateDirResultFrame,
    HostHelloFrame,
    HostListDirResultFrame,
    HostListWorktreesResultFrame,
    HostStatResultFrame,
    encode_host_frame,
)
from omnigent.runner.transports.ws_tunnel.frames import (
    PingFrame,
    PongFrame,
    decode_frame,
    encode_frame,
)
from tests._helpers.async_thread import run_in_fresh_loop as _run_in_fresh_loop
from tests.e2e_ui.conftest import _BUILD_OUTPUT, _REPO_ROOT, _find_free_port

_HOST_NAME = "old-host-0-6-0-e2e"
_HARNESS = "claude-native"

# The dedicated server cold-boots alongside the suite's shared one; give it a
# wider window than the shared fixture's 30s so CI can't flake on the spawn.
_SERVER_BOOT_TIMEOUT_S = 180.0

# Feedback must land well before the route's 30 s store_secret timeout; 20 s
# leaves slack for CI scheduling.
_PROMPT_FEEDBACK_S = 20.0


# ── Fake v0.6.0 host ─────────────────────────────────────────────────────────


async def _serve_old_host(ws: Any) -> None:
    """Serve frames like a v0.6.0 host daemon.

    Answers the filesystem probes the landing page may send and the tunnel
    keepalive pings; every frame kind 0.6.0 did not know (``store_secret``,
    ``detect_credentials``, ``model_options``, ...) is dropped without a reply,
    exactly like the real old daemon.
    """
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
        reply: Any = None
        if kind == "host.stat":
            reply = HostStatResultFrame(
                request_id=request_id,
                status="ok",
                exists=True,
                type="directory",
                canonical_path=payload.get("path", "~"),
            )
        elif kind == "host.list_dir":
            reply = HostListDirResultFrame(request_id=request_id, status="ok", entries=[])
        elif kind == "host.create_dir":
            reply = HostCreateDirResultFrame(request_id=request_id, status="ok")
        elif kind == "host.list_worktrees":
            reply = HostListWorktreesResultFrame(
                request_id=request_id, status="failed", error="not a git repository"
            )
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
async def _old_host(base_url: str) -> AsyncIterator[str]:
    """Connect a fake v0.6.0 host to the live server's host tunnel.

    Sends the hello a 0.6.0 daemon sends: ``version="0.6.0"``, wire protocol 1,
    and a readiness map marking claude-native installed-but-unauthenticated —
    the state whose fix is exactly the credential write under test.

    :param base_url: The dedicated server's base URL.
    :returns: Async context manager yielding the REST-reported host id.
    """
    import websockets

    host_id = uuid.uuid4().hex
    ws_url = base_url.replace("http://", "ws://") + f"/v1/hosts/{host_id}/tunnel"
    async with websockets.connect(ws_url) as ws:
        await ws.send(
            encode_host_frame(
                HostHelloFrame(
                    version="0.6.0",
                    frame_protocol_version=1,
                    name=_HOST_NAME,
                    runners=[],
                    configured_harnesses={_HARNESS: "needs-auth"},
                )
            )
        )
        serve_task = asyncio.create_task(_serve_old_host(ws))
        try:
            rest_host_id: str | None = None
            async with httpx.AsyncClient(trust_env=False) as client:
                for _ in range(100):
                    resp = await client.get(f"{base_url}/v1/hosts")
                    hosts = resp.json().get("hosts", [])
                    match = next(
                        (h for h in hosts if h["name"] == _HOST_NAME and h["status"] == "online"),
                        None,
                    )
                    if match is not None:
                        rest_host_id = match["host_id"]
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("fake v0.6.0 host never came online")
            assert rest_host_id is not None
            yield rest_host_id
        finally:
            serve_task.cancel()
            await asyncio.gather(serve_task, return_exceptions=True)


# ── Dedicated server (harness_install on) ────────────────────────────────────


@pytest.fixture(scope="module")
def old_host_server(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Spawn a server with ``OMNIGENT_FEATURES=harness_install`` and yield its URL.

    The suite's shared ``live_server`` doesn't enable the flag, and the
    credential route 404s without it. Mirrors the shared spawn (random port,
    per-module sqlite DB, log dumped into the RuntimeError on a failed boot)
    but starts no runner — the journey needs only a connected host, never an
    agent turn. The built-in Claude Code agent the picker offers is registered
    by the server itself.

    :param built_spa: Ensures the SPA bundle exists before the server mounts it.
    :param tmp_path_factory: Pytest temp dirs for the DB/log/artifacts.
    :returns: The dedicated server's base URL.
    """
    port = _find_free_port()
    server_tmp = tmp_path_factory.mktemp("old_host_server")
    log_path = server_tmp / "server.log"
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        # The whole point of the dedicated spawn: the credential route exists.
        "OMNIGENT_FEATURES": "harness_install",
        # Serve the HEAD SPA bundle regardless of what's installed in the venv.
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
        # No turns run here; strip ambient credentials so none can leak in.
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "mock-key",
    }
    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the Popen; closed in finally
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from omnigent.cli import main; main()",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{server_tmp / 'test.db'}",
            "--artifact-location",
            str(artifact_dir),
        ],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + _SERVER_BOOT_TIMEOUT_S
        ready = False
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"server exited early with code {proc.returncode}"
                break
            try:
                # trust_env=False: a CI HTTP(S) proxy must not intercept loopback.
                if httpx.get(f"{base_url}/health", timeout=2, trust_env=False).status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5)
        if not ready:
            log_handle.flush()
            log_text = log_path.read_text() if log_path.exists() else ""
            raise RuntimeError(
                f"old-host server not healthy within {_SERVER_BOOT_TIMEOUT_S:.0f}s on "
                f"{base_url} (last_error={last_error}).\n{log_text[-3000:]}"
            )
        yield base_url
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        log_handle.close()


# ── Tests ────────────────────────────────────────────────────────────────────


def test_setup_dialog_save_against_old_host_gives_prompt_feedback(old_host_server: str) -> None:
    """Saving a key in the setup dialog against the old host answers promptly.

    The user journey behind the report: pick the v0.6.0 host, pick a Claude
    agent (needs-auth there), open "Set up" → "Set up auth", paste a key, Save.
    On the buggy build the save spins ~30s with no feedback, then toasts
    ``Couldn't save the credential: host ... did not respond to store_secret
    within 30s``. The UI must surface feedback well before that dead wait, and
    the message must not blame host responsiveness.
    """
    _run_in_fresh_loop(_drive_setup_dialog_save(old_host_server))


async def _drive_setup_dialog_save(base_url: str) -> None:
    async with _old_host(base_url) as host_id, async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Explicit context so a conftest-recorded video is finalized on
        # context.close() even when the drive fails mid-way.
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Select the fake v0.6.0 host explicitly.
            await page.get_by_test_id("new-chat-landing-host-chip").click()
            await page.get_by_test_id(f"new-chat-landing-host-{host_id}").click(timeout=15_000)
            # Let the dropdown's exit animation unmount before the next popover
            # (same Radix timing artifact test_windows_workspace_picker.py notes).
            await expect(page.locator('[data-slot="dropdown-menu-content"]')).to_have_count(0)

            # Use the default Claude agent's "Set up" affordance directly: the agent
            # picker's model list never resolves on a v0.6.0 host (host.model_options
            # is another frame the old daemon drops).
            setup = page.get_by_test_id("new-chat-landing-harness-setup")
            await expect(setup).to_be_visible(timeout=60_000)
            await setup.click()
            await page.get_by_test_id("harness-setup-add-credential").click(timeout=15_000)
            await expect(page.get_by_test_id("harness-credential-form")).to_be_visible(
                timeout=5_000
            )

            await page.get_by_test_id("harness-credential-key").fill("fake-key-old-host")
            await page.get_by_test_id("harness-credential-save").click()

            # The user must get feedback promptly — the buggy build shows
            # nothing until the 30s server timeout lands.
            toast = page.get_by_test_id("toast").first
            await expect(toast).to_be_visible(timeout=int(_PROMPT_FEEDBACK_S * 1000))
            # … naming the remedy, never the misleading responsiveness blame.
            await expect(toast).to_contain_text("update omnigent on the host")
            await expect(toast).not_to_contain_text("did not respond")
            # The e2e_ui conftest films every context when OMNIGENT_E2E_RECORD_DIR is
            # set; keep the toast on screen so that clip ends on it.
            if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
                await page.wait_for_timeout(3_000)
        finally:
            await context.close()
            await browser.close()

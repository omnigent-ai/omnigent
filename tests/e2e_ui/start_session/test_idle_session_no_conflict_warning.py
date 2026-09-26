"""An idle session must not trigger the directory-conflict warning."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from omnigent.host.frames import (
    HostCreateDirFrame,
    HostCreateDirResultFrame,
    HostDetectCredentialsFrame,
    HostDetectCredentialsResultFrame,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostListDirEntry,
    HostListDirFrame,
    HostListDirResultFrame,
    HostListWorktreesFrame,
    HostListWorktreesResultFrame,
    HostModelOptionsFrame,
    HostModelOptionsResultFrame,
    HostRunnerStatusFrame,
    HostRunnerStatusResultFrame,
    HostStatFrame,
    HostStatResultFrame,
    HostStopRunnerFrame,
    HostStopRunnerResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runner.identity import token_bound_runner_id
from omnigent.runner.transports.ws_tunnel.frames import (
    PingFrame,
    PongFrame,
    decode_frame,
    encode_frame,
)
from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.start_session.helpers import (
    commit_landing_workspace_picker,
    open_landing_workspace_picker,
)

_HOST_NAME = "idle-occupancy-host"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _resolve(path: str, home: Path) -> Path:
    """Resolve a host-frame path against the test host's home directory."""
    if path in ("", "~"):
        return home
    if path.startswith("~/"):
        return home / path[2:]
    return Path(path)


class _LocalHost:
    """Answer host frames from disk and launch real runner subprocesses."""

    def __init__(self, base_url: str, mock_llm_url: str, home: Path, log_dir: Path) -> None:
        self.base_url = base_url
        self.mock_llm_url = mock_llm_url
        self.home = home
        self.log_dir = log_dir
        self.runners: dict[str, subprocess.Popen[bytes]] = {}

    def _spawn_runner(self, frame: HostLaunchRunnerFrame) -> str:
        """Spawn a real runner for a launch frame and return its runner id."""
        runner_id = token_bound_runner_id(frame.binding_token)
        # Do not inherit another runner's or host's identity.
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST")) and k != "RUNNER_SERVER_URL"
        }
        env.update(
            {
                # Include candidate SDKs used by managed runner auth.
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(_REPO_ROOT),
                        str(_REPO_ROOT / "sdks" / "python-client"),
                        str(_REPO_ROOT / "sdks" / "ui"),
                        os.environ.get("PYTHONPATH", ""),
                    ]
                ),
                "OMNIGENT_RUNNER_ID": runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": frame.binding_token,
                "OMNIGENT_RUNNER_WORKSPACE": frame.workspace,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "RUNNER_SERVER_URL": self.base_url,
                "OPENAI_BASE_URL": f"{self.mock_llm_url}/v1",
                "OPENAI_API_KEY": "mock-key",
            }
        )
        log_handle = open(  # noqa: SIM115 — fd dup'd into the child; closed below
            self.log_dir / f"runner-{runner_id[-8:]}.log", "w"
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            cwd=frame.workspace,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        log_handle.close()
        self.runners[runner_id] = proc
        return runner_id

    def _list_dir_reply(self, frame: HostListDirFrame) -> HostListDirResultFrame:
        target = _resolve(frame.path, self.home)
        if not target.is_dir():
            return HostListDirResultFrame(
                request_id=frame.request_id, status="ok", error="path does not exist"
            )
        entries = [
            HostListDirEntry(
                name=e.name,
                path=str(target / e.name),
                type="directory" if e.is_dir() else "file",
                bytes=None if e.is_dir() else e.stat().st_size,
                modified_at=int(e.stat().st_mtime),
            )
            for e in sorted(os.scandir(target), key=lambda e: e.name)
        ]
        return HostListDirResultFrame(
            request_id=frame.request_id,
            status="ok",
            entries=entries[: frame.limit],
            has_more=len(entries) > frame.limit,
        )

    def reply_for(self, frame: Any) -> Any:
        """Compute the host's reply for one decoded frame (None = no reply)."""
        if isinstance(frame, HostListDirFrame):
            return self._list_dir_reply(frame)
        if isinstance(frame, HostStatFrame):
            target = _resolve(frame.path, self.home)
            exists = target.exists()
            return HostStatResultFrame(
                request_id=frame.request_id,
                status="ok",
                exists=exists,
                type=("directory" if target.is_dir() else "file") if exists else None,
                canonical_path=str(target) if exists else None,
            )
        if isinstance(frame, HostCreateDirFrame):
            target = _resolve(frame.path, self.home)
            target.mkdir(parents=True, exist_ok=True)
            return HostCreateDirResultFrame(
                request_id=frame.request_id, status="ok", path=str(target)
            )
        if isinstance(frame, HostListWorktreesFrame):
            return HostListWorktreesResultFrame(
                request_id=frame.request_id, status="failed", error="not a git repository"
            )
        if isinstance(frame, HostModelOptionsFrame):
            return HostModelOptionsResultFrame(request_id=frame.request_id, status="ok")
        if isinstance(frame, HostDetectCredentialsFrame):
            return HostDetectCredentialsResultFrame(request_id=frame.request_id)
        if isinstance(frame, HostLaunchRunnerFrame):
            try:
                runner_id = self._spawn_runner(frame)
            except OSError as exc:
                return HostLaunchRunnerResultFrame(
                    request_id=frame.request_id,
                    status="failed",
                    error=f"failed to spawn runner: {exc}",
                )
            return HostLaunchRunnerResultFrame(
                request_id=frame.request_id, status="launched", runner_id=runner_id
            )
        if isinstance(frame, HostStopRunnerFrame):
            proc = self.runners.pop(frame.runner_id, None)
            if proc is not None:
                proc.terminate()
            return HostStopRunnerResultFrame(request_id=frame.request_id, status="stopped")
        if isinstance(frame, HostRunnerStatusFrame):
            proc = self.runners.get(frame.runner_id)
            if proc is None:
                status = "unknown"
            else:
                status = "alive" if proc.poll() is None else "dead"
            return HostRunnerStatusResultFrame(request_id=frame.request_id, status=status)
        return None

    def terminate_runners(self) -> None:
        """Tear down every runner this host spawned."""
        for proc in self.runners.values():
            proc.terminate()
        for proc in self.runners.values():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self.runners.clear()


async def _serve_host(ws: Any, host: _LocalHost) -> None:
    """Serve host frames over the real tunnel with the local test host."""
    async for raw in ws:
        if not isinstance(raw, str):
            continue
        try:
            frame = decode_host_frame(raw)
        except ValueError:
            # The server uses runner-tunnel encoding for keepalive pings.
            try:
                runner_frame = decode_frame(raw)
            except ValueError:
                continue
            if isinstance(runner_frame, PingFrame):
                await ws.send(encode_frame(PongFrame(ts=runner_frame.ts)))
            continue
        reply = await asyncio.to_thread(host.reply_for, frame)
        if reply is not None:
            await ws.send(encode_host_frame(reply))


@contextlib.asynccontextmanager
async def _local_host(host: _LocalHost) -> AsyncIterator[str]:
    """Connect the test host and yield its server-assigned ID."""
    import websockets

    host_id = uuid.uuid4().hex
    ws_url = host.base_url.replace("http://", "ws://") + f"/v1/hosts/{host_id}/tunnel"
    async with websockets.connect(ws_url) as ws:
        await ws.send(
            encode_host_frame(
                HostHelloFrame(
                    version="0.0.0-e2e",
                    frame_protocol_version=1,
                    name=_HOST_NAME,
                    # The landing requires hello_world's harness to be configured.
                    configured_harnesses={
                        "openai-agents": True,
                        "agents_sdk": True,
                        "openai-agents-sdk": True,
                    },
                )
            )
        )
        serve_task = asyncio.create_task(_serve_host(ws, host))
        try:
            rest_host_id: str | None = None
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    resp = await client.get(f"{host.base_url}/v1/hosts")
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
                    raise AssertionError("local e2e host never came online")
            assert rest_host_id is not None
            yield rest_host_id
        finally:
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await serve_task
            host.terminate_runners()


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Use a fresh loop after sync Playwright; re-raise worker failures."""
    error: list[BaseException] = []

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]


async def _wait_session_idle(base_url: str, session_id: str, *, timeout_s: float = 90.0) -> None:
    """Poll until the session becomes idle or the deadline expires."""
    deadline = time.monotonic() + timeout_s
    last = "<never fetched>"
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            resp = await client.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
            if resp.status_code == 200:
                last = resp.json().get("status", "<missing>")
                if last == "idle":
                    return
            await asyncio.sleep(0.5)
    raise AssertionError(f"session {session_id} never went idle (last status: {last})")


async def _drive(base_url: str, mock_llm_url: str, tmp_path: Path) -> None:
    """Launch, idle, reopen the picker, and check for a warning."""
    marker = uuid.uuid4().hex[:8]
    prompt = f"idle-occupancy hello {marker}"
    reply = f"idle-occupancy-reply-{marker}"
    configure_mock_llm(
        mock_llm_url, [{"text": reply}], key=f"idle-occupancy-{marker}", match=prompt
    )

    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    (project / "README.md").write_text("hello\n")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    host = _LocalHost(base_url, mock_llm_url, home, log_dir)
    session_id: str | None = None
    async with _local_host(host) as host_id, async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto(base_url)
            await expect(page.get_by_test_id("new-chat-landing")).to_be_visible(timeout=15_000)

            # A newer same-name upload can replace the server template's picker row.
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-custom-agents").click()
            agent_row = page.get_by_role("menuitem", name=re.compile(r"^hello_world$", re.I))
            await expect(agent_row).to_be_visible(timeout=15_000)
            await agent_row.click()

            await page.get_by_test_id("new-chat-landing-host-chip").click()
            await page.get_by_test_id(f"new-chat-landing-host-{host_id}").click()

            # Opening before home resolves can detach the picker row.
            workspace_chip = page.get_by_test_id("new-chat-landing-workspace-chip")
            await expect(workspace_chip).to_contain_text(home.name, timeout=15_000)
            await expect(workspace_chip).to_be_enabled()
            await expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)
            await open_landing_workspace_picker(page)
            path_input = page.get_by_test_id("workspace-picker-path-input")
            await expect(path_input).to_have_value(str(home), timeout=10_000)
            await path_input.fill(str(project))
            await path_input.press("Enter")
            await expect(page.get_by_test_id("workspace-picker-entry-README.md")).to_be_visible(
                timeout=10_000
            )
            await commit_landing_workspace_picker(page)

            await page.get_by_test_id("new-chat-landing-input").fill(prompt)
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url("**/c/**", timeout=60_000)
            await expect(page.get_by_text(reply)).to_be_visible(timeout=180_000)
            # Wait for the optimistic temp URL to become a persisted session ID.
            await page.wait_for_url(re.compile(r"/c/(?!temp)"), timeout=30_000)
            session_id = page.url.rstrip("/").split("/c/")[-1].split("?")[0]
            await _wait_session_idle(base_url, session_id)

            # Verify that the sole occupant is this idle, connected session.
            async with httpx.AsyncClient() as client:
                listed = (
                    await client.get(
                        f"{base_url}/v1/sessions?limit=200&visibility=all", timeout=10.0
                    )
                ).json()["data"]
                occupants = [
                    s
                    for s in listed
                    if s.get("host_id") == host_id and s.get("workspace") == str(project)
                ]
                assert [s["id"] for s in occupants] == [session_id], occupants
                health = (
                    await client.get(
                        f"{base_url}/health", params={"session_ids": session_id}, timeout=10.0
                    )
                ).json()
            assert health["sessions"][session_id]["runner_online"] is True, health

            await page.get_by_test_id("new-chat-button").click()
            await expect(page.get_by_test_id("new-chat-landing")).to_be_visible(timeout=15_000)
            await page.get_by_test_id("new-chat-landing-host-chip").click()
            await page.get_by_test_id(f"new-chat-landing-host-{host_id}").click()

            await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_be_enabled(
                timeout=15_000
            )
            await expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)
            await open_landing_workspace_picker(page)
            path_input2 = page.get_by_test_id("workspace-picker-path-input")
            if await path_input2.input_value() != str(project):
                await path_input2.fill(str(project))
                await path_input2.press("Enter")
            await expect(page.get_by_test_id("workspace-picker-entry-README.md")).to_be_visible(
                timeout=10_000
            )

            # Observe through one runner-health poll before concluding.
            conflict = page.get_by_test_id("workspace-picker-conflict")
            appeared = True
            try:
                await expect(conflict).to_be_visible(timeout=12_000)
            except AssertionError:
                appeared = False
            if appeared:
                text = " ".join((await conflict.inner_text()).split())
                pytest.fail(
                    f"Phantom conflict warning: the directory picker warns {text!r} in "
                    "the default state — the only session in this directory is the "
                    "user's own idle session (runner online, no turn running), so no "
                    "other agent is working there."
                )
        finally:
            with contextlib.suppress(Exception):
                await page.close()
            with contextlib.suppress(Exception):
                await browser.close()
            if session_id is not None:
                async with httpx.AsyncClient() as client:
                    with contextlib.suppress(httpx.HTTPError):
                        await client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


@pytest.mark.flaky(reruns=2, reruns_delay=5)
# Exercise the newer session-scoped version alongside the catalog template.
@pytest.mark.usefixtures("seeded_session")
def test_default_state_directory_picker_has_no_conflict_warning(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A sole idle session must not trigger an occupied-directory hint."""
    _run_in_fresh_loop(_drive(live_server, mock_llm_server_url, tmp_path))

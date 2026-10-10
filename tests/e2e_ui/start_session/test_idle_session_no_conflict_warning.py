"""E2E: the user's own idle session must not trigger the directory-conflict warning.

A real ``omnigent host`` daemon is started against the test server so the
landing can browse a directory and launch a runner for it. The first session
is created through the landing composer and left idle with its runner still
attached; opening a second new session's directory selector at the same
directory must then show no "other agent is working in this directory" banner.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.async_api import Locator, Page, async_playwright, expect

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.async_thread import run_in_fresh_loop
from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.start_session.helpers import (
    commit_landing_workspace_picker,
    open_landing_workspace_picker,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
# Two SPA runner-health poll cycles (POLL_INTERVAL_MS in
# web/src/hooks/useRunnerHealth.ts), so one delayed poll can't pass this vacuously.
_CONFLICT_OBSERVATION_MS = 20_000


@contextlib.asynccontextmanager
async def _host_daemon(base_url: str, mock_llm_url: str, home: Path) -> AsyncIterator[str]:
    """Run a real host daemon with an isolated identity; yield its id once online."""
    config_dir = home / ".omnigent"
    config_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"host": {"host_id": host_id, "name": f"idle-occupancy-{host_id[:8]}"}})
    )
    log_path = home / "host-daemon.log"
    # OPENAI_* are forwarded to launched runners, which routes hello_world to the mock.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST"))
    }
    env.update(
        {
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(config_dir),
            "OMNIGENT_DATA_DIR": str(home / "data"),
            "OMNIGENT_RUNNER_ZYGOTE": "0",
            "OPENAI_API_KEY": "mock-key",
            "OPENAI_BASE_URL": f"{mock_llm_url}/v1",
            "PYTHONPATH": os.pathsep.join(
                [str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep),
            PROCESS_LOG_FILE_ENV_VAR: str(log_path),
        }
    )
    # Append mode: the daemon's own process log writes to this file too.
    with open(log_path, "a") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", base_url],
            env=env,
            cwd=str(_REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    try:
        deadline = time.monotonic() + 60.0
        async with httpx.AsyncClient() as client:
            while True:
                if proc.poll() is not None:
                    tail = log_path.read_text(errors="replace")[-3000:]
                    raise AssertionError(f"host daemon exited early:\n{tail}")
                resp = await client.get(f"{base_url}/v1/hosts", timeout=5.0)
                hosts = resp.json().get("hosts", []) if resp.status_code == 200 else []
                if any(h["host_id"] == host_id and h["status"] == "online" for h in hosts):
                    break
                if time.monotonic() > deadline:
                    raise AssertionError(
                        f"host {host_id} never came online:\n"
                        f"{log_path.read_text(errors='replace')[-3000:]}"
                    )
                await asyncio.sleep(0.25)
        yield host_id
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


async def _select_host(page: Page, host_id: str) -> None:
    await page.get_by_test_id("new-chat-landing-host-chip").click()
    await page.get_by_test_id(f"new-chat-landing-host-{host_id}").click()
    await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_be_enabled(
        timeout=15_000
    )
    await expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)


async def _browse_to(page: Page, directory: Path) -> None:
    await open_landing_workspace_picker(page)
    path_input = page.get_by_test_id("workspace-picker-path-input")
    # Typing before the home directory resolves lets the late resolve overwrite the path.
    await expect(path_input).not_to_have_value("", timeout=15_000)
    if await path_input.input_value() != str(directory):
        await path_input.fill(str(directory))
        await path_input.press("Enter")
    await expect(page.get_by_test_id("workspace-picker-entry-README.md")).to_be_visible(
        timeout=15_000
    )


async def _wait_session_idle(
    client: httpx.AsyncClient, base_url: str, session_id: str, *, timeout_s: float = 90.0
) -> None:
    deadline = time.monotonic() + timeout_s
    last = "<never fetched>"
    while time.monotonic() < deadline:
        resp = await client.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        if resp.status_code == 200:
            last = resp.json().get("status", "<missing>")
            if last == "idle":
                return
        await asyncio.sleep(0.5)
    raise AssertionError(f"session {session_id} never went idle (last status: {last})")


async def _becomes_visible(locator: Locator, timeout_ms: int) -> bool:
    try:
        await expect(locator).to_be_visible(timeout=timeout_ms)
    except AssertionError:
        return False
    return True


async def _drive(base_url: str, mock_llm_url: str, tmp_path: Path, output_dir: Path) -> None:
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

    session_id: str | None = None
    async with _host_daemon(base_url, mock_llm_url, home) as host_id, async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        try:
            await page.goto(base_url)
            await expect(page.get_by_test_id("new-chat-landing")).to_be_visible(timeout=15_000)

            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-custom-agents").click()
            agent_row = page.get_by_role("menuitem", name=re.compile(r"^hello_world$", re.I))
            await expect(agent_row).to_be_visible(timeout=15_000)
            await agent_row.click()

            await _select_host(page, host_id)
            await _browse_to(page, project)
            await commit_landing_workspace_picker(page)

            await page.get_by_test_id("new-chat-landing-input").fill(prompt)
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url("**/c/**", timeout=60_000)
            await expect(page.get_by_text(reply)).to_be_visible(timeout=180_000)
            await page.wait_for_url(re.compile(r"/c/(?!temp)"), timeout=30_000)
            session_id = page.url.rstrip("/").split("/c/")[-1].split("?")[0]

            async with httpx.AsyncClient() as client:
                await _wait_session_idle(client, base_url, session_id)
                listed = (
                    await client.get(
                        f"{base_url}/v1/sessions?limit=200&visibility=all", timeout=10.0
                    )
                ).json()["data"]
                occupants = [
                    s["id"]
                    for s in listed
                    if s.get("host_id") == host_id and s.get("workspace") == str(project)
                ]
                assert occupants == [session_id], occupants
                health = (
                    await client.get(
                        f"{base_url}/health", params={"session_ids": session_id}, timeout=10.0
                    )
                ).json()
                assert health["sessions"][session_id]["runner_online"] is True, health

            await page.get_by_test_id("new-chat-button").click()
            await expect(page.get_by_test_id("new-chat-landing")).to_be_visible(timeout=15_000)
            await _select_host(page, host_id)
            await _browse_to(page, project)

            conflict = page.get_by_test_id("workspace-picker-conflict")
            appeared = await _becomes_visible(conflict, _CONFLICT_OBSERVATION_MS)
            await page.screenshot(path=output_dir / "directory-picker.png")
            if appeared:
                text = " ".join((await conflict.inner_text()).split())
                # Hold the banner on screen so a recording of the failure stays readable.
                await page.wait_for_timeout(2_000)
                pytest.fail(
                    f"Phantom conflict warning {text!r}: the only session in {project} is the "
                    "user's own idle session (runner online, no turn running)."
                )
        finally:
            with contextlib.suppress(Exception):
                await context.close()
            with contextlib.suppress(Exception):
                await browser.close()
            if session_id is not None:
                async with httpx.AsyncClient() as client:
                    with contextlib.suppress(httpx.HTTPError):
                        await client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


# seeded_session uploads hello_world so the landing picker offers it.
@pytest.mark.usefixtures("seeded_session")
def test_sole_idle_session_shows_no_directory_conflict_warning(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    output_dir = Path(request.config.getoption("--output")) / request.node.name
    output_dir.mkdir(parents=True, exist_ok=True)
    run_in_fresh_loop(_drive(live_server, mock_llm_server_url, tmp_path, output_dir))
